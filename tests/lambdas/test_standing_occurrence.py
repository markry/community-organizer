"""Tests for the standing_event per-occurrence drawer, member RSVP, and
AA per-occurrence actions (slice 4)."""
from __future__ import annotations

import datetime as dt
import urllib.parse

from community_organizer.core import db, standing
from community_organizer.core.models import (
    Application, Membership, StandingOccurrence, StandingSeries, User,
)
from community_organizer.lambdas import web


def _setup(*, attendance: bool = True):
    """App + AA + one plain member + a series (attendance opt-in) + one
    materialized occurrence a couple days out (relative to today, so the
    reminder-window logic isn't brittle to the calendar date)."""
    _occ_date = (dt.date.today() + dt.timedelta(days=2)).isoformat()
    app = Application(community_id="c1", name="K of C",
                      app_type="standing_event", app_id="a1")
    db.put_application(app)
    aa = User(community_id="c1", email="aa@example.com", name="Admin")
    db.put_user(aa)
    db.put_membership(Membership(community_id="c1", app_id="a1",
                                 user_id=aa.user_id, app_role="aa"))
    member = User(community_id="c1", email="bob@example.com", name="Bob")
    db.put_user(member)
    member_mem = Membership(community_id="c1", app_id="a1",
                            user_id=member.user_id, app_role="member")
    db.put_membership(member_mem)

    series = StandingSeries(
        community_id="c1", app_id="a1", recurrence="monthly_2nd_tue",
        default_location="Hall", default_start_time="19:00",
        attendance_tracking=attendance, reminder_lead_days=1)
    db.put_standing_series(series)
    occ = StandingOccurrence(
        community_id="c1", app_id="a1", series_id=series.series_id,
        iso_date=_occ_date,
        occurrence_id=standing.occurrence_id(series.series_id, _occ_date))
    db.put_standing_occurrence(occ)
    aa_mem = db.get_membership("a1", aa.user_id)
    return app, aa, aa_mem, member, member_mem, series, occ


def _post(occ, fields: dict) -> dict:
    body = {"occ": occ.occurrence_id, "d": occ.iso_date,
            "month_offset": "0", **fields}
    return {"requestContext": {"http": {"method": "POST"}},
            "body": urllib.parse.urlencode(body), "isBase64Encoded": False}


# --- member RSVP -----------------------------------------------------------

def test_rsvp_creates_then_updates(ddb_table) -> None:
    app, aa, aa_mem, member, member_mem, series, occ = _setup()
    resp = web._api_standing_rsvp(_post(occ, {"response": "yes"}),
                                  member, None, app, member_mem)
    assert resp["statusCode"] == 302
    assert "notice=" in resp["headers"]["Location"]
    r = db.get_standing_rsvp("a1", occ.occurrence_id, member.user_id)
    assert r is not None and r.response == "yes"
    v1 = r.version

    web._api_standing_rsvp(_post(occ, {"response": "maybe"}),
                           member, None, app, member_mem)
    r2 = db.get_standing_rsvp("a1", occ.occurrence_id, member.user_id)
    assert r2.response == "maybe"
    assert r2.version == v1 + 1  # upsert, not a duplicate row
    assert len(list(db.list_standing_rsvps_for_occurrence(
        "a1", occ.occurrence_id))) == 1


def test_rsvp_rejects_bad_response(ddb_table) -> None:
    app, aa, aa_mem, member, member_mem, series, occ = _setup()
    resp = web._api_standing_rsvp(_post(occ, {"response": "perhaps"}),
                                  member, None, app, member_mem)
    assert "error=" in resp["headers"]["Location"]
    assert db.get_standing_rsvp("a1", occ.occurrence_id, member.user_id) is None


def test_rsvp_rejected_when_attendance_off(ddb_table) -> None:
    app, aa, aa_mem, member, member_mem, series, occ = _setup(attendance=False)
    resp = web._api_standing_rsvp(_post(occ, {"response": "yes"}),
                                  member, None, app, member_mem)
    assert "error=" in resp["headers"]["Location"]
    assert db.get_standing_rsvp("a1", occ.occurrence_id, member.user_id) is None


def test_rsvp_rejected_for_cancelled_occurrence(ddb_table) -> None:
    app, aa, aa_mem, member, member_mem, series, occ = _setup()
    occ.state = "cancelled"
    db.put_standing_occurrence(occ, expected_version=occ.version)
    resp = web._api_standing_rsvp(_post(occ, {"response": "yes"}),
                                  member, None, app, member_mem)
    assert "error=" in resp["headers"]["Location"]
    assert db.get_standing_rsvp("a1", occ.occurrence_id, member.user_id) is None


# --- AA per-occurrence actions --------------------------------------------

def test_aa_cancel_and_reinstate(ddb_table) -> None:
    app, aa, aa_mem, member, member_mem, series, occ = _setup()
    web._api_standing_occurrence_action(_post(occ, {"action": "cancel"}),
                                        aa, None, app, aa_mem)
    assert db.get_standing_occurrence("a1", occ.iso_date,
                                      occ.occurrence_id).state == "cancelled"
    web._api_standing_occurrence_action(_post(occ, {"action": "reinstate"}),
                                        aa, None, app, aa_mem)
    assert db.get_standing_occurrence("a1", occ.iso_date,
                                      occ.occurrence_id).state == "scheduled"


def test_aa_update_overrides(ddb_table) -> None:
    app, aa, aa_mem, member, member_mem, series, occ = _setup()
    web._api_standing_occurrence_action(
        _post(occ, {"action": "update", "start_time": "18:30",
                    "location": "Rectory", "notes": "Guest: Fr. X"}),
        aa, None, app, aa_mem)
    o = db.get_standing_occurrence("a1", occ.iso_date, occ.occurrence_id)
    assert o.start_time == "18:30"
    assert o.location == "Rectory"
    assert o.notes == "Guest: Fr. X"


def test_aa_update_blank_clears_to_inherit(ddb_table) -> None:
    app, aa, aa_mem, member, member_mem, series, occ = _setup()
    web._api_standing_occurrence_action(
        _post(occ, {"action": "update", "start_time": "", "location": "",
                    "notes": ""}),
        aa, None, app, aa_mem)
    o = db.get_standing_occurrence("a1", occ.iso_date, occ.occurrence_id)
    assert o.start_time is None and o.location is None and o.notes is None


def test_aa_action_rejects_non_admin(ddb_table) -> None:
    app, aa, aa_mem, member, member_mem, series, occ = _setup()
    resp = web._api_standing_occurrence_action(
        _post(occ, {"action": "cancel"}), member, None, app, member_mem)
    assert resp["statusCode"] == 403
    assert db.get_standing_occurrence("a1", occ.iso_date,
                                      occ.occurrence_id).state == "scheduled"


def _reminders_for_occ(occ) -> list:
    far_future = "9999-12-31T00:00:00+00:00"
    return [n for n in db.list_pending_notifications(up_to=far_future)
            if n.slot_id == occ.occurrence_id]


def test_cancel_suppresses_reminder(ddb_table) -> None:
    app, aa, aa_mem, member, member_mem, series, occ = _setup()
    # Materialize the forward window once — both members get a reminder
    # for the occurrence.
    standing.materialize_occurrence_reminders(None, app, series)
    assert _reminders_for_occ(occ), "reminders should exist pre-cancel"
    web._api_standing_occurrence_action(_post(occ, {"action": "cancel"}),
                                        aa, None, app, aa_mem)
    # The cancel handler re-materializes; the cancelled occurrence is
    # skipped, so no reminder references it any more.
    assert _reminders_for_occ(occ) == []


# --- drawer rendering ------------------------------------------------------

def test_drawer_member_shows_rsvp_buttons(ddb_table) -> None:
    app, aa, aa_mem, member, member_mem, series, occ = _setup()
    db.put_standing_rsvp(web.StandingRSVP(
        community_id="c1", app_id="a1", occurrence_id=occ.occurrence_id,
        user_id=member.user_id, response="yes"))
    html = web._standing_occurrence_drawer(
        event={}, occ=occ, series=series, app=app, user=member,
        community=None, membership=member_mem, is_admin=False, month_offset=0)
    assert "Will you attend?" in html
    assert "value='yes'" in html and "value='no'" in html
    assert "coming" in html  # current-response readout
    assert "Edit this meeting" not in html  # AA-only


def test_drawer_admin_shows_roster_and_actions(ddb_table) -> None:
    app, aa, aa_mem, member, member_mem, series, occ = _setup()
    db.put_standing_rsvp(web.StandingRSVP(
        community_id="c1", app_id="a1", occurrence_id=occ.occurrence_id,
        user_id=member.user_id, response="yes"))
    html = web._standing_occurrence_drawer(
        event={}, occ=occ, series=series, app=app, user=aa,
        community=None, membership=aa_mem, is_admin=True, month_offset=0)
    assert "Attendance" in html
    assert "Coming (1)" in html
    assert "Bob" in html
    assert "No response (1)" in html  # the AA hasn't responded
    assert "Edit this meeting" in html
    assert "Cancel this meeting" in html
