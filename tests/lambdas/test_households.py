"""Community-level household (spousal/family) management — CA pages."""
from __future__ import annotations

import urllib.parse

from community_organizer.core import db
from community_organizer.core.models import Community, User
from community_organizer.lambdas import web


def _seed(ddb_table):
    cid = "c1"
    db.put_community(Community(community_id=cid, name="Parish"))
    ca = User(community_id=cid, email="ca@example.com", name="CA", community_role="ca")
    a = User(community_id=cid, email="andrew@example.com", name="Alex Sample")
    k = User(community_id=cid, email="kim@example.com", name="Kim Sample")
    kid = User(community_id=cid, email="kid@example.com", name="Sample Kid")
    for u in (ca, a, k, kid):
        db.put_user(u)
    return cid, ca, a, k, kid


def _post(fields: dict) -> dict:
    return {"requestContext": {"http": {"method": "POST"}},
            "body": urllib.parse.urlencode(fields, doseq=True),
            "isBase64Encoded": False}


def _make(cid, ca, *people):
    return web._api_ca_household_make(
        _post({"user_id": [p.user_id for p in people]}), ca,
        db.get_community(cid))


def test_make_and_render(ddb_table) -> None:
    cid, ca, a, k, kid = _seed(ddb_table)
    resp = _make(cid, ca, a, k)
    assert resp["statusCode"] == 302 and "notice=" in resp["headers"]["Location"]
    ua = db.get_user(cid, a.user_id)
    uk = db.get_user(cid, k.user_id)
    assert ua.household_id and ua.household_id == uk.household_id   # shared id
    # Everyone is listed with a checkbox and a search box; each row says who
    # they share a household with, with a remove button.
    body = web._ca_households_page({}, ca, db.get_community(cid))["body"]
    assert f"name='user_id' value='{kid.user_id}'" in body
    assert "hh-rows-q" in body
    assert f"name='unpair_id' value='{a.user_id}'" in body
    assert f"name='unpair_id' value='{kid.user_id}'" not in body
    assert "Make the selected people a household" in body


def test_make_three_at_once(ddb_table) -> None:
    cid, ca, a, k, kid = _seed(ddb_table)
    _make(cid, ca, a, k, kid)
    hids = {db.get_user(cid, u.user_id).household_id for u in (a, k, kid)}
    assert len(hids) == 1 and None not in hids


def test_make_adds_to_an_existing_household(ddb_table) -> None:
    cid, ca, a, k, kid = _seed(ddb_table)
    _make(cid, ca, a, k)
    hid = db.get_user(cid, a.user_id).household_id
    # ticking the kid with one parent (already in a household) joins it
    _make(cid, ca, a, kid)
    assert db.get_user(cid, kid.user_id).household_id == hid


def test_make_refuses_two_different_households(ddb_table) -> None:
    cid, ca, a, k, kid = _seed(ddb_table)
    other = User(community_id=cid, email="other@example.com", name="Other")
    db.put_user(other)
    _make(cid, ca, a, k)
    _make(cid, ca, kid, other)
    before = {u.user_id: db.get_user(cid, u.user_id).household_id
              for u in (a, k, kid, other)}
    resp = _make(cid, ca, a, kid)
    assert "error=" in resp["headers"]["Location"]
    assert before == {u.user_id: db.get_user(cid, u.user_id).household_id
                      for u in (a, k, kid, other)}


def test_make_needs_two_people(ddb_table) -> None:
    cid, ca, a, k, kid = _seed(ddb_table)
    resp = _make(cid, ca, a)
    assert "error=" in resp["headers"]["Location"]
    assert db.get_user(cid, a.user_id).household_id is None


def test_unpair(ddb_table) -> None:
    cid, ca, a, k, kid = _seed(ddb_table)
    _make(cid, ca, a, k)
    web._api_ca_household_unpair(_post({"unpair_id": a.user_id}),
                                 ca, db.get_community(cid))
    assert db.get_user(cid, a.user_id).household_id is None
    assert db.get_user(cid, k.user_id).household_id is not None   # spouse untouched
