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
            "body": urllib.parse.urlencode(fields), "isBase64Encoded": False}


def test_pair_and_render(ddb_table) -> None:
    cid, ca, a, k, kid = _seed(ddb_table)
    resp = web._api_ca_household_pair(
        _post({"user_a": a.user_id, "user_b": k.user_id}), ca, db.get_community(cid))
    assert resp["statusCode"] == 302
    ua = db.get_user(cid, a.user_id)
    uk = db.get_user(cid, k.user_id)
    assert ua.household_id and ua.household_id == uk.household_id   # shared id
    # The page groups them under one household.
    body = web._ca_households_page({}, ca, db.get_community(cid))["body"]
    assert "Alex Sample" in body and "Kim Sample" in body
    assert "Households" in body and "Not in a household" in body


def test_pair_adds_third_to_existing_household(ddb_table) -> None:
    cid, ca, a, k, kid = _seed(ddb_table)
    web._api_ca_household_pair(_post({"user_a": a.user_id, "user_b": k.user_id}),
                               ca, db.get_community(cid))
    hid = db.get_user(cid, a.user_id).household_id
    # pairing the kid with Andrew (already in a household) joins that household
    web._api_ca_household_pair(_post({"user_a": a.user_id, "user_b": kid.user_id}),
                               ca, db.get_community(cid))
    assert db.get_user(cid, kid.user_id).household_id == hid


def test_unpair(ddb_table) -> None:
    cid, ca, a, k, kid = _seed(ddb_table)
    web._api_ca_household_pair(_post({"user_a": a.user_id, "user_b": k.user_id}),
                               ca, db.get_community(cid))
    web._api_ca_household_unpair(_post({"user_id": a.user_id}),
                                 ca, db.get_community(cid))
    assert db.get_user(cid, a.user_id).household_id is None
    assert db.get_user(cid, k.user_id).household_id is not None   # spouse untouched


def test_pair_same_person_rejected(ddb_table) -> None:
    cid, ca, a, k, kid = _seed(ddb_table)
    resp = web._api_ca_household_pair(
        _post({"user_a": a.user_id, "user_b": a.user_id}), ca, db.get_community(cid))
    assert "error=" in resp["headers"]["Location"]
    assert db.get_user(cid, a.user_id).household_id is None
