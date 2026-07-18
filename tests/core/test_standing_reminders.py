"""Tests for standing_event reminder materialization (slice 3.x)."""
from __future__ import annotations

import datetime as dt

from community_organizer.core import db, standing
from community_organizer.core.models import (
    Application, Membership, StandingSeries, User,
)


def _make(lead_days: int = 2, channel: str = "email",
          email: str = "m@example.com") -> tuple:
    app = Application(community_id="c1", name="K of C",
                      app_type="standing_event", app_id="a1",
                      default_timezone="America/New_York")
    db.put_application(app)
    series = StandingSeries(community_id="c1", app_id="a1",
                            recurrence="monthly_2nd_tue",
                            default_start_time="19:00",
                            reminder_lead_days=lead_days)
    db.put_standing_series(series)
    u = User(community_id="c1", email=email, name="Mem", channel=channel)
    db.put_user(u)
    db.put_membership(Membership(community_id="c1", app_id="a1",
                                 user_id=u.user_id, app_role="member"))
    first = dt.date.today().replace(day=1)
    standing.materialize_occurrences(
        series, first, standing._add_months_date(first, 12))
    return app, series, u


def _occ_reminders(app_id: str) -> list:
    return [n for n in db.list_pending_notifications(
                up_to="9999-12-31T23:59:59+00:00")
            if n.app_id == app_id and n.source == "occurrence"]


def test_materializes_one_reminder_per_future_occurrence(ddb_table) -> None:
    app, series, u = _make(lead_days=2)
    n = standing.materialize_occurrence_reminders(None, app, series)
    rems = _occ_reminders("a1")
    assert n == len(rems) > 0
    assert all(r.source == "occurrence" for r in rems)
    assert all(r.lead_minutes == 2 * 1440 for r in rems)
    assert all(r.user_id == u.user_id for r in rems)
    # send_at strings are UTC isoformat with seconds (matches coverage)
    assert all(r.send_at.endswith("+00:00") for r in rems)
    # deterministic ids → re-running is idempotent (delete + rebuild)
    n2 = standing.materialize_occurrence_reminders(None, app, series)
    assert n2 == n
    assert len(_occ_reminders("a1")) == n


def test_lead_zero_clears_and_queues_nothing(ddb_table) -> None:
    app, series, u = _make(lead_days=2)
    assert standing.materialize_occurrence_reminders(None, app, series) > 0
    series.reminder_lead_days = 0
    db.put_standing_series(series, expected_version=series.version)
    assert standing.materialize_occurrence_reminders(None, app, series) == 0
    assert _occ_reminders("a1") == []


def test_ineligible_members_excluded(ddb_table) -> None:
    # channel='none' member gets no reminders
    app, series, _ = _make(lead_days=1, channel="none")
    assert standing.materialize_occurrence_reminders(None, app, series) == 0


def test_cancelled_occurrence_skipped(ddb_table) -> None:
    app, series, u = _make(lead_days=2)
    occs = list(db.list_standing_occurrences("a1"))
    # cancel the first future occurrence
    future = [o for o in occs
              if o.iso_date >= dt.date.today().isoformat()]
    target = future[0]
    target.state = "cancelled"
    db.put_standing_occurrence(target, expected_version=target.version)
    rems = []
    standing.materialize_occurrence_reminders(None, app, series)
    rems = _occ_reminders("a1")
    assert all(target.occurrence_id not in r.slot_id or r.slot_id != target.occurrence_id
               for r in rems)
    assert all(r.slot_id != target.occurrence_id for r in rems)


def test_make_occurrence_ics() -> None:
    from community_organizer.core.ical import make_occurrence_ics
    ics = make_occurrence_ics(
        occurrence_id="s1-2026-07-14", iso_date="2026-07-14",
        start_time="19:30", duration_minutes=90,
        summary="Knights of Columbus", user_id="u1",
        user_email="m@example.com", domain="community.example.org",
        community_name="St. Cat", location="Council Hall",
        timezone="America/New_York", notes="Guest speaker")
    assert "BEGIN:VEVENT" in ics and "END:VEVENT" in ics
    assert "METHOD:REQUEST" in ics
    assert "SUMMARY:Knights of Columbus" in ics
    assert "LOCATION:Council Hall" in ics
    assert "UID:" in ics and "s1-2026-07-14" in ics
    # 19:30 ET on 2026-07-14 -> 23:30 UTC
    assert "DTSTART:20260714T233000Z" in ics
    # +90 min -> 01:00 UTC next day
    assert "DTEND:20260715T010000Z" in ics


class _FakeProvider:
    def __init__(self):
        self.calls = []

    def send(self, **kw):
        from community_organizer.core.models import EmailLog
        self.calls.append(kw)
        return EmailLog(community_id=kw.get("community_id", ""),
                        direction="outbound", from_addr="x@example.com", to_addr="y@y",
                        subject="s", provider="fake", kind="reminder",
                        outcome="sent")


def _occ_notification(app, series, user, invites: bool):
    from community_organizer.core.models import Notification, StandingOccurrence
    series.send_calendar_invites = invites
    db.put_standing_series(series, expected_version=series.version)
    occ = StandingOccurrence(
        community_id="c1", app_id="a1", series_id=series.series_id,
        iso_date="2026-12-14", start_time="19:00",
        occurrence_id=standing.occurrence_id(series.series_id, "2026-12-14"))
    db.put_standing_occurrence(occ)
    return Notification(
        community_id="c1", app_id="a1", user_id=user.user_id,
        slot_id=occ.occurrence_id, yyyy_mm="2026-12", source="occurrence",
        send_at="2026-12-13T00:00:00+00:00", lead_minutes=1440)


def test_send_path_attaches_ics_only_when_enabled(ddb_table, monkeypatch) -> None:
    from community_organizer.lambdas import notifier
    app, series, user = _make(lead_days=1)

    fake = _FakeProvider()
    monkeypatch.setattr(notifier, "_get_provider", lambda: fake)

    notifier._send_reminder(_occ_notification(app, series, user, invites=True))
    assert fake.calls and fake.calls[-1]["ics_content"] is not None
    assert "BEGIN:VEVENT" in fake.calls[-1]["ics_content"]

    series2 = db.get_standing_series_for_app("a1")
    notifier._send_reminder(_occ_notification(app, series2, user, invites=False))
    assert fake.calls[-1]["ics_content"] is None


def test_occurrence_reminder_email_builder() -> None:
    from community_organizer.lambdas import notifier
    from community_organizer.core.models import StandingOccurrence
    occ = StandingOccurrence(community_id="c1", app_id="a1", series_id="s1",
                             iso_date="2026-07-14", start_time="19:30",
                             location="Council Hall")
    series = StandingSeries(community_id="c1", app_id="a1",
                            recurrence="monthly_2nd_tue")
    app = Application(community_id="c1", name="Knights of Columbus",
                      app_type="standing_event", app_id="a1")
    user = User(community_id="c1", email="m@example.com", name="Pat")
    subject, body = notifier._occurrence_reminder_email(
        user, occ, series, app, "Knights of Columbus", 2 * 1440)
    assert "Knights of Columbus" in subject
    assert "in 2 days" in subject
    assert "Tuesday, July 14" in body
    assert "7:30 PM" in body
    assert "Council Hall" in body
    assert "Hi Pat," in body
