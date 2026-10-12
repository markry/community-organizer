"""Authorization boundaries found by independent review (2026-10-11).

Each test drives the real routing or handler against a moto table; only the
JWT verifier is stubbed, so the user is resolved from the table the way a
signed-in request resolves them.

  - a member who belongs to no app must not be handed one;
  - deleting a user must not reach into another community.
"""
from __future__ import annotations

import os

import pytest

from community_organizer import auth
from community_organizer.core import db
from community_organizer.core.models import (
    Application, Community, Membership, User,
)
from community_organizer.lambdas import web

CID = os.environ["COMMUNITY_ID"]


@pytest.fixture
def world(ddb_table, monkeypatch):
    """One community with two apps; sign-in is whoever `as_user` names."""
    db.put_community(Community(community_id=CID, name="Parish",
                               default_timezone="America/New_York"))
    for app_id, name in (("ush", "Ushers"), ("choir", "Choir")):
        db.put_application(Application(community_id=CID, name=name,
                                       app_type="coverage", app_id=app_id))
    state = {"sub": None}
    monkeypatch.setattr(auth, "verify_id_token",
                        lambda _t: {"sub": state["sub"], "email_verified": True})

    def add(name: str, role: str = "member", apps: tuple = ()) -> User:
        u = User(community_id=CID, email=f"{name}@example.com", name=name,
                 community_role=role, cognito_sub=f"sub-{name}")
        db.put_user(u)
        for a in apps:
            db.put_membership(Membership(community_id=CID, app_id=a,
                                         user_id=u.user_id))
        return u

    def request(user: User, path: str = "/", cookie_app: str | None = None):
        """Route a request as `user`; returns (response, handler_calls)."""
        state["sub"] = user.cognito_sub
        cookies = [f"{auth.ID_COOKIE}=stub"]
        if cookie_app:
            cookies.append(f"{web.ACTIVE_APP_COOKIE}={cookie_app}")
        calls = []

        def handler(event, u, community, app, membership):
            calls.append((u.user_id, app.app_id, membership))
            return {"statusCode": 200, "headers": {}, "body": "ok"}

        resp = web._route({"rawPath": path, "rawQueryString": "",
                           "cookies": cookies,
                           "requestContext": {"http": {"method": "GET"}}},
                          handler)
        return resp, calls

    return add, request


# ---- a member of no app is not handed one ----------------------------------

def test_member_of_no_app_reaches_no_handler(world) -> None:
    add, request = world
    lapsed = add("lapsed")                         # belongs to nothing
    resp, calls = request(lapsed)
    assert resp["statusCode"] == 403 and calls == []
    assert "aren't currently a member of any group" in resp["body"]


def test_removed_member_with_a_stale_app_cookie_is_refused(world) -> None:
    """Removed from their last app while their browser still names it."""
    add, request = world
    m = add("usher", apps=("ush",))
    assert request(m, cookie_app="ush")[1][0][1] == "ush"       # was fine
    db.delete_membership("ush", m.user_id)
    for path in ("/", "/api/assignments/signup", "/your-schedule"):
        resp, calls = request(m, path=path, cookie_app="ush")
        assert resp["statusCode"] == 403 and calls == [], path


def test_member_of_one_app_still_lands_in_it(world) -> None:
    add, request = world
    m = add("singer", apps=("choir",))
    resp, calls = request(m)
    assert resp["statusCode"] == 200
    assert calls[0][1] == "choir" and calls[0][2] is not None


def test_member_cannot_enter_an_app_they_are_not_in(world) -> None:
    add, request = world
    m = add("singer", apps=("choir",))
    resp, calls = request(m, cookie_app="ush")     # hint for the other app
    assert calls[0][1] == "choir"                  # dropped; their own app


@pytest.mark.parametrize("role", ["ca", "ua"])
def test_community_admins_with_no_membership_still_get_in(world, role) -> None:
    """The bootstrap fallback is theirs: they manage apps they don't belong to.
    With several apps a page load goes to the launcher to pick one; an API
    call resolves a default app and runs."""
    add, request = world
    admin = add("admin", role=role)
    resp, calls = request(admin)
    assert resp["statusCode"] == 302
    assert resp["headers"]["Location"] == "/launcher" and calls == []
    resp, calls = request(admin, path="/api/users/edit")
    assert resp["statusCode"] == 200 and len(calls) == 1
    assert calls[0][2] is None


# ---- deleting a user stays inside the community ----------------------------

def _delete(actor: User, target_id: str) -> dict:
    app = db.get_application(CID, "ush")
    return web._api_user_delete(
        {"rawPath": "/api/users/delete",
         "requestContext": {"http": {"method": "POST"}},
         "queryStringParameters": {"user_id": target_id}},
        actor, db.get_community(CID), app, None)


def test_delete_does_not_touch_another_communitys_user(world) -> None:
    add, _request = world
    ca = add("admin", role="ca")
    # A second community sharing the table, with its own app and member.
    db.put_community(Community(community_id="other", name="Elsewhere",
                               default_timezone="America/New_York"))
    db.put_application(Application(community_id="other", name="Their app",
                                   app_type="coverage", app_id="theirs"))
    outsider = User(community_id="other", email="o@example.com", name="Outsider")
    db.put_user(outsider)
    db.put_membership(Membership(community_id="other", app_id="theirs",
                                 user_id=outsider.user_id))

    resp = _delete(ca, outsider.user_id)

    assert resp["statusCode"] == 302 and "error=" in resp["headers"]["Location"]
    assert db.get_membership("theirs", outsider.user_id) is not None
    assert db.get_user("other", outsider.user_id) is not None


def test_delete_of_an_unknown_id_changes_nothing(world) -> None:
    add, _request = world
    ca = add("admin", role="ca")
    resp = _delete(ca, "no-such-user")
    assert resp["statusCode"] == 302 and "error=" in resp["headers"]["Location"]


def test_delete_still_removes_a_user_of_this_community(world) -> None:
    add, _request = world
    ca = add("admin", role="ca")
    gone = add("leaver", apps=("ush", "choir"))
    resp = _delete(ca, gone.user_id)
    assert resp["statusCode"] == 302 and "error=" not in resp["headers"]["Location"]
    assert db.get_user(CID, gone.user_id) is None
    assert db.get_membership("ush", gone.user_id) is None
    assert db.get_membership("choir", gone.user_id) is None
