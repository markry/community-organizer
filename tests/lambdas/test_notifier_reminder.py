"""Slot-reminder dispatch guards in notifier._send_reminder.

Regression coverage for the "reminder after trade-out" bug: a member who
released or traded out of a slot kept getting the day-before reminder because
release/trade deletes the Assignment row but does not cancel the already-queued
reminder. _send_reminder now re-validates the assignment at send time.
"""
from __future__ import annotations

from community_organizer.core import db
from community_organizer.core.models import (
    Application, Assignment, EmailLog, Membership, Notification, Slot, User,
)
from community_organizer.lambdas import notifier


def _make_slot_reminder(*, assigned: bool):
    """Build a coverage app + slot + queued slot reminder for one member.

    When ``assigned`` is False the Assignment row is omitted, modelling a
    member who has released/traded out of the slot.
    """
    app = Application(community_id="c1", name="Ushers",
                      app_type="recurring_commitments", app_id="a1",
                      default_timezone="America/New_York")
    db.put_application(app)
    u = User(community_id="c1", email="m@example.com", name="Member",
             user_id="member-uid", channel="email")
    db.put_user(u)
    db.put_membership(Membership(community_id="c1", app_id="a1",
                                 user_id=u.user_id, app_role="member"))
    slot = Slot(community_id="c1", app_id="a1", yyyy_mm="2026-08",
                template_id="t", name="Usher for Sun 10:00 AM", day_of_week=6,
                start_time="10:00", arrival_offset_minutes=15,
                duration_minutes=60, required_volunteers=3, min_volunteers=1,
                concrete_date="2026-08-09T10:00", local_date="2026-08-09",
                slot_id="s1")
    db.put_slot(slot)
    if assigned:
        db.put_assignment(Assignment(
            community_id="c1", app_id="a1", yyyy_mm="2026-08",
            slot_id="s1", user_id=u.user_id, local_date="2026-08-09"))
    return Notification(community_id="c1", app_id="a1", user_id=u.user_id,
                        slot_id="s1", yyyy_mm="2026-08",
                        send_at="2026-08-08T13:45:00+00:00", lead_minutes=1440)


class _FakeEmail:
    def __init__(self):
        self.calls = []

    def send(self, **kw):
        self.calls.append(kw)
        return EmailLog(community_id=kw.get("community_id", ""),
                        direction="outbound", from_addr="x@example.com",
                        to_addr=kw.get("to_addr", "y@y"), subject="s",
                        provider="fake", kind="reminder", outcome="accepted")


def test_slot_reminder_sends_when_still_assigned(ddb_table, monkeypatch):
    email = _FakeEmail()
    monkeypatch.setattr(notifier, "_get_provider", lambda: email)
    ntf = _make_slot_reminder(assigned=True)

    assert notifier._send_reminder(ntf) is True
    assert len(email.calls) == 1
    assert ntf.state == "sent"


def test_slot_reminder_cancelled_after_trade_out(ddb_table, monkeypatch):
    """The bug: reminder must NOT fire once the member no longer holds the slot."""
    email = _FakeEmail()
    monkeypatch.setattr(notifier, "_get_provider", lambda: email)
    ntf = _make_slot_reminder(assigned=False)

    assert notifier._send_reminder(ntf) is False
    assert email.calls == []                      # no reminder sent
    assert ntf.state == "cancelled"               # row retired, won't retry
    # the cancelled row must not resurface on the next poll
    assert ntf.notification_id not in {
        n.notification_id for n in db.list_pending_notifications(up_to="2099-01-01")}
