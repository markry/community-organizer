"""Tests for the poll-reply auto-nudge (inbound).

When a flexible_event member REPLIES to a poll invite by email instead of
using their magic link, the inbound Lambda emails them back their personal
link (once) and still forwards the reply to the AA. Covered here:

    - a matching reply triggers exactly one nudge (kind=event_reply_nudge)
      carrying the member's link, AND the admin forward still happens
    - the nudge is bounded to once per (event, user) via reply_nudged_at
    - auto-responders (Auto-Submitted / Precedence / List-*) never get nudged
    - a reply whose subject doesn't match an open poll is not nudged
    - a revoked (opted-out) token is not nudged
"""
from __future__ import annotations

import datetime as dt
import email
from dataclasses import dataclass, field
from typing import Any

import pytest

from community_organizer.core import db
from community_organizer.core.models import (
    Application, Community, EmailLog, EventToken, FlexibleEvent,
    FlexiblePollOption, Membership, User,
)
from community_organizer.lambdas import inbound


@dataclass
class _CapturingProvider:
    name: str = "fake"
    sent: list[dict[str, Any]] = field(default_factory=list)

    def send(self, **kwargs: Any) -> EmailLog:
        self.sent.append(kwargs)
        return EmailLog(
            community_id=kwargs.get("community_id", ""), direction="outbound",
            from_addr=kwargs.get("from_addr", ""),
            to_addr=kwargs.get("to_addr", ""),
            subject=kwargs.get("subject", ""), provider=self.name,
            kind=kwargs.get("kind", "other"), outcome="accepted")


_TITLE = "Summer Couples Bookclub"


@pytest.fixture
def bc(ddb_table, monkeypatch):
    """A flexible_event app with an AA, one member, an open poll + a token
    for the member. Returns the handles the tests need."""
    cid = "bc-community"
    db.put_community(Community(community_id=cid, name="Test Parish"))
    app = Application(community_id=cid, name="Summer Book Club",
                      app_type="flexible_event", app_id="a1")
    db.put_application(app)

    aa = User(community_id=cid, email="aa@example.com", name="Organizer")
    member = User(community_id=cid, email="katy@example.com",
                  name="Katherine Member")
    db.put_user(aa)
    db.put_user(member)
    db.put_membership(Membership(community_id=cid, app_id="a1",
                                 user_id=aa.user_id, app_role="aa"))
    db.put_membership(Membership(community_id=cid, app_id="a1",
                                 user_id=member.user_id, app_role="member"))

    evt = FlexibleEvent(community_id=cid, app_id="a1", title=_TITLE,
                        state="poll")
    db.put_flexible_event(evt)
    db.put_flexible_poll_option(FlexiblePollOption(
        community_id=cid, app_id="a1", event_id=evt.event_id,
        iso_date="2026-08-15", start_time="18:30", sort_key=0))
    db.put_event_token(EventToken(
        community_id=cid, app_id="a1", event_id=evt.event_id,
        user_id=member.user_id, token="tok-katy",
        expires_at=dt.datetime(2099, 1, 1, tzinfo=dt.timezone.utc).isoformat()))

    provider = _CapturingProvider()
    monkeypatch.setattr(inbound, "get_email_provider", lambda: provider)
    monkeypatch.setattr(inbound, "COMMUNITY_ID", cid)
    return {"cid": cid, "app": app, "aa": aa, "member": member, "event": evt,
            "provider": provider}


def _reply(*, from_header="Katherine Member <katy@example.com>",
           subject=f"Re: Summer Book Club: vote on dates for {_TITLE}",
           body="All dates work for us.", headers=None):
    msg = email.message.EmailMessage()
    msg["From"] = from_header
    msg["Subject"] = subject
    for k, v in (headers or {}).items():
        msg[k] = v
    msg.set_content(body)
    return msg


def _sends_by_kind(provider):
    out = {}
    for s in provider.sent:
        out.setdefault(s.get("kind"), []).append(s)
    return out


# ---------------------------------------------------------------------------

def test_reply_to_poll_nudges_member_and_forwards_to_admin(bc) -> None:
    result = inbound._forward_to_admins(
        _reply(), "Katherine Member <katy@example.com>", verdicts_pass=True)
    assert result is True
    by_kind = _sends_by_kind(bc["provider"])
    # member got exactly one nudge with their link
    nudges = by_kind.get("event_reply_nudge", [])
    assert len(nudges) == 1
    assert nudges[0]["to_addr"] == "katy@example.com"
    assert "/e/tok-katy" in nudges[0]["body_text"]
    # admin still got the forward, with the nudge note
    fwds = [s for s in bc["provider"].sent if s["to_addr"] == "aa@example.com"]
    assert len(fwds) == 1
    assert "automatically emailed" in fwds[0]["body_text"]
    # token stamped so we won't nudge again
    tok = db.get_event_token("a1", bc["event"].event_id, bc["member"].user_id)
    assert tok.reply_nudged_at is not None


def test_second_reply_does_not_nudge_again(bc) -> None:
    frm = "Katherine Member <katy@example.com>"
    inbound._forward_to_admins(_reply(), frm, verdicts_pass=True)
    bc["provider"].sent.clear()
    inbound._forward_to_admins(_reply(body="ok thanks"), frm, verdicts_pass=True)
    by_kind = _sends_by_kind(bc["provider"])
    assert by_kind.get("event_reply_nudge", []) == []       # no second nudge
    # but the admin forward still happens
    assert any(s["to_addr"] == "aa@example.com" for s in bc["provider"].sent)


@pytest.mark.parametrize("headers", [
    {"Auto-Submitted": "auto-replied"},
    {"Precedence": "bulk"},
    {"X-Autoreply": "yes"},
    {"List-Id": "<something.example.com>"},
])
def test_auto_responder_is_not_nudged(bc, headers) -> None:
    inbound._forward_to_admins(
        _reply(headers=headers), "Katherine Member <katy@example.com>",
        verdicts_pass=True)
    by_kind = _sends_by_kind(bc["provider"])
    assert by_kind.get("event_reply_nudge", []) == []
    tok = db.get_event_token("a1", bc["event"].event_id, bc["member"].user_id)
    assert tok.reply_nudged_at is None


def test_unrelated_subject_not_nudged(bc) -> None:
    """A reply that doesn't reference the poll (e.g. a coverage-style
    subject) must not trigger a poll nudge."""
    inbound._forward_to_admins(
        _reply(subject="Re: can I swap my slot?"),
        "Katherine Member <katy@example.com>", verdicts_pass=True)
    by_kind = _sends_by_kind(bc["provider"])
    assert by_kind.get("event_reply_nudge", []) == []


def test_revoked_token_not_nudged(bc) -> None:
    tok = db.get_event_token("a1", bc["event"].event_id, bc["member"].user_id)
    tok.revoked = True
    db.put_event_token(tok)
    inbound._forward_to_admins(
        _reply(), "Katherine Member <katy@example.com>", verdicts_pass=True)
    by_kind = _sends_by_kind(bc["provider"])
    assert by_kind.get("event_reply_nudge", []) == []


def test_is_auto_response_unit() -> None:
    def mk(**h):
        m = email.message.EmailMessage()
        for k, v in h.items():
            m[k.replace("_", "-")] = v
        return m
    assert inbound._is_auto_response(mk(Auto_Submitted="auto-generated"))
    assert inbound._is_auto_response(mk(Precedence="bulk"))
    assert inbound._is_auto_response(mk(List_Unsubscribe="<mailto:x>"))
    assert not inbound._is_auto_response(mk(Auto_Submitted="no"))
    assert not inbound._is_auto_response(mk(Subject="Re: normal reply"))
