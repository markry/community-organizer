"""Tests for SMS reminder dispatch + the rollout allowlist safety gate."""
from __future__ import annotations

import datetime as dt

from community_organizer.core import db, standing
from community_organizer.core.models import (
    Application, EmailLog, Membership, Notification, Slot, StandingOccurrence,
    StandingSeries, User,
)
from community_organizer.lambdas import notifier


# ---- SMS body formatters --------------------------------------------------

def test_reminder_sms_is_tight_and_gsm7():
    slot = Slot(community_id="c", app_id="a", yyyy_mm="2026-06",
                template_id="t", name="Usher for Sun 12:00 PM", day_of_week=6,
                start_time="12:00", arrival_offset_minutes=15, duration_minutes=60,
                required_volunteers=2, min_volunteers=1,
                concrete_date="2026-06-21T12:00", local_date="2026-06-21",
                slot_id="s1")
    txt = notifier._reminder_sms(slot, "Example Ushers", "community.example.org")
    assert "Example Ushers" in txt
    assert "12:00 PM" in txt and "arrive 11:45 AM" in txt
    assert "community.example.org/your-schedule" in txt
    assert "—" not in txt and "’" not in txt   # no em-dash / smart quote
    assert txt.isascii()                                  # GSM-7 safe
    assert len(txt) <= 160                                # single segment


def test_occurrence_reminder_sms_includes_when_and_where():
    occ = StandingOccurrence(community_id="c", app_id="a", series_id="s",
                             iso_date="2026-06-21", start_time="19:00",
                             location="Council Hall", occurrence_id="s-2026-06-21")
    app = Application(community_id="c", name="Knights of Columbus",
                      app_type="standing_event", app_id="a")
    txt = notifier._occurrence_reminder_sms(occ, None, app, "Knights of Columbus")
    assert txt.startswith("Reminder: Knights of Columbus")
    assert "7:00 PM" in txt and "Council Hall" in txt
    assert txt.isascii() and len(txt) <= 160


class _FakeEmail:
    def __init__(self):
        self.calls = []

    def send(self, **kw):
        self.calls.append(kw)
        return EmailLog(community_id=kw.get("community_id", ""),
                        direction="outbound", from_addr="x@example.com",
                        to_addr=kw.get("to_addr", "y@y"), subject="s",
                        provider="fake", kind="reminder", outcome="accepted")


class _FakeSms:
    def __init__(self, outcome="accepted"):
        self.calls = []
        self._outcome = outcome

    def send(self, **kw):
        self.calls.append(kw)
        return EmailLog(community_id=kw.get("community_id", ""),
                        direction="outbound", from_addr="+18005550199",
                        to_addr=kw.get("to_phone", ""), subject="(sms reminder)",
                        provider="twilio", kind="reminder", outcome=self._outcome)


def _on(monkeypatch, allowlist=""):
    monkeypatch.setattr(notifier, "SMS_PROVIDER", "twilio")
    monkeypatch.setattr(notifier, "SMS_ALLOWLIST",
                        {x for x in allowlist.split(",") if x})


# ---- pure gate logic ------------------------------------------------------

def test_sms_allowed_requires_everything(monkeypatch):
    _on(monkeypatch)
    u = User(community_id="c", email="a@b.com", name="A",
             channel="sms", phone="555-555-0100")
    assert notifier._sms_allowed(u) is True
    # provider off
    monkeypatch.setattr(notifier, "SMS_PROVIDER", "none")
    assert notifier._sms_allowed(u) is False


def test_sms_allowed_channel_and_phone(monkeypatch):
    _on(monkeypatch)
    assert not notifier._sms_allowed(
        User(community_id="c", email="a@b", name="A", channel="email",
             phone="5555550100"))                      # wrong channel
    assert not notifier._sms_allowed(
        User(community_id="c", email="a@b", name="A", channel="sms"))  # no phone


def test_allowlist_blocks_non_listed_user(monkeypatch):
    _on(monkeypatch, allowlist="member-uid")
    mark = User(community_id="c", email="m@example.com", name="Morgan", user_id="member-uid",
                channel="both", phone="5555550100")
    other = User(community_id="c", email="o@example.com", name="Other", user_id="other-uid",
                 channel="both", phone="5555550100")
    assert notifier._sms_allowed(mark) is True
    assert notifier._sms_allowed(other) is False       # the safety gate


def test_channels_for(monkeypatch):
    _on(monkeypatch, allowlist="member-uid")
    mk = lambda ch, uid="member-uid": User(
        community_id="c", email="m@example.com", name="M", user_id=uid,
        channel=ch, phone="5555550100")
    assert notifier._channels_for(mk("email")) == (True, False)
    assert notifier._channels_for(mk("sms")) == (False, True)    # sms only
    assert notifier._channels_for(mk("both")) == (True, True)
    # sms channel but gated out -> email fallback so it's never dropped
    assert notifier._channels_for(mk("sms", uid="other")) == (True, False)


# ---- integration through _send_reminder (occurrence path) -----------------

def _make_occ(channel="both", phone="555-555-0100", uid="member-uid"):
    app = Application(community_id="c1", name="K of C",
                      app_type="standing_event", app_id="a1",
                      default_timezone="America/New_York")
    db.put_application(app)
    series = StandingSeries(community_id="c1", app_id="a1",
                            recurrence="monthly_2nd_tue",
                            default_start_time="19:00", reminder_lead_days=1)
    db.put_standing_series(series)
    u = User(community_id="c1", email="m@example.com", name="Mem",
             user_id=uid, channel=channel, phone=phone)
    db.put_user(u)
    db.put_membership(Membership(community_id="c1", app_id="a1",
                                 user_id=u.user_id, app_role="member"))
    occ = StandingOccurrence(
        community_id="c1", app_id="a1", series_id=series.series_id,
        iso_date="2026-12-14", start_time="19:00",
        occurrence_id=standing.occurrence_id(series.series_id, "2026-12-14"))
    db.put_standing_occurrence(occ)
    ntf = Notification(community_id="c1", app_id="a1", user_id=u.user_id,
                       slot_id=occ.occurrence_id, yyyy_mm="2026-12",
                       source="occurrence",
                       send_at="2026-12-13T00:00:00+00:00", lead_minutes=1440)
    return ntf


def test_both_channel_sends_email_and_sms(ddb_table, monkeypatch):
    _on(monkeypatch, allowlist="member-uid")
    email, sms = _FakeEmail(), _FakeSms()
    monkeypatch.setattr(notifier, "_get_provider", lambda: email)
    monkeypatch.setattr(notifier, "_get_sms", lambda: sms)

    assert notifier._send_reminder(_make_occ(channel="both")) is True
    assert len(email.calls) == 1
    assert len(sms.calls) == 1
    assert sms.calls[0]["to_phone"] == "555-555-0100"
    # SMS body is the purpose-built one-liner, not the email subject.
    assert sms.calls[0]["body"].startswith("Reminder: K of C,")
    assert "--" not in sms.calls[0]["body"]    # not the "<org> -- reminder:" subject


def test_non_allowlisted_user_gets_email_only(ddb_table, monkeypatch):
    _on(monkeypatch, allowlist="member-uid")
    email, sms = _FakeEmail(), _FakeSms()
    monkeypatch.setattr(notifier, "_get_provider", lambda: email)
    monkeypatch.setattr(notifier, "_get_sms", lambda: sms)

    # channel 'both' but NOT on the allowlist -> no text, email still goes
    assert notifier._send_reminder(_make_occ(channel="both", uid="other")) is True
    assert len(email.calls) == 1
    assert sms.calls == []


def test_sms_channel_sends_no_email(ddb_table, monkeypatch):
    _on(monkeypatch, allowlist="member-uid")
    email, sms = _FakeEmail(), _FakeSms()
    monkeypatch.setattr(notifier, "_get_provider", lambda: email)
    monkeypatch.setattr(notifier, "_get_sms", lambda: sms)

    assert notifier._send_reminder(_make_occ(channel="sms")) is True
    assert sms.calls and not email.calls
