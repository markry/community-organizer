"""Regression: a multi-app Community Admin working in CA-mode (no active-app
cookie) must be able to POST app-scoped user-CRUD actions (e.g. editing a
user's name from /admin/community-users). Before the fix, _route() bounced
such API POSTs to /launcher because the CA sees >1 app and has no active-app
cookie — silently dropping the save.
"""
from __future__ import annotations

import urllib.parse

from community_organizer import auth as _auth
from community_organizer.core import db
from community_organizer.core.models import Application, Community, Membership, User
from community_organizer.lambdas import web


def _seed_multi_app_ca(cid):
    db.put_community(Community(community_id=cid, name="Parish"))
    # TWO apps so the CA is "multi-app" (triggers the launcher path).
    db.put_application(Application(community_id=cid, name="Ushers",
                                   app_type="coverage", app_id="ush"))
    db.put_application(Application(community_id=cid, name="Lectors",
                                   app_type="coverage", app_id="lec"))
    ca = User(community_id=cid, email="ca@example.com", name="Boss",
              community_role="ca", cognito_sub="SUB-CA")
    db.put_user(ca)                 # NB: no Membership -> pure CA-mode
    target = User(community_id=cid, email="t@example.com", name="Old Name",
                  community_role="member")
    db.put_user(target)
    return ca, target


def test_ca_user_edit_without_active_app_persists(ddb_table, monkeypatch):
    cid = "c-ca-edit"
    ca, target = _seed_multi_app_ca(cid)
    # Authenticated, but the request carries NO active-app cookie and NO
    # ?app_id= — exactly the CA-mode state on /admin/community-users.
    monkeypatch.setattr(_auth, "parse_cookies",
                        lambda _e: {_auth.ID_COOKIE: "tok"})
    monkeypatch.setattr(_auth, "verify_id_token", lambda _t: {"sub": "SUB-CA"})
    monkeypatch.setenv("COMMUNITY_ID", cid)

    fresh0 = db.get_user(cid, target.user_id)
    event = {
        "rawPath": "/api/users/edit",
        "requestContext": {"http": {"method": "POST"}},
        "body": urllib.parse.urlencode({
            "user_id": target.user_id,
            "version": str(fresh0.version or 0),
            "next": f"/admin/community-users#user-{target.user_id}",
            "name": "New Name",
            "email": target.email,
            "community_role": "member",
        }),
        "isBase64Encoded": False,
    }
    resp = web._route(event, web._api_user_edit)
    # Must NOT be bounced to the launcher; must land back on community-users.
    loc = resp.get("headers", {}).get("Location", "")
    assert loc != "/launcher", "API POST was bounced to the launcher"
    assert "/admin/community-users" in loc
    # And the save actually persisted.
    assert db.get_user(cid, target.user_id).name == "New Name"


def test_ca_page_navigation_still_uses_launcher(ddb_table, monkeypatch):
    """The fix is scoped to /api/* — a plain GET page load with no active
    app for a multi-app user still redirects to the launcher (unchanged)."""
    cid = "c-nav"
    ca, _ = _seed_multi_app_ca(cid)
    monkeypatch.setattr(_auth, "parse_cookies",
                        lambda _e: {_auth.ID_COOKIE: "tok"})
    monkeypatch.setattr(_auth, "verify_id_token", lambda _t: {"sub": "SUB-CA"})
    monkeypatch.setenv("COMMUNITY_ID", cid)

    event = {"rawPath": "/", "requestContext": {"http": {"method": "GET"}}}
    resp = web._route(event, lambda *a, **k: web._text(200, "ok"))
    assert resp.get("headers", {}).get("Location") == "/launcher"
