"""Tests for ``inbound._forward_to_admins`` — the SES-inbound reply
forwarder (security fix D14).

Pre-fix behavior: any inbound to organizer@<domain> that didn't carry
a calendar action was forwarded verbatim to every App Admin in the
community — an anonymous fan-out amplifier and a phishing aid.

Post-fix invariants verified here:
    - SES verdicts pass is required (verdicts_pass=False drops)
    - Sender must be a registered community member (allowlist)
    - We use the *stored* display name from the member record, never
      the attacker-influenced parseaddr(from)[0]
    - Body content is capped at _MAX_FWD_BODY_CHARS
    - The forwarded body carries a "from a member, not from the
      system itself" banner
"""
from __future__ import annotations

import email
from dataclasses import dataclass, field
from typing import Any

import pytest

from community_organizer.core import db
from community_organizer.core.models import (
    Application, Community, EmailLog, Membership, User,
)
from community_organizer.lambdas import inbound


# ---------------------------------------------------------------------------
# Fake provider — captures send() calls.
# ---------------------------------------------------------------------------

@dataclass
class _CapturingProvider:
    name: str = "fake"
    sent: list[dict[str, Any]] = field(default_factory=list)

    def send(self, **kwargs: Any) -> EmailLog:
        self.sent.append(kwargs)
        return EmailLog(
            community_id=kwargs.get("community_id", ""),
            direction="outbound",
            from_addr=kwargs.get("from_addr", ""),
            to_addr=kwargs.get("to_addr", ""),
            subject=kwargs.get("subject", ""),
            provider=self.name,
            kind=kwargs.get("kind", "other"),
            outcome="accepted",
        )


@pytest.fixture
def setup_community(ddb_table, monkeypatch):
    """Seed community + one app + one admin + one regular member.
    Return the provider so the test can inspect captured sends."""
    cid = "test-community"
    db.put_community(Community(community_id=cid, name="Test Parish"))
    app = Application(community_id=cid, name="Test Ushers", app_type="coverage")
    db.put_application(app)

    admin = User(community_id=cid, email="admin@example.com", name="Admin A")
    member = User(community_id=cid, email="member@example.com",
                  name="Real Member")
    db.put_user(admin)
    db.put_user(member)

    db.put_membership(Membership(community_id=cid, app_id=app.app_id,
                                 user_id=admin.user_id, app_role="aa"))
    db.put_membership(Membership(community_id=cid, app_id=app.app_id,
                                 user_id=member.user_id, app_role="member"))

    provider = _CapturingProvider()
    monkeypatch.setattr(inbound, "get_email_provider", lambda: provider)
    # Point inbound at the same community_id our seed used.
    monkeypatch.setattr(inbound, "COMMUNITY_ID", cid)
    return {"provider": provider, "admin": admin, "member": member, "app": app}


def _build_msg(*, from_header: str, subject: str = "Re: schedule",
               body: str = "Sounds good!") -> email.message.Message:
    msg = email.message.EmailMessage()
    msg["From"] = from_header
    msg["Subject"] = subject
    msg.set_content(body)
    return msg


# ---------------------------------------------------------------------------
# Verdict gate
# ---------------------------------------------------------------------------

def test_forward_blocked_when_verdicts_fail(setup_community) -> None:
    """SES verdicts not all PASS → drop the forward, even if the
    sender would otherwise be on the allowlist."""
    msg = _build_msg(from_header="Real Member <member@example.com>")
    result = inbound._forward_to_admins(
        msg, "Real Member <member@example.com>",
        verdicts_pass=False,
    )
    assert result is False
    assert setup_community["provider"].sent == []


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------

def test_forward_blocked_when_sender_not_a_member(setup_community) -> None:
    """An email from an unknown address (not in the community
    user list) must NOT generate any forwards — that's the gate
    that closes anonymous fan-out (security fix D14)."""
    msg = _build_msg(from_header="Stranger <stranger@evil.com>")
    result = inbound._forward_to_admins(
        msg, "Stranger <stranger@evil.com>",
        verdicts_pass=True,
    )
    assert result is False
    assert setup_community["provider"].sent == []


def test_forward_proceeds_when_sender_is_member(setup_community) -> None:
    """Known member, verdicts pass → forward goes out to the admin."""
    msg = _build_msg(from_header="Real Member <member@example.com>",
                     subject="Quick question",
                     body="Can I swap my Sunday slot?")
    result = inbound._forward_to_admins(
        msg, "Real Member <member@example.com>",
        verdicts_pass=True,
    )
    assert result is True
    sent = setup_community["provider"].sent
    assert len(sent) == 1
    assert sent[0]["to_addr"] == "admin@example.com"


# ---------------------------------------------------------------------------
# Display-name substitution
# ---------------------------------------------------------------------------

def test_forward_uses_stored_display_name_not_attacker_supplied(setup_community) -> None:
    """An attacker-controlled From display name (e.g. "Admin Of Parish")
    must NOT survive into the forwarded body — we use the matched
    member's stored ``name`` field instead (the member changed the display name
    pushback on #3)."""
    msg = _build_msg(
        # Hostile display name — claims to be admin but the email
        # underneath is the real member's.
        from_header='"Fake Parish Admin" <member@example.com>',
        subject="urgent",
        body="please reset all passwords",
    )
    inbound._forward_to_admins(
        msg, '"Fake Parish Admin" <member@example.com>',
        verdicts_pass=True,
    )
    body = setup_community["provider"].sent[0]["body_text"]
    subject = setup_community["provider"].sent[0]["subject"]
    # The trusted "Real Member" name appears...
    assert "Real Member" in body
    assert "Real Member" in subject
    # ...and the attacker's display name does NOT.
    assert "Fake Parish Admin" not in body
    assert "Fake Parish Admin" not in subject


# ---------------------------------------------------------------------------
# Body cap + banner
# ---------------------------------------------------------------------------

def test_forward_truncates_long_body(setup_community) -> None:
    """Bodies longer than _MAX_FWD_BODY_CHARS get truncated to
    protect admin inboxes from amplification."""
    huge = "A" * (inbound._MAX_FWD_BODY_CHARS * 3)
    msg = _build_msg(from_header="<member@example.com>",
                     body=huge)
    inbound._forward_to_admins(
        msg, "<member@example.com>", verdicts_pass=True,
    )
    body = setup_community["provider"].sent[0]["body_text"]
    # The full 6000-char body never made it in.
    assert "A" * (inbound._MAX_FWD_BODY_CHARS * 2) not in body
    assert "[...truncated]" in body


def test_forward_includes_member_banner(setup_community) -> None:
    """The forwarded body must say clearly that the content below is
    from a member, not from the system itself (so admins don't
    treat instructions in the body as authoritative)."""
    msg = _build_msg(from_header="<member@example.com>",
                     body="hi")
    inbound._forward_to_admins(
        msg, "<member@example.com>", verdicts_pass=True,
    )
    body = setup_community["provider"].sent[0]["body_text"]
    assert "from a member's reply" in body.lower()
    assert "not from the system itself" in body.lower()


# ---------------------------------------------------------------------------
# Per-app scoping regression coverage
# ---------------------------------------------------------------------------

def test_forward_scoped_to_senders_app_only(ddb_table, monkeypatch) -> None:
    """A reply from a member who belongs to App A must NOT generate
    forwards to admins of App B / C. Pre-fix the forwarder looped
    EVERY app in the community and fanned out per app — so an admin
    who AA'd multiple apps got the same reply N times, and unrelated
    apps' admins got a forward they had no context for."""
    cid = "scope-cid"
    db.put_community(Community(community_id=cid, name="Test"))
    app_a = Application(community_id=cid, name="Coverage App A",
                        app_type="coverage")
    app_b = Application(community_id=cid, name="Example Ushers",
                        app_type="coverage")
    app_c = Application(community_id=cid, name="Adoration",
                        app_type="recurring_commitments")
    db.put_application(app_a)
    db.put_application(app_b)
    db.put_application(app_c)

    # Two-hatted admin: AA of both app_a and app_b.
    two_hat = User(community_id=cid, email="member@example.com",
                   name="Two-Hat Admin")
    db.put_user(two_hat)
    db.put_membership(Membership(community_id=cid, app_id=app_a.app_id,
                                 user_id=two_hat.user_id, app_role="aa"))
    db.put_membership(Membership(community_id=cid, app_id=app_b.app_id,
                                 user_id=two_hat.user_id, app_role="aa"))

    # Single-hat admin: AA of app_c only — must NOT be notified.
    c_only = User(community_id=cid, email="c-admin@example.com",
                  name="C-only Admin")
    db.put_user(c_only)
    db.put_membership(Membership(community_id=cid, app_id=app_c.app_id,
                                 user_id=c_only.user_id, app_role="aa"))

    # The reply-sender belongs to app_a only.
    sender = User(community_id=cid, email="sender@example.com",
                  name="A-only Sender")
    db.put_user(sender)
    db.put_membership(Membership(community_id=cid, app_id=app_a.app_id,
                                 user_id=sender.user_id, app_role="member"))

    provider = _CapturingProvider()
    monkeypatch.setattr(inbound, "get_email_provider", lambda: provider)
    monkeypatch.setattr(inbound, "COMMUNITY_ID", cid)

    msg = _build_msg(from_header="<sender@example.com>",
                     subject="Re: schedule", body="Thanks")
    inbound._forward_to_admins(msg, "<sender@example.com>",
                               verdicts_pass=True)

    # Only one forward: to the two-hat admin, tagged for app_a (the
    # one the sender is a member of). The two-hat admin must NOT be
    # notified again under app_b's tag, and app_c's admin must not
    # appear at all.
    assert len(provider.sent) == 1
    assert provider.sent[0]["to_addr"] == "member@example.com"
    assert "Coverage App A" in provider.sent[0]["subject"]
    assert "Example Ushers" not in provider.sent[0]["subject"]
    # c-only admin received nothing.
    assert "c-admin@example.com" not in {s["to_addr"] for s in provider.sent}


def test_forward_skipped_when_sender_has_no_memberships(
        ddb_table, monkeypatch) -> None:
    """A community user with no Memberships at all (community-bare)
    sending a reply produces NO forwards — there's no app to scope
    the reply to, and fanning to every app would be the bug we just
    fixed."""
    cid = "bare-cid"
    db.put_community(Community(community_id=cid, name="Test"))
    app = Application(community_id=cid, name="Test", app_type="coverage")
    db.put_application(app)
    admin = User(community_id=cid, email="admin@example.com",
                 name="App Admin")
    db.put_user(admin)
    db.put_membership(Membership(community_id=cid, app_id=app.app_id,
                                 user_id=admin.user_id, app_role="aa"))
    # Community user with NO app membership.
    bare = User(community_id=cid, email="bare@example.com",
                name="Bare User")
    db.put_user(bare)

    provider = _CapturingProvider()
    monkeypatch.setattr(inbound, "get_email_provider", lambda: provider)
    monkeypatch.setattr(inbound, "COMMUNITY_ID", cid)

    result = inbound._forward_to_admins(
        _build_msg(from_header="<bare@example.com>", body="hi"),
        "<bare@example.com>", verdicts_pass=True,
    )
    assert result is False
    assert provider.sent == []


# ---------------------------------------------------------------------------
# Cross-community sender resolution (shared-queue inbound)
# ---------------------------------------------------------------------------

def test_forward_routes_beta_only_sender_to_beta(ddb_table, monkeypatch) -> None:
    """A reply from a user whose email exists ONLY in c_other should
    route to c_other data — not silently dropped just because env
    COMMUNITY_ID points at prod."""
    db.put_community(Community(community_id="example-community", name="Prod"))
    db.put_community(Community(community_id="c_other", name="Beta"))

    beta_app = Application(community_id="c_other", name="Beta Volunteers",
                           app_type="coverage")
    db.put_application(beta_app)
    beta_aa = User(community_id="c_other", email="aa@beta.com", name="Beta AA")
    beta_member = User(community_id="c_other", email="m@beta.com",
                       name="Beta Member")
    db.put_user(beta_aa)
    db.put_user(beta_member)
    db.put_membership(Membership(community_id="c_other",
                                 app_id=beta_app.app_id,
                                 user_id=beta_aa.user_id, app_role="aa"))
    db.put_membership(Membership(community_id="c_other",
                                 app_id=beta_app.app_id,
                                 user_id=beta_member.user_id,
                                 app_role="member"))

    provider = _CapturingProvider()
    monkeypatch.setattr(inbound, "get_email_provider", lambda: provider)
    # Env defaults to prod — the cross-community lookup is what
    # finds the sender in beta.
    monkeypatch.setattr(inbound, "COMMUNITY_ID", "example-community")

    msg = _build_msg(from_header="Beta Member <m@beta.com>")
    ok = inbound._forward_to_admins(msg, "Beta Member <m@beta.com>",
                                    verdicts_pass=True)
    assert ok is True, "beta-only sender should be routed, not dropped"
    assert len(provider.sent) == 1
    sent = provider.sent[0]
    assert sent["community_id"] == "c_other"
    assert sent["to_addr"] == "aa@beta.com"


def test_forward_ambiguous_sender_falls_back_to_env(ddb_table, monkeypatch) -> None:
    """Same email exists in prod and beta → env COMMUNITY_ID wins.
    Documents the dual-community tie-breaker."""
    db.put_community(Community(community_id="example-community", name="Prod"))
    db.put_community(Community(community_id="c_other", name="Beta"))

    prod_app = Application(community_id="example-community", name="Prod Volunteers",
                           app_type="coverage")
    db.put_application(prod_app)
    prod_aa = User(community_id="example-community",
                   email="prod-aa@example.com", name="Prod AA")
    prod_member = User(community_id="example-community",
                     email="admin@example.com", name="Morgan (prod)")
    db.put_user(prod_aa)
    db.put_user(prod_member)
    db.put_membership(Membership(community_id="example-community",
                                 app_id=prod_app.app_id,
                                 user_id=prod_aa.user_id, app_role="aa"))
    db.put_membership(Membership(community_id="example-community",
                                 app_id=prod_app.app_id,
                                 user_id=prod_member.user_id,
                                 app_role="member"))

    beta_app = Application(community_id="c_other", name="Beta Volunteers",
                           app_type="coverage")
    db.put_application(beta_app)
    beta_aa = User(community_id="c_other", email="beta-aa@example.com",
                   name="Beta AA")
    beta_mark = User(community_id="c_other",
                     email="admin@example.com", name="Morgan (beta)")
    db.put_user(beta_aa)
    db.put_user(beta_mark)
    db.put_membership(Membership(community_id="c_other",
                                 app_id=beta_app.app_id,
                                 user_id=beta_mark.user_id,
                                 app_role="member"))

    provider = _CapturingProvider()
    monkeypatch.setattr(inbound, "get_email_provider", lambda: provider)
    monkeypatch.setattr(inbound, "COMMUNITY_ID", "example-community")

    msg = _build_msg(from_header="Morgan <admin@example.com>")
    inbound._forward_to_admins(msg, "Morgan <admin@example.com>",
                               verdicts_pass=True)
    assert len(provider.sent) == 1
    assert provider.sent[0]["community_id"] == "example-community"
    assert provider.sent[0]["to_addr"] == "prod-aa@example.com"


# ---------------------------------------------------------------------------
# Thread-aware routing (In-Reply-To)
# ---------------------------------------------------------------------------

def _build_reply_msg(*, from_header, subject="Re: schedule",
                     body="Got it.", in_reply_to=""):
    msg = email.message.EmailMessage()
    msg["From"] = from_header
    msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = "<" + in_reply_to + ">"
    msg.set_content(body)
    return msg


def test_forward_routes_via_in_reply_to_to_one_app(
        ddb_table, monkeypatch) -> None:
    """Sender belongs to apps A and B; reply In-Reply-To matches an
    outbound about app A. Forward goes to app A's AAs only, not B's.
    This is the #202 fix for duplicate notification noise."""
    cid = "test-community"
    db.put_community(Community(community_id=cid, name="Parish"))
    app_a = Application(community_id=cid, name="App A", app_type="coverage")
    app_b = Application(community_id=cid, name="App B", app_type="coverage")
    db.put_application(app_a)
    db.put_application(app_b)

    aa_a = User(community_id=cid, email="aa-a@example.com", name="AA-A")
    aa_b = User(community_id=cid, email="aa-b@example.com", name="AA-B")
    sender = User(community_id=cid, email="m@example.com", name="Member")
    db.put_user(aa_a)
    db.put_user(aa_b)
    db.put_user(sender)
    db.put_membership(Membership(community_id=cid, app_id=app_a.app_id,
                                 user_id=aa_a.user_id, app_role="aa"))
    db.put_membership(Membership(community_id=cid, app_id=app_b.app_id,
                                 user_id=aa_b.user_id, app_role="aa"))
    db.put_membership(Membership(community_id=cid, app_id=app_a.app_id,
                                 user_id=sender.user_id, app_role="member"))
    db.put_membership(Membership(community_id=cid, app_id=app_b.app_id,
                                 user_id=sender.user_id, app_role="member"))

    db.put_email_log(EmailLog(
        community_id=cid, direction="outbound",
        from_addr="organizer@example.com", to_addr=sender.email,
        subject="App A -- assigned", provider="ses",
        kind="change_notification", outcome="accepted",
        related_app_id=app_a.app_id,
        provider_message_id="orig-msg-app-a-001",
        ts="2026-06-04T10:00:00+00:00",
    ))

    provider = _CapturingProvider()
    monkeypatch.setattr(inbound, "get_email_provider", lambda: provider)
    monkeypatch.setattr(inbound, "COMMUNITY_ID", cid)

    msg = _build_reply_msg(
        from_header="Member <m@example.com>",
        in_reply_to="orig-msg-app-a-001",
    )
    ok = inbound._forward_to_admins(msg, "Member <m@example.com>",
                                    verdicts_pass=True)
    assert ok is True
    assert len(provider.sent) == 1
    sent = provider.sent[0]
    assert sent["to_addr"] == "aa-a@example.com"
    assert sent["related_app_id"] == app_a.app_id


def test_forward_falls_back_to_membership_when_thread_origin_missing(
        ddb_table, monkeypatch) -> None:
    """In-Reply-To references an unknown message-id: fallback to
    sender-membership scoping (pre-#202 behavior)."""
    cid = "test-community"
    db.put_community(Community(community_id=cid, name="Parish"))
    app_a = Application(community_id=cid, name="App A", app_type="coverage")
    app_b = Application(community_id=cid, name="App B", app_type="coverage")
    db.put_application(app_a)
    db.put_application(app_b)

    aa_a = User(community_id=cid, email="aa-a@example.com", name="AA-A")
    aa_b = User(community_id=cid, email="aa-b@example.com", name="AA-B")
    sender = User(community_id=cid, email="m@example.com", name="Member")
    db.put_user(aa_a)
    db.put_user(aa_b)
    db.put_user(sender)
    db.put_membership(Membership(community_id=cid, app_id=app_a.app_id,
                                 user_id=aa_a.user_id, app_role="aa"))
    db.put_membership(Membership(community_id=cid, app_id=app_b.app_id,
                                 user_id=aa_b.user_id, app_role="aa"))
    db.put_membership(Membership(community_id=cid, app_id=app_a.app_id,
                                 user_id=sender.user_id, app_role="member"))
    db.put_membership(Membership(community_id=cid, app_id=app_b.app_id,
                                 user_id=sender.user_id, app_role="member"))

    provider = _CapturingProvider()
    monkeypatch.setattr(inbound, "get_email_provider", lambda: provider)
    monkeypatch.setattr(inbound, "COMMUNITY_ID", cid)

    msg = _build_reply_msg(
        from_header="Member <m@example.com>",
        in_reply_to="not-a-tracked-message-id",
    )
    inbound._forward_to_admins(msg, "Member <m@example.com>", verdicts_pass=True)
    addrs = sorted(s["to_addr"] for s in provider.sent)
    assert addrs == ["aa-a@example.com", "aa-b@example.com"]


def test_forward_thread_origin_overrides_dual_community_fallback(
        ddb_table, monkeypatch) -> None:
    """Sender is in TWO communities. With a thread origin pointing at
    the non-env community, that community wins. Conversation context
    is more authoritative than the env tie-breaker."""
    db.put_community(Community(community_id="example-community", name="Prod"))
    db.put_community(Community(community_id="c_other", name="Beta"))

    beta_app = Application(community_id="c_other", name="Beta App",
                           app_type="coverage")
    db.put_application(beta_app)
    beta_aa = User(community_id="c_other", email="beta-aa@example.com",
                   name="Beta AA")
    beta_sender = User(community_id="c_other", email="dual@example.com",
                       name="Beta Sender")
    db.put_user(beta_aa)
    db.put_user(beta_sender)
    db.put_membership(Membership(community_id="c_other",
                                 app_id=beta_app.app_id,
                                 user_id=beta_aa.user_id, app_role="aa"))
    db.put_membership(Membership(community_id="c_other",
                                 app_id=beta_app.app_id,
                                 user_id=beta_sender.user_id,
                                 app_role="member"))

    prod_app = Application(community_id="example-community", name="Prod App",
                           app_type="coverage")
    db.put_application(prod_app)
    prod_aa = User(community_id="example-community", email="prod-aa@example.com",
                   name="Prod AA")
    prod_sender = User(community_id="example-community", email="dual@example.com",
                       name="Prod Sender")
    db.put_user(prod_aa)
    db.put_user(prod_sender)
    db.put_membership(Membership(community_id="example-community",
                                 app_id=prod_app.app_id,
                                 user_id=prod_aa.user_id, app_role="aa"))
    db.put_membership(Membership(community_id="example-community",
                                 app_id=prod_app.app_id,
                                 user_id=prod_sender.user_id,
                                 app_role="member"))

    db.put_email_log(EmailLog(
        community_id="c_other", direction="outbound",
        from_addr="organizer@example.com", to_addr="dual@example.com",
        subject="Beta App", provider="ses",
        kind="change_notification", outcome="accepted",
        related_app_id=beta_app.app_id,
        provider_message_id="orig-beta-msg-001",
        ts="2026-06-04T10:00:00+00:00",
    ))

    provider = _CapturingProvider()
    monkeypatch.setattr(inbound, "get_email_provider", lambda: provider)
    monkeypatch.setattr(inbound, "COMMUNITY_ID", "example-community")

    msg = _build_reply_msg(
        from_header="Dual <dual@example.com>",
        in_reply_to="orig-beta-msg-001",
    )
    inbound._forward_to_admins(msg, "Dual <dual@example.com>",
                               verdicts_pass=True)
    assert len(provider.sent) == 1
    assert provider.sent[0]["to_addr"] == "beta-aa@example.com"
    assert provider.sent[0]["community_id"] == "c_other"


def test_forward_uses_references_when_in_reply_to_missing(
        ddb_table, monkeypatch) -> None:
    """Some clients only set References. We walk it for matching ids."""
    cid = "test-community"
    db.put_community(Community(community_id=cid, name="Parish"))
    app_a = Application(community_id=cid, name="App A", app_type="coverage")
    app_b = Application(community_id=cid, name="App B", app_type="coverage")
    db.put_application(app_a)
    db.put_application(app_b)

    aa_a = User(community_id=cid, email="aa-a@example.com", name="AA-A")
    aa_b = User(community_id=cid, email="aa-b@example.com", name="AA-B")
    sender = User(community_id=cid, email="m@example.com", name="Member")
    db.put_user(aa_a)
    db.put_user(aa_b)
    db.put_user(sender)
    db.put_membership(Membership(community_id=cid, app_id=app_a.app_id,
                                 user_id=aa_a.user_id, app_role="aa"))
    db.put_membership(Membership(community_id=cid, app_id=app_b.app_id,
                                 user_id=aa_b.user_id, app_role="aa"))
    db.put_membership(Membership(community_id=cid, app_id=app_a.app_id,
                                 user_id=sender.user_id, app_role="member"))
    db.put_membership(Membership(community_id=cid, app_id=app_b.app_id,
                                 user_id=sender.user_id, app_role="member"))

    db.put_email_log(EmailLog(
        community_id=cid, direction="outbound",
        from_addr="organizer@example.com", to_addr=sender.email,
        subject="App A", provider="ses", kind="change_notification",
        outcome="accepted", related_app_id=app_a.app_id,
        provider_message_id="root-msg-app-a",
        ts="2026-06-04T10:00:00+00:00",
    ))

    provider = _CapturingProvider()
    monkeypatch.setattr(inbound, "get_email_provider", lambda: provider)
    monkeypatch.setattr(inbound, "COMMUNITY_ID", cid)

    msg = email.message.EmailMessage()
    msg["From"] = "Member <m@example.com>"
    msg["Subject"] = "Re: App A"
    msg["References"] = "<root-msg-app-a> <some-other-id>"
    msg.set_content("Reply.")

    inbound._forward_to_admins(msg, "Member <m@example.com>", verdicts_pass=True)
    assert len(provider.sent) == 1
    assert provider.sent[0]["to_addr"] == "aa-a@example.com"
