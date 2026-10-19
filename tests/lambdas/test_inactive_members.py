"""Members stepping out of a scheduling app's rotation (Membership.inactive_*).

Drives the real handlers against a moto table. An inactive member is left
out of the pick list, bulk assign, sign-ups and opening emails, and isn't
sent the schedule for a period they're out of with nothing assigned. Event
apps have no rotation and refuse the action.
"""
from __future__ import annotations

import urllib.parse

import pytest

from community_organizer.core import db, publishing
from community_organizer.core.models import (
    Application, Assignment, Community, Membership, Schedule, Slot, User,
)
from community_organizer.lambdas import web

CID = "c1"


class _Outbox:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(self, **kw):
        self.sent.append(kw)


@pytest.fixture
def world(ddb_table, monkeypatch):
    outbox = _Outbox()
    monkeypatch.setattr(
        "community_organizer.providers.email.get_email_provider",
        lambda: outbox)
    community = Community(community_id=CID, name="Parish",
                          default_timezone="America/New_York")
    db.put_community(community)
    app = Application(community_id=CID, name="Ushers", app_type="coverage")
    db.put_application(app)

    def add(name: str, role: str = "member") -> tuple[User, Membership]:
        u = User(community_id=CID, email=f"{name}@example.com", name=name)
        db.put_user(u)
        m = Membership(community_id=CID, app_id=app.app_id,
                       user_id=u.user_id, app_role=role)
        db.put_membership(m)
        return u, m

    admin, _ = add("Admin", role="aa")
    member, _ = add("Member")
    db.put_schedule(Schedule(community_id=CID, app_id=app.app_id,
                             yyyy_mm="2030-06", state="published"))
    slot = Slot(community_id=CID, app_id=app.app_id, yyyy_mm="2030-06",
                template_id="t1", name="Sun 8:00 AM", day_of_week=6,
                start_time="08:00", arrival_offset_minutes=10,
                duration_minutes=60, required_volunteers=2, min_volunteers=1,
                concrete_date="2030-06-02", local_date="2030-06-02")
    db.put_slot(slot)
    return dict(community=community, app=app, admin=admin, member=member,
                slot=slot, add=add, outbox=outbox)


def _post(path: str, form: dict | None = None, qs: dict | None = None) -> dict:
    return {"rawPath": path, "requestContext": {"http": {"method": "POST"}},
            "queryStringParameters": qs or {},
            "body": urllib.parse.urlencode(form or {}),
            "isBase64Encoded": False}


def _get(path: str, qs: dict | None = None) -> dict:
    return {"rawPath": path, "requestContext": {"http": {"method": "GET"}},
            "queryStringParameters": qs or {}}


def _mem(w, u: User) -> Membership:
    return db.get_membership(w["app"].app_id, u.user_id)


def _pause(w, actor: User, form: dict) -> dict:
    return web._api_membership_inactive(
        _post("/api/membership/inactive", form), actor, w["community"],
        w["app"], _mem(w, actor))


def _location(resp: dict) -> str:
    return urllib.parse.unquote(resp["headers"].get("Location", ""))


# ---- the model --------------------------------------------------------------

def test_inactive_on_respects_the_return_date() -> None:
    m = Membership(community_id=CID, app_id="a", user_id="u")
    assert not m.inactive_on("2030-06-02")
    m.inactive_since = "2030-05-01T00:00:00+00:00"
    assert m.inactive_on("2030-06-02")                 # until further notice
    m.inactive_until = "2030-06-10"
    assert m.inactive_on("2030-06-09")
    assert not m.inactive_on("2030-06-10")             # back on that date


def test_re_marking_keeps_the_original_since(world) -> None:
    app, member = world["app"], world["member"]
    db.set_membership_inactive(app.app_id, member.user_id, inactive=True)
    since = _mem(world, member).inactive_since
    db.set_membership_inactive(app.app_id, member.user_id, inactive=True,
                               until="2030-09-01", note="travel")
    m = _mem(world, member)
    assert (m.inactive_since, m.inactive_until, m.inactive_note) == (
        since, "2030-09-01", "travel")
    db.set_membership_inactive(app.app_id, member.user_id, inactive=False)
    m = _mem(world, member)
    assert (m.inactive_since, m.inactive_until, m.inactive_note) == (
        None, None, None)


def test_missing_membership_is_a_no_op(world) -> None:
    assert not db.set_membership_inactive(world["app"].app_id, "nobody",
                                          inactive=True)
    assert db.get_membership(world["app"].app_id, "nobody") is None


# ---- pausing and resuming ---------------------------------------------------

def test_member_pauses_themselves_and_admins_hear(world) -> None:
    member, outbox = world["member"], world["outbox"]
    resp = _pause(world, member, {"action": "pause"})
    assert "marked inactive until further notice" in _location(resp)
    assert _mem(world, member).inactive_since
    assert [s["to_addr"] for s in outbox.sent] == ["Admin@example.com"]
    assert "paused their scheduling" in outbox.sent[0]["subject"]

    resp = _pause(world, member, {"action": "resume"})
    assert "active again" in _location(resp)
    assert _mem(world, member).inactive_since is None
    assert "back in the rotation" in outbox.sent[-1]["subject"]


def test_pause_mentions_slots_still_held(world) -> None:
    w = world
    db.put_assignment(Assignment(
        community_id=CID, app_id=w["app"].app_id, yyyy_mm="2030-06",
        slot_id=w["slot"].slot_id, user_id=w["member"].user_id,
        local_date="2030-06-02"))
    resp = _pause(w, w["member"], {"action": "pause"})
    assert "still have 1 upcoming slot" in _location(resp)
    # The assignment itself is untouched.
    assert [a.user_id for a in db.list_assignments_for_slot(
        w["app"].app_id, "2030-06", w["slot"].slot_id)] == [w["member"].user_id]


def test_return_date_must_be_after_today(world) -> None:
    resp = _pause(world, world["member"],
                  {"action": "pause", "until": "2000-01-01"})
    assert "error=" in resp["headers"]["Location"]
    assert _mem(world, world["member"]).inactive_since is None


def test_admin_marks_a_member_inactive_without_notifying_admins(world) -> None:
    w = world
    resp = _pause(w, w["admin"], {"action": "pause",
                                  "user_id": w["member"].user_id,
                                  "until": "2030-06-10", "note": "travel"})
    assert "Member is marked inactive, back on" in _location(resp)
    m = _mem(w, w["member"])
    assert (m.inactive_until, m.inactive_note) == ("2030-06-10", "travel")
    assert w["outbox"].sent == []


def test_a_member_cannot_pause_someone_else(world) -> None:
    w = world
    other, _ = w["add"]("Other")
    resp = _pause(w, w["member"], {"action": "pause",
                                   "user_id": other.user_id})
    assert resp["statusCode"] == 403
    assert _mem(w, other).inactive_since is None


def test_event_apps_have_no_rotation(world) -> None:
    w = world
    w["app"].app_type = "flexible_event"
    resp = _pause(w, w["member"], {"action": "pause"})
    assert "no rotation" in _location(resp)
    assert _mem(w, w["member"]).inactive_since is None


# ---- what being inactive does -----------------------------------------------

def test_inactive_member_cannot_sign_up(world) -> None:
    w = world
    db.set_membership_inactive(w["app"].app_id, w["member"].user_id,
                               inactive=True)
    resp = web._signup_assignment(
        _post("/api/assignments/signup",
              qs={"slot_id": w["slot"].slot_id, "month": "2030-06"}),
        w["member"], w["community"], w["app"], _mem(w, w["member"]))
    assert "can't take on new slots" in _location(resp)
    assert list(db.list_assignments_for_slot(
        w["app"].app_id, "2030-06", w["slot"].slot_id)) == []


def test_sign_up_works_again_from_the_return_date(world) -> None:
    w = world
    db.set_membership_inactive(w["app"].app_id, w["member"].user_id,
                               inactive=True, until="2030-06-02")
    web._signup_assignment(
        _post("/api/assignments/signup",
              qs={"slot_id": w["slot"].slot_id, "month": "2030-06"}),
        w["member"], w["community"], w["app"], _mem(w, w["member"]))
    assert [a.user_id for a in db.list_assignments_for_slot(
        w["app"].app_id, "2030-06", w["slot"].slot_id)] == [w["member"].user_id]


def test_admin_cannot_assign_an_inactive_member(world) -> None:
    w = world
    db.set_membership_inactive(w["app"].app_id, w["member"].user_id,
                               inactive=True)
    resp = web._api_admin_assign(
        _post("/api/admin/assign", qs={"slot_id": w["slot"].slot_id,
                                       "month": "2030-06",
                                       "user_id": w["member"].user_id}),
        w["admin"], w["community"], w["app"], _mem(w, w["admin"]))
    assert "Member is inactive" in _location(resp)
    assert list(db.list_assignments_for_slot(
        w["app"].app_id, "2030-06", w["slot"].slot_id)) == []


def test_pick_list_greys_out_inactive_members(world) -> None:
    w = world
    db.set_membership_inactive(w["app"].app_id, w["member"].user_id,
                               inactive=True, until="2030-06-10")
    by_date = web._collect_blocked_by_date(w["app"], [w["slot"]])
    assert by_date["2030-06-02"] == {
        w["member"].user_id: "inactive, back on Mon, June 10"}


def test_schedule_email_skips_a_month_they_are_out(world) -> None:
    w = world
    db.set_membership_inactive(w["app"].app_id, w["member"].user_id,
                               inactive=True)
    names = {p.user.name for p in publishing.plan_publish(
        w["community"], w["app"], "2030-06")}
    assert names == {"Admin"}


def test_schedule_email_still_goes_when_they_hold_a_slot(world) -> None:
    w = world
    db.put_assignment(Assignment(
        community_id=CID, app_id=w["app"].app_id, yyyy_mm="2030-06",
        slot_id=w["slot"].slot_id, user_id=w["member"].user_id,
        local_date="2030-06-02"))
    db.set_membership_inactive(w["app"].app_id, w["member"].user_id,
                               inactive=True)
    names = {p.user.name for p in publishing.plan_publish(
        w["community"], w["app"], "2030-06")}
    assert names == {"Admin", "Member"}


def test_schedule_email_resumes_in_the_month_they_return(world) -> None:
    w = world
    db.set_membership_inactive(w["app"].app_id, w["member"].user_id,
                               inactive=True, until="2030-06-20")
    names = {p.user.name for p in publishing.plan_publish(
        w["community"], w["app"], "2030-06")}
    assert "Member" in names


def test_month_bounds_cover_months_and_iso_weeks() -> None:
    assert publishing._month_bounds("2030-02") == ("2030-02-01", "2030-02-28")
    assert publishing._month_bounds("2030-12") == ("2030-12-01", "2030-12-31")
    assert publishing._month_bounds("2030-W01") == ("2029-12-31", "2030-01-06")


# ---- the pages --------------------------------------------------------------

def test_my_availability_offers_pause_then_im_back(world) -> None:
    w = world
    page = web._my_availability_page(_get("/my-availability"), w["member"],
                                     w["community"], w["app"],
                                     _mem(w, w["member"]))["body"]
    assert "Pause my scheduling" in page
    db.set_membership_inactive(w["app"].app_id, w["member"].user_id,
                               inactive=True)
    page = web._my_availability_page(_get("/my-availability"), w["member"],
                                     w["community"], w["app"],
                                     _mem(w, w["member"]))["body"]
    assert "taking a break" in page and "I'm back" in page


def test_members_page_shows_status_and_count(world) -> None:
    w = world
    db.set_membership_inactive(w["app"].app_id, w["member"].user_id,
                               inactive=True, note="travel")
    page = web._users_page(_get("/admin/users"), w["admin"], w["community"],
                           w["app"], _mem(w, w["admin"]))["body"]
    assert "1 active &middot; 1 inactive" in page
    assert "Inactive, until further notice &middot; travel" in page
    assert "reactivate" in page


def test_event_app_members_page_has_no_status(world) -> None:
    w = world
    w["app"].app_type = "flexible_event"
    page = web._users_page(_get("/admin/users"), w["admin"], w["community"],
                           w["app"], _mem(w, w["admin"]))["body"]
    assert "mark inactive" not in page and " active" not in page.split(
        "Manage members")[1][:200]
