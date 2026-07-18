from __future__ import annotations

import json

from community_organizer.core import db
from community_organizer.lambdas import web


def test_rum_endpoint_accepts_beacon():
    ev = {"rawPath": "/api/rum",
          "requestContext": {"http": {"method": "POST"}},
          "body": json.dumps({"p": "/", "ttfb": 40, "dcl": 120,
                              "load": 300, "type": "navigate"}),
          "isBase64Encoded": False}
    r = web._api_rum(ev)
    assert r["statusCode"] == 204


def test_rum_endpoint_tolerates_garbage():
    for body in ("", "not json", "x" * 5000, json.dumps({"p": "/"})):
        r = web._api_rum({"body": body, "isBase64Encoded": False})
        assert r["statusCode"] == 204


def test_rum_routed_via_lambda_handler():
    ev = {"rawPath": "/api/rum",
          "requestContext": {"http": {"method": "POST"}},
          "body": json.dumps({"p": "/x", "ttfb": 1}), "isBase64Encoded": False}
    assert web.lambda_handler(ev, None)["statusCode"] == 204


def test_ddb_metrics_helpers():
    db.reset_ddb_metrics()
    assert db.get_ddb_metrics() == (0, 0.0)
    db._ddb_before()
    db._ddb_after()
    n, _ = db.get_ddb_metrics()
    assert n == 1
    db.reset_ddb_metrics()
    assert db.get_ddb_metrics()[0] == 0


def test_warmer_fully_warms_and_returns_ok(ddb_table, monkeypatch):
    # _fetch_jwks makes a network call; stub it so the test stays offline.
    monkeypatch.setattr("community_organizer.auth._fetch_jwks", lambda **k: {"keys": []})
    r = web._warm()
    assert r["statusCode"] == 200 and r["body"] == "warm"
    ev = {"rawPath": "/warmer", "requestContext": {"http": {"method": "GET"}}}
    assert web.lambda_handler(ev, None)["statusCode"] == 200
