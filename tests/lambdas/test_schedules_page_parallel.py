"""_schedules_all_page renders correctly after its DDB reads were fanned out
across a thread pool (perf work). Guards that parallelization didn't change
output: both active months, their assignees, and the archived expander."""
from __future__ import annotations

from community_organizer.core import db
from community_organizer.core.models import (
    Application, Assignment, Cohort, CohortMembership, Community, Membership,
    Schedule, Slot, User,
)
from community_organizer.lambdas import web


def _slot(cid, sid, month, tid, date, uid):
    s = Slot(community_id=cid, app_id="ush", yyyy_mm=month, template_id=tid,
             name=sid, day_of_week=6, start_time="08:00",
             arrival_offset_minutes=10, duration_minutes=60,
             required_volunteers=1, min_volunteers=1,
             concrete_date=date, local_date=date, slot_id=sid)
    db.put_slot(s)
    db.put_assignment(Assignment(community_id=cid, app_id="ush", yyyy_mm=month,
                                 slot_id=sid, user_id=uid, local_date=date))


def test_schedules_page_renders_all_active_months(ddb_table):
    cid = "c1"
    db.put_community(Community(community_id=cid, name="P"))
    app = Application(community_id=cid, name="Ushers", app_type="coverage",
                      app_id="ush")
    db.put_application(app)
    aa = User(community_id=cid, email="aa@example.com", name="Boss")
    alice = User(community_id=cid, email="al@example.com", name="Alice")
    bob = User(community_id=cid, email="bo@example.com", name="Bob")
    for u in (aa, alice, bob):
        db.put_user(u)
    db.put_membership(Membership(community_id=cid, app_id="ush",
                                 user_id=aa.user_id, app_role="aa"))
    for u in (alice, bob):
        db.put_membership(Membership(community_id=cid, app_id="ush", user_id=u.user_id))
    ca = Cohort(community_id=cid, app_id="ush", name="A", linked_template_id="t1")
    db.put_cohort(ca)
    db.put_cohort_membership(CohortMembership(cohort_id=ca.cohort_id, user_id=alice.user_id))
    # Two active months + one archived.
    for m in ("2030-06", "2030-07"):
        db.put_schedule(Schedule(community_id=cid, app_id="ush", yyyy_mm=m,
                                 state="published"))
    db.put_schedule(Schedule(community_id=cid, app_id="ush", yyyy_mm="2030-05",
                             state="archived"))
    _slot(cid, "s1", "2030-06", "t1", "2030-06-07", alice.user_id)
    _slot(cid, "s2", "2030-06", "t1", "2030-06-14", bob.user_id)
    _slot(cid, "s3", "2030-07", "t1", "2030-07-05", alice.user_id)

    ev = {"requestContext": {"http": {"method": "GET"}}}
    r = web._schedules_all_page(ev, aa, db.get_community(cid), app,
                                db.get_membership("ush", aa.user_id))
    assert r["statusCode"] == 200
    body = r["body"]
    assert "June 2030" in body and "July 2030" in body     # both active months
    assert "Alice" in body and "Bob" in body               # assignees rendered
    assert "Past schedules (1)" in body                    # archived expander


def test_schedules_page_empty(ddb_table):
    cid = "c2"
    db.put_community(Community(community_id=cid, name="P"))
    app = Application(community_id=cid, name="U", app_type="coverage", app_id="ush")
    db.put_application(app)
    aa = User(community_id=cid, email="aa@example.com", name="Boss")
    db.put_user(aa)
    db.put_membership(Membership(community_id=cid, app_id="ush",
                                 user_id=aa.user_id, app_role="aa"))
    ev = {"requestContext": {"http": {"method": "GET"}}}
    r = web._schedules_all_page(ev, aa, db.get_community(cid), app,
                                db.get_membership("ush", aa.user_id))
    assert r["statusCode"] == 200
    assert "No active schedules yet" in r["body"]
