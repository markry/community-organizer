"""Starting an app's members from another app: the picker ticks that app's
members, the admin adjusts, and only membership carries over."""
from __future__ import annotations

import re
import urllib.parse

from community_organizer.core import db
from community_organizer.core.models import (
    Application, Community, Membership, User,
)
from community_organizer.lambdas import web


def _seed(ddb_table):
    cid = "c1"
    db.put_community(Community(community_id=cid, name="Parish"))
    for aid, name in (("new", "New Group"), ("src", "Old Group"),
                      ("other", "Unrelated")):
        db.put_application(Application(community_id=cid, name=name,
                                       app_type="flexible_event", app_id=aid))
    ca = User(community_id=cid, email="ca@example.com", name="Community Admin",
              community_role="ca")
    aa = User(community_id=cid, email="aa@example.com", name="App Admin")
    ann = User(community_id=cid, email="ann@example.com", name="Ann Able")
    bob = User(community_id=cid, email="bob@example.com", name="Bob Baker")
    quiet = User(community_id=cid, email="q@example.com", name="Quiet Member")
    outsider = User(community_id=cid, email="o@example.com", name="Out Sider")
    for u in (ca, aa, ann, bob, quiet, outsider):
        db.put_user(u)

    def join(app_id, u, **kw):
        db.put_membership(Membership(community_id=cid, app_id=app_id,
                                     user_id=u.user_id, **kw))
    join("new", aa, app_role="aa")
    join("src", aa, app_role="aa")
    join("src", ann, app_role="aa")      # an admin there
    join("src", bob, inactive_since="2026-01-01")
    join("src", quiet, opted_out=True)
    join("other", outsider)
    return cid, ca, aa, ann, bob, quiet, outsider


def _page(u, cid, **params):
    app = db.get_application(cid, "new")
    return web._users_add_many_page(
        {"requestContext": {"http": {"method": "GET"}},
         "queryStringParameters": params},
        u, db.get_community(cid), app, db.get_membership("new", u.user_id))


def _add(u, cid, *picked):
    app = db.get_application(cid, "new")
    return web._api_users_add_many(
        {"rawPath": "/api/users/add-many",
         "requestContext": {"http": {"method": "POST"}},
         "body": urllib.parse.urlencode({"user_id": list(picked)}, doseq=True),
         "isBase64Encoded": False},
        u, db.get_community(cid), app, db.get_membership("new", u.user_id))


def _box(body, user) -> str:
    return re.search(f"<input type='checkbox' name='user_id' "
                     f"value='{user.user_id}'[^>]*>", body).group(0)


def test_choosing_an_app_ticks_its_members(ddb_table) -> None:
    cid, ca, aa, ann, bob, quiet, outsider = _seed(ddb_table)
    body = _page(ca, cid, **{"from": "src"})["body"]
    assert " checked" in _box(body, ann)
    assert " checked" in _box(body, bob)
    assert " checked" not in _box(body, outsider)
    assert f"value='{aa.user_id}'" not in body      # already in the new app
    # The count and buttons start from what is ticked.
    assert "<span class='add-many-n'>2</span>" in body
    assert "class='add-many-go' disabled" not in body


def test_nothing_ticked_until_an_app_is_chosen(ddb_table) -> None:
    cid, ca, *_ , ann, bob, quiet, outsider = _seed(ddb_table)
    body = _page(ca, cid)["body"]
    assert "checked" not in _box(body, ann)
    assert "<span class='add-many-n'>0</span>" in body
    assert "class='add-many-go' disabled" in body
    assert "Old Group" in body and "Unrelated" in body


def test_someone_who_opted_out_is_flagged_not_ticked(ddb_table) -> None:
    cid, ca, aa, ann, bob, quiet, outsider = _seed(ddb_table)
    body = _page(ca, cid, **{"from": "src"})["body"]
    assert " checked" not in _box(body, quiet)
    assert "Opted out of emails from Old Group" in body


def test_only_membership_carries_over(ddb_table) -> None:
    cid, ca, aa, ann, bob, quiet, outsider = _seed(ddb_table)
    _add(ca, cid, ann.user_id, bob.user_id, quiet.user_id)
    got = {u.user_id: db.get_membership("new", u.user_id)
           for u in (ann, bob, quiet)}
    assert got[ann.user_id].app_role == "member"
    assert got[bob.user_id].inactive_since is None
    assert got[quiet.user_id].opted_out is False
    # The source app is untouched.
    assert db.get_membership("src", ann.user_id).app_role == "aa"


def test_plain_admin_sees_only_their_own_apps(ddb_table) -> None:
    cid, ca, aa, ann, bob, quiet, outsider = _seed(ddb_table)
    body = _page(aa, cid)["body"]
    assert "ann@example.com" in body
    assert "o@example.com" not in body
    assert "Unrelated" not in body
    assert "Add members from your other apps" in body
    # An app they don't administer named in the URL ticks nobody.
    body = _page(aa, cid, **{"from": "other"})["body"]
    assert "o@example.com" not in body and " checked" not in _box(body, ann)


def test_plain_admin_cannot_post_someone_outside_their_apps(ddb_table) -> None:
    cid, ca, aa, ann, bob, quiet, outsider = _seed(ddb_table)
    _add(aa, cid, ann.user_id, outsider.user_id)
    assert db.get_membership("new", ann.user_id) is not None
    assert db.get_membership("new", outsider.user_id) is None


def test_a_member_of_the_source_only_cannot_use_it(ddb_table) -> None:
    """Being an ordinary member of the other app gives no access to its
    roster, and neither does being an admin of only this one."""
    cid, ca, aa, ann, bob, quiet, outsider = _seed(ddb_table)
    db.put_membership(Membership(community_id=cid, app_id="new",
                                 user_id=bob.user_id, app_role="aa"))
    assert _page(bob, cid)["statusCode"] == 403
    assert _add(bob, cid, ann.user_id)["statusCode"] == 403
    assert db.get_membership("new", ann.user_id) is None


def test_members_page_offers_it_to_a_plain_admin_with_other_apps(ddb_table) -> None:
    cid, ca, aa, *_ = _seed(ddb_table)
    app = db.get_application(cid, "new")
    body = web._users_page({"requestContext": {"http": {"method": "GET"}}},
                           aa, db.get_community(cid), app,
                           db.get_membership("new", aa.user_id))["body"]
    assert "Add several from your other apps" in body


def _create(**params) -> dict:
    return {"rawPath": "/api/apps/create",
            "requestContext": {"http": {"method": "POST"}},
            "queryStringParameters": {"name": "Supper Club",
                                      "app_type": "flexible_event",
                                      "admin_self": "1", **params}}


def test_create_with_a_source_goes_to_the_ticked_picker(ddb_table) -> None:
    cid, ca, aa, ann, *_ = _seed(ddb_table)
    resp = web._api_app_create(_create(seed_from="src"), ca,
                               db.get_community(cid))
    (new,) = [a for a in db.list_applications(cid) if a.name == "Supper Club"]
    loc = resp["headers"]["Location"]
    assert loc.startswith(f"/admin/users/add-many?app_id={new.app_id}&from=src")
    # Nobody is added until the admin confirms; exit lands in the new app.
    assert {m.user_id for m in db.list_memberships_for_app(new.app_id)} == {
        ca.user_id}
    qs = dict(urllib.parse.parse_qsl(loc.split("?", 1)[1]))
    assert web._safe_next(qs["next"]) == f"/?app_id={new.app_id}"


def test_create_without_a_source_goes_home_as_before(ddb_table) -> None:
    cid, ca, *_ = _seed(ddb_table)
    resp = web._api_app_create(_create(), ca, db.get_community(cid))
    assert resp["headers"]["Location"].startswith("/?app_id=")


def test_create_refuses_an_unknown_source_and_writes_nothing(ddb_table) -> None:
    cid, ca, *_ = _seed(ddb_table)
    resp = web._api_app_create(_create(seed_from="nope"), ca,
                               db.get_community(cid))
    assert "error=" in resp["headers"]["Location"]
    assert not [a for a in db.list_applications(cid)
                if a.name == "Supper Club"]


def _make_new_app_coverage(cid) -> None:
    db.put_application(Application(community_id=cid, name="New Group",
                                   app_type="coverage", app_id="new"))


def test_coverage_app_has_no_start_from(ddb_table) -> None:
    cid, ca, aa, ann, *_ = _seed(ddb_table)
    _make_new_app_coverage(cid)
    db.put_membership(Membership(community_id=cid, app_id="new",
                                 user_id=ca.user_id, app_role="aa"))
    body = _page(ca, cid, **{"from": "src"})["body"]
    assert "Start from the members of" not in body
    assert " checked" not in _box(body, ann)
    assert "<span class='add-many-n'>0</span>" in body


def test_coverage_app_picker_is_as_it_was_for_everyone_else(ddb_table) -> None:
    """A plain admin, and a community admin who doesn't administer the app,
    are still refused there."""
    cid, ca, aa, ann, *_ = _seed(ddb_table)
    _make_new_app_coverage(cid)
    for u in (aa, ca):
        assert _page(u, cid)["statusCode"] == 403
        assert _add(u, cid, ann.user_id)["statusCode"] == 403
    assert db.get_membership("new", ann.user_id) is None
    body = web._users_page({"requestContext": {"http": {"method": "GET"}}},
                           aa, db.get_community(cid),
                           db.get_application(cid, "new"),
                           db.get_membership("new", aa.user_id))["body"]
    assert "Add several from your other apps" not in body


def test_create_refuses_a_source_for_a_coverage_app(ddb_table) -> None:
    cid, ca, *_ = _seed(ddb_table)
    resp = web._api_app_create(_create(seed_from="src", app_type="coverage"),
                               ca, db.get_community(cid))
    assert "error=" in resp["headers"]["Location"]
    assert not [a for a in db.list_applications(cid)
                if a.name == "Supper Club"]


def test_create_form_offers_the_apps(ddb_table) -> None:
    cid, ca, *_ = _seed(ddb_table)
    body = web._ca_landing_page({"rawPath": "/admin/apps"}, ca,
                                db.get_community(cid))["body"]
    assert "name='seed_from'" in body and "Old Group" in body


def test_community_admin_who_is_not_the_app_admin_can_still_use_it(ddb_table) -> None:
    """Creating an app for someone else lands the community admin on the
    picker without an admin role in the new app."""
    cid, ca, aa, ann, *_ = _seed(ddb_table)
    assert _page(ca, cid, **{"from": "src"})["statusCode"] == 200
    _add(ca, cid, ann.user_id)
    assert db.get_membership("new", ann.user_id) is not None
