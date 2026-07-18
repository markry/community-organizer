"""Smoke + multi-verb tests for the D4 sandbox Lambda.

Verifies the handler returns 200 across every HTTP verb we might
reasonably see at the FURL: GET (no body), POST (form body), PUT
(JSON body), DELETE (no body), and OPTIONS (preflight).

Why: the D4 OAC investigation found that GET-only tests aren't
enough — the OAC + Lambda FURL combination has different code
paths for body-bearing requests (POST/PUT) than for body-less
ones, and we shipped a fix that worked end-to-end for GET but
broke admin POSTs in production. A test matrix across verbs
would have caught it.

Local pytest verifies the HANDLER's own behavior across verbs.
The CloudFront + OAC behavior across verbs has to be checked
end-to-end against the deployed site (see scripts/sandbox-verb-
check.sh below); that's separate from this unit test.
"""
from __future__ import annotations

import json

import pytest

from community_organizer.lambdas import sandbox


def _make_event(*, method: str, path: str = "/sandbox/hello",
                qs: str = "", body: str | None = None,
                extra_headers: dict | None = None,
                cookies: list[str] | None = None) -> dict:
    headers = {"host": "example.com"}
    if extra_headers:
        headers.update(extra_headers)
    event = {
        "rawPath": path,
        "rawQueryString": qs,
        "requestContext": {"http": {"method": method, "sourceIp": "1.2.3.4"}},
        "headers": headers,
        "cookies": cookies or [],
    }
    if body is not None:
        event["body"] = body
    return event


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"])
def test_sandbox_handles_all_common_verbs(method: str) -> None:
    """Every verb we might see at the FURL must return 200 with the
    expected marker. If we add a verb-specific code path later,
    this matrix catches anything we forget to handle."""
    body = '{"month":"2026-08"}' if method in ("POST", "PUT", "PATCH") else None
    extra = {"content-type": "application/json"} if body else None
    event = _make_event(method=method, body=body, extra_headers=extra)
    resp = sandbox.lambda_handler(event, None)
    assert resp["statusCode"] == 200, f"{method} failed: {resp}"
    payload = json.loads(resp["body"])
    assert payload["marker"] == "community-organizer-sandbox-v1"
    assert payload["method"] == method


def test_sandbox_get_with_query_string_and_cookies() -> None:
    """GET with query string + cookies — should echo both."""
    event = _make_event(method="GET", qs="x=1&y=2",
                        cookies=["a=1", "b=2"])
    resp = sandbox.lambda_handler(event, None)
    payload = json.loads(resp["body"])
    assert payload["rawQueryString"] == "x=1&y=2"
    assert payload["cookie_count"] == 2


def test_sandbox_post_form_body_is_accepted() -> None:
    """POST with form-encoded body — most common admin POST."""
    event = _make_event(
        method="POST",
        body="month=2026-08&app=ushers",
        extra_headers={"content-type": "application/x-www-form-urlencoded"},
    )
    resp = sandbox.lambda_handler(event, None)
    assert resp["statusCode"] == 200
    payload = json.loads(resp["body"])
    assert payload["method"] == "POST"


def test_sandbox_reports_authorization_header_presence() -> None:
    """If CloudFront forwards Authorization (or OAC adds its own
    signature header), the handler must reflect that — useful for
    debugging the OAC signing flow."""
    event = _make_event(
        method="GET",
        extra_headers={"authorization": "AWS4-HMAC-SHA256 Credential=..."},
    )
    resp = sandbox.lambda_handler(event, None)
    payload = json.loads(resp["body"])
    assert payload["authorization_present_in_headers"] is True


def test_sandbox_reports_no_authorization_when_absent() -> None:
    event = _make_event(method="GET")
    resp = sandbox.lambda_handler(event, None)
    payload = json.loads(resp["body"])
    assert payload["authorization_present_in_headers"] is False
