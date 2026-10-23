"""Members are entered and edited as first + last name, and member lists
sort by surname."""
from __future__ import annotations

from community_organizer.core import db
from community_organizer.core.models import (
    Application, Community, Membership, User,
)
from community_organizer.lambdas import web


def _seed(ddb_table):
    cid = "c1"
    db.put_community(Community(community_id=cid, name="Parish"))
    app = Application(community_id=cid, name="Club",
                      app_type="flexible_event", app_id="a1")
    db.put_application(app)
    ca = User(community_id=cid, email="ca@example.com", name="Ann Zulu",
              first_name="Ann", last_name="Zulu", community_role="ca")
    db.put_user(ca)
    db.put_membership(Membership(community_id=cid, app_id="a1",
                                 user_id=ca.user_id, app_role="aa"))
    return cid, app, ca, db.get_membership("a1", ca.user_id)


def _req(path, **params):
    return {"rawPath": path, "requestContext": {"http": {"method": "POST"}},
            "queryStringParameters": params}


def test_add_stores_first_and_last(ddb_table, monkeypatch) -> None:
    cid, app, ca, mem = _seed(ddb_table)
    monkeypatch.setattr(web, "_create_cognito_user", lambda *a, **k: None)
    web._api_user_add(_req("/api/users/add", email="p@example.com",
                           first_name=" Fr. Pat ", last_name="de Example"),
                      ca, db.get_community(cid), app, mem)
    web._api_ca_user_add(_req("/api/community-users/add", email="q@example.com",
                              first_name="Quinn", last_name="Sample"),
                         ca, db.get_community(cid))
    p = db.get_user_by_email(cid, "p@example.com")
    assert (p.first_name, p.last_name, p.name) == (
        "Fr. Pat", "de Example", "Fr. Pat de Example")
    q = db.get_user_by_email(cid, "q@example.com")
    assert (q.first_name, q.last_name, q.name) == ("Quinn", "Sample",
                                                   "Quinn Sample")


def test_edit_splits_an_old_record_and_saves_both_halves(ddb_table) -> None:
    cid, app, ca, mem = _seed(ddb_table)
    old = User(community_id=cid, email="o@example.com", name="Olive Q. Sample")
    db.put_user(old)
    db.put_membership(Membership(community_id=cid, app_id="a1",
                                 user_id=old.user_id))
    # The edit row offers the guessed split to confirm.
    page = web._users_page(
        {"requestContext": {"http": {"method": "GET"}},
         "queryStringParameters": {"edit": old.user_id}},
        ca, db.get_community(cid), app, mem)["body"]
    assert "name='first_name' value='Olive Q.'" in page
    assert "name='last_name' value='Sample'" in page
    web._api_user_edit(_req("/api/users/edit", user_id=old.user_id,
                            version="0", first_name="Olive",
                            last_name="Quill Sample"),
                       ca, db.get_community(cid), app, mem)
    got = db.get_user(cid, old.user_id)
    assert (got.first_name, got.last_name, got.name) == (
        "Olive", "Quill Sample", "Olive Quill Sample")


def test_member_lists_sort_by_surname(ddb_table) -> None:
    cid, app, ca, mem = _seed(ddb_table)
    for first, last in (("Bea", "Young"), ("Al", "Baker")):
        u = User(community_id=cid, email=f"{first.lower()}@example.com",
                 name=f"{first} {last}", first_name=first, last_name=last)
        db.put_user(u)
        db.put_membership(Membership(community_id=cid, app_id="a1",
                                     user_id=u.user_id))
    get = {"requestContext": {"http": {"method": "GET"}}}
    for body in (
        web._users_page(get, ca, db.get_community(cid), app, mem)["body"],
        web._ca_users_page(get, ca, db.get_community(cid))["body"],
        web._ca_households_page(get, ca, db.get_community(cid))["body"],
    ):
        assert (body.index("Al Baker") < body.index("Bea Young")
                < body.index("Ann Zulu"))
