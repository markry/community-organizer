"""Tests for the standing_event AA setup page + save handler (slice 3)."""
from __future__ import annotations

import datetime as dt
import urllib.parse

from community_organizer.core import db
from community_organizer.core.models import Application, Membership, User
from community_organizer.lambdas import web


def _setup(role: str = "aa") -> tuple[Application, User, Membership]:
    app = Application(community_id="c1", name="K of C",
                      app_type="standing_event", app_id="a1")
    db.put_application(app)
    user = User(community_id="c1", email="aa@example.com", name="Admin")
    db.put_user(user)
    mem = Membership(community_id="c1", app_id="a1", user_id=user.user_id,
                     app_role=role)
    db.put_membership(mem)
    return app, user, mem


def _post(fields: dict) -> dict:
    return {"requestContext": {"http": {"method": "POST"}},
            "body": urllib.parse.urlencode(fields), "isBase64Encoded": False}


def test_setup_creates_series_and_materializes(ddb_table) -> None:
    app, user, mem = _setup()
    resp = web._api_standing_setup_save(
        _post({"ordinal": "2nd", "weekday": "tue", "location": "Hall",
               "start_time": "19:00", "duration": "90", "lead_days": "2",
               "attendance": "on"}),
        user, None, app, mem)
    assert resp["statusCode"] == 302
    series = db.get_standing_series_for_app("a1")
    assert series is not None
    assert series.recurrence == "monthly_2nd_tue"
    assert series.default_location == "Hall"
    assert series.default_start_time == "19:00"
    assert series.default_duration_minutes == 90
    assert series.reminder_lead_days == 2
    assert series.attendance_tracking is True
    assert series.send_calendar_invites is False  # unchecked → not submitted
    occs = list(db.list_standing_occurrences("a1"))
    assert occs, "forward window should be materialized"
    assert all(dt.date.fromisoformat(o.iso_date).weekday() == 1 for o in occs)


def test_setup_updates_existing_series(ddb_table) -> None:
    app, user, mem = _setup()
    web._api_standing_setup_save(_post({"ordinal": "2nd", "weekday": "tue"}),
                                 user, None, app, mem)
    v1 = db.get_standing_series_for_app("a1")
    web._api_standing_setup_save(
        _post({"ordinal": "1st", "weekday": "mon", "invites": "on",
               "lead_days": "0"}),
        user, None, app, mem)
    v2 = db.get_standing_series_for_app("a1")
    assert v2.series_id == v1.series_id
    assert v2.recurrence == "monthly_1st_mon"
    assert v2.send_calendar_invites is True
    assert v2.reminder_lead_days == 0
    assert v2.version == v1.version + 1


def test_setup_rejects_non_admin(ddb_table) -> None:
    app, user, _ = _setup()
    member = Membership(community_id="c1", app_id="a1", user_id="u2",
                        app_role="member")
    resp = web._api_standing_setup_save(
        _post({"ordinal": "2nd", "weekday": "tue"}), user, None, app, member)
    assert resp["statusCode"] == 403
    assert db.get_standing_series_for_app("a1") is None


def test_setup_rejects_bad_recurrence(ddb_table) -> None:
    app, user, mem = _setup()
    resp = web._api_standing_setup_save(
        _post({"ordinal": "9th", "weekday": "xyz"}), user, None, app, mem)
    assert resp["statusCode"] == 302  # error redirect
    assert "error=" in resp["headers"]["Location"]
    assert db.get_standing_series_for_app("a1") is None


def test_setup_page_renders_form(ddb_table) -> None:
    app, user, mem = _setup()
    resp = web._standing_setup_page(
        {"requestContext": {"http": {"method": "GET"}}}, user, None, app, mem)
    assert resp["statusCode"] == 200
    body = resp["body"]
    assert "Meeting schedule" in body
    assert "action='/api/standing/setup'" in body
    assert "name='ordinal'" in body and "name='weekday'" in body


def test_setup_page_prefills_existing(ddb_table) -> None:
    app, user, mem = _setup()
    web._api_standing_setup_save(
        _post({"ordinal": "last", "weekday": "fri", "location": "Rectory"}),
        user, None, app, mem)
    resp = web._standing_setup_page(
        {"requestContext": {"http": {"method": "GET"}}}, user, None, app, mem)
    body = resp["body"]
    assert "value='last' selected" in body
    assert "value='fri' selected" in body
    assert "Rectory" in body
