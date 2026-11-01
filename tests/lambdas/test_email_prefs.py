"""Member email choices: calendar invites, and emails about other members'
changes (requests to cover or trade; who joined or left a shift). Both on by
default; a member, or an admin for them, can turn either off. With reminders
also off, a member gets the schedule announcement plus news of their own
assignments, and nothing else."""
from __future__ import annotations

import urllib.parse

from community_organizer.core import db, prefs, publishing
from community_organizer.core.models import Assignment, User
from community_organizer.lambdas import notifier, web

from .test_inactive_members import _cohort, _get, _mem, _post, world  # noqa: F401


def _off(u: User, *, calendar=False, peer=False) -> User:
    u.calendar_invites, u.peer_emails = not calendar, not peer
    db.put_user(u)
    return db.get_user(u.community_id, u.user_id)


def _assign(w, u: User) -> None:
    db.put_assignment(Assignment(community_id=w["community"].community_id,
                                 app_id=w["app"].app_id, yyyy_mm="2030-06",
                                 slot_id=w["slot"].slot_id, user_id=u.user_id,
                                 local_date=w["slot"].local_date))


def _to(w, who: User) -> list[dict]:
    return [m for m in w["outbox"].sent if m["to_addr"] == who.email]


def test_defaults_on_and_old_records_read_as_on(world) -> None:
    w = world
    stored = db.get_user(w["community"].community_id, w["member"].user_id)
    assert stored.calendar_invites and stored.peer_emails
    # A record written before the fields existed.
    item = db._table().get_item(Key={
        "PK": f"COMM#{w['community'].community_id}",
        "SK": f"USER#{w['member'].user_id}"})["Item"]
    item.pop("calendar_invites"), item.pop("peer_emails")
    old = db._user_from_item(item)
    assert prefs.wants_calendar(old) and prefs.wants_peer_emails(old)


def test_member_turns_both_off_in_settings(world) -> None:
    w = world
    page = web._settings_page(_get("/settings"), w["member"], w["community"],
                              w["app"], _mem(w, w["member"]))["body"]
    assert "Send me calendar invites" in page
    assert "Ask me to cover or trade" in page
    web._api_settings_save(
        _post("/api/settings/save", {"has_prefs": "1", "alarm": "60",
                                     "channel": "email"}),
        w["member"], w["community"], w["app"], _mem(w, w["member"]))
    u = db.get_user(w["community"].community_id, w["member"].user_id)
    assert not u.calendar_invites and not u.peer_emails
    web._api_settings_save(
        _post("/api/settings/save", {"has_prefs": "1", "calendar_invites": "1",
                                     "channel": "email"}),
        u, w["community"], w["app"], _mem(w, u))
    u = db.get_user(w["community"].community_id, w["member"].user_id)
    assert u.calendar_invites and not u.peer_emails


def test_admin_sets_them_for_a_member_and_sees_it(world) -> None:
    w = world
    m = w["member"]
    web._api_user_edit(
        _post("/api/users/edit", {"user_id": m.user_id, "has_prefs": "1",
                                  "peer_emails": "1",
                                  "version": str(m.version or 0)}),
        w["admin"], w["community"], w["app"], _mem(w, w["admin"]))
    u = db.get_user(w["community"].community_id, m.user_id)
    assert not u.calendar_invites and u.peer_emails
    page = web._users_page(_get("/admin/users"), w["admin"], w["community"],
                           w["app"], _mem(w, w["admin"]))["body"]
    assert "No calendar invites" in page
    assert "No cover/trade requests" not in page


def test_admin_form_without_the_boxes_leaves_choices_alone(world) -> None:
    w = world
    m = _off(w["member"], calendar=True, peer=True)
    web._api_user_edit(
        _post("/api/users/edit", {"user_id": m.user_id, "notes": "x",
                                  "version": str(m.version or 0)}),
        w["admin"], w["community"], w["app"], _mem(w, w["admin"]))
    u = db.get_user(w["community"].community_id, m.user_id)
    assert not u.calendar_invites and not u.peer_emails


def test_no_per_slot_invites_but_still_the_announcement(world) -> None:
    w = world
    _assign(w, w["member"])
    assert publishing.plan_invites(w["community"], w["app"], "2030-06")
    _off(w["member"], calendar=True)
    assert publishing.plan_invites(w["community"], w["app"], "2030-06") == []
    plans = publishing.plan_publish(w["community"], w["app"], "2030-06")
    assert w["member"].email in {p.user.email for p in plans}


def test_removal_news_still_sent_without_the_cancellation(world) -> None:
    w = world
    m = _off(w["member"], calendar=True, peer=True)
    web._send_removal_notifications(m, w["admin"], w["community"], w["app"],
                                    w["slot"], "2030-06", self_release=False)
    mine = _to(w, m)
    assert len(mine) == 1 and "removed you from" in mine[0]["body_text"]
    assert mine[0].get("ics_content") is None


def test_co_assignee_news_respects_the_peer_switch(world) -> None:
    w = world
    other, _ = w["add"]("Other")
    _assign(w, w["member"])
    _off(w["member"], peer=True)
    web._send_removal_notifications(other, w["admin"], w["community"],
                                    w["app"], w["slot"], "2030-06",
                                    self_release=True)
    assert _to(w, w["member"]) == []


def test_cover_requests_respect_the_peer_switch(world) -> None:
    w = world
    other, _ = w["add"]("Other")
    _cohort(w, w["app"], "t1", w["member"], other)
    _off(w["member"], peer=True)
    web._notify_cohort_of_opening(w["admin"], w["community"], w["app"],
                                  w["slot"], "2030-06")
    assert _to(w, w["member"]) == []
    assert _to(w, other)


def test_own_assignment_email_drops_calendar_wording(world) -> None:
    m = _off(world["member"], calendar=True)
    _s, on = notifier._assigned_email(m, world["slot"], "Ushers", "Mass")
    _s, off = notifier._assigned_email(m, world["slot"], "Ushers", "Mass",
                                       calendar=False)
    assert "decline this calendar invitation" in on
    assert "calendar" not in off and "your-schedule" in off
    _s, rem = notifier._reminder_email(m, world["slot"], "Ushers", 1440)
    assert "calendar" not in rem


def test_pickup_note_goes_without_the_invite(world) -> None:
    w = world
    m = _off(w["member"], calendar=True)
    web._send_pickup_invite(m, w["community"], w["app"], w["slot"], "2030-06")
    (mail,) = _to(w, m)
    assert mail.get("ics_content") is None
    assert "attached" not in mail["body_text"]


def test_co_assignee_news_still_goes_by_default(world) -> None:
    w = world
    other, _ = w["add"]("Other")
    _assign(w, w["member"])
    web._send_removal_notifications(other, w["admin"], w["community"],
                                    w["app"], w["slot"], "2030-06",
                                    self_release=True)
    assert _to(w, w["member"])        # the control for the test above


def test_member_save_does_not_undo_an_admin_change(world) -> None:
    w = world
    stale = db.get_user(w["community"].community_id, w["member"].user_id)
    web._api_user_edit(
        _post("/api/users/edit", {"user_id": stale.user_id, "has_prefs": "1",
                                  "version": str(stale.version or 0)}),
        w["admin"], w["community"], w["app"], _mem(w, w["admin"]))
    resp = web._api_settings_save(
        _post("/api/settings/save", {"channel": "email"}),
        stale, w["community"], w["app"], _mem(w, stale))
    u = db.get_user(w["community"].community_id, stale.user_id)
    assert not u.calendar_invites and not u.peer_emails
    assert "error=" in resp["headers"]["Location"]


def test_announcement_wording_for_calendar_off(world) -> None:
    w = world
    _assign(w, w["member"])
    _off(w["member"], calendar=True)
    plan = next(p for p in publishing.plan_publish(
        w["community"], w["app"], "2030-06")
        if p.user.user_id == w["member"].user_id)
    assert "calendar" not in plan.body_text
    assert "decline" not in plan.body_html


def test_admin_added_recurring_duty_still_told_without_calendar(world) -> None:
    from community_organizer.core.models import Cohort, SlotTemplate
    w = world
    app = w["app"]
    app.app_type = "recurring_commitments"
    db.put_application(app)
    m = _off(w["member"], calendar=True, peer=True)
    tpl = SlotTemplate(community_id=app.community_id, app_id=app.app_id,
                       name="Duty", day_of_week=0, start_time="12:00",
                       duration_minutes=60, template_id="t9")
    db.put_template(tpl)
    cohort = Cohort(community_id=app.community_id, app_id=app.app_id,
                    name="Duty cohort", linked_template_id="t9")
    db.put_cohort(cohort)
    w["outbox"].sent.clear()
    web._api_cohort_add_member(
        _post("/api/cohorts/add-member", {"cohort_id": cohort.cohort_id,
                                          "user_id": m.user_id}),
        w["admin"], w["community"], app, _mem(w, w["admin"]))
    (mail,) = _to(w, m)
    assert "signed up for Duty" in mail["body_text"]
    assert mail.get("ics_content") is None
    assert "calendar" not in mail["body_text"]


def test_standing_reminders_honor_all_reminders_off(world) -> None:
    import datetime as dt
    from unittest.mock import patch
    from community_organizer.core.models import (
        Notification, StandingOccurrence, StandingSeries,
    )
    w = world
    app = w["app"]
    app.app_type = "standing_event"
    db.put_application(app)
    m = w["member"]
    m.lead_times_minutes = []
    db.put_user(m)
    series = StandingSeries(community_id=app.community_id, app_id=app.app_id,
                            recurrence="weekly", default_start_time="12:00",
                            send_calendar_invites=True)
    db.put_standing_series(series)
    occ = StandingOccurrence(community_id=app.community_id, app_id=app.app_id,
                             series_id=series.series_id, iso_date="2099-03-15",
                             occurrence_id="occ-test")
    db.put_standing_occurrence(occ)
    n = Notification(community_id=app.community_id, app_id=app.app_id,
                     user_id=m.user_id, slot_id=occ.occurrence_id,
                     yyyy_mm="2099-03", lead_minutes=1440,
                     send_at=(dt.datetime.now(dt.timezone.utc)
                              - dt.timedelta(minutes=1)).isoformat(),
                     source="occurrence")
    sent = []
    with patch.object(notifier, "_get_provider", return_value=type(
            "S", (), {"send": lambda self, **kw: sent.append(kw)})()):
        notifier._send_reminder(n)
    assert sent == [] and n.state == "cancelled"
