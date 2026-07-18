"""DDB-backed tests for ``community_organizer.core.publishing``.

These tests exercise the publish flow end-to-end against a moto-mocked
DynamoDB table (the ``ddb_table`` fixture from ``tests/conftest.py``).
No real AWS account is touched — every boto3 call is intercepted by
moto inside the fixture's ``mock_aws`` context.

Each test follows the same pattern:

    1. Seed: build a Community + Application + Users + Memberships +
       Schedule + Slots + Assignments via the ``db`` helpers.
    2. Act:   call the publishing function under test.
    3. Assert: read DDB state and the FakeProvider's recorded calls.

The ``FakeProvider`` class mirrors the ``EmailProvider`` protocol but
just records what it was called with — no I/O. Each ``.send(...)``
appends to ``provider.sent`` and returns an ``EmailLog`` with
``outcome="accepted"`` so the publish summary tallies normally.

Pure-helper tests live in ``test_publishing_pure.py``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from community_organizer.core import db, publishing
from community_organizer.core.models import (
    Application, Assignment, Community, EmailLog, Membership,
    Schedule, Slot, User,
)


# ---------------------------------------------------------------------------
# Fake email provider — records calls instead of sending.
# ---------------------------------------------------------------------------

@dataclass
class FakeProvider:
    """Minimal stand-in for the SES/M365 provider used in tests.

    Records each ``send()`` call as a dict in ``self.sent`` and returns
    a minimal EmailLog so ``publish_schedule`` can tally outcomes.
    Configurable per-call outcome via ``self.outcome`` (default
    ``"accepted"``); set to ``"bounced"`` etc. to test failure paths.
    """
    name: str = "fake"
    outcome: str = "accepted"
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
            outcome=self.outcome,
        )


# ---------------------------------------------------------------------------
# Seeding helpers — build a small but realistic test dataset.
# ---------------------------------------------------------------------------

def _seed_minimal(ddb_table) -> tuple[Community, Application, list[User]]:
    """Create one Community, one Application, three Users (two members)
    and a draft Schedule for 2030-06 with one Slot and one Assignment.

    Returned users:
        users[0] = "Alice" — App member, has an assignment
        users[1] = "Bob"   — App member, no assignment (gets no-slots email)
        users[2] = "Eve"   — NOT a member — must not appear in plans

    The schedule uses 2030 to keep ``_materialize_reminders`` lead-time
    math always-future, so the reminder count is deterministic in CI.
    """
    community = Community(community_id="c1", name="Test Parish")
    db.put_community(community)

    app = Application(community_id="c1", name="Test Ushers",
                      event_noun="Mass",
                      default_timezone="America/New_York",
                      arrival_label="please arrive by", app_type="coverage")
    db.put_application(app)

    users = [
        User(community_id="c1", email="alice@example.com", name="Alice",
             lead_times_minutes=[1440, 120]),
        User(community_id="c1", email="bob@example.com", name="Bob",
             lead_times_minutes=[1440]),
        User(community_id="c1", email="eve@example.com", name="Eve"),
    ]
    for u in users:
        db.put_user(u)

    # Alice + Bob are members; Eve is not.
    for u in users[:2]:
        db.put_membership(Membership(community_id="c1", app_id=app.app_id,
                                     user_id=u.user_id))

    sch = Schedule(community_id="c1", app_id=app.app_id, yyyy_mm="2030-06")
    db.put_schedule(sch)

    slot = Slot(community_id="c1", app_id=app.app_id, yyyy_mm="2030-06",
                template_id="t1", name="Sun 8:00 AM",
                day_of_week=6, start_time="08:00",
                arrival_offset_minutes=10, duration_minutes=60,
                required_volunteers=2, min_volunteers=1,
                concrete_date="2030-06-02", local_date="2030-06-02")
    db.put_slot(slot)

    asg = Assignment(community_id="c1", app_id=app.app_id, yyyy_mm="2030-06",
                     slot_id=slot.slot_id, user_id=users[0].user_id,
                     local_date="2030-06-02")
    db.put_assignment(asg)

    return community, app, users


# ---------------------------------------------------------------------------
# plan_publish — pure-ish (DDB reads only). No state changes.
# ---------------------------------------------------------------------------

def test_plan_publish_only_members_receive(ddb_table) -> None:
    """Eve isn't a member of the app → she must NOT be in the plans."""
    community, app, users = _seed_minimal(ddb_table)
    plans = publishing.plan_publish(community, app, "2030-06")
    names = {p.user.name for p in plans}
    assert names == {"Alice", "Bob"}
    assert "Eve" not in names


def test_plan_publish_skips_undeliverable_email(ddb_table) -> None:
    """Bouncing email → silently skipped (no plan generated for them)."""
    community, app, users = _seed_minimal(ddb_table)
    users[1].email_undeliverable = True
    db.put_user(users[1])

    plans = publishing.plan_publish(community, app, "2030-06")
    assert {p.user.name for p in plans} == {"Alice"}


def test_plan_publish_skips_channel_none(ddb_table) -> None:
    """``channel="none"`` is the safety hatch used during initial import
    — those users must never receive automatic broadcasts."""
    community, app, users = _seed_minimal(ddb_table)
    users[1].channel = "none"
    db.put_user(users[1])

    plans = publishing.plan_publish(community, app, "2030-06")
    assert {p.user.name for p in plans} == {"Alice"}


def test_plan_publish_broadcasts_carry_no_ics(ddb_table) -> None:
    """Broadcast emails never carry .ics — calendar invites are sent
    separately via ``plan_invites`` so every email client renders one
    accept/decline per event regardless of multi-event handling."""
    community, app, users = _seed_minimal(ddb_table)
    plans = publishing.plan_publish(community, app, "2030-06")
    for p in plans:
        assert not hasattr(p, "ics_content") or p.ics_content is None  # legacy guard


def test_plan_invites_one_per_assignment(ddb_table) -> None:
    """Alice has one assignment → one InvitePlan with single-event .ics.
    Bob has none → no invite plan."""
    community, app, users = _seed_minimal(ddb_table)
    invites = publishing.plan_invites(community, app, "2030-06")
    by_user = {ip.user.name: ip for ip in invites}
    assert set(by_user) == {"Alice"}
    inv = by_user["Alice"]
    assert "BEGIN:VCALENDAR" in inv.ics_content
    # Single-event .ics: exactly one VEVENT.
    assert inv.ics_content.count("BEGIN:VEVENT") == 1
    assert inv.ics_content.count("END:VEVENT") == 1
    # Subject names the slot and date.
    assert "Sun 8:00 AM" in inv.subject


def test_plan_invites_skips_undeliverable(ddb_table) -> None:
    """Skip filters mirror plan_publish: bouncing email -> no invite."""
    community, app, users = _seed_minimal(ddb_table)
    users[0].email_undeliverable = True
    db.put_user(users[0])
    invites = publishing.plan_invites(community, app, "2030-06")
    assert invites == []


def test_plan_invites_skips_cancelled_slot(ddb_table) -> None:
    """A cancelled slot generates no invite even if the assignment row
    is still in the table."""
    community, app, users = _seed_minimal(ddb_table)
    slot = next(iter(db.list_slots(app.app_id, "2030-06")))
    slot.cancelled = True
    db.put_slot(slot)
    invites = publishing.plan_invites(community, app, "2030-06")
    assert invites == []


def test_plan_publish_subject_uses_app_name(ddb_table) -> None:
    """Subject line carries the **app** name, not the community name."""
    community, app, users = _seed_minimal(ddb_table)
    plans = publishing.plan_publish(community, app, "2030-06")
    for p in plans:
        assert "Test Ushers" in p.subject
        assert "Test Parish" not in p.subject


def test_publish_subject_and_prefix_overrides(ddb_table) -> None:
    """Compose-and-publish: subject_override replaces the auto subject
    on every broadcast; body prefixes are prepended and a divider line
    separates them from the auto body. Per-slot invites are unaffected."""
    community, app, users = _seed_minimal(ddb_table)
    provider = FakeProvider()

    publishing.publish_schedule(community, app, "2030-06")
    publishing.send_published_schedule_broadcast(
        community, app, "2030-06",
        provider=provider, from_addr="from@example.com",
        subject_override="Important — please read",
        body_prefix_text="Note from your admin: the rota is heavier this month.",
        body_prefix_html="<p>Note from your admin: the rota is heavier this month.</p>",
    )

    broadcasts = [s for s in provider.sent if not s.get("ics_content")]
    invites = [s for s in provider.sent if s.get("ics_content")]
    assert broadcasts and invites
    for b in broadcasts:
        # Subject replaced wholesale.
        assert b["subject"] == "Important — please read"
        # Prefix text is at the top, divider line separates.
        assert b["body_text"].startswith("Note from your admin:")
        assert "-" * 20 in b["body_text"]
        assert "schedule has been published" in b["body_text"]
        # HTML body has the prefix then <hr> then the auto content.
        assert b["body_html"].index(
            "Note from your admin") < b["body_html"].index("<hr>")
    # Invite emails keep their per-event subject — overrides don't touch them.
    for i in invites:
        assert i["subject"] != "Important — please read"
        assert "Sun 8:00 AM" in i["subject"]


# ---------------------------------------------------------------------------
# publish_schedule — happy path.
# ---------------------------------------------------------------------------

def test_publish_transitions_state_only(ddb_table) -> None:
    """Post-#215: publish flips state to ``published`` and materializes
    reminders. NO broadcast or invite emails go out at publish time —
    broadcasting is a separate admin action via
    send_published_schedule_broadcast."""
    community, app, users = _seed_minimal(ddb_table)

    summary = publishing.publish_schedule(community, app, "2030-06")

    sch = db.get_schedule(app.app_id, "2030-06")
    assert sch.state == "published"
    assert sch.published_at is not None
    # Summary surfaces the WOULD-be counts so admins can preview, but
    # no `sent` / `invites_sent` / `by_outcome` keys (those belong to
    # the separate broadcast action).
    assert summary["would_send"] == 2
    assert summary["would_send_invites"] == 1
    assert "sent" not in summary
    assert "invites_sent" not in summary


def test_send_published_schedule_broadcast_sends_broadcast_and_invites(
        ddb_table) -> None:
    """The separated broadcast action sends one broadcast email per
    member (no .ics) plus one calendar-invite email per assignment
    (with .ics). Same shape as the pre-#215 publish-time send."""
    community, app, users = _seed_minimal(ddb_table)
    publishing.publish_schedule(community, app, "2030-06")

    provider = FakeProvider()
    summary = publishing.send_published_schedule_broadcast(
        community, app, "2030-06",
        provider=provider, from_addr="from@example.com",
    )

    assert summary["sent"] == 2          # broadcasts (Alice + Bob)
    assert summary["invites_sent"] == 1  # Alice's one assignment
    assert summary["by_outcome"] == {"accepted": 3}
    assert len(provider.sent) == 3

    with_ics = [s for s in provider.sent if s.get("ics_content")]
    without_ics = [s for s in provider.sent if not s.get("ics_content")]
    assert len(with_ics) == 1
    assert len(without_ics) == 2
    assert with_ics[0]["related_slot_id"] is not None
    assert all(s.get("related_slot_id") is None for s in without_ics)


def test_broadcast_on_non_published_schedule_raises(ddb_table) -> None:
    """Broadcast against a draft schedule raises — the AA must publish
    first. Avoids accidental broadcast of a not-ready schedule."""
    community, app, users = _seed_minimal(ddb_table)
    with pytest.raises(ValueError, match="not.*published"):
        publishing.send_published_schedule_broadcast(
            community, app, "2030-06",
            provider=FakeProvider(), from_addr="from@example.com",
        )


def test_publish_dry_run_makes_no_changes(ddb_table) -> None:
    """``dry_run=True`` returns the planned summary without writing
    anything: the Schedule stays ``draft``. The summary surfaces the
    would-be broadcast counts so admins can sanity-check before the
    separate broadcast step."""
    community, app, users = _seed_minimal(ddb_table)

    summary = publishing.publish_schedule(
        community, app, "2030-06", dry_run=True)

    sch = db.get_schedule(app.app_id, "2030-06")
    assert sch.state == "draft"
    assert summary["dry_run"] is True
    assert summary["would_send"] == 2
    assert summary["would_send_invites"] == 1


# ---------------------------------------------------------------------------
# publish_schedule — the idempotency contract.
# ---------------------------------------------------------------------------

def test_publish_twice_raises_already_published(ddb_table) -> None:
    """Second publish after the first completed raises ValueError.

    This catches the user-level "I clicked publish twice" case where the
    first publish already moved the schedule to ``published``. The
    second click is rejected at the top of ``publish_schedule`` before
    we even build plans, so no duplicate emails go out.
    """
    community, app, users = _seed_minimal(ddb_table)
    publishing.publish_schedule(community, app, "2030-06")
    with pytest.raises(ValueError, match="already published"):
        publishing.publish_schedule(community, app, "2030-06")


def test_publish_loses_concurrency_race(ddb_table, monkeypatch) -> None:
    """Concurrent publish race: B sneaks in and transitions the state
    between A's "is it draft?" read and A's conditional update. A's
    update fails, A raises, no broadcast emails go out from A.

    Simulated by patching ``db.transition_schedule_state`` to raise
    ``ConcurrencyConflict`` before the provider ever sees a send.

    This is the test that pins the "no duplicate broadcast" guarantee.
    """
    community, app, users = _seed_minimal(ddb_table)

    def boom(*args, **kwargs):
        raise db.ConcurrencyConflict("simulated race")

    monkeypatch.setattr(db, "transition_schedule_state", boom)

    with pytest.raises(ValueError, match="not in draft state"):
        publishing.publish_schedule(community, app, "2030-06")


# ---------------------------------------------------------------------------
# _materialize_reminders + unpublish.
# ---------------------------------------------------------------------------

def test_publish_materializes_reminders(ddb_table) -> None:
    """Publish queues one Notification per (assignment, lead time)
    whose send_at is still in the future."""
    community, app, users = _seed_minimal(ddb_table)
    summary = publishing.publish_schedule(
        community, app, "2030-06",
    )
    # Alice is assigned and has 2 leads (1440 + 120 min). Bob has no
    # assignments. So we expect 2 reminders total.
    assert summary["reminders_created"] == 2


def test_publish_skips_reminders_when_template_opts_out(ddb_table) -> None:
    """SlotTemplate.auto_reminders=False suppresses notification
    materialization for slots derived from that template.

    Recurring Commitments apps (e.g. weekly Adoration) want this:
    the calendar app already holds the recurrence — auto reminders
    would be noise."""
    from community_organizer.core.models import SlotTemplate

    community, app, users = _seed_minimal(ddb_table)
    # Replace the seed's faux template_id with a real template that
    # opts out, and re-key the slot + assignment to point at it.
    tpl = SlotTemplate(
        community_id=community.community_id, app_id=app.app_id,
        name="Wed 2 PM", day_of_week=2, start_time="14:00",
        duration_minutes=60, auto_reminders=False,
    )
    db.put_template(tpl)
    # Re-link the existing slot's template_id.
    slot = next(db.list_slots(app.app_id, "2030-06"))
    slot.template_id = tpl.template_id
    db.put_slot(slot)

    summary = publishing.publish_schedule(
        community, app, "2030-06",
    )
    assert summary["reminders_created"] == 0


def test_publish_materializes_reminders_when_template_opts_in(ddb_table) -> None:
    """Sanity check the opposite branch: an explicit auto_reminders=True
    template materializes reminders as usual."""
    from community_organizer.core.models import SlotTemplate

    community, app, users = _seed_minimal(ddb_table)
    tpl = SlotTemplate(
        community_id=community.community_id, app_id=app.app_id,
        name="First Friday 8 PM", day_of_week=4, start_time="20:00",
        duration_minutes=60, auto_reminders=True,
    )
    db.put_template(tpl)
    slot = next(db.list_slots(app.app_id, "2030-06"))
    slot.template_id = tpl.template_id
    db.put_slot(slot)

    summary = publishing.publish_schedule(
        community, app, "2030-06",
    )
    # Same as the no-opt-out case: 2 leads × 1 assignment = 2.
    assert summary["reminders_created"] == 2


def test_unpublish_reverts_state_and_clears_reminders(ddb_table) -> None:
    """Unpublish flips state back to draft and removes pending
    reminders. The schedule + slots + assignments remain intact so
    republish picks up the same data."""
    community, app, users = _seed_minimal(ddb_table)
    publishing.publish_schedule(
        community, app, "2030-06",
    )

    sch = publishing.unpublish_schedule(app, "2030-06")
    assert sch.state == "draft"
    assert sch.published_at is None

    # Reminders are gone. Assignments untouched.
    assert db.delete_notifications_for_schedule(app.app_id, "2030-06") == 0
    assignments = list(db.list_assignments_for_month(app.app_id, "2030-06"))
    assert len(assignments) == 1  # Alice's still there


def test_archive_marks_history_nondestructive(ddb_table) -> None:
    """Archiving is admin-declared age-out, NOT a cancellation. State goes
    to 'archived' and archived_at is stamped, but published_at, reminders,
    and assignments are all left intact (the opposite of unpublish)."""
    community, app, users = _seed_minimal(ddb_table)
    publishing.publish_schedule(community, app, "2030-06")
    assert db.get_schedule(app.app_id, "2030-06").published_at is not None

    sch = publishing.archive_schedule(
        app, "2030-06", archived_at="2030-06-20T12:00:00+00:00")
    assert sch.state == "archived"
    assert sch.archived_at == "2030-06-20T12:00:00+00:00"
    # published_at preserved (it really was published).
    fresh = db.get_schedule(app.app_id, "2030-06")
    assert fresh.state == "archived"
    assert fresh.published_at is not None
    # Assignments intact.
    assert len(list(db.list_assignments_for_month(app.app_id, "2030-06"))) == 1
    # Reminders NOT cleared (archive never touches notifications). The
    # delete both proves they survived and cleans up.
    assert db.delete_notifications_for_schedule(app.app_id, "2030-06") > 0


def test_archive_then_reactivate_restores_published(ddb_table) -> None:
    community, app, _ = _seed_minimal(ddb_table)
    publishing.publish_schedule(community, app, "2030-06")
    publishing.archive_schedule(app, "2030-06", archived_at="2030-06-20T00:00:00+00:00")
    sch = publishing.reactivate_schedule(app, "2030-06")
    assert sch.state == "published"
    assert sch.archived_at is None
    fresh = db.get_schedule(app.app_id, "2030-06")
    assert fresh.state == "published" and fresh.archived_at is None


def test_archive_requires_published(ddb_table) -> None:
    community, app, _ = _seed_minimal(ddb_table)
    # Still draft — archiving must refuse.
    with pytest.raises(ValueError, match="not currently published"):
        publishing.archive_schedule(app, "2030-06", archived_at="2030-06-01T00:00:00+00:00")


def test_reactivate_requires_archived(ddb_table) -> None:
    community, app, _ = _seed_minimal(ddb_table)
    publishing.publish_schedule(community, app, "2030-06")
    # Published, not archived — reactivate must refuse.
    with pytest.raises(ValueError, match="not currently archived"):
        publishing.reactivate_schedule(app, "2030-06")


def test_unpublish_uses_conditional_transition(ddb_table) -> None:
    """Unpublish must be a conditional update on state=published, not
    an unconditional put. Otherwise a concurrent publish could be
    silently clobbered (security fix M4).

    We verify by trying to unpublish a draft schedule — it must raise
    rather than silently overwrite."""
    community, app, _ = _seed_minimal(ddb_table)
    # Schedule is still draft (we never published it).
    with pytest.raises(ValueError, match="not currently published"):
        publishing.unpublish_schedule(app, "2030-06")


def test_publish_twice_concurrent_only_one_locks(ddb_table) -> None:
    """Two simultaneous publishes both pass the initial "is it draft"
    check but only one wins the atomic transition. The loser gets a
    clear error referencing the publish-in-progress."""
    community, app, _ = _seed_minimal(ddb_table)
    # Pretend a publish is already in flight.
    db.transition_schedule_state(
        app.app_id, "2030-06",
        from_state="draft", to_state="publishing",
        published_at="2030-06-01T00:00:00+00:00",
    )
    with pytest.raises(ValueError, match="already being published"):
        publishing.publish_schedule(community, app, "2030-06")


def test_broadcast_continues_on_per_send_failure(ddb_table) -> None:
    """Post-#215: if provider.send raises mid-broadcast, the action
    must continue to later recipients. (Pre-#215 this also asserted
    reminder materialization continuing — that's now part of publish,
    not broadcast, so reminder behavior is covered by the
    materialization tests above.)"""
    community, app, users = _seed_minimal(ddb_table)
    publishing.publish_schedule(community, app, "2030-06")

    @dataclass
    class FlakyProvider:
        name: str = "flaky"
        sent: list = field(default_factory=list)

        def send(self, **kwargs):
            # First call raises; subsequent succeed.
            if not self.sent:
                self.sent.append(kwargs)
                raise RuntimeError("simulated SES throttle")
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

    flaky = FlakyProvider()
    summary = publishing.send_published_schedule_broadcast(
        community, app, "2030-06",
        provider=flaky, from_addr="from@example.com",
    )
    # All sends attempted: 2 broadcasts + 1 invite. The first send raises
    # so we expect 1 error + 2 accepted.
    assert summary["by_outcome"].get("error", 0) == 1
    assert summary["by_outcome"].get("accepted", 0) == 2


def test_republish_after_unpublish_succeeds(ddb_table) -> None:
    """Once unpublished, republish should work — state is draft again,
    so the conditional update accepts. The cycle preserves state +
    reminder behavior; broadcast is now a separate concern (#215)."""
    community, app, users = _seed_minimal(ddb_table)
    first = publishing.publish_schedule(community, app, "2030-06")
    publishing.unpublish_schedule(app, "2030-06")
    second = publishing.publish_schedule(community, app, "2030-06")
    # Both cycles re-materialize the same reminders.
    assert first["reminders_created"] == 2
    assert second["reminders_created"] == 2
    # Schedule ended back at published.
    assert db.get_schedule(app.app_id, "2030-06").state == "published"
