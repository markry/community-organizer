"""Tests for the web Lambda's auth-flow gates.

Currently focuses on the email-based auto-link gate (security fix D2)
in ``_route``: an unverified-email claim must NOT cause an existing
user record to be silently bonded to the Cognito sub of whoever is
signing in.

We monkeypatch ``db.get_user_by_cognito_sub`` /
``db.get_user_by_email`` / ``db.put_user`` so the test doesn't need
DynamoDB. ``auth.verify_id_token`` is also stubbed to return whatever
claims we want.
"""
from __future__ import annotations

import pytest

from community_organizer import auth, lambdas
from community_organizer.core.models import User


@pytest.fixture
def patch_user_lookup(monkeypatch):
    """Stub the DB calls + JWT verifier so we can drive _route
    through a per-test ``claims`` value stashed on the fixture state.

    Real Cognito tokens are opaque base64 — we don't try to
    construct one here. Instead the cookie carries any non-empty
    token string, and the stubbed verifier returns
    ``state["claims"]`` regardless of input.
    """
    from community_organizer.core import db
    from community_organizer.lambdas import web

    state = {"put_calls": [], "linked_user": None,
             "user_by_email": None, "claims": None}

    monkeypatch.setattr(db, "get_user_by_cognito_sub",
                        lambda *_args, **_kwargs: None)
    monkeypatch.setattr(db, "get_user_by_email",
                        lambda *_args, **_kwargs: state["user_by_email"])

    def _stub_put_user(user, **kwargs):
        state["put_calls"].append(user)
        state["linked_user"] = user

    monkeypatch.setattr(db, "put_user", _stub_put_user)
    monkeypatch.setattr(auth, "verify_id_token",
                        lambda _token: state["claims"])
    # Stub out the rest of _route's flow we don't care about.
    monkeypatch.setattr(db, "get_community", lambda *_: None)
    monkeypatch.setattr(db, "list_applications", lambda *_: iter([]))
    return state


def _event_with_token() -> dict:
    """Build a minimal authenticated event — value of the ID cookie
    doesn't matter because the verifier is stubbed."""
    return {
        "rawPath": "/",
        "rawQueryString": "",
        "cookies": [f"{auth.ID_COOKIE}=stub-token"],
    }


def test_auto_link_requires_email_verified(patch_user_lookup) -> None:
    """email_verified=False MUST NOT auto-link to an existing user
    record. Otherwise an attacker who can get an unverified-email
    token from any IdP could take over a pre-provisioned account
    (security fix D2)."""
    from community_organizer.lambdas import web

    existing = User(community_id="c1", email="victim@example.com",
                    name="Victim")
    patch_user_lookup["user_by_email"] = existing
    patch_user_lookup["claims"] = {
        "sub": "ATTACKER-SUB",
        "email": "victim@example.com",
        "email_verified": False,
    }
    resp = web._route(_event_with_token(),
                      lambda *_a, **_k: web._text(200, "ok"))
    assert resp["statusCode"] == 403       # unprovisioned page
    # Critically: db.put_user MUST NOT have been called — no auto-link.
    assert patch_user_lookup["put_calls"] == []


def test_auto_link_proceeds_when_email_verified(patch_user_lookup) -> None:
    """The happy case: email_verified=True, no existing cognito_sub
    link — auto-link the sub to the email-matched user."""
    from community_organizer.lambdas import web

    existing = User(community_id="c1", email="real@example.com",
                    name="Real")
    patch_user_lookup["user_by_email"] = existing
    patch_user_lookup["claims"] = {
        "sub": "LEGIT-SUB",
        "email": "real@example.com",
        "email_verified": True,
    }
    web._route(_event_with_token(), lambda *_a, **_k: web._text(200, "ok"))
    assert len(patch_user_lookup["put_calls"]) == 1
    linked = patch_user_lookup["linked_user"]
    assert linked.cognito_sub == "LEGIT-SUB"
    assert linked.email == "real@example.com"


def test_auto_link_proceeds_for_google_federation(patch_user_lookup) -> None:
    """Google sign-ins arrive with email_verified=False (Cognito doesn't
    propagate Google's verified-email flag), but Google verifies the
    address, so the auto-link MUST proceed for a Google federation. This
    is the fix for the usher who couldn't log in via 'Sign in with
    Google' (kept landing on the unprovisioned page)."""
    from community_organizer.lambdas import web

    existing = User(community_id="c1", email="usher@gmail.com", name="Usher")
    patch_user_lookup["user_by_email"] = existing
    patch_user_lookup["claims"] = {
        "sub": "a408c4c8-google-sub",
        "email": "usher@gmail.com",
        "email_verified": False,                       # Cognito's gotcha
        "cognito:username": "Google_116052141356115983967",
    }
    web._route(_event_with_token(), lambda *_a, **_k: web._text(200, "ok"))
    assert len(patch_user_lookup["put_calls"]) == 1    # auto-linked
    assert patch_user_lookup["linked_user"].cognito_sub == "a408c4c8-google-sub"


def test_login_email_trusted_matrix() -> None:
    """_login_email_trusted: verified OR a Google federation is trusted;
    everything else (incl. other IdPs with unverified email) is not."""
    from community_organizer.lambdas import web
    t = web._login_email_trusted
    assert t({"email_verified": True}) is True
    assert t({"email_verified": False,
              "cognito:username": "Google_12345"}) is True
    assert t({"email_verified": False,
              "identities": [{"providerName": "Google"}]}) is True
    assert t({"email_verified": False,
              "identities": '[{"providerName":"Google"}]'}) is True   # json str
    assert t({"email_verified": False,
              "cognito:username": "f4e86438-native"}) is False
    assert t({"email_verified": False,
              "identities": [{"providerName": "Facebook"}]}) is False
    assert t({}) is False


# ---------------------------------------------------------------------------
# Self-service cohort join (Recurring Commitments app type setup)
# ---------------------------------------------------------------------------

@pytest.fixture
def cohort_setup(ddb_table, monkeypatch):
    """Seed a small community with one app, one cohort, two users
    (one admin, one regular member). Returns a dict the tests use."""
    from community_organizer.core import db
    from community_organizer.core.models import (
        Application, Cohort, Community, Membership, User as U,
    )

    cid = "test-community"
    db.put_community(Community(community_id=cid, name="Test"))
    app = Application(community_id=cid, name="Adoration",
                      app_type="recurring_commitments")
    db.put_application(app)
    admin = U(community_id=cid, email="admin@example.com", name="Admin")
    member = U(community_id=cid, email="m@example.com", name="Member")
    other = U(community_id=cid, email="other@example.com", name="Other")
    for u in (admin, member, other):
        db.put_user(u)
    db.put_membership(Membership(community_id=cid, app_id=app.app_id,
                                 user_id=admin.user_id, app_role="aa"))
    db.put_membership(Membership(community_id=cid, app_id=app.app_id,
                                 user_id=member.user_id))
    db.put_membership(Membership(community_id=cid, app_id=app.app_id,
                                 user_id=other.user_id))
    cohort = Cohort(community_id=cid, app_id=app.app_id, name="Wed 2 PM")
    db.put_cohort(cohort)
    return {"app": app, "admin": admin, "member": member,
            "other": other, "cohort": cohort}


def _cohort_event(cohort_id: str, user_id: str) -> dict:
    return {
        "rawPath": "/api/cohort/add-member",
        "rawQueryString": f"cohort_id={cohort_id}&user_id={user_id}",
        "queryStringParameters": {"cohort_id": cohort_id, "user_id": user_id},
        "requestContext": {"http": {"method": "POST"}},
        "cookies": [],
        "headers": {},
    }


def test_member_can_self_join_cohort(cohort_setup) -> None:
    """A regular member adding THEMSELVES to a cohort must succeed —
    this is the central self-service guarantee for the Recurring
    Commitments app type."""
    from community_organizer.core import db
    from community_organizer.lambdas import web

    s = cohort_setup
    member_membership = db.get_membership(s["app"].app_id, s["member"].user_id)
    event = _cohort_event(s["cohort"].cohort_id, s["member"].user_id)
    resp = web._api_cohort_add_member(
        event, s["member"], None, s["app"], member_membership)
    assert resp["statusCode"] == 302
    members = list(db.list_cohort_members(s["cohort"].cohort_id))
    assert s["member"].user_id in {m.user_id for m in members}


def test_member_cannot_add_someone_else_to_cohort(cohort_setup) -> None:
    """A regular member must NOT be able to add a DIFFERENT user.
    The self-service relaxation is strictly limited to self-add."""
    from community_organizer.core import db
    from community_organizer.lambdas import web

    s = cohort_setup
    member_membership = db.get_membership(s["app"].app_id, s["member"].user_id)
    event = _cohort_event(s["cohort"].cohort_id, s["other"].user_id)
    resp = web._api_cohort_add_member(
        event, s["member"], None, s["app"], member_membership)
    # Now redirects with a styled error banner instead of bare 403 page.
    assert resp["statusCode"] == 302
    assert "error=" in resp["headers"]["Location"]
    members = list(db.list_cohort_members(s["cohort"].cohort_id))
    assert s["other"].user_id not in {m.user_id for m in members}


def test_admin_can_still_add_anyone_to_cohort(cohort_setup) -> None:
    """Admin path is unchanged — admins can add any user."""
    from community_organizer.core import db
    from community_organizer.lambdas import web

    s = cohort_setup
    admin_membership = db.get_membership(s["app"].app_id, s["admin"].user_id)
    event = _cohort_event(s["cohort"].cohort_id, s["other"].user_id)
    resp = web._api_cohort_add_member(
        event, s["admin"], None, s["app"], admin_membership)
    assert resp["statusCode"] == 302
    members = list(db.list_cohort_members(s["cohort"].cohort_id))
    assert s["other"].user_id in {m.user_id for m in members}


def test_cross_app_cohort_manipulation_blocked(cohort_setup, monkeypatch) -> None:
    """A user signed into App A must not be able to mutate a cohort
    that belongs to App B by guessing its cohort_id. Closes the
    cross-app gap noted in the audit's original finding #14."""
    from community_organizer.core import db
    from community_organizer.core.models import Application, Cohort, Membership
    from community_organizer.lambdas import web

    s = cohort_setup
    # Stand up a second app + its own cohort that the user has NO
    # membership in.
    cid = s["app"].community_id
    other_app = Application(community_id=cid, name="Ushers",
                            app_type="coverage")
    db.put_application(other_app)
    secret_cohort = Cohort(community_id=cid, app_id=other_app.app_id,
                           name="Saturday 5:30 PM")
    db.put_cohort(secret_cohort)

    # Member of the Adoration app tries to self-add to the Ushers
    # cohort by passing its cohort_id.
    member_membership = db.get_membership(s["app"].app_id, s["member"].user_id)
    event = _cohort_event(secret_cohort.cohort_id, s["member"].user_id)
    resp = web._api_cohort_add_member(
        event, s["member"], None, s["app"], member_membership)
    # Now redirects with a styled error banner instead of bare 404 page.
    assert resp["statusCode"] == 302
    assert "error=" in resp["headers"]["Location"]
    members = list(db.list_cohort_members(secret_cohort.cohort_id))
    assert s["member"].user_id not in {m.user_id for m in members}


# ---------------------------------------------------------------------------
# _home explicit app_type dispatch
# ---------------------------------------------------------------------------

def test_home_routes_recurring_commitments_empty_state(ddb_table) -> None:
    """A recurring_commitments app with no slots in the 4-week
    window renders the empty-state message — proves the dispatch
    reaches _recurring_home and that the grid renderer handles
    the zero-slots case gracefully."""
    from community_organizer.core import db
    from community_organizer.core.models import Application, Community
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Test"))
    app = Application(community_id=cid, name="Adoration",
                      app_type="recurring_commitments")
    db.put_application(app)
    user = User(community_id=cid, email="m@example.com", name="Member")

    resp = web._home({}, user, None, app, None)
    assert resp["statusCode"] == 200
    assert "No slots scheduled in this window" in resp["body"]
    # Pagination scaffolding always shows even on empty pages.
    assert "Previous month" in resp["body"]
    assert "Next month" in resp["body"]


def test_recurring_home_renders_slots_with_user_actions(ddb_table) -> None:
    """Seed slots and an assignment for the current user; the home
    page renders Trade/Withdraw inline on the user's own slot. Also
    confirms the cohort join affordance shows up for a slot whose
    template has a linked cohort that the user is NOT yet a member of."""
    import datetime as dt
    from community_organizer.core import db
    from community_organizer.core.models import (
        Application, Assignment, Cohort, Community, Slot, SlotTemplate,
    )
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Test"))
    app = Application(community_id=cid, name="Adoration",
                      app_type="recurring_commitments")
    db.put_application(app)
    user = User(community_id=cid, email="m@example.com", name="Member")
    db.put_user(user)

    # Template + a cohort linked to it (matches our auto-cohort-per-
    # template pattern in production).
    tpl = SlotTemplate(community_id=cid, app_id=app.app_id,
                       name="Wed 2 PM", day_of_week=2,
                       start_time="14:00", duration_minutes=60,
                       auto_reminders=False)
    db.put_template(tpl)
    cohort = Cohort(community_id=cid, app_id=app.app_id,
                    name="Wed 2 PM regulars",
                    linked_template_id=tpl.template_id)
    db.put_cohort(cohort)

    # Seed a slot for next week (well within the 4-week window).
    today = dt.date.today()
    next_wed = today + dt.timedelta(days=(2 - today.weekday()) % 7 or 7)
    yyyy_mm = next_wed.strftime("%Y-%m")
    slot = Slot(
        community_id=cid, app_id=app.app_id, yyyy_mm=yyyy_mm,
        template_id=tpl.template_id, name="Wed 2 PM",
        day_of_week=2, start_time="14:00",
        arrival_offset_minutes=0, duration_minutes=60,
        required_volunteers=1, min_volunteers=1,
        concrete_date=next_wed.isoformat() + "T14:00:00",
        local_date=next_wed.isoformat(),
    )
    db.put_slot(slot)
    db.put_assignment(Assignment(
        community_id=cid, app_id=app.app_id, yyyy_mm=yyyy_mm,
        slot_id=slot.slot_id, user_id=user.user_id,
        local_date=next_wed.isoformat(),
    ))

    resp = web._home({}, user, None, app, None)
    assert resp["statusCode"] == 200
    body = resp["body"]
    # The slot itself appears.
    assert "Wed 2 PM" in body
    # User is assigned, so their inline actions render.
    assert "Trade" in body
    assert "Withdraw" in body
    # And the cohort opt-in affordance for the linked cohort renders,
    # showing the "+ Notify me" state because the user isn't joined.
    assert "Take this slot weekly" in body


def test_recurring_home_weekly_app_finds_iso_week_slots(ddb_table) -> None:
    """A recurring_commitments app with period_type='weekly' stores
    slots under ISO-week partition keys (e.g. "2026-W22"). Pin that
    _recurring_home queries those keys, not the monthly form."""
    import datetime as dt
    from community_organizer.core import db
    from community_organizer.core.models import (
        Application, Assignment, Community, Slot, SlotTemplate,
    )
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Test"))
    app = Application(community_id=cid, name="Adoration",
                      app_type="recurring_commitments",
                      period_type="weekly")
    db.put_application(app)
    user = User(community_id=cid, email="m@example.com", name="Member")
    db.put_user(user)

    tpl = SlotTemplate(community_id=cid, app_id=app.app_id,
                       name="Wed 2 PM", day_of_week=2,
                       start_time="14:00", duration_minutes=60)
    db.put_template(tpl)

    today = dt.date.today()
    next_wed = today + dt.timedelta(days=(2 - today.weekday()) % 7 or 7)
    iy, iw, _ = next_wed.isocalendar()
    period_id = f"{iy:04d}-W{iw:02d}"

    slot = Slot(
        community_id=cid, app_id=app.app_id, yyyy_mm=period_id,
        template_id=tpl.template_id, name="Wed 2 PM",
        day_of_week=2, start_time="14:00",
        arrival_offset_minutes=0, duration_minutes=60,
        required_volunteers=1, min_volunteers=1,
        concrete_date=next_wed.isoformat() + "T14:00:00",
        local_date=next_wed.isoformat(),
    )
    db.put_slot(slot)
    db.put_assignment(Assignment(
        community_id=cid, app_id=app.app_id, yyyy_mm=period_id,
        slot_id=slot.slot_id, user_id=user.user_id,
        local_date=next_wed.isoformat(),
    ))

    resp = web._home({}, user, None, app, None)
    assert resp["statusCode"] == 200
    body = resp["body"]
    # The weekly-partition slot shows up — proves the home page
    # queried the ISO-week partition, not the "YYYY-MM" form.
    assert "Wed 2 PM" in body
    assert "Withdraw" in body


def _seed_recurring_assignment(*, schedule_state: str = "draft"):
    """Helper: seed a recurring_commitments app, a slot, the caller's
    Assignment to it, and (optionally) a Schedule in the given state.

    Returns (community, app, user, slot) so tests can call
    web._serve_ics_for_assignment with the right path."""
    import datetime as dt
    from community_organizer.core import db
    from community_organizer.core.models import (
        Application, Assignment, Community, Schedule, Slot, SlotTemplate,
    )

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Test Parish"))
    app = Application(community_id=cid, name="Adoration",
                      app_type="recurring_commitments",
                      period_type="weekly")
    db.put_application(app)
    user = User(community_id=cid, email="m@example.com", name="Member")
    db.put_user(user)

    tpl = SlotTemplate(community_id=cid, app_id=app.app_id,
                       name="Wed 2 PM", day_of_week=2,
                       start_time="14:00", duration_minutes=60)
    db.put_template(tpl)

    today = dt.date.today()
    next_wed = today + dt.timedelta(days=(2 - today.weekday()) % 7 or 7)
    iy, iw, _ = next_wed.isocalendar()
    period_id = f"{iy:04d}-W{iw:02d}"
    slot = Slot(
        community_id=cid, app_id=app.app_id, yyyy_mm=period_id,
        template_id=tpl.template_id, name="Wed 2 PM",
        day_of_week=2, start_time="14:00",
        arrival_offset_minutes=0, duration_minutes=60,
        required_volunteers=1, min_volunteers=1,
        concrete_date=next_wed.isoformat() + "T14:00:00",
        local_date=next_wed.isoformat(),
    )
    db.put_slot(slot)
    db.put_assignment(Assignment(
        community_id=cid, app_id=app.app_id, yyyy_mm=period_id,
        slot_id=slot.slot_id, user_id=user.user_id,
        local_date=next_wed.isoformat(),
    ))
    db.put_schedule(Schedule(
        community_id=cid, app_id=app.app_id,
        yyyy_mm=period_id, state=schedule_state,
    ))

    community = db.get_community(cid)
    return community, app, user, slot


def test_ics_route_serves_when_user_assigned(ddb_table) -> None:
    """Happy path: caller is assigned, gets a single-event .ics."""
    from community_organizer.lambdas import web

    community, app, user, slot = _seed_recurring_assignment()
    event = {"rawPath": f"/ics/{slot.yyyy_mm}/{slot.slot_id}"}
    resp = web._serve_ics_for_assignment(event, user, community, app, None)
    assert resp["statusCode"] == 200
    ct = resp["headers"]["Content-Type"]
    assert ct.startswith("text/calendar")
    assert "attachment" in resp["headers"]["Content-Disposition"]
    body = resp["body"]
    assert "BEGIN:VCALENDAR" in body
    assert "BEGIN:VEVENT" in body
    assert "SUMMARY:Wed 2 PM" in body


def test_ics_route_ignores_schedule_state(ddb_table) -> None:
    """The point of Step 6: an assignment becomes .ics-able the
    moment it exists, NOT only after the enclosing Schedule is
    published. Pin that draft, publishing, and archived states
    all serve the same way published does."""
    from community_organizer.lambdas import web

    for state in ("draft", "publishing", "published", "archived"):
        # Wipe and re-seed per state so each iteration is independent.
        community, app, user, slot = _seed_recurring_assignment(
            schedule_state=state)
        event = {"rawPath": f"/ics/{slot.yyyy_mm}/{slot.slot_id}"}
        resp = web._serve_ics_for_assignment(
            event, user, community, app, None)
        assert resp["statusCode"] == 200, (
            f"schedule state={state!r} should still serve the .ics")


def test_ics_route_returns_404_when_not_assigned(ddb_table) -> None:
    """If the caller has no Assignment on the slot, no .ics — even
    if some OTHER user does. (Admins use a separate path; this
    route is self-service only.)"""
    from community_organizer.core import db
    from community_organizer.core.models import User as UserModel
    from community_organizer.lambdas import web

    community, app, assigned_user, slot = _seed_recurring_assignment()
    # A different user with no assignment.
    other = UserModel(community_id=community.community_id,
                      email="o@example.com", name="Other")
    db.put_user(other)
    event = {"rawPath": f"/ics/{slot.yyyy_mm}/{slot.slot_id}"}
    resp = web._serve_ics_for_assignment(event, other, community, app, None)
    assert resp["statusCode"] == 404


def test_ics_route_returns_404_for_missing_slot(ddb_table) -> None:
    """Unknown slot_id under a real period returns 404, not 500."""
    from community_organizer.lambdas import web

    community, app, user, slot = _seed_recurring_assignment()
    event = {"rawPath": f"/ics/{slot.yyyy_mm}/does-not-exist"}
    resp = web._serve_ics_for_assignment(event, user, community, app, None)
    assert resp["statusCode"] == 404


def test_ics_route_returns_404_for_malformed_path(ddb_table) -> None:
    """Path missing slot_id (or missing period_id) returns 404."""
    from community_organizer.lambdas import web

    community, app, user, slot = _seed_recurring_assignment()
    for path in ("/ics/", "/ics/just-one-segment", "/ics//slot-id"):
        resp = web._serve_ics_for_assignment(
            {"rawPath": path}, user, community, app, None)
        assert resp["statusCode"] == 404, f"path={path!r}"


def test_recurring_grid_renders_add_to_calendar_link(ddb_table) -> None:
    """The recurring home grid shows an `Add to calendar` action
    inline next to Trade/Withdraw for the user's own slot. Confirms
    the surface that points users at /ics/... is wired up."""
    import datetime as dt
    from community_organizer.core import db
    from community_organizer.core.models import (
        Application, Assignment, Community, Slot, SlotTemplate,
    )
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Test"))
    app = Application(community_id=cid, name="Adoration",
                      app_type="recurring_commitments",
                      period_type="weekly")
    db.put_application(app)
    u = User(community_id=cid, email="m@example.com", name="Member")
    db.put_user(u)

    tpl = SlotTemplate(community_id=cid, app_id=app.app_id,
                       name="Wed 2 PM", day_of_week=2,
                       start_time="14:00", duration_minutes=60)
    db.put_template(tpl)

    today = dt.date.today()
    next_wed = today + dt.timedelta(days=(2 - today.weekday()) % 7 or 7)
    iy, iw, _ = next_wed.isocalendar()
    pid = f"{iy:04d}-W{iw:02d}"
    slot = Slot(
        community_id=cid, app_id=app.app_id, yyyy_mm=pid,
        template_id=tpl.template_id, name="Wed 2 PM",
        day_of_week=2, start_time="14:00",
        arrival_offset_minutes=0, duration_minutes=60,
        required_volunteers=1, min_volunteers=1,
        concrete_date=next_wed.isoformat() + "T14:00:00",
        local_date=next_wed.isoformat(),
    )
    db.put_slot(slot)
    db.put_assignment(Assignment(
        community_id=cid, app_id=app.app_id, yyyy_mm=pid,
        slot_id=slot.slot_id, user_id=u.user_id,
        local_date=next_wed.isoformat(),
    ))

    resp = web._home({}, u, None, app, None)
    assert resp["statusCode"] == 200
    assert f"/ics/{pid}/{slot.slot_id}" in resp["body"]
    assert "Add to calendar" in resp["body"]


def test_home_raises_on_unknown_app_type(ddb_table) -> None:
    """The explicit-dispatch design says any unhandled app_type
    must raise loud, not silently fall into 'coverage' (the original
    default-app_type bug class we removed). Confirm that's what
    happens — this test would have caught a missing elif branch."""
    from community_organizer.core.models import Application
    from community_organizer.lambdas import web

    # standing_event and flexible_event are now wired up (slice 2);
    # use a bogus type the type checker would have rejected.
    app = Application(community_id="c1", name="Future App",
                      app_type="not_a_real_type")  # type: ignore[arg-type]
    user = User(community_id="c1", email="x@example.com", name="X")
    with pytest.raises(ValueError, match="unhandled app_type"):
        web._home({}, user, None, app, None)


def test_member_can_self_remove_from_cohort(cohort_setup) -> None:
    """Symmetric to self-add: a member can remove themselves — provided
    they remain a member of at least one other cohort in the app. The
    #219 self-service rule requires keeping at least one cohort
    affinity so the admin still has them on a picklist."""
    from community_organizer.core import db
    from community_organizer.core.models import Cohort, CohortMembership
    from community_organizer.lambdas import web

    s = cohort_setup
    # Pre-seed: member is in the cohort_setup cohort PLUS a second one
    # (the #219 rule blocks removal that would drop the user to 0).
    db.put_cohort_membership(CohortMembership(
        cohort_id=s["cohort"].cohort_id, user_id=s["member"].user_id))
    second = Cohort(community_id=s["app"].community_id,
                    app_id=s["app"].app_id, name="Wed 6 PM")
    db.put_cohort(second)
    db.put_cohort_membership(CohortMembership(
        cohort_id=second.cohort_id, user_id=s["member"].user_id))

    member_membership = db.get_membership(s["app"].app_id, s["member"].user_id)
    event = _cohort_event(s["cohort"].cohort_id, s["member"].user_id)
    resp = web._api_cohort_remove_member(
        event, s["member"], None, s["app"], member_membership)
    assert resp["statusCode"] == 302
    members = list(db.list_cohort_members(s["cohort"].cohort_id))
    assert s["member"].user_id not in {m.user_id for m in members}
    # Still in the second cohort.
    second_members = list(db.list_cohort_members(second.cohort_id))
    assert s["member"].user_id in {m.user_id for m in second_members}


def test_member_cannot_self_remove_from_last_cohort(cohort_setup) -> None:
    """#219 rule: a member trying to leave their ONLY remaining cohort
    is bounced with an error and the membership stays. Admin-driven
    removal is unaffected — only self-service is gated."""
    from community_organizer.core import db
    from community_organizer.core.models import CohortMembership
    from community_organizer.lambdas import web

    s = cohort_setup
    db.put_cohort_membership(CohortMembership(
        cohort_id=s["cohort"].cohort_id, user_id=s["member"].user_id))

    member_membership = db.get_membership(s["app"].app_id, s["member"].user_id)
    event = _cohort_event(s["cohort"].cohort_id, s["member"].user_id)
    resp = web._api_cohort_remove_member(
        event, s["member"], None, s["app"], member_membership)
    assert resp["statusCode"] == 302
    assert "error=" in resp["headers"]["Location"]
    # Membership unchanged.
    members = list(db.list_cohort_members(s["cohort"].cohort_id))
    assert s["member"].user_id in {m.user_id for m in members}


def test_admin_can_remove_last_cohort_membership(cohort_setup) -> None:
    """The "must keep one cohort" rule is self-service-only.
    Admins can fully detach a member from every cohort."""
    from community_organizer.core import db
    from community_organizer.core.models import CohortMembership
    from community_organizer.lambdas import web

    s = cohort_setup
    db.put_cohort_membership(CohortMembership(
        cohort_id=s["cohort"].cohort_id, user_id=s["member"].user_id))

    admin_membership = db.get_membership(s["app"].app_id, s["admin"].user_id)
    event = _cohort_event(s["cohort"].cohort_id, s["member"].user_id)
    resp = web._api_cohort_remove_member(
        event, s["admin"], None, s["app"], admin_membership)
    assert resp["statusCode"] == 302
    members = list(db.list_cohort_members(s["cohort"].cohort_id))
    assert s["member"].user_id not in {m.user_id for m in members}


def test_auto_link_skipped_when_email_claim_absent(patch_user_lookup) -> None:
    """No email claim at all → fall through to the unprovisioned
    page; no DB writes."""
    from community_organizer.lambdas import web

    patch_user_lookup["user_by_email"] = User(
        community_id="c1", email="x@example.com", name="X")
    patch_user_lookup["claims"] = {"sub": "SOME-SUB"}
    resp = web._route(_event_with_token(),
                      lambda *_a, **_k: web._text(200, "ok"))
    assert resp["statusCode"] == 403
    assert patch_user_lookup["put_calls"] == []


# ---------------------------------------------------------------------------
# CA landing + community-users surfaces (Section A of the CA work)
# ---------------------------------------------------------------------------


def _seed_two_apps(ddb_table):
    """Seed a community with two apps + a CA + a plain member; returns
    (community, ca_user, member_user, app_a, app_b)."""
    from community_organizer.core import db
    from community_organizer.core.models import Application, Community

    cid = "c1"
    db.put_community(Community(community_id=cid, name="St. Test"))
    ca = User(community_id=cid, email="ca@example.com", name="CA",
              community_role="ca")
    db.put_user(ca)
    mem = User(community_id=cid, email="m@example.com", name="Member")
    db.put_user(mem)
    app_a = Application(community_id=cid, name="Ushers",
                        app_type="coverage", period_type="monthly")
    db.put_application(app_a)
    app_b = Application(community_id=cid, name="Adoration",
                        app_type="recurring_commitments",
                        period_type="weekly")
    db.put_application(app_b)
    return db.get_community(cid), ca, mem, app_a, app_b


def test_ca_landing_shows_description_and_edit_link(ddb_table) -> None:
    """Each app row on /admin/apps shows its description (or a
    '(no description)' placeholder) plus an 'edit name & description'
    link that bounces back with ?edit=<id>."""
    from community_organizer.core import db
    from community_organizer.lambdas import web

    community, ca, _, app_a, app_b = _seed_two_apps(ddb_table)
    # Pre-set a description on app_a; leave app_b blank.
    app_a.description = "Sunday Mass usher rotation."
    db.put_application(app_a)

    resp = web._ca_landing_page({}, ca, community)
    body = resp["body"]
    assert "Sunday Mass usher rotation." in body
    assert "(no description)" in body         # app_b's empty state
    assert f"edit={app_a.app_id}" in body


def test_ca_landing_uses_aa_section_style(ddb_table) -> None:
    """#193: CA landing matches the AA home's visual scheme — three
    left-flush `<section>` blocks (Apps, Member management, Create
    new app), each with a h2 at the standard
    font-size:1.1em / color:#444. Pre-fix the Apps heading used the
    bare default <h2> styling and Create new app was centered.
    """
    from community_organizer.lambdas import web

    community, ca, _, _, _ = _seed_two_apps(ddb_table)
    body = web._ca_landing_page({}, ca, community)["body"]

    # Each section uses the AA-home shape (#196: "App" spelled out
    # as "Application" on the CA landing per the public wording choice).
    for section in ("Applications", "Member management",
                    "Create new Application"):
        assert (f"<h2 style='font-size:1.1em;color:#444'>{section}"
                in body), f"section {section!r} missing AA-style heading"

    # No more centered Create new app block.
    assert "text-align:center'>Create new" not in body
    # The Member management section's action link points at
    # /admin/community-users, not /admin/users (which is per-app).
    assert "/admin/community-users" in body


def test_ca_landing_member_widget_lists_recent_community_users(
        ddb_table) -> None:
    """#193: the Member-management section on the CA landing renders
    community-wide (NOT app-scoped) — that's the distinguishing
    feature vs the per-app Member-management widget on /."""
    from community_organizer.core import db
    from community_organizer.core.models import User
    from community_organizer.lambdas import web

    community, ca, _, _, _ = _seed_two_apps(ddb_table)
    # Add a few community users with explicit created_at so we know
    # which appear in "Most recently added".
    db.put_user(User(community_id=community.community_id,
                     email="latest@example.com", name="Latest User",
                     created_at="2030-01-05T00:00:00+00:00"))
    db.put_user(User(community_id=community.community_id,
                     email="older@example.com", name="Older User",
                     created_at="2020-01-01T00:00:00+00:00"))

    body = web._ca_landing_page({}, ca, community)["body"]
    # Both community users appear; the seed's "CA" + "Member" do too.
    for name in ("Latest User", "Older User"):
        assert name in body
    # Total count = 4 (CA, Member, Latest, Older).
    assert "4 users total" in body


def test_ca_landing_edit_desc_renders_inline_form(ddb_table) -> None:
    """?edit_desc=<id> on /admin/apps still routes through the same
    edit form (legacy alias for ?edit=)."""
    from community_organizer.lambdas import web

    community, ca, _, app_a, _ = _seed_two_apps(ddb_table)
    event = {
        "rawPath": "/admin/apps",
        "queryStringParameters": {"edit_desc": app_a.app_id},
    }
    resp = web._ca_landing_page(event, ca, community)
    body = resp["body"]
    assert "name='description'" in body
    assert "/api/apps/update" in body
    assert f"value='{app_a.app_id}'" in body


def test_ca_landing_edit_renders_name_and_description_inputs(ddb_table) -> None:
    """?edit=<id> renders an inline form with BOTH a name input and
    a description textarea — the name-editing affordance requested
    on 2026-06-03."""
    from community_organizer.lambdas import web

    community, ca, _, app_a, _ = _seed_two_apps(ddb_table)
    event = {
        "rawPath": "/admin/apps",
        "queryStringParameters": {"edit": app_a.app_id},
    }
    resp = web._ca_landing_page(event, ca, community)
    body = resp["body"]
    # Name input is required and pre-filled with the current name.
    assert "name='name'" in body
    assert f"value='{app_a.name}'" in body
    assert "name='description'" in body
    assert "/api/apps/update" in body


def test_api_app_update_description_saves(ddb_table) -> None:
    """Posting /api/apps/update-description updates the field and
    redirects back to /admin/apps."""
    from community_organizer.core import db
    from community_organizer.lambdas import web

    community, ca, _, app_a, _ = _seed_two_apps(ddb_table)
    event = {
        "rawPath": "/api/apps/update-description",
        "requestContext": {"http": {"method": "POST"}},
        "queryStringParameters": {
            "app_id": app_a.app_id,
            "description": "Volunteers needed at the 8 AM and 10:30 Masses.",
        },
    }
    resp = web._api_app_update_description(event, ca, community)
    assert resp["statusCode"] == 302
    assert resp["headers"]["Location"] == "/admin/apps"
    fresh = db.get_application(community.community_id, app_a.app_id)
    assert fresh.description == (
        "Volunteers needed at the 8 AM and 10:30 Masses.")


def test_api_app_update_description_allowed_for_ua(ddb_table) -> None:
    """Description is per-app metadata, editable by UA too (not a
    structural change like app create/delete)."""
    from community_organizer.core import db
    from community_organizer.lambdas import web

    community, _, _, app_a, _ = _seed_two_apps(ddb_table)
    ua = User(community_id=community.community_id, email="ua@example.com",
              name="UA", community_role="ua")
    db.put_user(ua)
    event = {
        "rawPath": "/api/apps/update-description",
        "requestContext": {"http": {"method": "POST"}},
        "queryStringParameters": {
            "app_id": app_a.app_id, "description": "UA wrote this.",
        },
    }
    resp = web._api_app_update_description(event, ua, community)
    assert resp["statusCode"] == 302
    fresh = db.get_application(community.community_id, app_a.app_id)
    assert fresh.description == "UA wrote this."


def test_api_app_update_renames_app(ddb_table) -> None:
    """POSTing /api/apps/update with both name and description applies
    both fields. The requested behavior: let CAs rename apps from the
    CA landing page so a co-admin can rebrand a test app to something
    more recognizable before walking through it."""
    from community_organizer.core import db
    from community_organizer.lambdas import web

    community, ca, _, app_a, _ = _seed_two_apps(ddb_table)
    event = {
        "rawPath": "/api/apps/update",
        "requestContext": {"http": {"method": "POST"}},
        "queryStringParameters": {
            "app_id": app_a.app_id,
            "name": "Volunteer App",
            "description": "Renamed for co-admin walkthrough.",
        },
    }
    resp = web._api_app_update(event, ca, community)
    assert resp["statusCode"] == 302
    fresh = db.get_application(community.community_id, app_a.app_id)
    assert fresh.name == "Volunteer App"
    assert fresh.description == "Renamed for co-admin walkthrough."


def test_api_app_update_rejects_blank_name(ddb_table) -> None:
    """A submitted name that's only whitespace would silently blank
    the app's display name. Reject it with a styled error banner
    instead, leaving the existing name intact."""
    from community_organizer.core import db
    from community_organizer.lambdas import web

    community, ca, _, app_a, _ = _seed_two_apps(ddb_table)
    original_name = app_a.name
    event = {
        "rawPath": "/api/apps/update",
        "requestContext": {"http": {"method": "POST"}},
        "queryStringParameters": {
            "app_id": app_a.app_id,
            "name": "   ",
            "description": "irrelevant",
        },
    }
    resp = web._api_app_update(event, ca, community)
    assert resp["statusCode"] == 302
    assert "error=" in resp["headers"]["Location"]
    fresh = db.get_application(community.community_id, app_a.app_id)
    assert fresh.name == original_name


def test_api_app_update_description_only_leaves_name_unchanged(ddb_table) -> None:
    """A POST without a `name` param (legacy description-only form,
    or the in-app /admin/settings description editor) must leave the
    name field alone — only the description gets written."""
    from community_organizer.core import db
    from community_organizer.lambdas import web

    community, ca, _, app_a, _ = _seed_two_apps(ddb_table)
    original_name = app_a.name
    event = {
        "rawPath": "/api/apps/update-description",
        "requestContext": {"http": {"method": "POST"}},
        "queryStringParameters": {
            "app_id": app_a.app_id,
            "description": "Just the description.",
        },
    }
    resp = web._api_app_update_description(event, ca, community)
    assert resp["statusCode"] == 302
    fresh = db.get_application(community.community_id, app_a.app_id)
    assert fresh.name == original_name
    assert fresh.description == "Just the description."


def test_ca_landing_for_ua_hides_create_and_delete(ddb_table) -> None:
    """UA viewing /admin/apps sees the same app list as CA but
    without the per-row Delete buttons or the Create-new-app form.
    They can still click an app to pivot in."""
    from community_organizer.lambdas import web

    community, ca, _, app_a, app_b = _seed_two_apps(ddb_table)
    ua = User(community_id=community.community_id, email="ua@example.com",
              name="UA", community_role="ua")
    from community_organizer.core import db
    db.put_user(ua)
    resp = web._ca_landing_page({}, ua, community)
    assert resp["statusCode"] == 200
    body = resp["body"]
    # Apps still listed + pivot links work.
    assert "Ushers" in body and "Adoration" in body
    assert f"/?app_id={app_a.app_id}" in body
    # No Delete affordance on any row.
    assert "Delete</button>" not in body
    # No Create form (heading uses spelled-out "Application" per #196).
    assert "Create new Application" not in body
    # Label reflects UA.
    assert "User Admin landing" in body
    # And the CA landing for the same data DOES show the form +
    # Delete buttons — proves the UA hiding is conditional.
    resp_ca = web._ca_landing_page({}, ca, community)
    assert "Create new Application" in resp_ca["body"]
    assert "Delete</button>" in resp_ca["body"]
    assert "Community Admin landing" in resp_ca["body"]


def test_api_app_create_rejects_ua(ddb_table) -> None:
    """Even if a UA hand-crafts a POST to /api/apps/create, the
    server rejects it. App create/delete is CA only."""
    from community_organizer.core import db
    from community_organizer.core.models import Community
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Test"))
    ua = User(community_id=cid, email="ua@example.com", name="UA",
              community_role="ua")
    db.put_user(ua)
    event = {
        "rawPath": "/api/apps/create",
        "requestContext": {"http": {"method": "POST"}},
        "queryStringParameters": {
            "name": "Sneaky", "app_type": "coverage",
        },
    }
    resp = web._api_app_create(event, ua, db.get_community(cid))
    assert resp["statusCode"] == 403
    # No app created.
    assert list(db.list_applications(cid)) == []


def test_api_app_delete_rejects_ua(ddb_table) -> None:
    from community_organizer.core import db
    from community_organizer.lambdas import web

    community, _, _, app_a, _ = _seed_two_apps(ddb_table)
    ua = User(community_id=community.community_id, email="ua@example.com",
              name="UA", community_role="ua")
    db.put_user(ua)
    event = {
        "rawPath": "/api/apps/delete",
        "requestContext": {"http": {"method": "POST"}},
        "queryStringParameters": {"app_id": app_a.app_id},
    }
    resp = web._api_app_delete(event, ua, community)
    assert resp["statusCode"] == 403
    # app_a still exists.
    assert db.get_application(community.community_id, app_a.app_id) is not None


def test_ua_can_do_community_user_roster_ops(ddb_table) -> None:
    """UA retains full roster authority across apps: add/edit/delete
    community users, add/remove/toggle memberships."""
    from community_organizer.core import db
    from community_organizer.core.models import Application, Community, Membership
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Test"))
    app = Application(community_id=cid, name="Ushers",
                      app_type="coverage", period_type="monthly")
    db.put_application(app)
    ua = User(community_id=cid, email="ua@example.com", name="UA",
              community_role="ua")
    db.put_user(ua)
    target = User(community_id=cid, email="t@example.com", name="Target")
    db.put_user(target)
    # 1) UA can add a membership for target via CA users page.
    event = {
        "rawPath": "/api/community-users/add-membership",
        "requestContext": {"http": {"method": "POST"}},
        "queryStringParameters": {
            "user_id": target.user_id,
            "target_app_id": app.app_id,
            "role": "member",
        },
    }
    resp = web._api_ca_membership_add(event, ua, db.get_community(cid))
    assert resp["statusCode"] == 302
    assert db.get_membership(app.app_id, target.user_id) is not None
    # 2) UA can delete the target user from the community.
    event_del = {
        "rawPath": "/api/users/delete",
        "requestContext": {"http": {"method": "POST"}},
        "queryStringParameters": {"user_id": target.user_id},
    }
    # Stub Cognito to avoid real boto calls.
    import unittest.mock as _mock
    with _mock.patch.object(web, "_get_cognito") as _cog:
        _cog.return_value.admin_delete_user = lambda **k: None
        resp_del = web._api_user_delete(event_del, ua, db.get_community(cid),
                                        app, None)
    assert resp_del["statusCode"] == 302
    assert db.get_user(cid, target.user_id) is None


def test_ua_cannot_promote_users_to_ca(ddb_table) -> None:
    """Only CAs can change community_role via _api_user_edit. A UA
    POSTing community_role gets silently ignored (the value isn't
    applied) — the user can still edit name/email/etc."""
    from community_organizer.core import db
    from community_organizer.core.models import Application, Community, Membership
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Test"))
    app = Application(community_id=cid, name="Ushers",
                      app_type="coverage", period_type="monthly")
    db.put_application(app)
    ua = User(community_id=cid, email="ua@example.com", name="UA",
              community_role="ua")
    db.put_user(ua)
    # UA must have a membership in the app to use the per-app edit path.
    db.put_membership(Membership(community_id=cid, app_id=app.app_id,
                                 user_id=ua.user_id, app_role="aa"))
    target = User(community_id=cid, email="t@example.com", name="Target",
                  community_role="member")
    db.put_user(target)
    db.put_membership(Membership(community_id=cid, app_id=app.app_id,
                                 user_id=target.user_id))
    ua_mem = db.get_membership(app.app_id, ua.user_id)
    event = {
        "rawPath": "/api/users/edit",
        "requestContext": {"http": {"method": "POST"}},
        "queryStringParameters": {
            "user_id": target.user_id,
            "name": "Renamed Target",
            "community_role": "ca",   # UA shouldn't be able to set this
        },
    }
    resp = web._api_user_edit(event, ua, db.get_community(cid),
                              app, ua_mem)
    assert resp["statusCode"] in (200, 302)
    fresh = db.get_user(cid, target.user_id)
    # Name was updated (UA can edit normal fields).
    assert fresh.name == "Renamed Target"
    # community_role was NOT escalated.
    assert fresh.community_role == "member"


def test_ca_landing_lists_apps_with_types(ddb_table) -> None:
    """The CA landing renders every app in the community with its
    app_type label and a pivot link to /?app_id=<id>."""
    from community_organizer.lambdas import web

    community, ca, _, app_a, app_b = _seed_two_apps(ddb_table)
    resp = web._ca_landing_page({}, ca, community)
    assert resp["statusCode"] == 200
    body = resp["body"]
    assert "Ushers" in body
    assert "Adoration" in body
    assert "Coverage" in body                       # type label
    assert "Recurring Commitments" in body
    assert f"/?app_id={app_a.app_id}" in body       # pivot links
    assert f"/?app_id={app_b.app_id}" in body
    # Delete affordance for each row.
    assert body.count("Delete</button>") >= 2


def test_ca_landing_empty_state(ddb_table) -> None:
    """A community with no apps shows the empty-state copy and the
    create form (CA can bootstrap)."""
    from community_organizer.core import db
    from community_organizer.core.models import Community
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Empty"))
    ca = User(community_id=cid, email="ca@example.com", name="CA",
              community_role="ca")
    db.put_user(ca)
    community = db.get_community(cid)
    resp = web._ca_landing_page({}, ca, community)
    assert resp["statusCode"] == 200
    assert "No apps yet" in resp["body"]
    assert "Create new Application" in resp["body"]


def test_api_app_create_happy_path(ddb_table) -> None:
    """POST /api/apps/create with name+type+description+period creates
    the application with all fields populated. Description is the
    same field surfaced on /launcher; capturing it at creation
    avoids the 'create then immediately edit description' two-step."""
    from community_organizer.core import db
    from community_organizer.core.models import Community
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Test"))
    ca = User(community_id=cid, email="ca@example.com", name="CA",
              community_role="ca")
    db.put_user(ca)

    event = {
        "rawPath": "/api/apps/create",
        "requestContext": {"http": {"method": "POST"}},
        "queryStringParameters": {
            "name": "New Adoration",
            "app_type": "recurring_commitments",
            "period_type": "weekly",
            "description": "Weekly Eucharistic adoration cohort.",
        },
    }
    resp = web._api_app_create(event, ca, db.get_community(cid))
    assert resp["statusCode"] == 302
    apps = list(db.list_applications(cid))
    assert len(apps) == 1
    assert apps[0].name == "New Adoration"
    assert apps[0].app_type == "recurring_commitments"
    assert apps[0].period_type == "weekly"
    assert apps[0].description == "Weekly Eucharistic adoration cohort."


def test_api_app_create_defaults_period_when_omitted(ddb_table) -> None:
    """Backward compat: if period_type isn't posted (e.g. older form,
    or CLI without --period-type), API still derives it from app_type."""
    from community_organizer.core import db
    from community_organizer.core.models import Community
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Test"))
    ca = User(community_id=cid, email="ca@example.com", name="CA",
              community_role="ca")
    db.put_user(ca)
    event = {
        "rawPath": "/api/apps/create",
        "requestContext": {"http": {"method": "POST"}},
        "queryStringParameters": {
            "name": "Sunday Ushers", "app_type": "coverage",
        },
    }
    resp = web._api_app_create(event, ca, db.get_community(cid))
    assert resp["statusCode"] == 302
    apps = list(db.list_applications(cid))
    assert apps[0].period_type == "monthly"     # defaulted from coverage
    assert apps[0].description == ""             # blank, not None


def test_api_app_create_rejects_unsupported_type(ddb_table) -> None:
    """A garbage app_type at the create boundary must be refused with
    a styled error redirect, not silently create a useless row."""
    from community_organizer.core import db
    from community_organizer.core.models import Community
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Test"))
    ca = User(community_id=cid, email="ca@example.com", name="CA",
              community_role="ca")
    db.put_user(ca)
    event = {
        "rawPath": "/api/apps/create",
        "requestContext": {"http": {"method": "POST"}},
        "queryStringParameters": {
            "name": "X", "app_type": "not_a_real_type",
        },
    }
    resp = web._api_app_create(event, ca, db.get_community(cid))
    # Now redirects to /admin/apps with a styled error banner.
    assert resp["statusCode"] == 302
    assert "error=" in resp["headers"]["Location"]


def test_api_app_create_accepts_standing_event(ddb_table) -> None:
    """Slice 2: standing_event apps are now creatable. Round-trip
    via list_applications to confirm the row landed."""
    from community_organizer.core import db
    from community_organizer.core.models import Community
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Test"))
    ca = User(community_id=cid, email="ca@example.com", name="CA",
              community_role="ca")
    db.put_user(ca)
    event = {
        "rawPath": "/api/apps/create",
        "requestContext": {"http": {"method": "POST"}},
        "queryStringParameters": {
            "name": "Book Club", "app_type": "flexible_event",
        },
    }
    resp = web._api_app_create(event, ca, db.get_community(cid))
    assert resp["statusCode"] == 302
    assert resp["headers"]["Location"] == "/admin/apps"
    apps = list(db.list_applications(cid))
    assert len(apps) == 1
    assert apps[0].app_type == "flexible_event"
    assert apps[0].name == "Book Club"


def test_api_app_delete_cascades_per_app_rows(ddb_table) -> None:
    """Delete wipes the Application AND every per-app row — templates,
    schedules, slots, assignments, memberships, cohorts and their
    members, swaps, notifications. The deleted app's user is still
    in the community afterwards (only app-scoped state is touched)."""
    import datetime as dt
    from community_organizer.core import db
    from community_organizer.core.models import (
        Assignment, Cohort, CohortMembership, Membership, Schedule,
        Slot, SlotTemplate, SwapRequest,
    )
    from community_organizer.lambdas import web

    community, ca, mem_user, app_a, app_b = _seed_two_apps(ddb_table)
    cid = community.community_id

    # Stuff every kind of per-app row into app_a so we can pin that
    # every kind gets cleared. app_b gets a parallel row to confirm
    # the cascade doesn't bleed across apps.
    db.put_template(SlotTemplate(community_id=cid, app_id=app_a.app_id,
                                 name="Sun 8 AM", day_of_week=6,
                                 start_time="08:00", duration_minutes=60))
    db.put_template(SlotTemplate(community_id=cid, app_id=app_b.app_id,
                                 name="Wed 2 PM", day_of_week=2,
                                 start_time="14:00", duration_minutes=60))
    db.put_membership(Membership(community_id=cid, app_id=app_a.app_id,
                                 user_id=mem_user.user_id))
    db.put_schedule(Schedule(community_id=cid, app_id=app_a.app_id,
                             yyyy_mm="2026-05"))
    db.put_slot(Slot(community_id=cid, app_id=app_a.app_id,
                     yyyy_mm="2026-05",
                     template_id="t1", name="Sun 8 AM", day_of_week=6,
                     start_time="08:00", arrival_offset_minutes=0,
                     duration_minutes=60, required_volunteers=1,
                     min_volunteers=1, concrete_date="2026-05-03T12:00",
                     local_date="2026-05-03"))
    db.put_assignment(Assignment(community_id=cid, app_id=app_a.app_id,
                                 yyyy_mm="2026-05", slot_id="slot1",
                                 user_id=mem_user.user_id,
                                 local_date="2026-05-03"))
    cohort = Cohort(community_id=cid, app_id=app_a.app_id,
                    name="Regulars")
    db.put_cohort(cohort)
    db.put_cohort_membership(CohortMembership(
        cohort_id=cohort.cohort_id, user_id=mem_user.user_id))
    db.put_swap(SwapRequest(community_id=cid, app_id=app_a.app_id,
                            yyyy_mm="2026-05",
                            requester_user_id=mem_user.user_id,
                            release_slot_id="slot1"))

    event = {
        "rawPath": "/api/apps/delete",
        "requestContext": {"http": {"method": "POST"}},
        "queryStringParameters": {"app_id": app_a.app_id},
    }
    resp = web._api_app_delete(event, ca, community)
    assert resp["statusCode"] == 302

    # app_a wiped completely
    assert db.get_application(cid, app_a.app_id) is None
    assert not list(db.list_templates(app_a.app_id))
    assert not list(db.list_memberships_for_app(app_a.app_id))
    assert not list(db.list_schedules(app_a.app_id))
    assert not list(db.list_slots(app_a.app_id, "2026-05"))
    assert not list(db.list_assignments_for_month(app_a.app_id, "2026-05"))
    assert not list(db.list_cohorts(app_a.app_id))
    assert not list(db.list_cohort_members(cohort.cohort_id))
    assert not list(db.list_swaps_for_month(app_a.app_id, "2026-05"))

    # app_b untouched
    assert db.get_application(cid, app_b.app_id) is not None
    assert list(db.list_templates(app_b.app_id))

    # User still in the community (cascade is app-scoped only).
    assert db.get_user(cid, mem_user.user_id) is not None


def test_delete_application_recreate_does_not_restore(ddb_table) -> None:
    """The user asked: if I delete an app and create a new one with
    the same name/type, does old data re-attach? Answer: no, because
    the new app gets a fresh UUID. Pin that explicitly so the cascade
    behavior + UUID-keying invariant stay coupled."""
    from community_organizer.core import db
    from community_organizer.core.models import Application, Community, SlotTemplate

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Test"))
    app_v1 = Application(community_id=cid, name="Adoration",
                         app_type="recurring_commitments",
                         period_type="weekly")
    db.put_application(app_v1)
    db.put_template(SlotTemplate(community_id=cid, app_id=app_v1.app_id,
                                 name="Wed 2 PM", day_of_week=2,
                                 start_time="14:00", duration_minutes=60))

    db.delete_application(cid, app_v1.app_id)

    # New app, same name + type. UUID is fresh, so it has zero templates.
    app_v2 = Application(community_id=cid, name="Adoration",
                         app_type="recurring_commitments",
                         period_type="weekly")
    db.put_application(app_v2)
    assert app_v2.app_id != app_v1.app_id
    assert list(db.list_templates(app_v2.app_id)) == []
    # The old app's templates are GONE (cascaded), not orphaned.
    assert list(db.list_templates(app_v1.app_id)) == []


def test_corner_menu_caps_apps_with_overflow_link(ddb_table) -> None:
    """When a CA has more apps than the corner cap, render the first
    N-1 + a "More apps" link to /launcher. Replaces the pre-#188
    spill-into-second-column path (which ate mobile real estate)."""
    from community_organizer.core import db
    from community_organizer.core.models import Application, Community
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Test"))
    # 8 apps named A01..A08 so the alpha sort is predictable.
    apps = []
    for i in range(8):
        a = Application(community_id=cid, name=f"A{i+1:02d}",
                        app_type="coverage", period_type="monthly")
        db.put_application(a)
        apps.append(a)
    ca = User(community_id=cid, email="ca@example.com", name="CA",
              community_role="ca")
    db.put_user(ca)

    corner = web._build_user_corner(ca, db.get_community(cid),
                                    current_app=None)
    # No two-column path — single column, no flex wrapper.
    assert "flex-direction:row-reverse" not in corner
    # The first 4 apps (alpha-sorted) appear; the next 4 don't.
    for a in apps[:4]:
        assert f">{a.name}<" in corner
    for a in apps[4:]:
        assert f">{a.name}<" not in corner
    # Overflow link to the launcher names the count of hidden apps.
    assert "/launcher" in corner
    assert "More apps (4)" in corner
    # Sign out still appears exactly once.
    assert corner.count("Sign out") == 1


def test_corner_menu_keeps_current_app_under_overflow(ddb_table) -> None:
    """When more apps exist than fit, the current app sorts to the
    top so the chip never drops the very app the user is in."""
    from community_organizer.core import db
    from community_organizer.core.models import Application, Community
    from community_organizer.lambdas import web

    cid = "c-current"
    db.put_community(Community(community_id=cid, name="Test"))
    apps = []
    for i in range(8):
        a = Application(community_id=cid, name=f"A{i+1:02d}",
                        app_type="coverage", period_type="monthly")
        db.put_application(a)
        apps.append(a)
    ca = User(community_id=cid, email="ca@example.com", name="CA",
              community_role="ca")
    db.put_user(ca)
    # Pivot the LAST alpha-sorted app (A08) — it would normally fall
    # into the overflow, but should be promoted to #1 because it's
    # the current_app.
    last = apps[-1]
    corner = web._build_user_corner(ca, db.get_community(cid),
                                    current_app=last)
    assert f">{last.name}<" in corner
    # Bold weight applied to the current app's link.
    assert "font-weight:600" in corner


def test_corner_menu_stays_single_column_for_few_apps(ddb_table) -> None:
    """A CA with 1-2 apps renders without the overflow link — apps
    are all in the chip directly."""
    from community_organizer.core import db
    from community_organizer.core.models import Application, Community
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Test"))
    db.put_application(Application(community_id=cid, name="Only",
                                   app_type="coverage",
                                   period_type="monthly"))
    ca = User(community_id=cid, email="ca@example.com", name="CA",
              community_role="ca")
    db.put_user(ca)
    corner = web._build_user_corner(ca, db.get_community(cid),
                                    current_app=None)
    assert "flex-direction:row-reverse" not in corner


def test_corner_menu_shows_all_apps_for_ca(ddb_table) -> None:
    """For a CA, the upper-right corner lists every app in the
    community as a clickable pivot — even apps they have no explicit
    Membership row for."""
    from community_organizer.lambdas import web

    community, ca, _, app_a, app_b = _seed_two_apps(ddb_table)
    corner = web._build_user_corner(ca, community, current_app=app_a)
    assert "Ushers" in corner
    assert "Adoration" in corner
    assert f"/?app_id={app_a.app_id}" in corner
    assert f"/?app_id={app_b.app_id}" in corner
    assert "Community admin" in corner             # CA shortcut


def test_corner_menu_collapses_when_tall(ddb_table) -> None:
    """A CA with many apps gets a tap-to-expand chip: the name shows in
    a clickable summary, the full app list + community-admin link are
    tucked in a hidden .sc-more block, and Sign out stays visible."""
    from community_organizer.core import db
    from community_organizer.core.models import Application, Community
    from community_organizer.lambdas import web

    cid = "c-collapse"
    db.put_community(Community(community_id=cid, name="Test"))
    apps = []
    for i in range(6):
        a = Application(community_id=cid, name=f"A{i+1:02d}",
                        app_type="coverage", period_type="monthly")
        db.put_application(a)
        apps.append(a)
    ca = User(community_id=cid, email="ca@example.com", name="CA",
              community_role="ca")
    db.put_user(ca)
    corner = web._build_user_corner(ca, db.get_community(cid),
                                    current_app=apps[0])
    # Collapse scaffolding present.
    assert "scCornerToggle(this)" in corner
    assert "class='sc-more'" in corner
    assert "display:none" in corner
    assert "sc-caret" in corner
    # The detail (community-admin link) lives inside the hidden block, not
    # the always-visible summary. Split on the hidden-block marker and
    # confirm the link is on the hidden side.
    summary, _, hidden = corner.partition("class='sc-more'")
    assert "Community admin" in hidden
    assert "Community admin" not in summary
    # Sign out remains in the always-visible tail (after the hidden block).
    assert corner.count("Sign out") == 1
    assert corner.rindex("Sign out") > corner.index("class='sc-more'")
    # Current app named in the summary for context.
    assert "A01" in summary


def test_corner_menu_stays_flat_when_short(ddb_table) -> None:
    """A single-app member (name + 1 app + sign out = 3 rows) is under
    the collapse threshold, so no expander scaffolding is emitted."""
    from community_organizer.core import db
    from community_organizer.core.models import Application, Community, Membership
    from community_organizer.lambdas import web

    cid = "c-flat"
    db.put_community(Community(community_id=cid, name="Test"))
    app = Application(community_id=cid, name="Only", app_type="coverage",
                      period_type="monthly")
    db.put_application(app)
    mem = User(community_id=cid, email="m@example.com", name="Member")
    db.put_user(mem)
    db.put_membership(Membership(community_id=cid, app_id=app.app_id,
                                 user_id=mem.user_id))
    corner = web._build_user_corner(mem, db.get_community(cid),
                                    current_app=app)
    assert "scCornerToggle" not in corner
    assert "sc-more" not in corner
    assert "Sign out" in corner and "Only" in corner


def test_corner_menu_hides_other_apps_for_plain_member(ddb_table) -> None:
    """A plain member sees only apps they belong to — not every app
    in the community."""
    from community_organizer.core import db
    from community_organizer.core.models import Membership
    from community_organizer.lambdas import web

    community, _, mem_user, app_a, app_b = _seed_two_apps(ddb_table)
    # Join only app_a.
    db.put_membership(Membership(community_id=community.community_id,
                                 app_id=app_a.app_id,
                                 user_id=mem_user.user_id))
    corner = web._build_user_corner(mem_user, community, current_app=app_a)
    assert "Ushers" in corner
    assert "Adoration" not in corner
    # No "Community admin" link for plain members.
    assert "Community admin" not in corner


def test_ca_users_page_lists_every_user(ddb_table) -> None:
    """The community users page shows all users in the community,
    independent of app membership."""
    from community_organizer.lambdas import web

    community, ca, mem_user, _, _ = _seed_two_apps(ddb_table)
    resp = web._ca_users_page({}, ca, community)
    assert resp["statusCode"] == 200
    body = resp["body"]
    assert "CA" in body          # ca's display name
    assert "Member" in body      # mem's display name
    assert "/admin/community-users?edit=" in body   # edit affordance


def test_api_ca_user_add_creates_user_without_membership(
        ddb_table, monkeypatch) -> None:
    """The CA-level Add User form creates a User in the community
    but does NOT auto-join them to any app — app membership is
    granted from the per-app /admin/users page."""
    from community_organizer.core import db
    from community_organizer.lambdas import web

    community, ca, _, _, _ = _seed_two_apps(ddb_table)
    # Skip Cognito provisioning in the test.
    monkeypatch.setattr(web, "_create_cognito_user", lambda *a, **k: None)

    before = {u.user_id for u in db.list_users(community.community_id)}
    event = {
        "rawPath": "/api/community-users/add",
        "requestContext": {"http": {"method": "POST"}},
        "queryStringParameters": {
            "name": "Newbie", "email": "new@example.com",
        },
    }
    resp = web._api_ca_user_add(event, ca, community)
    assert resp["statusCode"] == 302
    after = {u.user_id for u in db.list_users(community.community_id)}
    new_ids = after - before
    assert len(new_ids) == 1
    new_id = next(iter(new_ids))
    # No membership rows for this user in any app.
    assert list(db.list_memberships_for_user(new_id)) == []


def test_api_ca_user_add_refuses_duplicate_email(
        ddb_table, monkeypatch) -> None:
    """the CA add-user form must refuse an email
    that's already in the community. Pre-fix, a CA could create a
    second User row pointing at the same Cognito sub — an orphan
    that caused duplicate-detection bugs downstream."""
    from community_organizer.core import db
    from community_organizer.lambdas import web

    community, ca, _, _, _ = _seed_two_apps(ddb_table)
    monkeypatch.setattr(web, "_create_cognito_user", lambda *a, **k: None)

    # Seed an existing community user with the target email.
    db.put_user(User(community_id=community.community_id,
                     email="existing@example.com", name="Existing"))
    before = len(list(db.list_users(community.community_id)))
    event = {
        "rawPath": "/api/community-users/add",
        "requestContext": {"http": {"method": "POST"}},
        "queryStringParameters": {
            "name": "Different Name",
            "email": "existing@example.com",
        },
    }
    resp = web._api_ca_user_add(event, ca, community)
    assert resp["statusCode"] == 302
    assert "error=" in resp["headers"]["Location"]
    # No new User row created.
    assert len(list(db.list_users(community.community_id))) == before


def test_route_redirects_multi_app_user_to_launcher(
        ddb_table, monkeypatch) -> None:
    """User who belongs to 2+ apps, with no ?app_id and no
    active-app cookie, lands on /launcher to choose."""
    from community_organizer import auth as _auth
    from community_organizer.core import db
    from community_organizer.core.models import Application, Community, Membership
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Test"))
    app_a = Application(community_id=cid, name="Ushers",
                        app_type="coverage", period_type="monthly")
    db.put_application(app_a)
    app_b = Application(community_id=cid, name="Adoration",
                        app_type="recurring_commitments",
                        period_type="weekly")
    db.put_application(app_b)
    u = User(community_id=cid, email="m@example.com", name="Multi",
             cognito_sub="MULTI-SUB")
    db.put_user(u)
    db.put_membership(Membership(community_id=cid, app_id=app_a.app_id,
                                 user_id=u.user_id))
    db.put_membership(Membership(community_id=cid, app_id=app_b.app_id,
                                 user_id=u.user_id))

    monkeypatch.setenv("COMMUNITY_ID", cid)
    monkeypatch.setattr(_auth, "verify_id_token",
                        lambda _t: {"sub": "MULTI-SUB"})
    event = {
        "rawPath": "/", "rawQueryString": "",
        "cookies": [f"{_auth.ID_COOKIE}=stub"],
    }
    resp = web._route(event, lambda *a, **k: web._text(200, "ok"))
    assert resp["statusCode"] == 302
    assert resp["headers"]["Location"] == "/launcher"


def test_route_single_app_user_skips_launcher(
        ddb_table, monkeypatch) -> None:
    """A user who only belongs to one app flows straight to that app
    — no launcher detour."""
    from community_organizer import auth as _auth
    from community_organizer.core import db
    from community_organizer.core.models import Application, Community, Membership
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Test"))
    app_a = Application(community_id=cid, name="Ushers",
                        app_type="coverage", period_type="monthly")
    db.put_application(app_a)
    # app_b exists in the community but the user is NOT a member.
    app_b = Application(community_id=cid, name="Adoration",
                        app_type="recurring_commitments",
                        period_type="weekly")
    db.put_application(app_b)
    u = User(community_id=cid, email="m@example.com", name="Member",
             cognito_sub="SINGLE-SUB")
    db.put_user(u)
    db.put_membership(Membership(community_id=cid, app_id=app_a.app_id,
                                 user_id=u.user_id))

    monkeypatch.setenv("COMMUNITY_ID", cid)
    monkeypatch.setattr(_auth, "verify_id_token",
                        lambda _t: {"sub": "SINGLE-SUB"})

    captured = {}

    def _handler(event, user, community, app, membership):
        captured["app_id"] = app.app_id
        return web._text(200, "ok")

    event = {
        "rawPath": "/", "rawQueryString": "",
        "cookies": [f"{_auth.ID_COOKIE}=stub"],
    }
    resp = web._route(event, _handler)
    # No launcher redirect — fell through to app_a.
    assert resp["statusCode"] == 200
    assert captured["app_id"] == app_a.app_id


def test_launcher_lists_member_apps_with_description(ddb_table) -> None:
    """The /launcher renders a tile per visible app with name + admin-
    supplied description and the user's role label."""
    from community_organizer.core import db
    from community_organizer.core.models import Application, Community, Membership
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Test"))
    app_a = Application(community_id=cid, name="Ushers",
                        app_type="coverage", period_type="monthly",
                        description="Sunday Mass usher rotation.")
    db.put_application(app_a)
    app_b = Application(community_id=cid, name="Adoration",
                        app_type="recurring_commitments",
                        period_type="weekly",
                        description="Eucharistic adoration weekly cohort.")
    db.put_application(app_b)
    u = User(community_id=cid, email="m@example.com", name="Mary")
    db.put_user(u)
    db.put_membership(Membership(community_id=cid, app_id=app_a.app_id,
                                 user_id=u.user_id))
    db.put_membership(Membership(community_id=cid, app_id=app_b.app_id,
                                 user_id=u.user_id))

    resp = web._launcher_page({}, u, db.get_community(cid))
    assert resp["statusCode"] == 200
    body = resp["body"]
    assert "Ushers" in body and "Adoration" in body
    assert "Sunday Mass usher rotation." in body
    assert "Eucharistic adoration weekly cohort." in body
    # Each tile is a link with the right pivot URL.
    assert f"/?app_id={app_a.app_id}" in body
    assert f"/?app_id={app_b.app_id}" in body


def test_launcher_for_ca_includes_community_admin_section(ddb_table) -> None:
    """A CA viewing the launcher gets a Community admin tile."""
    from community_organizer.core import db
    from community_organizer.core.models import Application, Community
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Test"))
    Application_x = Application(community_id=cid, name="Ushers",
                                app_type="coverage", period_type="monthly")
    db.put_application(Application_x)
    ca = User(community_id=cid, email="ca@example.com", name="CA",
              community_role="ca")
    db.put_user(ca)
    resp = web._launcher_page({}, ca, db.get_community(cid))
    body = resp["body"]
    assert "Community admin" in body
    assert "/admin/apps" in body


def test_admin_nav_renames_home_to_app_home() -> None:
    """Multi-app users need the distinction from the launcher."""
    from community_organizer.core.models import Application
    from community_organizer.lambdas import web

    app = Application(community_id="c1", name="X",
                      app_type="coverage", period_type="monthly")
    nav = web._admin_nav_bar("home", app=app)
    assert "App home" in nav
    # The literal "Home" without "App" prefix must not appear as a
    # standalone tab item — the only matches are within "App home".
    standalone_home_count = nav.count(">Home<")
    assert standalone_home_count == 0


def test_route_honors_active_app_cookie_when_no_query_param(
        ddb_table, monkeypatch) -> None:
    """When ?app_id is absent but the active-app cookie holds an
    app_id, _route picks that app instead of falling back to the
    "first by created_at" app. This is the fix for the bug where
    CA → Ushers home → Manage cohorts ended up on Adoration's
    cohort page."""
    from community_organizer import auth as _auth
    from community_organizer.core import db
    from community_organizer.core.models import Application, Community
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Test"))
    # app_a goes in first → would be the "first app" fallback.
    app_a = Application(community_id=cid, name="Adoration",
                        app_type="recurring_commitments",
                        period_type="weekly")
    db.put_application(app_a)
    app_b = Application(community_id=cid, name="Ushers",
                        app_type="coverage", period_type="monthly")
    db.put_application(app_b)
    ca = User(community_id=cid, email="ca@example.com", name="CA",
              community_role="ca", cognito_sub="CA-SUB")
    db.put_user(ca)

    monkeypatch.setenv("COMMUNITY_ID", cid)
    monkeypatch.setattr(_auth, "verify_id_token",
                        lambda _t: {"sub": "CA-SUB"})

    captured = {}

    def _handler(event, user, community, app, membership):
        captured["app_id"] = app.app_id
        return web._text(200, "ok")

    # Cookie stamps app_b (Ushers) even though app_a (Adoration) is
    # the first app in the community. _route should pick app_b.
    event = {
        "rawPath": "/admin/cohorts",
        "rawQueryString": "",
        "cookies": [
            f"{_auth.ID_COOKIE}=stub",
            f"{web.ACTIVE_APP_COOKIE}={app_b.app_id}",
        ],
    }
    web._route(event, _handler)
    assert captured["app_id"] == app_b.app_id, (
        "active-app cookie should override the first-app fallback")


def test_route_query_param_beats_cookie(ddb_table, monkeypatch) -> None:
    """Explicit ?app_id=... in the URL wins over the cookie. Lets
    deep-links / corner-menu pivots override the persisted choice."""
    from community_organizer import auth as _auth
    from community_organizer.core import db
    from community_organizer.core.models import Application, Community
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Test"))
    app_a = Application(community_id=cid, name="Adoration",
                        app_type="recurring_commitments",
                        period_type="weekly")
    db.put_application(app_a)
    app_b = Application(community_id=cid, name="Ushers",
                        app_type="coverage", period_type="monthly")
    db.put_application(app_b)
    ca = User(community_id=cid, email="ca@example.com", name="CA",
              community_role="ca", cognito_sub="CA-SUB")
    db.put_user(ca)
    monkeypatch.setenv("COMMUNITY_ID", cid)
    monkeypatch.setattr(_auth, "verify_id_token",
                        lambda _t: {"sub": "CA-SUB"})

    captured = {}

    def _handler(event, user, community, app, membership):
        captured["app_id"] = app.app_id
        return web._text(200, "ok")

    event = {
        "rawPath": "/",
        "rawQueryString": f"app_id={app_a.app_id}",
        "queryStringParameters": {"app_id": app_a.app_id},
        "cookies": [
            f"{_auth.ID_COOKIE}=stub",
            f"{web.ACTIVE_APP_COOKIE}={app_b.app_id}",
        ],
    }
    resp = web._route(event, _handler)
    assert captured["app_id"] == app_a.app_id
    # And the explicit pivot refreshes the cookie to app_a.
    cookie_strs = resp.get("cookies", [])
    assert any(f"{web.ACTIVE_APP_COOKIE}={app_a.app_id}" in c
               for c in cookie_strs)


def test_new_template_auto_reminders_default_per_app_type(ddb_table) -> None:
    """Recurring apps force auto_reminders=False so the notifier
    doesn't spam cohort members alongside the RRULE invite. Coverage
    apps keep auto_reminders=True (the existing Ushers behavior).
    Verifies the shared _create_template_with_cohort path applies
    the right default for the app."""
    from community_organizer.core import db
    from community_organizer.core.models import Application, Community
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Test"))
    recurring = Application(community_id=cid, name="Adoration",
                            app_type="recurring_commitments",
                            period_type="weekly",
                            default_timezone="America/New_York")
    db.put_application(recurring)
    coverage = Application(community_id=cid, name="Ushers",
                           app_type="coverage", period_type="monthly",
                           default_timezone="America/New_York")
    db.put_application(coverage)

    r_tpl, _ = web._create_template_with_cohort(
        community_id=cid, app=recurring, name="Wed 2 PM",
        day_of_week=2, start_time="14:00", duration_minutes=60,
        arrival_offset_minutes=0, required_volunteers=1,
        min_volunteers=1, max_volunteers=None,
    )
    c_tpl, _ = web._create_template_with_cohort(
        community_id=cid, app=coverage, name="Sun 8 AM",
        day_of_week=6, start_time="08:00", duration_minutes=60,
        arrival_offset_minutes=10, required_volunteers=2,
        min_volunteers=1, max_volunteers=5,
    )
    assert r_tpl.auto_reminders is False
    assert c_tpl.auto_reminders is True


def test_template_delete_cascades_in_recurring_app(ddb_table, monkeypatch) -> None:
    """Deleting a template in a recurring app cancels every cohort
    member's recurring invite, drops cohort memberships, deletes the
    cohort, and removes future Slots + Assignments tied to it."""
    from community_organizer.core import db
    from community_organizer.core.models import (
        Application, Assignment, Cohort, CohortMembership, Community,
        Membership, Schedule, Slot, SlotTemplate,
    )
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Test"))
    app = Application(community_id=cid, name="Adoration",
                      app_type="recurring_commitments",
                      period_type="weekly",
                      default_timezone="America/New_York")
    db.put_application(app)
    tpl = SlotTemplate(community_id=cid, app_id=app.app_id,
                       name="Wed 2 PM", day_of_week=2,
                       start_time="14:00", duration_minutes=60,
                       required_volunteers=1, min_volunteers=1)
    db.put_template(tpl)
    cohort = Cohort(community_id=cid, app_id=app.app_id,
                    name="Wed 2 PM regulars",
                    linked_template_id=tpl.template_id)
    db.put_cohort(cohort)
    mary = User(community_id=cid, email="m@example.com", name="Mary")
    db.put_user(mary)
    db.put_cohort_membership(CohortMembership(
        cohort_id=cohort.cohort_id, user_id=mary.user_id))
    # Future materialized period with the template's slot.
    period_id = "2099-W22"
    slot = Slot(community_id=cid, app_id=app.app_id,
                yyyy_mm=period_id, template_id=tpl.template_id,
                name="Wed 2 PM", day_of_week=2, start_time="14:00",
                arrival_offset_minutes=0, duration_minutes=60,
                required_volunteers=1, min_volunteers=1,
                concrete_date="2099-05-27T14:00",
                local_date="2099-05-27")
    db.put_slot(slot)
    db.put_assignment(Assignment(
        community_id=cid, app_id=app.app_id, yyyy_mm=period_id,
        slot_id=slot.slot_id, user_id=mary.user_id,
        local_date=slot.local_date))
    db.put_schedule(Schedule(community_id=cid, app_id=app.app_id,
                             yyyy_mm=period_id, state="materialized"))
    # Admin (CA) actor.
    ca = User(community_id=cid, email="ca@example.com", name="CA",
              community_role="ca")
    db.put_user(ca)
    db.put_membership(Membership(community_id=cid, app_id=app.app_id,
                                 user_id=ca.user_id, app_role="aa"))

    sent = []
    monkeypatch.setattr(
        "community_organizer.providers.email.get_email_provider",
        lambda: type("S", (), {
            "send": lambda self, **kw: sent.append(kw),
        })(),
    )

    event = {
        "rawPath": "/api/templates/delete",
        "requestContext": {"http": {"method": "POST"}},
        "queryStringParameters": {"template_id": tpl.template_id},
    }
    mem = db.get_membership(app.app_id, ca.user_id)
    web._api_template_delete(event, ca, db.get_community(cid), app, mem)

    # Template + cohort + cohort membership all gone.
    assert db.get_template(app.app_id, tpl.template_id) is None
    assert db.get_cohort(app.app_id, cohort.cohort_id) is None
    assert list(db.list_cohort_members(cohort.cohort_id)) == []
    # Future slot + assignment gone.
    assert list(db.list_slots(app.app_id, period_id)) == []
    assert list(db.list_assignments_for_month(app.app_id, period_id)) == []
    # CANCEL email went to Mary.
    assert len(sent) == 1
    assert sent[0]["to_addr"] == mary.email
    assert "METHOD:CANCEL" in sent[0]["ics_content"]


def test_new_template_backfills_into_materialized_periods(ddb_table) -> None:
    """Admin adds a new template AFTER some weeks were materialized.
    The new template's slots should appear in those existing
    materialized periods (recurring apps only). Past periods are
    left alone."""
    from community_organizer.core import db
    from community_organizer.core.models import (
        Application, Community, Schedule, SlotTemplate,
    )
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Test"))
    app = Application(community_id=cid, name="Adoration",
                      app_type="recurring_commitments",
                      period_type="weekly",
                      default_timezone="America/New_York")
    db.put_application(app)
    # A future period that's been materialized (no slots yet).
    db.put_schedule(Schedule(community_id=cid, app_id=app.app_id,
                             yyyy_mm="2099-W22", state="materialized"))

    # Add a new template via the shared helper (mirrors single-Add).
    tpl, _cohort = web._create_template_with_cohort(
        community_id=cid, app=app, name="Wed 2 PM",
        day_of_week=2, start_time="14:00", duration_minutes=60,
        arrival_offset_minutes=0, required_volunteers=1,
        min_volunteers=1, max_volunteers=None,
    )
    # Slot now exists in the future materialized period.
    slots = list(db.list_slots(app.app_id, "2099-W22"))
    assert len(slots) == 1
    assert slots[0].template_id == tpl.template_id
    # Coverage apps don't back-fill.
    coverage = Application(community_id=cid, name="Ushers",
                           app_type="coverage", period_type="monthly",
                           default_timezone="America/New_York")
    db.put_application(coverage)
    db.put_schedule(Schedule(community_id=cid, app_id=coverage.app_id,
                             yyyy_mm="2099-05", state="draft"))
    web._create_template_with_cohort(
        community_id=cid, app=coverage, name="Sun 8 AM",
        day_of_week=6, start_time="08:00", duration_minutes=60,
        arrival_offset_minutes=10, required_volunteers=2,
        min_volunteers=1, max_volunteers=5,
    )
    assert list(db.list_slots(coverage.app_id, "2099-05")) == []


def _seed_three_adjacent_slots(ddb_table):
    """Seed a recurring app with three back-to-back 1-hour slots
    (1 PM Alice, 2 PM Mary, 3 PM Bob). Returns
    (community, app, alice, mary, bob, mid_slot)."""
    from community_organizer.core import db
    from community_organizer.core.models import (
        Application, Assignment, Community, Schedule, Slot, SlotTemplate,
    )

    cid = "c1"
    db.put_community(Community(community_id=cid, name="St. Test"))
    app = Application(community_id=cid, name="Adoration",
                      app_type="recurring_commitments",
                      period_type="weekly",
                      default_timezone="America/New_York")
    db.put_application(app)
    tpl_1 = SlotTemplate(community_id=cid, app_id=app.app_id,
                         name="Wed 1 PM", day_of_week=2,
                         start_time="13:00", duration_minutes=60,
                         required_volunteers=1, min_volunteers=1,
                         max_volunteers=None)
    tpl_2 = SlotTemplate(community_id=cid, app_id=app.app_id,
                         name="Wed 2 PM", day_of_week=2,
                         start_time="14:00", duration_minutes=60,
                         required_volunteers=1, min_volunteers=1,
                         max_volunteers=None)
    tpl_3 = SlotTemplate(community_id=cid, app_id=app.app_id,
                         name="Wed 3 PM", day_of_week=2,
                         start_time="15:00", duration_minutes=60,
                         required_volunteers=1, min_volunteers=1,
                         max_volunteers=None)
    for t in (tpl_1, tpl_2, tpl_3):
        db.put_template(t)

    alice = User(community_id=cid, email="a@example.com", name="Alice")
    mary = User(community_id=cid, email="m@example.com", name="Mary")
    bob = User(community_id=cid, email="b@example.com", name="Bob")
    for u in (alice, mary, bob):
        db.put_user(u)

    period_id = "2099-W22"
    date = "2099-05-27"
    db.put_schedule(Schedule(community_id=cid, app_id=app.app_id,
                             yyyy_mm=period_id, state="materialized"))
    slot_1 = Slot(community_id=cid, app_id=app.app_id,
                  yyyy_mm=period_id, template_id=tpl_1.template_id,
                  name="Wed 1 PM", day_of_week=2,
                  start_time="13:00", arrival_offset_minutes=0,
                  duration_minutes=60, required_volunteers=1,
                  min_volunteers=1, max_volunteers=None,
                  concrete_date=f"{date}T13:00", local_date=date)
    slot_2 = Slot(community_id=cid, app_id=app.app_id,
                  yyyy_mm=period_id, template_id=tpl_2.template_id,
                  name="Wed 2 PM", day_of_week=2,
                  start_time="14:00", arrival_offset_minutes=0,
                  duration_minutes=60, required_volunteers=1,
                  min_volunteers=1, max_volunteers=None,
                  concrete_date=f"{date}T14:00", local_date=date)
    slot_3 = Slot(community_id=cid, app_id=app.app_id,
                  yyyy_mm=period_id, template_id=tpl_3.template_id,
                  name="Wed 3 PM", day_of_week=2,
                  start_time="15:00", arrival_offset_minutes=0,
                  duration_minutes=60, required_volunteers=1,
                  min_volunteers=1, max_volunteers=None,
                  concrete_date=f"{date}T15:00", local_date=date)
    for s in (slot_1, slot_2, slot_3):
        db.put_slot(s)

    for u, s in [(alice, slot_1), (mary, slot_2), (bob, slot_3)]:
        db.put_assignment(Assignment(
            community_id=cid, app_id=app.app_id, yyyy_mm=period_id,
            slot_id=s.slot_id, user_id=u.user_id, local_date=date))
    return db.get_community(cid), app, alice, mary, bob, slot_2


def test_users_page_lists_only_app_members(ddb_table) -> None:
    """Per-app /admin/users must show only users who hold a Membership
    in the current app — not every community user (PRIVACY-AUDIT
    HIGH-1)."""
    from community_organizer.core import db
    from community_organizer.core.models import (
        Application, Community, Membership,
    )
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Test"))
    app_a = Application(community_id=cid, name="Ushers",
                       app_type="coverage", period_type="monthly")
    db.put_application(app_a)
    app_b = Application(community_id=cid, name="Adoration",
                        app_type="recurring_commitments",
                        period_type="weekly")
    db.put_application(app_b)

    aa = User(community_id=cid, email="aa@example.com", name="AA")
    db.put_user(aa)
    db.put_membership(Membership(community_id=cid, app_id=app_a.app_id,
                                 user_id=aa.user_id, app_role="aa"))
    in_a = User(community_id=cid, email="ia@example.com", name="In A")
    db.put_user(in_a)
    db.put_membership(Membership(community_id=cid, app_id=app_a.app_id,
                                 user_id=in_a.user_id, app_role="member"))
    only_b = User(community_id=cid, email="ib@example.com", name="Only In B")
    db.put_user(only_b)
    db.put_membership(Membership(community_id=cid, app_id=app_b.app_id,
                                 user_id=only_b.user_id, app_role="member"))

    aa_mem = db.get_membership(app_a.app_id, aa.user_id)
    resp = web._users_page({}, aa, db.get_community(cid), app_a, aa_mem)
    assert resp["statusCode"] == 200
    body = resp["body"]
    # AA in app A should see In A (and themselves), but NOT Only In B.
    assert "In A" in body
    assert "Only In B" not in body
    assert "ib@example.com" not in body


def test_api_user_add_allows_aa(ddb_table, monkeypatch) -> None:
    """AAs CAN add members to their app — they get full roster
    authority within their app. The new user lands as a community
    user with a Membership in THIS app only; AAs can't enumerate
    or add to other apps."""
    from community_organizer.core import db
    from community_organizer.core.models import (
        Application, Community, Membership,
    )
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Test"))
    app = Application(community_id=cid, name="Ushers",
                      app_type="coverage", period_type="monthly")
    db.put_application(app)
    aa = User(community_id=cid, email="aa@example.com", name="AA",
              community_role="member")
    db.put_user(aa)
    db.put_membership(Membership(community_id=cid, app_id=app.app_id,
                                 user_id=aa.user_id, app_role="aa"))
    aa_mem = db.get_membership(app.app_id, aa.user_id)
    monkeypatch.setattr(web, "_create_cognito_user",
                        lambda *a, **k: None)
    before = {u.user_id for u in db.list_users(cid)}
    event = {
        "rawPath": "/api/users/add",
        "requestContext": {"http": {"method": "POST"}},
        "queryStringParameters": {
            "name": "New Usher", "email": "new@example.com",
        },
    }
    resp = web._api_user_add(event, aa, db.get_community(cid), app, aa_mem)
    assert resp["statusCode"] == 302
    after = {u.user_id for u in db.list_users(cid)}
    new_ids = after - before
    assert len(new_ids) == 1
    new_uid = next(iter(new_ids))
    # Membership exists ONLY in this app.
    mems = list(db.list_memberships_for_user(new_uid))
    assert [m.app_id for m in mems] == [app.app_id]


def test_api_user_add_existing_community_email_no_duplicate(
        ddb_table, monkeypatch) -> None:
    """#172: when an AA adds an email that already belongs to a
    community user (e.g., that person is a member of another app),
    just grant the new Membership — don't create a duplicate User
    row or trip Cognito's UsernameExistsException."""
    from community_organizer.core import db
    from community_organizer.core.models import (
        Application, Community, Membership,
    )
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Test"))
    app_a = Application(community_id=cid, name="Ushers",
                        app_type="coverage", period_type="monthly")
    app_b = Application(community_id=cid, name="Adoration",
                        app_type="recurring_commitments",
                        period_type="weekly")
    db.put_application(app_a)
    db.put_application(app_b)
    aa = User(community_id=cid, email="aa@example.com", name="AA",
              community_role="member")
    db.put_user(aa)
    db.put_membership(Membership(community_id=cid, app_id=app_a.app_id,
                                 user_id=aa.user_id, app_role="aa"))
    aa_mem = db.get_membership(app_a.app_id, aa.user_id)

    # An existing community user, currently only in app_b.
    existing = User(community_id=cid, email="existing@example.com",
                    name="Existing User", community_role="member",
                    cognito_sub="EXIST-SUB")
    db.put_user(existing)
    db.put_membership(Membership(community_id=cid, app_id=app_b.app_id,
                                 user_id=existing.user_id, app_role="member"))

    # Cognito must NOT be called when we reuse an existing user — if
    # it is, the test mock raises so we catch the regression.
    def _fail(*a, **k):
        raise AssertionError("Cognito provisioning attempted for "
                              "existing user")
    monkeypatch.setattr(web, "_create_cognito_user", _fail)

    user_count_before = len(list(db.list_users(cid)))
    event = {
        "rawPath": "/api/users/add",
        "requestContext": {"http": {"method": "POST"}},
        "queryStringParameters": {
            "name": "Existing User",
            # Different casing — get_user_by_email is case-insensitive
            # so this must still match.
            "email": "EXISTING@example.com",
        },
    }
    resp = web._api_user_add(event, aa, db.get_community(cid),
                              app_a, aa_mem)
    assert resp["statusCode"] == 302
    assert "error=" not in resp["headers"]["Location"]
    # No new User row.
    user_count_after = len(list(db.list_users(cid)))
    assert user_count_after == user_count_before, (
        f"expected 0 new User rows, got "
        f"{user_count_after - user_count_before}")
    # The existing user now has a Membership in app_a too.
    mems = sorted(m.app_id for m in
                  db.list_memberships_for_user(existing.user_id))
    assert mems == sorted([app_a.app_id, app_b.app_id])


def test_api_user_add_existing_email_with_name_mismatch_notice(
        ddb_table, monkeypatch) -> None:
    """#198: when an AA adds an existing community email but types a
    DIFFERENT name (e.g. 'Joe B' for an existing 'Joe Bennett'),
    the membership is granted to the existing user — and a
    notice= banner surfaces on the redirect so the admin learns
    the name they typed was discarded."""
    import urllib.parse

    from community_organizer.core import db
    from community_organizer.core.models import (
        Application, Community, Membership,
    )
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Test"))
    app_a = Application(community_id=cid, name="Ushers",
                        app_type="coverage", period_type="monthly")
    db.put_application(app_a)
    aa = User(community_id=cid, email="aa@example.com", name="AA",
              community_role="member")
    db.put_user(aa)
    db.put_membership(Membership(community_id=cid, app_id=app_a.app_id,
                                 user_id=aa.user_id, app_role="aa"))
    aa_mem = db.get_membership(app_a.app_id, aa.user_id)

    existing = User(community_id=cid, email="joe@example.com",
                    name="Joe Bennett", community_role="member",
                    cognito_sub="EXIST-SUB")
    db.put_user(existing)
    monkeypatch.setattr(web, "_create_cognito_user",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("no Cognito call expected")))

    event = {
        "rawPath": "/api/users/add",
        "requestContext": {"http": {"method": "POST"}},
        "queryStringParameters": {
            "name": "Joe B",        # mismatched
            "email": "joe@example.com",
        },
    }
    resp = web._api_user_add(event, aa, db.get_community(cid),
                              app_a, aa_mem)
    assert resp["statusCode"] == 302
    location = resp["headers"]["Location"]
    assert "notice=" in location
    decoded = urllib.parse.unquote(
        location.split("notice=", 1)[1].split("&", 1)[0])
    assert "Joe Bennett" in decoded
    assert "Joe B" in decoded
    # Membership IS granted to the existing user.
    assert db.get_membership(app_a.app_id, existing.user_id) is not None


def test_api_user_add_existing_email_matching_name_no_notice(
        ddb_table, monkeypatch) -> None:
    """The notice fires ONLY on a name mismatch. Whitespace and case
    differences alone don't trigger it — those are common harmless
    re-typings the AA wouldn't want pestered about."""
    from community_organizer.core import db
    from community_organizer.core.models import (
        Application, Community, Membership,
    )
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Test"))
    app_a = Application(community_id=cid, name="Ushers",
                        app_type="coverage", period_type="monthly")
    db.put_application(app_a)
    aa = User(community_id=cid, email="aa@example.com", name="AA",
              community_role="member")
    db.put_user(aa)
    db.put_membership(Membership(community_id=cid, app_id=app_a.app_id,
                                 user_id=aa.user_id, app_role="aa"))
    aa_mem = db.get_membership(app_a.app_id, aa.user_id)

    existing = User(community_id=cid, email="jane@example.com",
                    name="Jane Doe", community_role="member",
                    cognito_sub="EXIST-SUB")
    db.put_user(existing)
    monkeypatch.setattr(web, "_create_cognito_user",
                        lambda *a, **k: None)

    event = {
        "rawPath": "/api/users/add",
        "requestContext": {"http": {"method": "POST"}},
        "queryStringParameters": {
            # Whitespace + case differs, but normalizes to the same.
            "name": "  jane   doe  ",
            "email": "jane@example.com",
        },
    }
    resp = web._api_user_add(event, aa, db.get_community(cid),
                              app_a, aa_mem)
    assert resp["statusCode"] == 302
    assert "notice=" not in resp["headers"]["Location"]


def test_api_user_add_already_in_this_app_returns_error(
        ddb_table, monkeypatch) -> None:
    """#172: re-adding a user who's already a member of THIS app
    surfaces a styled "already a member" error banner via the
    /admin/users redirect — no Cognito call, no extra Membership."""
    from community_organizer.core import db
    from community_organizer.core.models import (
        Application, Community, Membership,
    )
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Test"))
    app = Application(community_id=cid, name="Ushers",
                      app_type="coverage", period_type="monthly")
    db.put_application(app)
    aa = User(community_id=cid, email="aa@example.com", name="AA",
              community_role="member")
    db.put_user(aa)
    db.put_membership(Membership(community_id=cid, app_id=app.app_id,
                                 user_id=aa.user_id, app_role="aa"))
    aa_mem = db.get_membership(app.app_id, aa.user_id)

    # The existing member of THIS app.
    member = User(community_id=cid, email="member@example.com",
                  name="Already Here", community_role="member")
    db.put_user(member)
    db.put_membership(Membership(community_id=cid, app_id=app.app_id,
                                 user_id=member.user_id, app_role="member"))

    def _fail(*a, **k):
        raise AssertionError("Cognito provisioning attempted for "
                              "already-in-app user")
    monkeypatch.setattr(web, "_create_cognito_user", _fail)

    event = {
        "rawPath": "/api/users/add",
        "requestContext": {"http": {"method": "POST"}},
        "queryStringParameters": {
            "name": "Already Here", "email": "member@example.com",
        },
    }
    resp = web._api_user_add(event, aa, db.get_community(cid),
                              app, aa_mem)
    assert resp["statusCode"] == 302
    loc = resp["headers"]["Location"]
    assert "error=" in loc
    assert "already%20a%20member" in loc
    # No second Membership row appended.
    mems = list(db.list_memberships_for_user(member.user_id))
    assert len(mems) == 1


def test_api_user_edit_aa_blocked_for_other_app_user(ddb_table) -> None:
    """AA of app A cannot edit a user who is only in app B
    (PRIVACY-AUDIT HIGH-2)."""
    from community_organizer.core import db
    from community_organizer.core.models import (
        Application, Community, Membership,
    )
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Test"))
    app_a = Application(community_id=cid, name="Ushers",
                       app_type="coverage", period_type="monthly")
    db.put_application(app_a)
    app_b = Application(community_id=cid, name="Adoration",
                        app_type="recurring_commitments",
                        period_type="weekly")
    db.put_application(app_b)
    aa = User(community_id=cid, email="aa@example.com", name="AA")
    db.put_user(aa)
    db.put_membership(Membership(community_id=cid, app_id=app_a.app_id,
                                 user_id=aa.user_id, app_role="aa"))
    other = User(community_id=cid, email="other@example.com", name="Other")
    db.put_user(other)
    db.put_membership(Membership(community_id=cid, app_id=app_b.app_id,
                                 user_id=other.user_id, app_role="member"))
    aa_mem = db.get_membership(app_a.app_id, aa.user_id)
    event = {
        "rawPath": "/api/users/edit",
        "requestContext": {"http": {"method": "POST"}},
        "queryStringParameters": {
            "user_id": other.user_id, "name": "Hacked Name",
        },
    }
    resp = web._api_user_edit(event, aa, db.get_community(cid),
                              app_a, aa_mem)
    # Now redirects with a styled error banner.
    assert resp["statusCode"] == 302
    assert "error=" in resp["headers"]["Location"]
    # Name unchanged in DDB.
    assert db.get_user(cid, other.user_id).name == "Other"


def test_api_user_edit_ca_plain_member_of_active_app(ddb_table) -> None:
    """A community CA who is only a plain MEMBER (app_role != 'aa') of the
    app currently in context can still edit users via _api_user_edit.

    Regression: the CA community-users page POSTs to /api/users/edit, which
    routes app-scoped. When the CA's active app is a flexible_event book club
    they merely belong to, _is_admin() (app_role-only) returned False and the
    front gate 403'd before the CA-aware scoping below — so the save silently
    no-op'd (the live book-club launch bug, 2026-06-16)."""
    from community_organizer.core import db
    from community_organizer.core.models import Application, Community, Membership
    from community_organizer.lambdas import web

    cid = "example-community"
    db.put_community(Community(community_id=cid, name="Parish"))
    book = Application(community_id=cid, name="Summer Couples Book Club",
                       app_type="flexible_event", app_id="bookclub")
    db.put_application(book)
    ca = User(community_id=cid, email="admin@example.com", name="Casey Admin",
              community_role="ca")
    db.put_user(ca)
    # CA is only a plain member of the flexible app — NOT an AA.
    db.put_membership(Membership(community_id=cid, app_id="bookclub",
                                 user_id=ca.user_id, app_role="member"))
    andrew = User(community_id=cid, email="ada@example.com", name="Ada Member")
    db.put_user(andrew)
    db.put_membership(Membership(community_id=cid, app_id="bookclub",
                                 user_id=andrew.user_id, app_role="member"))
    ca_mem = db.get_membership("bookclub", ca.user_id)
    event = {
        "rawPath": "/api/users/edit",
        "requestContext": {"http": {"method": "POST"}},
        "queryStringParameters": {
            "user_id": andrew.user_id, "version": "0",
            "name": "Ada Member", "email": "ada@example.com",
        },
    }
    resp = web._api_user_edit(event, ca, db.get_community(cid), book, ca_mem)
    assert resp["statusCode"] in (200, 302)
    fresh = db.get_user(cid, andrew.user_id)
    assert fresh.email == "ada@example.com"   # the edit actually persisted
    assert fresh.version == 1


def test_api_admin_assign_rejects_non_app_member(ddb_table) -> None:
    """Admin cannot assign a user who is not a member of this app
    (PRIVACY-AUDIT MED-3)."""
    from community_organizer.core import db
    from community_organizer.core.models import (
        Application, Community, Membership, Schedule, Slot, SlotTemplate,
    )
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Test"))
    app_a = Application(community_id=cid, name="Ushers",
                       app_type="coverage", period_type="monthly")
    db.put_application(app_a)
    app_b = Application(community_id=cid, name="Adoration",
                        app_type="recurring_commitments",
                        period_type="weekly")
    db.put_application(app_b)
    aa = User(community_id=cid, email="aa@example.com", name="AA")
    db.put_user(aa)
    db.put_membership(Membership(community_id=cid, app_id=app_a.app_id,
                                 user_id=aa.user_id, app_role="aa"))
    other = User(community_id=cid, email="o@example.com", name="Other")
    db.put_user(other)
    db.put_membership(Membership(community_id=cid, app_id=app_b.app_id,
                                 user_id=other.user_id))
    tpl = SlotTemplate(community_id=cid, app_id=app_a.app_id,
                       name="Sun 8 AM", day_of_week=6,
                       start_time="08:00", duration_minutes=60,
                       required_volunteers=2, min_volunteers=1)
    db.put_template(tpl)
    db.put_schedule(Schedule(community_id=cid, app_id=app_a.app_id,
                             yyyy_mm="2099-05", state="draft"))
    slot = Slot(community_id=cid, app_id=app_a.app_id, yyyy_mm="2099-05",
                template_id=tpl.template_id, name="Sun 8 AM",
                day_of_week=6, start_time="08:00",
                arrival_offset_minutes=10, duration_minutes=60,
                required_volunteers=2, min_volunteers=1,
                concrete_date="2099-05-03T08:00", local_date="2099-05-03")
    db.put_slot(slot)
    aa_mem = db.get_membership(app_a.app_id, aa.user_id)
    event = {
        "rawPath": "/api/admin/assign",
        "requestContext": {"http": {"method": "POST"}},
        "queryStringParameters": {
            "month": "2099-05", "slot_id": slot.slot_id,
            "user_id": other.user_id,
        },
    }
    resp = web._api_admin_assign(event, aa, db.get_community(cid),
                                 app_a, aa_mem)
    # Now redirects with a styled error banner.
    assert resp["statusCode"] == 302
    assert "error=" in resp["headers"]["Location"]
    # No Assignment row created.
    assert not list(db.list_assignments_for_slot(
        app_a.app_id, "2099-05", slot.slot_id))


def test_recurring_home_personalizes_to_my_and_adjacent_slots(ddb_table) -> None:
    """The grid renders only my slots + chronologically adjacent slots
    per day. Slots that are covered by someone else and aren't
    adjacent collapse into a "+ N more" expandable section."""
    import datetime as dt
    from community_organizer.core import db
    from community_organizer.core.models import (
        Application, Assignment, Community, Membership, Schedule,
        Slot, SlotTemplate,
    )
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Test"))
    app = Application(community_id=cid, name="Adoration",
                      app_type="recurring_commitments",
                      period_type="weekly",
                      default_timezone="America/New_York")
    db.put_application(app)
    mary = User(community_id=cid, email="m@example.com", name="Mary")
    db.put_user(mary)
    db.put_membership(Membership(community_id=cid, app_id=app.app_id,
                                 user_id=mary.user_id))
    alice = User(community_id=cid, email="a@example.com", name="Alice")
    db.put_user(alice)
    bob = User(community_id=cid, email="b@example.com", name="Bob")
    db.put_user(bob)
    eve = User(community_id=cid, email="e@example.com", name="Eve")
    db.put_user(eve)

    # Find next Wednesday and build 4 hourly slots: 1, 2, 3, 4 PM.
    today = dt.date.today()
    next_wed = today + dt.timedelta(days=((2 - today.weekday()) % 7 or 7))
    iy, iw, _ = next_wed.isocalendar()
    pid = f"{iy:04d}-W{iw:02d}"
    db.put_schedule(Schedule(community_id=cid, app_id=app.app_id,
                             yyyy_mm=pid, state="materialized"))

    def _mk(hour: int, tid: str):
        # concrete_date is UTC ISO. America/New_York in summer is -4,
        # so 14:00 local → 18:00 UTC; close enough for adjacency math.
        utc_start = dt.datetime(next_wed.year, next_wed.month, next_wed.day,
                                hour + 4, 0, tzinfo=dt.timezone.utc)
        return Slot(community_id=cid, app_id=app.app_id, yyyy_mm=pid,
                    template_id=tid, name=f"Wed {hour} PM",
                    day_of_week=2, start_time=f"{hour:02d}:00",
                    arrival_offset_minutes=0, duration_minutes=60,
                    required_volunteers=1, min_volunteers=1,
                    max_volunteers=None,
                    concrete_date=utc_start.isoformat(),
                    local_date=next_wed.isoformat())

    slot_1 = _mk(13, "t1")     # 1 PM
    slot_2 = _mk(14, "t2")     # 2 PM (Mary's)
    slot_3 = _mk(15, "t3")     # 3 PM
    slot_4 = _mk(16, "t4")     # 4 PM (covered by Eve, NOT adjacent to Mary)
    for s in (slot_1, slot_2, slot_3, slot_4):
        db.put_slot(s)
    db.put_assignment(Assignment(
        community_id=cid, app_id=app.app_id, yyyy_mm=pid,
        slot_id=slot_1.slot_id, user_id=alice.user_id,
        local_date=slot_1.local_date))
    db.put_assignment(Assignment(
        community_id=cid, app_id=app.app_id, yyyy_mm=pid,
        slot_id=slot_2.slot_id, user_id=mary.user_id,
        local_date=slot_2.local_date))
    db.put_assignment(Assignment(
        community_id=cid, app_id=app.app_id, yyyy_mm=pid,
        slot_id=slot_3.slot_id, user_id=bob.user_id,
        local_date=slot_3.local_date))
    db.put_assignment(Assignment(
        community_id=cid, app_id=app.app_id, yyyy_mm=pid,
        slot_id=slot_4.slot_id, user_id=eve.user_id,
        local_date=slot_4.local_date))

    resp = web._home({}, mary, db.get_community(cid), app, None)
    body = resp["body"]
    # Alice's name is in the visible row; Mary's, Bob's too.
    assert "Alice" in body and "Bob" in body and "Mary" in body
    # Eve's covered slot (not adjacent) is HIDDEN in the collapsed
    # section; the wrap is a <details> block with the count.
    assert "+ 1 more slot" in body
    # Eve's name shows up inside the <details>, not as a top-level row.
    # Both visible and hidden tables include Eve in the rendered HTML;
    # the test verifies the count signal.


def test_recurring_home_member_with_no_assignments_sees_empty_visible(ddb_table) -> None:
    """A member who isn't assigned anywhere sees a near-empty page —
    just date headers + collapsed "+ N more" chevrons. Discovery
    happens via welcome emails / cohort opt-in, not browsing."""
    import datetime as dt
    from community_organizer.core import db
    from community_organizer.core.models import (
        Application, Assignment, Community, Membership, Schedule,
        Slot, SlotTemplate,
    )
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Test"))
    app = Application(community_id=cid, name="Adoration",
                      app_type="recurring_commitments",
                      period_type="weekly",
                      default_timezone="America/New_York")
    db.put_application(app)
    newbie = User(community_id=cid, email="n@example.com", name="Newbie")
    db.put_user(newbie)
    db.put_membership(Membership(community_id=cid, app_id=app.app_id,
                                 user_id=newbie.user_id))
    alice = User(community_id=cid, email="a@example.com", name="Alice")
    db.put_user(alice)

    today = dt.date.today()
    next_wed = today + dt.timedelta(days=((2 - today.weekday()) % 7 or 7))
    iy, iw, _ = next_wed.isocalendar()
    pid = f"{iy:04d}-W{iw:02d}"
    db.put_schedule(Schedule(community_id=cid, app_id=app.app_id,
                             yyyy_mm=pid, state="materialized"))
    s = Slot(community_id=cid, app_id=app.app_id, yyyy_mm=pid,
             template_id="t1", name="Wed 2 PM", day_of_week=2,
             start_time="14:00", arrival_offset_minutes=0,
             duration_minutes=60, required_volunteers=1,
             min_volunteers=1, max_volunteers=None,
             concrete_date=f"{next_wed.isoformat()}T18:00:00+00:00",
             local_date=next_wed.isoformat())
    db.put_slot(s)
    db.put_assignment(Assignment(
        community_id=cid, app_id=app.app_id, yyyy_mm=pid,
        slot_id=s.slot_id, user_id=alice.user_id,
        local_date=s.local_date))

    resp = web._home({}, newbie, db.get_community(cid), app, None)
    body = resp["body"]
    # The slot covered by Alice is NOT in the visible top-level list —
    # it's collapsed under "+ N more".
    assert "+ 1 more slot" in body


def test_add_months_clamps_day() -> None:
    """Jan 31 + 1 month is Feb 28 (or 29 in a leap year), not Mar 3."""
    import datetime as dt
    from community_organizer.lambdas import web

    assert web._add_months(dt.date(2026, 1, 31), 1) == dt.date(2026, 2, 28)
    assert web._add_months(dt.date(2024, 1, 31), 1) == dt.date(2024, 2, 29)
    assert web._add_months(dt.date(2026, 12, 15), 1) == dt.date(2027, 1, 15)


def test_recurring_home_uses_month_offset_pagination(ddb_table) -> None:
    """Default home shows current + next month; ?month_offset=N
    advances by N calendar months; nav links point at month_offset
    not week_offset."""
    from community_organizer.core import db
    from community_organizer.core.models import (
        Application, Community, SlotTemplate,
    )
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Test"))
    app = Application(community_id=cid, name="Adoration",
                      app_type="recurring_commitments",
                      period_type="weekly",
                      default_timezone="America/New_York")
    db.put_application(app)
    tpl = SlotTemplate(community_id=cid, app_id=app.app_id,
                       name="Wed 2 PM", day_of_week=2,
                       start_time="14:00", duration_minutes=60,
                       required_volunteers=1, min_volunteers=1,
                       auto_reminders=False)
    db.put_template(tpl)
    user = User(community_id=cid, email="m@example.com", name="Member")
    db.put_user(user)

    resp = web._home({}, user, db.get_community(cid), app, None)
    assert resp["statusCode"] == 200
    body = resp["body"]
    # Old week-based nav must not appear.
    assert "week_offset" not in body
    # New month-based nav must appear.
    assert "month_offset=1" in body
    # "Previous month" is greyed out at offset 0 (no link href).
    assert "Previous month" in body


def test_recurring_home_admin_sees_assign_picker(ddb_table) -> None:
    """An admin viewing the recurring home gets a per-slot 'Assign other'
    select listing app members not already on the slot. Plain members
    see only the self affordances."""
    import datetime as dt
    from community_organizer.core import db
    from community_organizer.core.models import (
        Application, Community, Membership, SlotTemplate,
    )
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Test"))
    app = Application(community_id=cid, name="Adoration",
                      app_type="recurring_commitments",
                      period_type="weekly",
                      default_timezone="America/New_York")
    db.put_application(app)
    tpl = SlotTemplate(community_id=cid, app_id=app.app_id,
                       name="Wed 2 PM", day_of_week=2,
                       start_time="14:00", duration_minutes=60,
                       required_volunteers=1, min_volunteers=1,
                       auto_reminders=False)
    db.put_template(tpl)
    # Admin viewer is an AA; Mary is a plain app member.
    aa = User(community_id=cid, email="aa@example.com", name="App Admin")
    db.put_user(aa)
    db.put_membership(Membership(community_id=cid, app_id=app.app_id,
                                 user_id=aa.user_id, app_role="aa"))
    mary = User(community_id=cid, email="m@example.com", name="Mary")
    db.put_user(mary)
    db.put_membership(Membership(community_id=cid, app_id=app.app_id,
                                 user_id=mary.user_id, app_role="member"))

    aa_mem = db.get_membership(app.app_id, aa.user_id)
    community = db.get_community(cid)
    resp = web._home({}, aa, community, app, aa_mem)
    assert resp["statusCode"] == 200
    body = resp["body"]
    assert "+ Assign other" in body
    assert "/api/admin/assign?" in body
    # Mary should appear as an option.
    assert f"value='{mary.user_id}'" in body

    # Plain member shouldn't see the assign picker.
    resp_member = web._home({}, mary, community, app, None)
    assert resp_member["statusCode"] == 200
    assert "+ Assign other" not in resp_member["body"]


def test_find_adjacent_assignees(ddb_table) -> None:
    """Alice is the slot before Mary; Bob is the slot after.
    The helper should return (Alice, Bob)."""
    from community_organizer.lambdas import web

    community, app, alice, mary, bob, mid_slot = (
        _seed_three_adjacent_slots(ddb_table))
    prior, nxt = web._find_adjacent_assignees(
        app, mid_slot, mid_slot.yyyy_mm)
    assert [u.user_id for u in prior] == [alice.user_id]
    assert [u.user_id for u in nxt] == [bob.user_id]


def test_release_sends_take_or_split_emails(ddb_table, monkeypatch) -> None:
    """Mary releases her 2 PM slot. Alice (1 PM) and Bob (3 PM) each
    receive a "Take it or Split it" email with links pointing at
    /assignments/cover."""
    from community_organizer.lambdas import web

    community, app, alice, mary, bob, mid_slot = (
        _seed_three_adjacent_slots(ddb_table))

    sent = []
    monkeypatch.setattr(
        "community_organizer.providers.email.get_email_provider",
        lambda: type("S", (), {
            "send": lambda self, **kw: sent.append(kw),
        })(),
    )

    event = {
        "rawPath": "/api/assignments/release",
        "requestContext": {"http": {"method": "POST"}},
        "queryStringParameters": {
            "slot_id": mid_slot.slot_id, "month": mid_slot.yyyy_mm,
        },
    }
    web._release_assignment(event, mary, community, app, None)
    addrs = {kw["to_addr"] for kw in sent}
    assert alice.email in addrs
    assert bob.email in addrs
    # Alice's email should contain a Take link for the mid slot AND a
    # Split link with Bob.
    alice_msgs = [kw for kw in sent if kw["to_addr"] == alice.email
                  and "can you cover" in kw["subject"].lower()]
    assert alice_msgs
    body = alice_msgs[0]["body_text"]
    assert f"slot_id={mid_slot.slot_id}" in body
    assert "mode=take" in body
    assert "mode=split" in body
    assert f"with={bob.user_id}" in body
    # Same shape for Bob's email (split should reference Alice).
    bob_msgs = [kw for kw in sent if kw["to_addr"] == bob.email
                and "can you cover" in kw["subject"].lower()]
    assert bob_msgs
    bob_body = bob_msgs[0]["body_text"]
    assert f"with={alice.user_id}" in bob_body


def test_release_take_or_split_skips_when_no_neighbors(ddb_table, monkeypatch) -> None:
    """A standalone slot (no adjacent assigned slot) — no take-or-split
    email goes out, only the cohort/admin notifications."""
    import datetime as dt
    from community_organizer.core import db
    from community_organizer.core.models import (
        Application, Assignment, Community, Schedule, Slot, SlotTemplate,
    )
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Test"))
    app = Application(community_id=cid, name="Adoration",
                      app_type="recurring_commitments",
                      period_type="weekly",
                      default_timezone="America/New_York")
    db.put_application(app)
    tpl = SlotTemplate(community_id=cid, app_id=app.app_id,
                       name="Wed 2 PM", day_of_week=2,
                       start_time="14:00", duration_minutes=60,
                       required_volunteers=1, min_volunteers=1)
    db.put_template(tpl)
    mary = User(community_id=cid, email="m@example.com", name="Mary")
    db.put_user(mary)
    period_id = "2099-W22"
    slot = Slot(community_id=cid, app_id=app.app_id,
                yyyy_mm=period_id, template_id=tpl.template_id,
                name="Wed 2 PM", day_of_week=2,
                start_time="14:00", arrival_offset_minutes=0,
                duration_minutes=60, required_volunteers=1,
                min_volunteers=1,
                concrete_date="2099-05-27T14:00",
                local_date="2099-05-27")
    db.put_slot(slot)
    db.put_assignment(Assignment(
        community_id=cid, app_id=app.app_id, yyyy_mm=period_id,
        slot_id=slot.slot_id, user_id=mary.user_id,
        local_date=slot.local_date))
    db.put_schedule(Schedule(community_id=cid, app_id=app.app_id,
                             yyyy_mm=period_id, state="materialized"))

    sent = []
    monkeypatch.setattr(
        "community_organizer.providers.email.get_email_provider",
        lambda: type("S", (), {
            "send": lambda self, **kw: sent.append(kw),
        })(),
    )

    event = {
        "rawPath": "/api/assignments/release",
        "requestContext": {"http": {"method": "POST"}},
        "queryStringParameters": {
            "slot_id": slot.slot_id, "month": period_id,
        },
    }
    web._release_assignment(event, mary, db.get_community(cid), app, None)
    # No "can you cover" subject — there were no neighbors.
    assert not any("can you cover" in kw["subject"].lower()
                   for kw in sent)


def test_cover_released_take_assigns_caller(ddb_table, monkeypatch) -> None:
    """Click the Take link → confirm → caller is assigned to the slot
    and gets a one-off .ics."""
    from community_organizer.core import db
    from community_organizer.lambdas import web

    community, app, alice, mary, bob, mid_slot = (
        _seed_three_adjacent_slots(ddb_table))
    # Remove Mary's assignment first to simulate the release.
    db.delete_assignment(app.app_id, mid_slot.yyyy_mm,
                         mid_slot.slot_id, mary.user_id)

    sent = []
    monkeypatch.setattr(
        "community_organizer.providers.email.get_email_provider",
        lambda: type("S", (), {
            "send": lambda self, **kw: sent.append(kw),
        })(),
    )

    event = {
        "rawPath": "/api/assignments/cover",
        "requestContext": {"http": {"method": "POST"}},
        "queryStringParameters": {
            "slot_id": mid_slot.slot_id, "month": mid_slot.yyyy_mm,
            "mode": "take",
        },
    }
    resp = web._api_cover_released(event, alice, community, app, None)
    assert resp["statusCode"] == 302
    asgns = list(db.list_assignments_for_slot(
        app.app_id, mid_slot.yyyy_mm, mid_slot.slot_id))
    assert {a.user_id for a in asgns} == {alice.user_id}
    # Alice received the pickup .ics.
    assert any("BEGIN:VEVENT" in (kw.get("ics_content") or "")
               for kw in sent if kw["to_addr"] == alice.email)


def test_cover_released_split_assigns_both(ddb_table, monkeypatch) -> None:
    """Click Split → confirm → caller AND named partner both
    assigned, both get the pickup .ics, partner also gets a separate
    "you've been signed up" plain-text email."""
    from community_organizer.core import db
    from community_organizer.lambdas import web

    community, app, alice, mary, bob, mid_slot = (
        _seed_three_adjacent_slots(ddb_table))
    db.delete_assignment(app.app_id, mid_slot.yyyy_mm,
                         mid_slot.slot_id, mary.user_id)

    sent = []
    monkeypatch.setattr(
        "community_organizer.providers.email.get_email_provider",
        lambda: type("S", (), {
            "send": lambda self, **kw: sent.append(kw),
        })(),
    )

    event = {
        "rawPath": "/api/assignments/cover",
        "requestContext": {"http": {"method": "POST"}},
        "queryStringParameters": {
            "slot_id": mid_slot.slot_id, "month": mid_slot.yyyy_mm,
            "mode": "split", "with": bob.user_id,
        },
    }
    resp = web._api_cover_released(event, alice, community, app, None)
    assert resp["statusCode"] == 302
    asgns = list(db.list_assignments_for_slot(
        app.app_id, mid_slot.yyyy_mm, mid_slot.slot_id))
    assert {a.user_id for a in asgns} == {alice.user_id, bob.user_id}
    # Bob got a plain-text heads-up explaining Alice signed him up.
    bob_msgs = [kw for kw in sent
                if kw["to_addr"] == bob.email
                and "signed you up to split" in kw["subject"].lower()]
    assert bob_msgs


def test_recurring_release_notifies_cohort_and_admins(ddb_table, monkeypatch) -> None:
    """Mary releases a recurring slot → cohort + admin notifications go
    out. _schedule_visible includes state=materialized so the cohort
    notify isn't suppressed."""
    from community_organizer.core import db
    from community_organizer.core.models import (
        Application, Assignment, Cohort, CohortMembership, Community,
        Membership, Schedule, Slot, SlotTemplate,
    )
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Test"))
    app = Application(community_id=cid, name="Adoration",
                      app_type="recurring_commitments",
                      period_type="weekly",
                      default_timezone="America/New_York")
    db.put_application(app)
    tpl = SlotTemplate(community_id=cid, app_id=app.app_id,
                       name="Wed 2 PM", day_of_week=2,
                       start_time="14:00", duration_minutes=60,
                       required_volunteers=1, min_volunteers=1)
    db.put_template(tpl)
    cohort = Cohort(community_id=cid, app_id=app.app_id,
                    name="Wed 2 PM regulars",
                    linked_template_id=tpl.template_id)
    db.put_cohort(cohort)
    mary = User(community_id=cid, email="m@example.com", name="Mary")
    db.put_user(mary)
    # Another cohort member who should be notified.
    sue = User(community_id=cid, email="s@example.com", name="Sue")
    db.put_user(sue)
    db.put_cohort_membership(CohortMembership(
        cohort_id=cohort.cohort_id, user_id=sue.user_id))
    # App admin to also notify.
    aa = User(community_id=cid, email="aa@example.com", name="Adoration Admin")
    db.put_user(aa)
    db.put_membership(Membership(community_id=cid, app_id=app.app_id,
                                 user_id=aa.user_id, app_role="aa"))

    period_id = "2099-W22"
    slot = Slot(community_id=cid, app_id=app.app_id,
                yyyy_mm=period_id, template_id=tpl.template_id,
                name="Wed 2 PM", day_of_week=2, start_time="14:00",
                arrival_offset_minutes=0, duration_minutes=60,
                required_volunteers=1, min_volunteers=1,
                concrete_date="2099-05-27T14:00",
                local_date="2099-05-27")
    db.put_slot(slot)
    db.put_assignment(Assignment(
        community_id=cid, app_id=app.app_id, yyyy_mm=period_id,
        slot_id=slot.slot_id, user_id=mary.user_id,
        local_date=slot.local_date))
    db.put_schedule(Schedule(community_id=cid, app_id=app.app_id,
                             yyyy_mm=period_id, state="materialized"))

    sent = []
    monkeypatch.setattr(
        "community_organizer.providers.email.get_email_provider",
        lambda: type("S", (), {
            "send": lambda self, **kw: sent.append(kw),
        })(),
    )

    event = {
        "rawPath": "/api/assignments/release",
        "requestContext": {"http": {"method": "POST"}},
        "queryStringParameters": {
            "slot_id": slot.slot_id, "month": period_id,
        },
    }
    web._release_assignment(event, mary, db.get_community(cid),
                            app, None)
    to_addrs = {kw["to_addr"] for kw in sent}
    # Sue (other cohort member) AND aa (app admin) both got notified.
    assert sue.email in to_addrs
    assert aa.email in to_addrs


def test_coverage_release_notifies_coassignees_and_admins(
        ddb_table, monkeypatch) -> None:
    """Regression for the usher-app bug (2026-06-14): a coverage release
    that leaves the slot AT/ABOVE required must STILL notify the remaining
    co-assignees and the app admins — symmetric to the signup fan-out — so
    a sign-up-then-release doesn't leave a stale "X signed up" record.

    Slot required=2 with three assignees; one releases, two remain (so the
    old under-coverage gate would have stayed silent for everyone)."""
    from community_organizer.core import db
    from community_organizer.core.models import (
        Application, Assignment, Community, Membership, Schedule, Slot,
        SlotTemplate, User,
    )
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Parish",
                              default_timezone="America/New_York"))
    app = Application(community_id=cid, name="Ushers", app_type="coverage",
                      default_timezone="America/New_York")
    db.put_application(app)
    tpl = SlotTemplate(community_id=cid, app_id=app.app_id, name="Sun 12 PM",
                       day_of_week=6, start_time="12:00",
                       duration_minutes=60, required_volunteers=2,
                       min_volunteers=1)
    db.put_template(tpl)

    releaser = User(community_id=cid, email="rel@example.com", name="Gmail Member")
    co1 = User(community_id=cid, email="co1@example.com", name="Co One")
    co2aa = User(community_id=cid, email="co2@example.com", name="Co Two AA")
    aa_only = User(community_id=cid, email="aa@example.com", name="Thomas")
    for u in (releaser, co1, co2aa, aa_only):
        db.put_user(u)
    # co2aa is BOTH a co-assignee and an AA (the reporter's situation);
    # aa_only is an AA not on the slot.
    db.put_membership(Membership(community_id=cid, app_id=app.app_id,
                                 user_id=co2aa.user_id, app_role="aa"))
    db.put_membership(Membership(community_id=cid, app_id=app.app_id,
                                 user_id=aa_only.user_id, app_role="aa"))

    period_id = "2099-05"
    slot = Slot(community_id=cid, app_id=app.app_id, yyyy_mm=period_id,
                template_id=tpl.template_id, name="Sun 12 PM",
                day_of_week=6, start_time="12:00", arrival_offset_minutes=0,
                duration_minutes=60, required_volunteers=2, min_volunteers=1,
                concrete_date="2099-05-24T12:00", local_date="2099-05-24")
    db.put_slot(slot)
    for u in (releaser, co1, co2aa):
        db.put_assignment(Assignment(
            community_id=cid, app_id=app.app_id, yyyy_mm=period_id,
            slot_id=slot.slot_id, user_id=u.user_id,
            local_date=slot.local_date))
    db.put_schedule(Schedule(community_id=cid, app_id=app.app_id,
                             yyyy_mm=period_id, state="published"))

    sent = []
    monkeypatch.setattr(
        "community_organizer.providers.email.get_email_provider",
        lambda: type("S", (), {"send": lambda self, **kw: sent.append(kw)})(),
    )

    event = {
        "rawPath": "/api/assignments/release",
        "requestContext": {"http": {"method": "POST"}},
        "queryStringParameters": {"slot_id": slot.slot_id, "month": period_id},
    }
    web._release_assignment(event, releaser, db.get_community(cid), app, None)

    # Remaining co-assignees were told the releaser left.
    co_msgs = [kw for kw in sent if "released a slot you're also covering"
               in kw["body_text"]]
    co_to = {kw["to_addr"] for kw in co_msgs}
    assert co1.email in co_to
    assert co2aa.email in co_to          # co-assignee who is also an AA
    assert releaser.email not in co_to   # not told about their own release

    # App admins were told, even though coverage stayed at the required 2.
    admin_msgs = [kw for kw in sent
                  if "released this slot" in kw["subject"].lower()]
    admin_to = {kw["to_addr"] for kw in admin_msgs}
    assert aa_only.email in admin_to
    assert co2aa.email in admin_to       # AA who is also a co-assignee
    # The coverage line reflects that the slot is still fully covered.
    assert any("still has full coverage" in kw["body_text"]
               for kw in admin_msgs)


def test_admin_removal_names_actor_and_no_bad_grammar(ddb_table, monkeypatch):
    """An admin removing ANOTHER user names the actor ('was removed by <name>',
    never 'by admin'), and nothing uses reflexive / singular-they grammar."""
    from community_organizer.core import db
    from community_organizer.core.models import (
        Application, Assignment, Community, Membership, Schedule, Slot, User,
    )
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Parish"))
    app = Application(community_id=cid, name="Ushers", app_type="coverage")
    db.put_application(app)
    removed = User(community_id=cid, email="bob@example.com", name="Bob Jones")
    admin = User(community_id=cid, email="jane@example.com", name="Jane Smith")
    watcher = User(community_id=cid, email="aa@example.com", name="Other Admin")
    for u in (removed, admin, watcher):
        db.put_user(u)
    db.put_membership(Membership(community_id=cid, app_id=app.app_id,
                                 user_id=watcher.user_id, app_role="aa"))
    slot = Slot(community_id=cid, app_id=app.app_id, yyyy_mm="2099-05",
                template_id="one-off", name="Sun 12 PM", day_of_week=6,
                start_time="12:00", arrival_offset_minutes=0, duration_minutes=60,
                required_volunteers=2, min_volunteers=1,
                concrete_date="2099-05-24T12:00", local_date="2099-05-24",
                slot_id="s1")
    db.put_slot(slot)
    db.put_assignment(Assignment(community_id=cid, app_id=app.app_id,
                                 yyyy_mm="2099-05", slot_id="s1",
                                 user_id=removed.user_id, local_date=slot.local_date))
    db.put_schedule(Schedule(community_id=cid, app_id=app.app_id,
                             yyyy_mm="2099-05", state="published"))
    sent = []
    monkeypatch.setattr("community_organizer.providers.email.get_email_provider",
                        lambda: type("S", (), {"send": lambda self, **kw: sent.append(kw)})())

    web._send_removal_notifications(removed, admin, db.get_community(cid), app,
                                    slot, "2099-05", self_release=False)

    admin_msg = [kw for kw in sent if kw["to_addr"] == watcher.email]
    assert admin_msg, "the other admin should be notified"
    assert any("was removed by Jane Smith" in kw["subject"] for kw in admin_msg)
    # Never the anonymous phrasing, never reflexive / singular-they.
    blob = " ".join(kw["subject"] + " " + kw["body_text"] for kw in sent).lower()
    assert "by admin" not in blob
    assert "themselves" not in blob
    assert "their slot" not in blob


def test_self_removal_via_admin_screen_says_released(ddb_table, monkeypatch):
    """An admin removing their OWN slot reads as 'released this slot', not
    'removed by <self>' and not reflexive grammar."""
    from community_organizer.core import db
    from community_organizer.core.models import (
        Application, Assignment, Community, Membership, Schedule, Slot, User,
    )
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Parish"))
    app = Application(community_id=cid, name="Ushers", app_type="coverage")
    db.put_application(app)
    tom = User(community_id=cid, email="tom@example.com", name="Riley Tester")
    other = User(community_id=cid, email="aa@example.com", name="Morgan")
    for u in (tom, other):
        db.put_user(u)
    for u in (tom, other):
        db.put_membership(Membership(community_id=cid, app_id=app.app_id,
                                     user_id=u.user_id, app_role="aa"))
    slot = Slot(community_id=cid, app_id=app.app_id, yyyy_mm="2099-05",
                template_id="one-off", name="Sun 12 PM", day_of_week=6,
                start_time="12:00", arrival_offset_minutes=0, duration_minutes=60,
                required_volunteers=2, min_volunteers=1,
                concrete_date="2099-05-24T12:00", local_date="2099-05-24",
                slot_id="s1")
    db.put_slot(slot)
    db.put_assignment(Assignment(community_id=cid, app_id=app.app_id,
                                 yyyy_mm="2099-05", slot_id="s1",
                                 user_id=tom.user_id, local_date=slot.local_date))
    db.put_schedule(Schedule(community_id=cid, app_id=app.app_id,
                             yyyy_mm="2099-05", state="published"))
    sent = []
    monkeypatch.setattr("community_organizer.providers.email.get_email_provider",
                        lambda: type("S", (), {"send": lambda self, **kw: sent.append(kw)})())

    # remover == removed (admin removed their own slot through the admin screen)
    web._send_removal_notifications(tom, tom, db.get_community(cid), app,
                                    slot, "2099-05", self_release=False)
    mark_msg = [kw for kw in sent if kw["to_addr"] == other.email]
    assert any("Riley Tester released this slot" in kw["subject"] for kw in mark_msg)
    blob = " ".join(kw["subject"] + " " + kw["body_text"] for kw in sent).lower()
    assert "removed by riley tester" not in blob     # not "removed by <self>"
    assert "themselves" not in blob and "their slot" not in blob


def test_cohort_opening_coverage_app_no_duplicate_and_names_coverers(
        ddb_table, monkeypatch):
    """For a COVERAGE app, the cohort-opening fan-out sends 'opening' to
    available members but NOT a 'coverage update' to in-slot peers (they get
    the dedicated co-assignee note instead) — and the opening names who's
    still covering."""
    from community_organizer.core import db
    from community_organizer.core.models import (
        Application, Assignment, Cohort, CohortMembership, Community,
        Membership, Schedule, Slot, SlotTemplate, User,
    )
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Parish"))
    app = Application(community_id=cid, name="Ushers", app_type="coverage",
                      group_email_mode=True)
    db.put_application(app)
    tpl = SlotTemplate(community_id=cid, app_id=app.app_id, name="Sun 12 PM",
                       day_of_week=6, start_time="12:00", duration_minutes=60,
                       required_volunteers=3, min_volunteers=1)
    db.put_template(tpl)
    cohort = Cohort(community_id=cid, app_id=app.app_id, name="Noon",
                    linked_template_id=tpl.template_id)
    db.put_cohort(cohort)
    releaser = User(community_id=cid, email="rel@example.com", name="Riley Tester")
    onslot = User(community_id=cid, email="member@example.com", name="Morgan")   # in slot
    avail = User(community_id=cid, email="ed@example.com", name="Ed Buttarazzi")  # not
    for u in (releaser, onslot, avail):
        db.put_user(u)
        db.put_cohort_membership(CohortMembership(cohort_id=cohort.cohort_id,
                                                  user_id=u.user_id))
    period = "2099-05"
    slot = Slot(community_id=cid, app_id=app.app_id, yyyy_mm=period,
                template_id=tpl.template_id, name="Sun 12 PM", day_of_week=6,
                start_time="12:00", arrival_offset_minutes=0, duration_minutes=60,
                required_volunteers=3, min_volunteers=1,
                concrete_date="2099-05-24T12:00", local_date="2099-05-24",
                slot_id="s1")
    db.put_slot(slot)
    db.put_assignment(Assignment(community_id=cid, app_id=app.app_id,
                                 yyyy_mm=period, slot_id="s1",
                                 user_id=onslot.user_id, local_date=slot.local_date))
    db.put_schedule(Schedule(community_id=cid, app_id=app.app_id,
                             yyyy_mm=period, state="published"))
    sent = []
    monkeypatch.setattr("community_organizer.providers.email.get_email_provider",
                        lambda: type("S", (), {"send": lambda self, **kw: sent.append(kw)})())

    web._notify_cohort_of_opening(releaser, db.get_community(cid), app, slot, period)

    subjects = " ".join(kw["subject"] for kw in sent)
    assert "opening:" in subjects
    assert "coverage update" not in subjects     # coverage app: no in-slot dup
    # The opening names who's still on the slot.
    assert any("Still covering: Morgan" in kw["body_text"] for kw in sent)


def test_recurring_signup_emails_pickup_ics(ddb_table, monkeypatch) -> None:
    """Sue picks up a slot she's not in the cohort for → one-off .ics
    emailed (no RRULE; it's just for this single occurrence)."""
    from community_organizer.core import db
    from community_organizer.core.models import (
        Application, Community, Schedule, Slot, SlotTemplate,
    )
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Test"))
    app = Application(community_id=cid, name="Adoration",
                      app_type="recurring_commitments",
                      period_type="weekly",
                      default_timezone="America/New_York")
    db.put_application(app)
    tpl = SlotTemplate(community_id=cid, app_id=app.app_id,
                       name="Wed 2 PM", day_of_week=2,
                       start_time="14:00", duration_minutes=60,
                       required_volunteers=1, min_volunteers=1,
                       max_volunteers=None)
    db.put_template(tpl)
    sue = User(community_id=cid, email="s@example.com", name="Sue")
    db.put_user(sue)
    period_id = "2099-W22"
    slot = Slot(community_id=cid, app_id=app.app_id,
                yyyy_mm=period_id, template_id=tpl.template_id,
                name="Wed 2 PM", day_of_week=2, start_time="14:00",
                arrival_offset_minutes=0, duration_minutes=60,
                required_volunteers=1, min_volunteers=1,
                max_volunteers=None,
                concrete_date="2099-05-27T14:00",
                local_date="2099-05-27")
    db.put_slot(slot)
    db.put_schedule(Schedule(community_id=cid, app_id=app.app_id,
                             yyyy_mm=period_id, state="materialized"))

    sent = []
    monkeypatch.setattr(
        "community_organizer.providers.email.get_email_provider",
        lambda: type("S", (), {
            "send": lambda self, **kw: sent.append(kw),
        })(),
    )

    event = {
        "rawPath": "/api/assignments/signup",
        "requestContext": {"http": {"method": "POST"}},
        "queryStringParameters": {
            "slot_id": slot.slot_id, "month": period_id,
        },
    }
    resp = web._signup_assignment(event, sue, db.get_community(cid),
                                  app, None)
    assert resp["statusCode"] in (200, 302)
    assert len(sent) == 1
    payload = sent[0]
    assert payload["to_addr"] == sue.email
    # One-off .ics — not the RRULE variant.
    assert "RRULE:" not in payload["ics_content"]
    assert "BEGIN:VEVENT" in payload["ics_content"]


def _seed_recurring_with_materialized_slot(ddb_table):
    """Return (community, app, cohort, mary, slot) — a recurring app
    with one materialized future period, ready to test cohort
    onboarding side-effects."""
    from community_organizer.core import db
    from community_organizer.core.models import (
        Application, Cohort, Community, Schedule, Slot, SlotTemplate,
    )

    cid = "c1"
    db.put_community(Community(community_id=cid, name="St. Test"))
    app = Application(community_id=cid, name="Adoration",
                      app_type="recurring_commitments",
                      period_type="weekly",
                      default_timezone="America/New_York")
    db.put_application(app)
    tpl = SlotTemplate(community_id=cid, app_id=app.app_id,
                       name="Wed 2 PM", day_of_week=2,
                       start_time="14:00", duration_minutes=60,
                       required_volunteers=1, min_volunteers=1,
                       auto_reminders=False)
    db.put_template(tpl)
    cohort = Cohort(community_id=cid, app_id=app.app_id,
                    name="Wed 2 PM regulars",
                    linked_template_id=tpl.template_id)
    db.put_cohort(cohort)
    mary = User(community_id=cid, email="m@example.com", name="Mary")
    db.put_user(mary)
    # Already-materialized future period.
    period_id = "2099-W22"
    slot = Slot(community_id=cid, app_id=app.app_id,
                yyyy_mm=period_id, template_id=tpl.template_id,
                name="Wed 2 PM", day_of_week=2, start_time="14:00",
                arrival_offset_minutes=0, duration_minutes=60,
                required_volunteers=1, min_volunteers=1,
                concrete_date="2099-05-27T14:00",
                local_date="2099-05-27")
    db.put_slot(slot)
    db.put_schedule(Schedule(community_id=cid, app_id=app.app_id,
                             yyyy_mm=period_id, state="materialized"))
    return db.get_community(cid), app, cohort, mary, slot


def test_cohort_join_backfills_existing_future_assignments(
        ddb_table, monkeypatch) -> None:
    """Mary joins the Wed 2 PM cohort AFTER 2099-W22 was materialized.
    The handler should retro-fill her Assignment row for that slot
    plus send the RRULE invite (we stub the email provider out)."""
    from community_organizer.core import db
    from community_organizer.core.models import CohortMembership
    from community_organizer.lambdas import web

    community, app, cohort, mary, slot = (
        _seed_recurring_with_materialized_slot(ddb_table))

    sent_messages = []
    monkeypatch.setattr(
        "community_organizer.providers.email.get_email_provider",
        lambda: type("Stub", (), {
            "send": lambda self, **kw: sent_messages.append(kw),
        })(),
    )

    event = {
        "rawPath": "/api/cohorts/add-member",
        "requestContext": {"http": {"method": "POST"}},
        "queryStringParameters": {
            "cohort_id": cohort.cohort_id, "user_id": mary.user_id,
        },
    }
    web._api_cohort_add_member(event, mary, community, app, None)

    # Assignment row exists now.
    asgns = list(db.list_assignments_for_slot(
        app.app_id, slot.yyyy_mm, slot.slot_id))
    assert len(asgns) == 1 and asgns[0].user_id == mary.user_id
    # RRULE invite was sent.
    assert len(sent_messages) == 1
    sent = sent_messages[0]
    assert sent["to_addr"] == mary.email
    assert "RRULE:FREQ=WEEKLY;BYDAY=WE" in sent["ics_content"]
    # CohortMembership row should also exist.
    cms = list(db.list_cohort_members(cohort.cohort_id))
    assert any(cm.user_id == mary.user_id for cm in cms)


def test_cohort_leave_deletes_future_assignments_and_sends_cancel(
        ddb_table, monkeypatch) -> None:
    """When Mary leaves the cohort, her future Assignment row is
    removed and a METHOD:CANCEL is emailed."""
    from community_organizer.core import db
    from community_organizer.core.models import Assignment, CohortMembership
    from community_organizer.lambdas import web

    from community_organizer.core.models import Cohort
    community, app, cohort, mary, slot = (
        _seed_recurring_with_materialized_slot(ddb_table))
    # Pre-seed Mary as a cohort member + already-back-filled Assignment.
    db.put_cohort_membership(CohortMembership(
        cohort_id=cohort.cohort_id, user_id=mary.user_id))
    # Plus a second cohort membership so the #219 "must keep one
    # cohort" self-service guard doesn't block the leave under test.
    fallback = Cohort(community_id=community.community_id,
                      app_id=app.app_id, name="Wed 6 PM")
    db.put_cohort(fallback)
    db.put_cohort_membership(CohortMembership(
        cohort_id=fallback.cohort_id, user_id=mary.user_id))
    db.put_assignment(Assignment(
        community_id=community.community_id, app_id=app.app_id,
        yyyy_mm=slot.yyyy_mm, slot_id=slot.slot_id,
        user_id=mary.user_id, local_date=slot.local_date))

    sent_messages = []
    monkeypatch.setattr(
        "community_organizer.providers.email.get_email_provider",
        lambda: type("Stub", (), {
            "send": lambda self, **kw: sent_messages.append(kw),
        })(),
    )

    event = {
        "rawPath": "/api/cohorts/remove-member",
        "requestContext": {"http": {"method": "POST"}},
        "queryStringParameters": {
            "cohort_id": cohort.cohort_id, "user_id": mary.user_id,
        },
    }
    web._api_cohort_remove_member(event, mary, community, app, None)

    # Future Assignment gone.
    asgns = list(db.list_assignments_for_slot(
        app.app_id, slot.yyyy_mm, slot.slot_id))
    assert not asgns
    # CANCEL emailed.
    assert len(sent_messages) == 1
    sent = sent_messages[0]
    assert "METHOD:CANCEL" in sent["ics_content"]
    assert "SEQUENCE:1" in sent["ics_content"]


def test_cohort_join_in_coverage_app_no_invite_no_assignments(
        ddb_table, monkeypatch) -> None:
    """Cohort-join in a coverage app must NOT trigger the recurring
    invite or back-fill — that flow is recurring-only."""
    from community_organizer.core import db
    from community_organizer.core.models import (
        Application, Cohort, Community, Membership, SlotTemplate,
    )
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Test"))
    app = Application(community_id=cid, name="Ushers",
                      app_type="coverage", period_type="monthly",
                      default_timezone="America/New_York")
    db.put_application(app)
    tpl = SlotTemplate(community_id=cid, app_id=app.app_id,
                       name="Sun 8 AM", day_of_week=6,
                       start_time="08:00", duration_minutes=60,
                       required_volunteers=2, min_volunteers=1)
    db.put_template(tpl)
    cohort = Cohort(community_id=cid, app_id=app.app_id,
                    name="Sun 8 AM regulars",
                    linked_template_id=tpl.template_id)
    db.put_cohort(cohort)
    bob = User(community_id=cid, email="b@example.com", name="Bob")
    db.put_user(bob)
    db.put_membership(Membership(community_id=cid, app_id=app.app_id,
                                 user_id=bob.user_id))

    sent_messages = []
    monkeypatch.setattr(
        "community_organizer.providers.email.get_email_provider",
        lambda: type("Stub", (), {
            "send": lambda self, **kw: sent_messages.append(kw),
        })(),
    )

    event = {
        "rawPath": "/api/cohorts/add-member",
        "requestContext": {"http": {"method": "POST"}},
        "queryStringParameters": {
            "cohort_id": cohort.cohort_id, "user_id": bob.user_id,
        },
    }
    web._api_cohort_add_member(event, bob, db.get_community(cid),
                               app, None)
    # Coverage apps don't get the recurring invite.
    assert sent_messages == []


def test_ensure_period_materialized_creates_slots_and_assignments(ddb_table) -> None:
    """Recurring app, no Schedule yet, with one template + cohort + one
    cohort member → materialize creates the Slot, the Assignment, and
    the Schedule marker (state=materialized). Re-running is a no-op."""
    from community_organizer.core import db
    from community_organizer.core.models import (
        Application, Cohort, CohortMembership, Community, SlotTemplate,
    )
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Test"))
    app = Application(community_id=cid, name="Adoration",
                      app_type="recurring_commitments",
                      period_type="weekly",
                      default_timezone="America/New_York")
    db.put_application(app)
    tpl = SlotTemplate(community_id=cid, app_id=app.app_id,
                       name="Wed 2 PM", day_of_week=2,
                       start_time="14:00", duration_minutes=60,
                       required_volunteers=1, min_volunteers=1,
                       auto_reminders=False)
    db.put_template(tpl)
    cohort = Cohort(community_id=cid, app_id=app.app_id,
                    name="Wed 2 PM regulars",
                    linked_template_id=tpl.template_id)
    db.put_cohort(cohort)
    mary = User(community_id=cid, email="m@example.com", name="Mary")
    db.put_user(mary)
    db.put_cohort_membership(CohortMembership(
        cohort_id=cohort.cohort_id, user_id=mary.user_id))

    community = db.get_community(cid)
    period_id = "2026-W22"
    did = web._ensure_period_materialized(community, app, period_id)
    assert did is True

    slots = list(db.list_slots(app.app_id, period_id))
    assert len(slots) == 1
    asgns = list(db.list_assignments_for_month(app.app_id, period_id))
    assert len(asgns) == 1
    assert asgns[0].user_id == mary.user_id
    assert db.get_schedule(app.app_id, period_id).state == "materialized"

    # Idempotent: second call is a no-op.
    again = web._ensure_period_materialized(community, app, period_id)
    assert again is False
    assert len(list(db.list_slots(app.app_id, period_id))) == 1
    assert len(list(db.list_assignments_for_month(app.app_id, period_id))) == 1


def test_ensure_period_materialized_skips_coverage_apps(ddb_table) -> None:
    """Coverage apps still use the explicit Create-Schedule flow;
    materialize-on-view must NOT auto-create slots there."""
    from community_organizer.core import db
    from community_organizer.core.models import (
        Application, Community, SlotTemplate,
    )
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Test"))
    app = Application(community_id=cid, name="Ushers",
                      app_type="coverage", period_type="monthly",
                      default_timezone="America/New_York")
    db.put_application(app)
    db.put_template(SlotTemplate(
        community_id=cid, app_id=app.app_id, name="Sun 8 AM",
        day_of_week=6, start_time="08:00", duration_minutes=60,
        required_volunteers=2, min_volunteers=1,
    ))
    community = db.get_community(cid)
    did = web._ensure_period_materialized(community, app, "2026-05")
    assert did is False
    assert list(db.list_slots(app.app_id, "2026-05")) == []
    assert db.get_schedule(app.app_id, "2026-05") is None


def test_recurring_home_view_lazy_materializes_visible_window(ddb_table) -> None:
    """Loading the recurring home for an empty (un-materialized) app
    triggers materialization for every period in the visible window."""
    import datetime as dt
    from community_organizer.core import db
    from community_organizer.core.models import (
        Application, Community, SlotTemplate,
    )
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Test"))
    app = Application(community_id=cid, name="Adoration",
                      app_type="recurring_commitments",
                      period_type="weekly",
                      default_timezone="America/New_York")
    db.put_application(app)
    tpl = SlotTemplate(community_id=cid, app_id=app.app_id,
                       name="Wed 2 PM", day_of_week=2,
                       start_time="14:00", duration_minutes=60,
                       required_volunteers=1, min_volunteers=1,
                       auto_reminders=False)
    db.put_template(tpl)
    mary = User(community_id=cid, email="m@example.com", name="Mary")
    db.put_user(mary)

    # No Schedule rows yet anywhere.
    assert list(db.list_schedules(app.app_id)) == []

    community = db.get_community(cid)
    web._home({}, mary, community, app, None)

    # 4-week window → at least 3-5 distinct ISO weeks now have
    # Schedule rows + materialized slots.
    materialized = list(db.list_schedules(app.app_id))
    assert len(materialized) >= 3
    assert all(s.state == "materialized" for s in materialized)


def test_api_template_generate_range_creates_hourly_chain(ddb_table) -> None:
    """Wed 13:00 → Thu 8:00 with length=60, gap=0 should produce
    19 templates (13–14, 14–15, …, 23–24, 0–1, …, 7–8). Day-of-week
    bumps correctly across midnight."""
    from community_organizer.core import db
    from community_organizer.core.models import Application, Community, Membership
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Test"))
    app = Application(community_id=cid, name="Adoration",
                      app_type="recurring_commitments",
                      period_type="weekly")
    db.put_application(app)
    ca = User(community_id=cid, email="ca@example.com", name="CA",
              community_role="ca")
    db.put_user(ca)
    mem = Membership(community_id=cid, app_id=app.app_id,
                     user_id=ca.user_id, app_role="aa")
    db.put_membership(mem)

    event = {
        "rawPath": "/api/templates/generate-range",
        "requestContext": {"http": {"method": "POST"}},
        "queryStringParameters": {
            "start_day": "2", "start_time": "13:00",
            "end_day": "3", "end_time": "08:00",
            "length": "60", "gap": "0",
            "arrival": "0", "required": "1", "min_vol": "1",
            "max_vol": "",
        },
    }
    resp = web._api_template_generate_range(event, ca, None, app, mem)
    assert resp["statusCode"] == 302
    assert "generated=19" in resp["headers"]["Location"]
    tpls = sorted(db.list_templates(app.app_id),
                  key=lambda t: (t.day_of_week, t.start_time))
    assert len(tpls) == 19
    # First slot: Wed 13:00.
    assert (tpls[0].day_of_week, tpls[0].start_time) == (2, "13:00")
    # Last slot starts at Thu 07:00 (runs through 08:00 = end).
    assert (tpls[-1].day_of_week, tpls[-1].start_time) == (3, "07:00")
    # Every slot is 60 min, max=None (uncapped — blank in form).
    assert all(t.duration_minutes == 60 for t in tpls)
    assert all(t.max_volunteers is None for t in tpls)
    # Each slot has its auto-cohort linked back.
    cohorts = list(db.list_cohorts(app.app_id))
    assert len(cohorts) == 19
    template_ids = {t.template_id for t in tpls}
    cohort_template_ids = {c.linked_template_id for c in cohorts}
    assert template_ids == cohort_template_ids


def test_api_template_generate_range_respects_gap(ddb_table) -> None:
    """length=60, gap=30 means slot starts step by 90 min (1h on /
    30m off)."""
    from community_organizer.core import db
    from community_organizer.core.models import Application, Community, Membership
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Test"))
    app = Application(community_id=cid, name="X",
                      app_type="recurring_commitments",
                      period_type="weekly")
    db.put_application(app)
    ca = User(community_id=cid, email="ca@example.com", name="CA",
              community_role="ca")
    db.put_user(ca)
    mem = Membership(community_id=cid, app_id=app.app_id,
                     user_id=ca.user_id, app_role="aa")
    db.put_membership(mem)

    # 4-hour window with 90-min step + 60-min slot length:
    # 8:00–9:00, 9:30–10:30, 11:00–12:00 → 3 templates.
    event = {
        "rawPath": "/api/templates/generate-range",
        "requestContext": {"http": {"method": "POST"}},
        "queryStringParameters": {
            "start_day": "0", "start_time": "08:00",
            "end_day": "0", "end_time": "12:00",
            "length": "60", "gap": "30",
            "arrival": "0", "required": "1", "min_vol": "1",
            "max_vol": "",
        },
    }
    resp = web._api_template_generate_range(event, ca, None, app, mem)
    assert resp["statusCode"] == 302
    starts = sorted(t.start_time for t in db.list_templates(app.app_id))
    assert starts == ["08:00", "09:30", "11:00"]


def test_api_template_generate_range_skips_duplicates(ddb_table) -> None:
    """A template already at (day, start) is skipped, and the
    redirect surfaces a dup count so the banner can mention it."""
    from community_organizer.core import db
    from community_organizer.core.models import (
        Application, Community, Membership, SlotTemplate,
    )
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Test"))
    app = Application(community_id=cid, name="X",
                      app_type="recurring_commitments",
                      period_type="weekly")
    db.put_application(app)
    ca = User(community_id=cid, email="ca@example.com", name="CA",
              community_role="ca")
    db.put_user(ca)
    mem = Membership(community_id=cid, app_id=app.app_id,
                     user_id=ca.user_id, app_role="aa")
    db.put_membership(mem)
    # Pre-existing template at Wed 14:00 — bulk would otherwise hit it.
    db.put_template(SlotTemplate(
        community_id=cid, app_id=app.app_id, name="Wed 2 PM",
        day_of_week=2, start_time="14:00", duration_minutes=60,
        required_volunteers=1, min_volunteers=1,
        arrival_offset_minutes=0,
    ))

    # 13:00–16:00 hourly = would create 3 (13, 14, 15) but 14 is dup.
    event = {
        "rawPath": "/api/templates/generate-range",
        "requestContext": {"http": {"method": "POST"}},
        "queryStringParameters": {
            "start_day": "2", "start_time": "13:00",
            "end_day": "2", "end_time": "16:00",
            "length": "60", "gap": "0",
            "arrival": "0", "required": "1", "min_vol": "1",
            "max_vol": "",
        },
    }
    resp = web._api_template_generate_range(event, ca, None, app, mem)
    loc = resp["headers"]["Location"]
    assert "generated=2" in loc
    assert "dups=1" in loc
    tpls = sorted(db.list_templates(app.app_id),
                  key=lambda t: t.start_time)
    # 13:00 (new), 14:00 (existing), 15:00 (new) — 3 total.
    assert [t.start_time for t in tpls] == ["13:00", "14:00", "15:00"]


def test_api_template_generate_range_rejects_ambiguous_same_day(ddb_table) -> None:
    """Same end_day as start_day with end_time <= start_time would
    imply a 7-day wrap that the admin almost certainly didn't mean.
    Reject with 400."""
    from community_organizer.core import db
    from community_organizer.core.models import Application, Community, Membership
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Test"))
    app = Application(community_id=cid, name="X",
                      app_type="recurring_commitments",
                      period_type="weekly")
    db.put_application(app)
    ca = User(community_id=cid, email="ca@example.com", name="CA",
              community_role="ca")
    db.put_user(ca)
    mem = Membership(community_id=cid, app_id=app.app_id,
                     user_id=ca.user_id, app_role="aa")
    db.put_membership(mem)
    event = {
        "rawPath": "/api/templates/generate-range",
        "requestContext": {"http": {"method": "POST"}},
        "queryStringParameters": {
            "start_day": "2", "start_time": "14:00",
            "end_day": "2", "end_time": "13:00",
            "length": "60", "gap": "0",
            "arrival": "0", "required": "1", "min_vol": "1",
        },
    }
    resp = web._api_template_generate_range(event, ca, None, app, mem)
    # Now redirects to /admin/templates with a styled error banner.
    assert resp["statusCode"] == 302
    assert "error=" in resp["headers"]["Location"]


def test_admin_nav_bar_hides_schedules_for_recurring_commitments() -> None:
    """Recurring apps don't have a per-period publish workflow —
    the home page is the schedule view. Hide the Schedules tab so
    the nav doesn't include a meaningless link."""
    from community_organizer.core.models import Application
    from community_organizer.lambdas import web

    recurring = Application(community_id="c1", name="Adoration",
                            app_type="recurring_commitments",
                            period_type="weekly")
    coverage = Application(community_id="c1", name="Ushers",
                           app_type="coverage", period_type="monthly")
    nav_recurring = web._admin_nav_bar("home", app=recurring)
    nav_coverage = web._admin_nav_bar("home", app=coverage)
    assert "Schedules" not in nav_recurring
    assert "Schedules" in nav_coverage
    # Other items still present in both.
    for item in ("Templates", "Members", "Cohorts", "Settings"):
        assert item in nav_recurring, f"{item} missing for recurring"
        assert item in nav_coverage, f"{item} missing for coverage"


def test_admin_nav_bar_back_arrow_on_app_home_when_not_current() -> None:
    """#194: when the user is NOT on the home page, the App home link
    leads with a back-arrow so it reads as a back-to-home affordance
    even when scanning a long page in a hurry. When the user IS on
    the home page, no arrow (the link becomes the bold "you are here"
    span anyway)."""
    from community_organizer.core.models import Application
    from community_organizer.lambdas import web

    app = Application(community_id="c1", name="Ushers",
                      app_type="coverage", period_type="monthly")
    # On any non-home page, the back-arrow leads App home.
    nav_on_members = web._admin_nav_bar("members", app=app)
    assert "&larr; App home" in nav_on_members
    # On the home page itself, App home becomes the current-page
    # bold marker — no arrow, no link.
    nav_on_home = web._admin_nav_bar("home", app=app)
    assert "&larr; App home" not in nav_on_home
    assert "<span style='font-weight:600;color:#333'>App home</span>" in nav_on_home


def test_admin_nav_bar_visual_chrome_separator() -> None:
    """#194: nav reads as a chrome block, not body text. Pinned by
    the heavier border-top (2px solid #ccc) above the row, so a
    refactor that drops the border would fail the test."""
    from community_organizer.core.models import Application
    from community_organizer.lambdas import web

    nav = web._admin_nav_bar(
        "members",
        app=Application(community_id="c1", name="X",
                        app_type="coverage", period_type="monthly"))
    assert "border-top:2px solid #ccc" in nav


def test_ca_nav_bar_two_items_with_current_bold(ddb_table) -> None:
    """#195: CA-mode pages get a bottom nav with the two CA surfaces
    (Apps + Community users), styled the same as the AA admin nav.
    Current page is bold; the other is a green link."""
    from community_organizer.lambdas import web

    # On the Applications page, "Applications" is bold and
    # "Community users" is a link.
    nav_apps = web._ca_nav_bar("apps")
    assert ("<span style='font-weight:600;color:#333'>"
            "Applications</span>") in nav_apps
    assert "/admin/community-users" in nav_apps
    # And vice versa.
    nav_users = web._ca_nav_bar("community-users")
    assert ("<span style='font-weight:600;color:#333'>"
            "Community users</span>") in nav_users
    assert "/admin/apps" in nav_users
    # Same chrome treatment as the AA nav.
    assert "border-top:2px solid #ccc" in nav_apps
    assert "border-top:2px solid #ccc" in nav_users


def test_ca_landing_renders_ca_nav_with_apps_marked_current(ddb_table) -> None:
    """The CA landing emits the CA bottom nav with Apps as the
    current-page bold marker."""
    from community_organizer.lambdas import web

    community, ca, _, _, _ = _seed_two_apps(ddb_table)
    body = web._ca_landing_page({}, ca, community)["body"]
    assert "/admin/community-users" in body
    assert ("<span style='font-weight:600;color:#333'>"
            "Applications</span>") in body


def test_text_to_html_paragraphs_basic() -> None:
    """#196: textarea body → structural HTML paragraphs. Blank lines
    become <p>; single newlines within a paragraph become <br>."""
    from community_organizer.lambdas import web

    # Single paragraph, no newlines.
    assert (web._text_to_html_paragraphs("hello world")
            == "<p style='margin:0 0 12px 0'>hello world</p>")
    # Two lines, single newline → one paragraph with <br>.
    assert (web._text_to_html_paragraphs("line one\nline two")
            == "<p style='margin:0 0 12px 0'>line one<br>line two</p>")
    # Blank line → two paragraphs.
    assert (web._text_to_html_paragraphs("Hi team,\n\nThanks,\nTeam")
            == ("<p style='margin:0 0 12px 0'>Hi team,</p>"
                "<p style='margin:0 0 12px 0'>Thanks,<br>Team</p>"))
    # Empty / whitespace-only input.
    assert web._text_to_html_paragraphs("") == ""
    assert web._text_to_html_paragraphs("   \n\n  ") == ""


def test_text_to_html_paragraphs_escapes_html() -> None:
    """The helper HTML-escapes the input so a textarea body
    containing markup can't break out of the email body or inject
    script content."""
    from community_organizer.lambdas import web

    out = web._text_to_html_paragraphs("<script>alert(1)</script>\n\nok")
    assert "<script>alert(1)</script>" not in out
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out
    assert "<p style='margin:0 0 12px 0'>ok</p>" in out


def test_ca_users_page_renders_ca_nav_and_drops_inline_back_link(
        ddb_table) -> None:
    """The community users page emits the CA bottom nav with
    "Community users" marked current. The old inline "← Back to
    community admin" link is gone (replaced by the bottom nav)."""
    from community_organizer.lambdas import web

    community, ca, _, _, _ = _seed_two_apps(ddb_table)
    body = web._ca_users_page({}, ca, community)["body"]
    assert ("<span style='font-weight:600;color:#333'>"
            "Community users</span>") in body
    # The /admin/apps link now lives in the bottom nav, not in an
    # inline "Back" link at the top.
    assert "Back to community admin" not in body
    # But the bottom nav links to /admin/apps.
    assert "/admin/apps" in body


def test_templates_page_auto_seeds_prefill_from_most_recent(ddb_table) -> None:
    """When the admin returns to /admin/templates without
    ?prefill_from, default to the most-recently-created template so
    they can continue the chain instead of starting from scratch."""
    import datetime as dt
    from community_organizer.core import db
    from community_organizer.core.models import (
        Application, Community, Membership, SlotTemplate,
    )
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Test"))
    app = Application(community_id=cid, name="Adoration",
                      app_type="recurring_commitments",
                      period_type="weekly")
    db.put_application(app)
    ca = User(community_id=cid, email="ca@example.com", name="CA",
              community_role="ca")
    db.put_user(ca)
    mem = Membership(community_id=cid, app_id=app.app_id,
                     user_id=ca.user_id, app_role="aa")
    db.put_membership(mem)

    # Two templates with explicit created_at — the second one is the
    # "most recent" and should drive the prefill defaults.
    earlier = SlotTemplate(
        community_id=cid, app_id=app.app_id,
        name="Wed 12:45 PM", day_of_week=2, start_time="12:45",
        duration_minutes=45, required_volunteers=1, min_volunteers=1,
        arrival_offset_minutes=0, max_volunteers=None,
        created_at="2026-05-01T00:00:00+00:00",
    )
    later = SlotTemplate(
        community_id=cid, app_id=app.app_id,
        name="Wed 1:30 PM", day_of_week=2, start_time="13:30",
        duration_minutes=60, required_volunteers=1, min_volunteers=1,
        arrival_offset_minutes=0, max_volunteers=None,
        created_at="2026-05-02T00:00:00+00:00",
    )
    db.put_template(earlier)
    db.put_template(later)

    resp = web._templates_page({}, ca, None, app, mem)
    body = resp["body"]
    # later's start_time (13:30) + duration (60) = 14:30 — that's
    # what should appear in the Add form's Start field.
    assert "value='14:30'" in body
    # Day stays Wed (no midnight wrap).
    assert "value='2' selected" in body


def test_api_template_add_blocks_exact_duplicate(ddb_table) -> None:
    """Adding a template with the same (day, start) as an existing
    one redirects to /admin/templates?dup=1 instead of creating a
    duplicate. The duplicate-rejection redirect preserves prefill_from
    so the chain doesn't break."""
    from community_organizer.core import db
    from community_organizer.core.models import (
        Application, Community, Membership, SlotTemplate,
    )
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="Test"))
    app = Application(community_id=cid, name="Adoration",
                      app_type="recurring_commitments",
                      period_type="weekly")
    db.put_application(app)
    ca = User(community_id=cid, email="ca@example.com", name="CA",
              community_role="ca")
    db.put_user(ca)
    mem = Membership(community_id=cid, app_id=app.app_id,
                     user_id=ca.user_id, app_role="aa")
    db.put_membership(mem)
    existing = SlotTemplate(
        community_id=cid, app_id=app.app_id, name="Wed 2 PM",
        day_of_week=2, start_time="14:00", duration_minutes=60,
        required_volunteers=1, min_volunteers=1,
        arrival_offset_minutes=0,
    )
    db.put_template(existing)

    event = {
        "rawPath": "/api/templates/add",
        "requestContext": {"http": {"method": "POST"}},
        "queryStringParameters": {
            "day": "2", "start": "14:00", "duration": "60",
            "arrival": "0", "required": "1", "min_vol": "1",
            "max_vol": "", "prefill_from": "some-other-tid",
        },
    }
    resp = web._api_template_add(event, ca, None, app, mem)
    assert resp["statusCode"] == 302
    loc = resp["headers"]["Location"]
    assert loc.startswith("/admin/templates?")
    assert "dup=1" in loc
    assert "dup_day=2" in loc
    assert "dup_start=14:00" in loc
    assert "prefill_from=some-other-tid" in loc
    # Still only one template (no duplicate created).
    assert len(list(db.list_templates(app.app_id))) == 1


def test_advance_start_simple() -> None:
    """Add minutes to (day, hh:mm), no day-bump when in-bounds."""
    from community_organizer.lambdas import web
    assert web._advance_start(2, "14:00", 60) == (2, "15:00")
    assert web._advance_start(2, "12:45", 45) == (2, "13:30")


def test_advance_start_bumps_day_on_midnight_wrap() -> None:
    """Wed 23:00 + 60 min → Thu 00:00 (day 2 → day 3). Critical for
    overnight adoration schedules."""
    from community_organizer.lambdas import web
    assert web._advance_start(2, "23:00", 60) == (3, "00:00")
    # Multi-day wrap from Sun: Sun 23:30 + 60 → Mon 00:30 (6 → 0).
    assert web._advance_start(6, "23:30", 60) == (0, "00:30")
    # Sun 00:00 + (48 * 60 + 30) min = Tue 00:30 (6 → 1).
    assert web._advance_start(6, "00:00", 48 * 60 + 30) == (1, "00:30")


def test_template_form_uses_app_defaults_when_no_prior_template(ddb_table) -> None:
    """First Add-form lands with app defaults pre-filled where set,
    and hardcoded fallbacks where the app has no default."""
    from community_organizer.core.models import Application
    from community_organizer.lambdas import web

    app = Application(
        community_id="c1", name="Adoration",
        app_type="recurring_commitments", period_type="weekly",
        template_default_day_of_week=2,           # Wed
        template_default_start_time="12:45",
        template_default_duration_minutes=60,
        template_default_required_volunteers=1,
        template_default_min_volunteers=1,
        # arrival + max left None → hardcoded fallback kicks in.
    )
    html_out = web._template_form(
        action="/api/templates/add", app=app)
    # Confirmed app defaults flowed in.
    assert "value='12:45'" in html_out
    assert "value='60'" in html_out
    assert "value='1'" in html_out
    # day_of_week=2 → selected option for Wed
    assert "value='2' selected" in html_out
    # Arrival left null on the app → hardcoded fallback (10).
    assert "value='10'" in html_out
    # Max left null on the app → hardcoded fallback (5).
    assert "value='5'" in html_out


def test_template_form_prefill_advances_start_and_copies_other_fields(ddb_table) -> None:
    """Successive-add: prior template's start advances by its
    duration, and the rest of its fields carry forward."""
    from community_organizer.core.models import Application, SlotTemplate
    from community_organizer.lambdas import web

    app = Application(community_id="c1", name="A",
                      app_type="recurring_commitments",
                      period_type="weekly")
    prior = SlotTemplate(
        community_id="c1", app_id="a1", name="Wed 2 PM",
        day_of_week=2, start_time="14:00", duration_minutes=60,
        required_volunteers=1, min_volunteers=1,
        arrival_offset_minutes=0, max_volunteers=None,
    )
    html_out = web._template_form(
        action="/api/templates/add", app=app, prefill_from=prior)
    # Next slot starts at 15:00 (14:00 + 60).
    assert "value='15:00'" in html_out
    # Day stays Wed (no midnight wrap).
    assert "value='2' selected" in html_out
    # Duration carries.
    assert "value='60'" in html_out
    # max=None carries forward as blank.
    assert "value=''" in html_out


def test_template_form_prefill_wraps_day_at_midnight(ddb_table) -> None:
    """Wed 23:00 (60 min) → next form is Thu 00:00."""
    from community_organizer.core.models import SlotTemplate
    from community_organizer.lambdas import web

    prior = SlotTemplate(
        community_id="c1", app_id="a1", name="Wed 11 PM",
        day_of_week=2, start_time="23:00", duration_minutes=60,
        required_volunteers=1, min_volunteers=1,
        arrival_offset_minutes=0,
    )
    html_out = web._template_form(
        action="/api/templates/add", prefill_from=prior)
    assert "value='00:00'" in html_out
    assert "value='3' selected" in html_out      # Thu


def test_api_template_add_redirects_with_prefill_from(ddb_table) -> None:
    """Saving a template redirects to /admin/templates?prefill_from=
    <new_id> so the next Add lands pre-populated."""
    from community_organizer.core import db
    from community_organizer.core.models import Application, Community, Membership
    from community_organizer.lambdas import web

    cid = "c1"
    db.put_community(Community(community_id=cid, name="T"))
    app = Application(community_id=cid, name="X",
                      app_type="recurring_commitments",
                      period_type="weekly")
    db.put_application(app)
    user = User(community_id=cid, email="u@example.com", name="U",
                community_role="ca")
    db.put_user(user)
    mem = Membership(community_id=cid, app_id=app.app_id,
                     user_id=user.user_id, app_role="aa")
    db.put_membership(mem)

    event = {
        "rawPath": "/api/templates/add",
        "requestContext": {"http": {"method": "POST"}},
        "queryStringParameters": {
            "day": "2", "start": "14:00", "duration": "60",
            "arrival": "0", "required": "1", "min_vol": "1",
            "max_vol": "",
        },
    }
    resp = web._api_template_add(event, user, None, app, mem)
    assert resp["statusCode"] == 302
    loc = resp["headers"]["Location"]
    assert loc.startswith("/admin/templates?prefill_from=")
    # The id in the redirect matches the just-created template.
    new_tpl = next(iter(db.list_templates(app.app_id)))
    assert new_tpl.template_id in loc
    # max was blank → stored as None (uncapped).
    assert new_tpl.max_volunteers is None


def test_ca_users_page_apps_column_shows_memberships(ddb_table) -> None:
    """Each user's Apps cell lists current memberships with role +
    toggle + remove, and an add form for apps they aren't in yet."""
    from community_organizer.core import db
    from community_organizer.core.models import Membership
    from community_organizer.lambdas import web

    community, ca, mem_user, app_a, app_b = _seed_two_apps(ddb_table)
    # mem_user is in app_a as Member but not in app_b.
    db.put_membership(Membership(community_id=community.community_id,
                                 app_id=app_a.app_id,
                                 user_id=mem_user.user_id,
                                 app_role="member"))

    resp = web._ca_users_page({}, ca, community)
    assert resp["statusCode"] == 200
    body = resp["body"]
    # Existing membership chip: app name + role + toggle + remove.
    assert "Ushers" in body and "Member" in body
    assert "make admin" in body
    # Remove (×) form for the existing membership.
    assert "/api/community-users/remove-membership" in body
    # Add form for app_b — not yet a member.
    assert "/api/community-users/add-membership" in body
    assert f"<option value='{app_b.app_id}'>" in body


def test_api_ca_membership_add(ddb_table) -> None:
    """CA can add a user to any app in the community; role defaults
    to member, can be set to 'aa' via form."""
    from community_organizer.core import db
    from community_organizer.lambdas import web

    community, ca, mem_user, app_a, _ = _seed_two_apps(ddb_table)
    event = {
        "rawPath": "/api/community-users/add-membership",
        "requestContext": {"http": {"method": "POST"}},
        "queryStringParameters": {
            "user_id": mem_user.user_id,
            "target_app_id": app_a.app_id,
            "role": "aa",
        },
    }
    resp = web._api_ca_membership_add(event, ca, community)
    assert resp["statusCode"] == 302
    mem = db.get_membership(app_a.app_id, mem_user.user_id)
    assert mem is not None
    assert mem.app_role == "aa"


def test_api_ca_membership_add_idempotent(ddb_table) -> None:
    """Adding an existing membership doesn't clobber its role —
    re-clicking Add must not silently demote an App Admin to Member."""
    from community_organizer.core import db
    from community_organizer.core.models import Membership
    from community_organizer.lambdas import web

    community, ca, mem_user, app_a, _ = _seed_two_apps(ddb_table)
    db.put_membership(Membership(community_id=community.community_id,
                                 app_id=app_a.app_id,
                                 user_id=mem_user.user_id,
                                 app_role="aa"))
    event = {
        "rawPath": "/api/community-users/add-membership",
        "requestContext": {"http": {"method": "POST"}},
        "queryStringParameters": {
            "user_id": mem_user.user_id,
            "target_app_id": app_a.app_id,
            "role": "member",
        },
    }
    resp = web._api_ca_membership_add(event, ca, community)
    assert resp["statusCode"] == 302
    # Role stays "aa" — idempotent add does not demote.
    assert db.get_membership(app_a.app_id, mem_user.user_id).app_role == "aa"


def test_api_ca_membership_remove(ddb_table) -> None:
    from community_organizer.core import db
    from community_organizer.core.models import Membership
    from community_organizer.lambdas import web

    community, ca, mem_user, app_a, _ = _seed_two_apps(ddb_table)
    db.put_membership(Membership(community_id=community.community_id,
                                 app_id=app_a.app_id,
                                 user_id=mem_user.user_id))
    event = {
        "rawPath": "/api/community-users/remove-membership",
        "requestContext": {"http": {"method": "POST"}},
        "queryStringParameters": {
            "user_id": mem_user.user_id,
            "target_app_id": app_a.app_id,
        },
    }
    resp = web._api_ca_membership_remove(event, ca, community)
    assert resp["statusCode"] == 302
    assert db.get_membership(app_a.app_id, mem_user.user_id) is None


def test_api_ca_membership_toggle(ddb_table) -> None:
    from community_organizer.core import db
    from community_organizer.core.models import Membership
    from community_organizer.lambdas import web

    community, ca, mem_user, app_a, _ = _seed_two_apps(ddb_table)
    db.put_membership(Membership(community_id=community.community_id,
                                 app_id=app_a.app_id,
                                 user_id=mem_user.user_id,
                                 app_role="member"))
    event = {
        "rawPath": "/api/community-users/toggle-membership",
        "requestContext": {"http": {"method": "POST"}},
        "queryStringParameters": {
            "user_id": mem_user.user_id,
            "target_app_id": app_a.app_id,
            "new_role": "aa",
        },
    }
    resp = web._api_ca_membership_toggle(event, ca, community)
    assert resp["statusCode"] == 302
    assert db.get_membership(app_a.app_id, mem_user.user_id).app_role == "aa"


def test_api_ca_membership_blocks_cross_community(ddb_table) -> None:
    """A CA in community A can't manipulate memberships in community B
    by submitting community-B ids. Confirms _ca_membership_args
    enforces the community boundary."""
    from community_organizer.core import db
    from community_organizer.core.models import Application, Community, Membership
    from community_organizer.lambdas import web

    community_a, ca_a, _, _, _ = _seed_two_apps(ddb_table)
    # Build a second community with its own app + user.
    db.put_community(Community(community_id="c2", name="Other"))
    app_b = Application(community_id="c2", name="OtherUshers",
                        app_type="coverage", period_type="monthly")
    db.put_application(app_b)
    user_b = User(community_id="c2", email="b@example.com", name="OtherUser")
    db.put_user(user_b)
    db.put_membership(Membership(community_id="c2", app_id=app_b.app_id,
                                 user_id=user_b.user_id))

    # CA from community 1 tries to remove a membership in community 2.
    event = {
        "rawPath": "/api/community-users/remove-membership",
        "requestContext": {"http": {"method": "POST"}},
        "queryStringParameters": {
            "user_id": user_b.user_id,
            "target_app_id": app_b.app_id,
        },
    }
    resp = web._api_ca_membership_remove(event, ca_a, community_a)
    # Now redirects to /admin/community-users with a styled error banner.
    assert resp["statusCode"] == 302
    assert "error=" in resp["headers"]["Location"]
    # Membership in community 2 is untouched.
    assert db.get_membership(app_b.app_id, user_b.user_id) is not None


def test_ca_route_blocks_plain_member(ddb_table, monkeypatch) -> None:
    """A non-CA/UA user hitting a CA route gets a 403, not a 200.
    Tests the _ca_route gate that sits above per-route gates."""
    from community_organizer import auth as _auth
    from community_organizer.core import db
    from community_organizer.lambdas import web

    community, _, mem_user, _, _ = _seed_two_apps(ddb_table)
    mem_user.cognito_sub = "SUB-MEMBER"
    db.put_user(mem_user)
    monkeypatch.setattr(_auth, "parse_cookies",
                        lambda _e: {_auth.ID_COOKIE: "tok"})
    monkeypatch.setattr(_auth, "verify_id_token",
                        lambda _t: {"sub": "SUB-MEMBER"})

    handler_called = []

    def _handler(*a, **k):
        handler_called.append(True)
        return web._html(200, "<body>nope</body>")

    resp = web._ca_route({"rawPath": "/admin/apps"}, _handler)
    assert resp["statusCode"] == 403
    assert not handler_called


def test_pick_next_draft_prefers_upcoming(ddb_table) -> None:
    """Among draft schedules, prefer the earliest one whose period is
    at or after the current month — the case the reported: today is
    June, June draft exists, July draft exists, pick June."""
    import datetime as _dt
    from community_organizer.core import db
    from community_organizer.core.models import Application, Community, Schedule
    from community_organizer.lambdas import web

    cid = "c-pick"
    db.put_community(Community(community_id=cid, name="P"))
    app = Application(community_id=cid, name="Ushers", app_type="coverage")
    db.put_application(app)
    db.put_schedule(Schedule(community_id=cid, app_id=app.app_id,
                             yyyy_mm="2030-06", state="draft"))
    db.put_schedule(Schedule(community_id=cid, app_id=app.app_id,
                             yyyy_mm="2030-07", state="draft"))

    pick = web._pick_next_draft_schedule(app.app_id,
                                         today=_dt.date(2030, 6, 2))
    assert pick is not None
    assert pick.yyyy_mm == "2030-06"


def test_pick_next_draft_falls_back_to_stale(ddb_table) -> None:
    """If the only drafts are in the past, surface the most recent one
    rather than returning None — the admin probably forgot to publish it
    and seeing the stale month on the page is the cue to investigate."""
    import datetime as _dt
    from community_organizer.core import db
    from community_organizer.core.models import Application, Community, Schedule
    from community_organizer.lambdas import web

    cid = "c-pick-stale"
    db.put_community(Community(community_id=cid, name="P"))
    app = Application(community_id=cid, name="Ushers", app_type="coverage")
    db.put_application(app)
    db.put_schedule(Schedule(community_id=cid, app_id=app.app_id,
                             yyyy_mm="2029-12", state="draft"))
    db.put_schedule(Schedule(community_id=cid, app_id=app.app_id,
                             yyyy_mm="2030-01", state="draft"))

    pick = web._pick_next_draft_schedule(app.app_id,
                                         today=_dt.date(2030, 6, 2))
    assert pick is not None
    assert pick.yyyy_mm == "2030-01"


def test_pick_next_draft_none_when_no_drafts(ddb_table) -> None:
    """No drafts at all → None, which the page renders as a disabled
    radio with explanatory text rather than a misleading default."""
    from community_organizer.core import db
    from community_organizer.core.models import Application, Community, Schedule
    from community_organizer.lambdas import web

    cid = "c-pick-empty"
    db.put_community(Community(community_id=cid, name="P"))
    app = Application(community_id=cid, name="Ushers", app_type="coverage")
    db.put_application(app)
    db.put_schedule(Schedule(community_id=cid, app_id=app.app_id,
                             yyyy_mm="2030-06", state="published"))

    assert web._pick_next_draft_schedule(app.app_id) is None


def test_is_admin_requires_aa_membership_not_just_ca() -> None:
    """#181: per-app admin powers come from Membership.app_role, not
    community_role. A CA who is only a Member in this app sees the
    member view; a CA who has no membership at all is not an admin
    here. CA-wide powers (delete app, manage community users) flow
    through _ca_route which checks community_role directly."""
    from community_organizer.core.models import Membership
    from community_organizer.lambdas import web

    ca = User(community_id="c1", email="ca@example.com", name="CA",
              community_role="ca")
    aa_mem = Membership(community_id="c1", app_id="a1",
                        user_id=ca.user_id, app_role="aa")
    member_mem = Membership(community_id="c1", app_id="a1",
                            user_id=ca.user_id, app_role="member")

    assert web._is_admin(ca, aa_mem) is True
    assert web._is_admin(ca, member_mem) is False
    assert web._is_admin(ca, None) is False
    # Plain member with AA membership is also an admin — role
    # comes from the per-app row.
    plain = User(community_id="c1", email="p@example.com", name="P",
                 community_role="member")
    assert web._is_admin(plain, aa_mem) is True
    assert web._is_admin(plain, member_mem) is False


def test_users_summary_section_scoped_to_app_members(ddb_table) -> None:
    """#185: the home-page Member-management widget renders THIS app's
    members only — count + recent-additions both come from the per-app
    Membership set. Pre-fix the widget showed every community user."""
    from community_organizer.core import db
    from community_organizer.core.models import Application, Community, Membership
    from community_organizer.lambdas import web

    cid = "c-users-summary"
    db.put_community(Community(community_id=cid, name="P"))
    app_a = Application(community_id=cid, name="Test Ushers",
                        app_type="coverage")
    app_b = Application(community_id=cid, name="Other App",
                        app_type="coverage")
    db.put_application(app_a)
    db.put_application(app_b)

    in_app = User(community_id=cid, email="member-a@example.com",
                  name="Member A")
    not_in_app = User(community_id=cid, email="member-b@example.com",
                      name="Member B")
    db.put_user(in_app)
    db.put_user(not_in_app)
    db.put_membership(Membership(community_id=cid, app_id=app_a.app_id,
                                 user_id=in_app.user_id, app_role="member"))
    db.put_membership(Membership(community_id=cid, app_id=app_b.app_id,
                                 user_id=not_in_app.user_id, app_role="member"))

    section = web._users_summary_section(cid, app_a.app_id)
    # Only app_a's member appears — count and listing.
    assert "Member A" in section
    assert "Member B" not in section
    # Count line names "1 users total." (singular grammar isn't pretty,
    # but the count is what we're pinning here).
    assert "1 users total" in section


def test_page_chrome_hides_spinner_on_pageshow() -> None:
    """#186: bfcache restore must hide the loading spinner. Otherwise
    a back-button navigation lands on a cached page with the spinner
    still showing — looks like an indefinite hang. Pin the pageshow
    event handler in the shared chrome."""
    from community_organizer.lambdas import web

    page = web._page("<p>hi</p>")
    assert "pageshow" in page
    assert "loading" in page
    # The handler reads the spinner element and sets display:none, so
    # both substrings should travel together.
    assert "style.display='none'" in page


def test_corner_menu_carries_translucent_background(ddb_table) -> None:
    """#186: floating corner gets a light-gray translucent background
    so it stands out from the page underneath. Pin the rgba styling
    so it can't silently disappear in a future refactor."""
    from community_organizer.core import db
    from community_organizer.core.models import Application, Community
    from community_organizer.lambdas import web

    cid = "corner-bg"
    db.put_community(Community(community_id=cid, name="P"))
    db.put_application(Application(community_id=cid, name="X",
                                   app_type="coverage"))
    u = User(community_id=cid, email="u@example.com", name="U",
             community_role="ca")
    corner = web._build_user_corner(u, db.get_community(cid),
                                    current_app=None)
    assert "rgba(245,245,245" in corner   # light-gray translucent
    assert "border-radius" in corner


def test_route_drops_app_hint_for_non_member(ddb_table, monkeypatch) -> None:
    """#189: a plain member pointing ?app_id= at an app they have no
    Membership in must NOT land on that app's home. Pre-fix the route
    accepted any app in the community and rendered the volunteer view.
    Now the hint is dropped and the route falls through to /launcher
    (if multi-app) or the user's single visible app.

    Setup: community with 2 apps. User is a Member of app_a only.
    They navigate to ?app_id=<app_b>. Because they're only in app_a,
    the route falls through and (multi-app via launcher logic) but
    since they only have ONE visible app, they land on app_a.
    """
    from community_organizer import auth as _auth
    from community_organizer.core import db
    from community_organizer.core.models import Application, Community, Membership
    from community_organizer.lambdas import web

    cid = "c-pivot"
    db.put_community(Community(community_id=cid, name="P"))
    app_a = Application(community_id=cid, name="MyApp",
                        app_type="coverage", period_type="monthly")
    app_b = Application(community_id=cid, name="NotMyApp",
                        app_type="coverage", period_type="monthly")
    db.put_application(app_a)
    db.put_application(app_b)
    u = User(community_id=cid, email="u@example.com", name="U",
             community_role="member", cognito_sub="SUB-MEM")
    db.put_user(u)
    db.put_membership(Membership(community_id=cid, app_id=app_a.app_id,
                                 user_id=u.user_id, app_role="member"))

    monkeypatch.setattr(_auth, "parse_cookies",
                        lambda _e: {_auth.ID_COOKIE: "tok"})
    monkeypatch.setenv("COMMUNITY_ID", cid)
    monkeypatch.setattr(_auth, "verify_id_token",
                        lambda _t: {"sub": "SUB-MEM"})

    captured = {}

    def _handler(event, user, community, app, membership):
        captured["app_id"] = app.app_id
        return web._text(200, "ok")

    # Point app_id at app_b — the one they have no membership in.
    event = {
        "rawPath": "/",
        "queryStringParameters": {"app_id": app_b.app_id},
        "rawQueryString": f"app_id={app_b.app_id}",
    }
    web._route(event, _handler)
    # The handler should have been invoked with app_a (the user's
    # actual app), NOT app_b (the one they tried to hint at).
    assert captured["app_id"] == app_a.app_id
    assert captured["app_id"] != app_b.app_id


def test_route_ca_can_still_pivot_to_any_app(ddb_table, monkeypatch) -> None:
    """#189 fix preserves the CA/UA bypass: a CA can pivot into any
    app via ?app_id= even without a Membership row. Required for
    cross-app roster work — CAs land on /admin/apps then click any
    app to manage it."""
    from community_organizer import auth as _auth
    from community_organizer.core import db
    from community_organizer.core.models import Application, Community
    from community_organizer.lambdas import web

    cid = "c-ca-pivot"
    db.put_community(Community(community_id=cid, name="P"))
    app_a = Application(community_id=cid, name="A",
                        app_type="coverage", period_type="monthly")
    db.put_application(app_a)
    ca = User(community_id=cid, email="ca@example.com", name="CA",
              community_role="ca", cognito_sub="SUB-CA")
    db.put_user(ca)
    # Note: NO Membership row for the CA.

    monkeypatch.setattr(_auth, "parse_cookies",
                        lambda _e: {_auth.ID_COOKIE: "tok"})
    monkeypatch.setenv("COMMUNITY_ID", cid)
    monkeypatch.setattr(_auth, "verify_id_token",
                        lambda _t: {"sub": "SUB-CA"})

    captured = {}

    def _handler(event, user, community, app, membership):
        captured["app_id"] = app.app_id
        captured["membership"] = membership
        return web._text(200, "ok")

    event = {
        "rawPath": "/",
        "queryStringParameters": {"app_id": app_a.app_id},
        "rawQueryString": f"app_id={app_a.app_id}",
    }
    web._route(event, _handler)
    assert captured["app_id"] == app_a.app_id
    # No membership row but the CA is still allowed in.
    assert captured["membership"] is None


def test_home_email_widget_scoped_to_this_app(ddb_table, monkeypatch) -> None:
    """#190: the home-page Email widget rendered for a coverage AA
    must list only THIS app's emails — not the community-wide log.
    Pre-fix, AAs saw subjects (and recipient addresses + member
    names) from unrelated apps' activity."""
    import datetime as _dt
    from community_organizer import auth as _auth
    from community_organizer.core import db
    from community_organizer.core.models import (
        Application, Community, EmailLog, Membership, SlotTemplate,
        Schedule,
    )
    from community_organizer.lambdas import web

    cid = "c-email-widget"
    db.put_community(Community(community_id=cid, name="P"))
    app_a = Application(community_id=cid, name="MyApp",
                        app_type="coverage", period_type="monthly")
    app_b = Application(community_id=cid, name="OtherApp",
                        app_type="coverage", period_type="monthly")
    db.put_application(app_a)
    db.put_application(app_b)

    aa = User(community_id=cid, email="aa@example.com", name="AA",
              community_role="member", cognito_sub="SUB-AA")
    db.put_user(aa)
    db.put_membership(Membership(community_id=cid, app_id=app_a.app_id,
                                 user_id=aa.user_id, app_role="aa"))

    # Seed three email logs: two tagged app_a, one tagged app_b.
    for i, (subject, app_id) in enumerate([
        ("APP-A SUBJECT 1", app_a.app_id),
        ("APP-B SUBJECT (must not surface)", app_b.app_id),
        ("APP-A SUBJECT 2", app_a.app_id),
    ]):
        db.put_email_log(EmailLog(
            community_id=cid, direction="outbound",
            from_addr="x@example.com", to_addr="y@example.com",
            subject=subject, provider="fake", kind="other",
            outcome="accepted", related_app_id=app_id,
            ts=f"2026-06-03T1{i}:00:00+00:00",
        ))

    monkeypatch.setattr(_auth, "parse_cookies",
                        lambda _e: {_auth.ID_COOKIE: "tok"})
    monkeypatch.setenv("COMMUNITY_ID", cid)
    monkeypatch.setattr(_auth, "verify_id_token",
                        lambda _t: {"sub": "SUB-AA"})

    event = {
        "rawPath": "/",
        "queryStringParameters": {"app_id": app_a.app_id},
        "rawQueryString": f"app_id={app_a.app_id}",
    }
    resp = web._route(event, web._home)
    body = resp["body"]
    assert "APP-A SUBJECT 1" in body
    assert "APP-A SUBJECT 2" in body
    # The cross-app subject must NOT leak into the widget.
    assert "APP-B SUBJECT" not in body


def test_emails_page_scoped_to_this_app(ddb_table, monkeypatch) -> None:
    """#190: full /admin/emails page also scopes to the current app.
    Same leak as the home-page widget but on a bigger surface."""
    from community_organizer import auth as _auth
    from community_organizer.core import db
    from community_organizer.core.models import (
        Application, Community, EmailLog, Membership,
    )
    from community_organizer.lambdas import web

    cid = "c-emails-page"
    db.put_community(Community(community_id=cid, name="P"))
    app_a = Application(community_id=cid, name="MyApp",
                        app_type="coverage", period_type="monthly")
    app_b = Application(community_id=cid, name="OtherApp",
                        app_type="coverage", period_type="monthly")
    db.put_application(app_a)
    db.put_application(app_b)
    aa = User(community_id=cid, email="aa@example.com", name="AA",
              community_role="member")
    db.put_user(aa)
    mem = Membership(community_id=cid, app_id=app_a.app_id,
                     user_id=aa.user_id, app_role="aa")
    db.put_membership(mem)
    for i, (subject, app_id) in enumerate([
        ("a-subj-1", app_a.app_id),
        ("b-subj-1 leak check", app_b.app_id),
        ("a-subj-2", app_a.app_id),
    ]):
        db.put_email_log(EmailLog(
            community_id=cid, direction="outbound",
            from_addr="x@example.com", to_addr="y@example.com",
            subject=subject, provider="fake", kind="other",
            outcome="accepted", related_app_id=app_id,
            ts=f"2026-06-03T1{i}:00:00+00:00",
        ))

    resp = web._emails_page({}, aa, db.get_community(cid),
                            app_a, mem)
    body = resp["body"]
    assert "a-subj-1" in body
    assert "a-subj-2" in body
    assert "b-subj-1 leak check" not in body


def test_auth_cookies_host_only_when_cookie_domain_unset(monkeypatch) -> None:
    """COOKIE_DOMAIN unset → cookies emit no Domain= attribute, so
    they're host-only (the pre-2026-06-04 behaviour). Test exercises
    both set_cookie and clear_cookie."""
    from community_organizer import auth

    monkeypatch.delenv("COOKIE_DOMAIN", raising=False)
    set_str = auth.set_cookie("foo", "bar", max_age=3600)
    clear_str = auth.clear_cookie("foo")
    assert "Domain=" not in set_str
    assert "Domain=" not in clear_str
    # Other attributes unchanged.
    assert "HttpOnly" in set_str and "Secure" in set_str
    assert "SameSite=Lax" in set_str
    assert "Max-Age=3600" in set_str
    assert "Max-Age=0" in clear_str


def test_auth_cookies_pick_up_cookie_domain_env(monkeypatch) -> None:
    """COOKIE_DOMAIN=community.example.org → both set_cookie and
    clear_cookie emit `Domain=community.example.org;` so a session at
    one subdomain (prod) flows to another (beta) and vice versa."""
    from community_organizer import auth

    monkeypatch.setenv("COOKIE_DOMAIN", "community.example.org")
    set_str = auth.set_cookie("foo", "bar", max_age=3600)
    clear_str = auth.clear_cookie("foo")
    assert "Domain=community.example.org;" in set_str
    assert "Domain=community.example.org;" in clear_str


def test_clear_cookie_variants_covers_host_only_and_domain(monkeypatch) -> None:
    """With COOKIE_DOMAIN set, clear_cookie_variants must emit BOTH a
    host-only deletion and a domain-scoped one — otherwise a legacy
    host-only cookie (set before 2026-06-04) survives logout."""
    from community_organizer import auth

    monkeypatch.setenv("COOKIE_DOMAIN", "community.example.org")
    out = auth.clear_cookie_variants("scheduler_refresh")
    assert len(out) == 2
    host_only = [c for c in out if "Domain=" not in c]
    domain = [c for c in out if "Domain=community.example.org;" in c]
    assert len(host_only) == 1 and len(domain) == 1
    assert all("Max-Age=0" in c for c in out)


def test_clear_cookie_variants_host_only_when_domain_unset(monkeypatch) -> None:
    """No COOKIE_DOMAIN → a single host-only deletion (nothing to
    double up)."""
    from community_organizer import auth

    monkeypatch.delenv("COOKIE_DOMAIN", raising=False)
    out = auth.clear_cookie_variants("scheduler_refresh")
    assert out == ["scheduler_refresh=; Path=/; HttpOnly; Secure; "
                   "SameSite=Lax; Max-Age=0"]


def test_logout_clears_both_cookie_variants(monkeypatch) -> None:
    """_logout must wipe id, refresh AND active-app cookies under both
    identities so a pre-COOKIE_DOMAIN session can't re-mint itself."""
    monkeypatch.setenv("COOKIE_DOMAIN", "community.example.org")
    from community_organizer.lambdas import web

    resp = web._logout()
    assert resp["statusCode"] == 302
    jar = resp["cookies"]
    for name in ("scheduler_id", "scheduler_refresh", web.ACTIVE_APP_COOKIE):
        variants = [c for c in jar if c.startswith(f"{name}=")]
        assert any("Domain=" not in c for c in variants), name   # host-only
        assert any("Domain=community.example.org;" in c for c in variants), name
    assert all("Max-Age=0" in c for c in jar)
