"""Members stepping out of a scheduling app's rotation (Membership.inactive_*).

Drives the real handlers against a moto table. An inactive member is left
out of the pick list, bulk assign, sign-ups and opening emails, and isn't
sent the schedule for a period they're out of with nothing assigned. Event
apps have no rotation and refuse the action.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import shutil
import subprocess
import urllib.parse
from html.parser import HTMLParser

import pytest

from community_organizer.core import db, publishing
from community_organizer.core.models import (
    Application, Assignment, Community, Membership, Schedule, Slot,
    SwapRequest, User,
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


# ---- openings, cohorts and weekly (recurring) apps --------------------------

from community_organizer.core.models import (  # noqa: E402
    Cohort, CohortMembership, SlotTemplate,
)


def _cohort(w, app: Application, template_id: str, *users: User) -> Cohort:
    c = Cohort(community_id=CID, app_id=app.app_id, name="Sun 8",
               linked_template_id=template_id)
    db.put_cohort(c)
    for u in users:
        db.put_cohort_membership(CohortMembership(cohort_id=c.cohort_id,
                                                  user_id=u.user_id))
    return c


def test_opening_email_skips_inactive_cohort_members(world) -> None:
    w = world
    other, _ = w["add"]("Other")
    _cohort(w, w["app"], "t1", w["member"], other)
    db.set_membership_inactive(w["app"].app_id, w["member"].user_id,
                               inactive=True)
    web._notify_cohort_of_opening(w["admin"], w["community"], w["app"],
                                  w["slot"], "2030-06")
    openings = [s for s in w["outbox"].sent if "opening" in s["subject"]]
    assert [s["to_addr"] for s in openings] == ["Other@example.com"]


def test_cohorts_page_marks_inactive_members(world) -> None:
    w = world
    _cohort(w, w["app"], "t1", w["member"])
    db.set_membership_inactive(w["app"].app_id, w["member"].user_id,
                               inactive=True)
    page = web._cohorts_page(_get("/admin/cohorts"), w["admin"],
                             w["community"], w["app"],
                             _mem(w, w["admin"]))["body"]
    assert "Member <i style='color:#a80'>(inactive)</i>" in page


@pytest.fixture
def weekly(world):
    """A recurring-commitments app with one weekly template and cohort."""
    w = world
    app = Application(community_id=CID, name="Adoration",
                      app_type="recurring_commitments", period_type="weekly")
    db.put_application(app)
    tpl = SlotTemplate(community_id=CID, app_id=app.app_id, name="Wed 3 AM",
                       day_of_week=2, start_time="03:00", duration_minutes=60)
    db.put_template(tpl)
    people = []
    for name in ("Paused", "Steady"):
        u = User(community_id=CID, email=f"{name}@example.com", name=name)
        db.put_user(u)
        db.put_membership(Membership(community_id=CID, app_id=app.app_id,
                                     user_id=u.user_id))
        people.append(u)
    cohort = _cohort(w, app, tpl.template_id, *people)
    return dict(w, app=app, tpl=tpl, paused=people[0], steady=people[1],
                cohort=cohort)


def _assigned(app: Application, period: str) -> set[str]:
    return {a.user_id for a in db.list_assignments_for_month(app.app_id,
                                                             period)}


def test_new_weeks_skip_a_paused_cohort_member(weekly) -> None:
    w = weekly
    db.set_membership_inactive(w["app"].app_id, w["paused"].user_id,
                               inactive=True, until="2030-06-10")
    assert web._ensure_period_materialized(w["community"], w["app"],
                                           "2030-W23")     # Wed Jun 5
    assert _assigned(w["app"], "2030-W23") == {w["steady"].user_id}
    web._ensure_period_materialized(w["community"], w["app"], "2030-W24")
    assert _assigned(w["app"], "2030-W24") == {      # Wed Jun 12: back
        w["paused"].user_id, w["steady"].user_id}


def test_cohort_backfill_skips_weeks_they_are_out(weekly) -> None:
    w = weekly
    newcomer = User(community_id=CID, email="new@example.com", name="New")
    db.put_user(newcomer)
    db.put_membership(Membership(community_id=CID, app_id=w["app"].app_id,
                                 user_id=newcomer.user_id))
    for period in ("2030-W23", "2030-W24"):
        web._ensure_period_materialized(w["community"], w["app"], period)
    db.set_membership_inactive(w["app"].app_id, newcomer.user_id,
                               inactive=True, until="2030-06-10")
    added = web._fill_future_cohort_assignments(
        w["community"], w["app"], w["cohort"], newcomer.user_id)
    assert added == 1
    assert newcomer.user_id not in _assigned(w["app"], "2030-W23")
    assert newcomer.user_id in _assigned(w["app"], "2030-W24")


def test_paused_member_cannot_join_a_weekly_cohort(weekly) -> None:
    w = weekly
    joiner = User(community_id=CID, email="j@example.com", name="Joiner")
    db.put_user(joiner)
    db.put_membership(Membership(community_id=CID, app_id=w["app"].app_id,
                                 user_id=joiner.user_id))
    db.set_membership_inactive(w["app"].app_id, joiner.user_id,
                               inactive=True)
    resp = web._api_cohort_add_member(
        _post("/api/cohorts/add-member",
              qs={"cohort_id": w["cohort"].cohort_id,
                  "user_id": joiner.user_id}),
        joiner, w["community"], w["app"],
        db.get_membership(w["app"].app_id, joiner.user_id))
    assert "can't take on new slots" in _location(resp)
    assert joiner.user_id not in {cm.user_id for cm in
                                  db.list_cohort_members(w["cohort"].cohort_id)}


def test_weekly_grid_hides_sign_up_while_paused(weekly) -> None:
    w = weekly
    web._ensure_period_materialized(w["community"], w["app"], "2030-W23")
    slots = list(db.list_slots(w["app"].app_id, "2030-W23"))
    outsider = User(community_id=CID, email="o@example.com", name="Outsider")
    db.put_user(outsider)
    db.put_membership(Membership(community_id=CID, app_id=w["app"].app_id,
                                 user_id=outsider.user_id))

    def grid() -> str:
        return web._render_recurring_grid(
            slots=slots, asgns_by_slot={}, users_by_id={},
            current_user=outsider, cohorts_by_template_id={
                w["tpl"].template_id: w["cohort"]},
            my_cohort_ids=set(),
            viewer_membership=db.get_membership(w["app"].app_id,
                                                outsider.user_id))

    assert "+ Sign up" in grid() and "Take this slot weekly" in grid()
    db.set_membership_inactive(w["app"].app_id, outsider.user_id,
                               inactive=True)
    assert "+ Sign up" not in grid()
    assert "Take this slot weekly" not in grid()


# ---- review fixes -----------------------------------------------------------

def _assign(w, u: User, slot: Slot | None = None) -> None:
    s = slot or w["slot"]
    db.put_assignment(Assignment(
        community_id=CID, app_id=w["app"].app_id, yyyy_mm=s.yyyy_mm,
        slot_id=s.slot_id, user_id=u.user_id, local_date=s.local_date))


def _slot_like(w, slot_id: str, **changes) -> Slot:
    s = copy.deepcopy(w["slot"])
    s.slot_id = slot_id
    for k, v in changes.items():
        setattr(s, k, v)
    db.put_slot(s)
    return s


def _onsubmit_handlers(page: str) -> list[str]:
    class Forms(HTMLParser):
        handlers: list[str] = []

        def handle_starttag(self, tag, attrs):
            d = dict(attrs)
            if tag == "form" and d.get("onsubmit"):
                self.handlers.append(d["onsubmit"])
    p = Forms()
    p.handlers = []
    p.feed(page)
    return p.handlers


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_confirm_messages_survive_apostrophes_and_quotes(world) -> None:
    w = world
    w["member"].name = "Member's label'+(globalThis.marker=1)+'"
    db.put_user(w["member"])
    page = web._users_page(_get("/admin/users"), w["admin"], w["community"],
                           w["app"], _mem(w, w["admin"]))["body"]
    handlers = [h for h in _onsubmit_handlers(page) if "Member" in h]
    assert len(handlers) >= 2          # mark inactive + remove from app
    js = ("globalThis.marker=0;let msgs=[];"
          "globalThis.confirmSubmit=(f,m)=>{msgs.push(m);return true};"
          + "".join(f"new Function({json.dumps(h)}).call({{}});"
                    for h in handlers)
          + "if(globalThis.marker!==0)throw Error('ran');"
          "if(!msgs.every(m=>m.includes(\"Member's label'+\")))"
          "throw Error(JSON.stringify(msgs));")
    subprocess.run(["node", "-e", js], check=True, capture_output=True,
                   text=True)


def test_impossible_return_date_is_refused(world) -> None:
    resp = _pause(world, world["member"],
                  {"action": "pause", "until": "2030-02-31"})
    assert "Invalid date" in _location(resp)
    assert not _mem(world, world["member"]).inactive_since


def test_a_malformed_stored_date_does_not_break_pages(world) -> None:
    w = world
    db.set_membership_inactive(w["app"].app_id, w["member"].user_id,
                               inactive=True, until="2030-02-31")
    web._users_page(_get("/admin/users"), w["admin"], w["community"],
                    w["app"], _mem(w, w["admin"]))
    web._my_availability_page(_get("/my-availability"), w["member"],
                              w["community"], w["app"],
                              _mem(w, w["member"]))


def test_copy_from_leaves_out_inactive_members(world) -> None:
    w = world
    _assign(w, w["member"])
    target = _slot_like(w, "next-month", yyyy_mm="2030-07",
                        local_date="2030-07-07", concrete_date="2030-07-07")
    db.set_membership_inactive(w["app"].app_id, w["member"].user_id,
                               inactive=True)
    resp = web._api_schedule_copy_from(
        _post("/api/schedules/copy-from",
              {"month": "2030-07", "source": "2030-06"}),
        w["admin"], w["community"], w["app"], _mem(w, w["admin"]))
    assert "Left out 1" in _location(resp)
    assert not list(db.list_assignments_for_slot(w["app"].app_id, "2030-07",
                                                 target.slot_id))


def test_split_refuses_an_inactive_partner(world) -> None:
    w = world
    prior = _slot_like(w, "prior", start_time="07:00")
    _assign(w, w["member"], prior)
    db.set_membership_inactive(w["app"].app_id, w["member"].user_id,
                               inactive=True)
    resp = web._api_cover_released(
        _post("/api/assignments/cover",
              {"slot_id": w["slot"].slot_id, "month": "2030-06",
               "mode": "split", "with": w["member"].user_id}),
        w["admin"], w["community"], w["app"], _mem(w, w["admin"]))
    assert "taking a break" in _location(resp)
    assert not list(db.list_assignments_for_slot(w["app"].app_id, "2030-06",
                                                 w["slot"].slot_id))


def test_trade_refused_once_the_requester_pauses(world) -> None:
    w = world
    second = _slot_like(w, "second", local_date="2030-06-09",
                        concrete_date="2030-06-09")
    _assign(w, w["member"])
    _assign(w, w["admin"], second)
    swap = SwapRequest(community_id=CID, app_id=w["app"].app_id,
                       yyyy_mm="2030-06", requester_user_id=w["member"].user_id,
                       release_slot_id=w["slot"].slot_id,
                       preferred_slot_ids=[second.slot_id])
    db.put_swap(swap)
    db.set_membership_inactive(w["app"].app_id, w["member"].user_id,
                               inactive=True)
    resp = web._api_swap_accept(
        _post("/api/swap/accept",
              {"swap_id": swap.swap_id, "month": "2030-06",
               "accept_slot_id": second.slot_id}),
        w["admin"], w["community"], w["app"], _mem(w, w["admin"]))
    assert "taking a break" in _location(resp)
    assert {a.user_id for a in db.list_assignments_for_slot(
        w["app"].app_id, "2030-06", second.slot_id)} == {w["admin"].user_id}


def test_neighbor_cover_emails_skip_inactive_members(world) -> None:
    w = world
    w["app"].app_type = "recurring_commitments"
    prior = _slot_like(w, "prior", start_time="07:00")
    _assign(w, w["member"], prior)
    db.set_membership_inactive(w["app"].app_id, w["member"].user_id,
                               inactive=True)
    web._send_take_or_split_emails(w["admin"], w["community"], w["app"],
                                   w["slot"], "2030-06")
    assert not [m for m in w["outbox"].sent
                if m["to_addr"] == w["member"].email]


def test_resume_leaves_weeks_made_while_paused_alone(weekly) -> None:
    w = weekly
    u = w["paused"]
    _pause(w, u, {"action": "pause"})
    web._ensure_period_materialized(w["community"], w["app"], "2030-W23")
    resp = _pause(w, u, {"action": "resume"})
    assert "sign up for any open spots" in _location(resp)
    assert u.user_id not in _assigned(w["app"], "2030-W23")
    web._ensure_period_materialized(w["community"], w["app"], "2030-W24")
    assert u.user_id in _assigned(w["app"], "2030-W24")


def test_earlier_return_date_leaves_generated_weeks_alone(weekly) -> None:
    w = weekly
    u = w["paused"]
    db.set_membership_inactive(w["app"].app_id, u.user_id, inactive=True)
    web._ensure_period_materialized(w["community"], w["app"], "2030-W24")
    _pause(w, u, {"action": "pause", "until": "2030-06-10"})
    assert u.user_id not in _assigned(w["app"], "2030-W24")


def test_today_is_the_apps_day_not_the_admins(world, monkeypatch) -> None:
    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 20, 2, tzinfo=dt.timezone.utc).astimezone(tz)
    monkeypatch.setattr(web.dt, "datetime", Clock)
    w = world
    w["app"].default_timezone = "America/New_York"     # still Oct 19 there
    w["admin"].preferred_tz = "Asia/Tokyo"             # already Oct 20
    resp = _pause(w, w["admin"], {"action": "pause", "until": "2026-10-20",
                                  "user_id": w["member"].user_id})
    assert "notice=" in resp["headers"]["Location"]
    assert _mem(w, w["member"]).inactive_until == "2026-10-20"


def test_a_vanished_membership_is_not_reported_as_saved(world,
                                                        monkeypatch) -> None:
    w = world
    real = db.set_membership_inactive

    def vanish(app_id, user_id, **kw):
        db.delete_membership(app_id, user_id)
        return real(app_id, user_id, **kw)
    monkeypatch.setattr(db, "set_membership_inactive", vanish)
    resp = _pause(w, w["member"], {"action": "pause"})
    assert "error=" in resp["headers"]["Location"]
    assert not w["outbox"].sent


def test_a_mail_failure_does_not_fail_the_pause(world, monkeypatch) -> None:
    def broken():
        raise RuntimeError("no mail today")
    monkeypatch.setattr(
        "community_organizer.providers.email.get_email_provider", broken)
    resp = _pause(world, world["member"], {"action": "pause"})
    assert "notice=" in resp["headers"]["Location"]
    assert _mem(world, world["member"]).inactive_since
