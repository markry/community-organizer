"""Tests for ``community_organizer.auth`` — focused on the OAuth state-cookie
CSRF protection (security fix D1).

The auth module also has JWT verification and token-exchange logic
that talks to Cognito over the network; those are not unit-tested
here (would need recorded responses + a mock JWKS). What we DO pin:

    - ``new_oauth_state`` returns high-entropy URL-safe values
    - ``validate_oauth_state`` is constant-time, rejects mismatches,
      and rejects missing values fail-closed
    - The state cookie helper produces the expected attributes
    - The authorize URLs carry the supplied state
"""
from __future__ import annotations

import urllib.parse

import pytest

from community_organizer import auth


# ---- new_oauth_state ------------------------------------------------------

def test_new_oauth_state_is_long_url_safe() -> None:
    """Should be a URL-safe (no padding chars problematic for query
    strings) string of substantial entropy — at least 32 bytes worth."""
    s = auth.new_oauth_state()
    # secrets.token_urlsafe(32) yields ~43 base64url chars.
    assert len(s) >= 40
    # URL-safe alphabet only: A-Z a-z 0-9 - _
    assert all(c.isalnum() or c in "-_" for c in s), f"bad char in {s!r}"


def test_new_oauth_state_unique_per_call() -> None:
    """Repeated calls must produce different values."""
    seen = {auth.new_oauth_state() for _ in range(100)}
    assert len(seen) == 100


# ---- validate_oauth_state -------------------------------------------------

def test_validate_oauth_state_match() -> None:
    s = auth.new_oauth_state()
    assert auth.validate_oauth_state(s, s) is True


def test_validate_oauth_state_mismatch() -> None:
    a, b = auth.new_oauth_state(), auth.new_oauth_state()
    assert auth.validate_oauth_state(a, b) is False


@pytest.mark.parametrize("qs, cookie", [
    (None, "x"),       # no state in query
    ("x", None),       # no state cookie
    ("", "x"),         # empty query
    ("x", ""),         # empty cookie
    (None, None),      # both absent — most common attacker case
])
def test_validate_oauth_state_fails_closed_on_missing(qs, cookie) -> None:
    """Any missing/empty side must reject — never allow a callback
    whose state we can't verify (security fix D1)."""
    assert auth.validate_oauth_state(qs, cookie) is False


# ---- state_cookie_value ---------------------------------------------------

def test_state_cookie_value_attributes() -> None:
    """The cookie must be HttpOnly + Secure + SameSite=Lax so it isn't
    readable by JS and isn't sent on cross-site POSTs."""
    s = "abc-state-token"
    cookie = auth.state_cookie_value(s)
    assert cookie.startswith(f"{auth.OAUTH_STATE_COOKIE}={s};")
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=Lax" in cookie
    assert "Path=/" in cookie
    # Default TTL: 10 minutes, short enough that abandoned flows
    # expire quickly but long enough for a slow human.
    assert "Max-Age=600" in cookie


def test_state_cookie_value_custom_max_age() -> None:
    cookie = auth.state_cookie_value("x", max_age=120)
    assert "Max-Age=120" in cookie


# ---- URL builders include state ------------------------------------------

def test_google_signin_url_includes_state() -> None:
    s = auth.new_oauth_state()
    url = auth.google_signin_url(s)
    parsed = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert parsed["state"] == [s]
    # And keeps the Google federation hint.
    assert parsed["identity_provider"] == ["Google"]


def test_login_redirect_url_includes_state() -> None:
    s = auth.new_oauth_state()
    url = auth.login_redirect_url(s)
    parsed = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert parsed["state"] == [s]


# ---- D3: JWKS cache TTL --------------------------------------------------

class _FakeJWKSResp:
    """Context-manager that mimics ``urllib.request.urlopen`` enough
    for ``_fetch_jwks`` to read a JSON payload from it."""

    def __init__(self, payload: dict) -> None:
        import json as _json
        self._body = _json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


@pytest.fixture(autouse=True)
def _reset_jwks_cache(monkeypatch):
    """Reset auth module's JWKS cache between tests."""
    monkeypatch.setattr(auth, "_jwks_cache", None)
    monkeypatch.setattr(auth, "_jwks_cache_at", 0.0)
    yield


def test_fetch_jwks_caches_first_call(monkeypatch) -> None:
    """Two calls within the TTL should fetch over HTTP exactly once."""
    calls = []

    def stub_urlopen(url, timeout=5):
        calls.append(url)
        return _FakeJWKSResp({"keys": [{"kid": "K1"}]})

    monkeypatch.setattr(auth.urllib.request, "urlopen", stub_urlopen)
    auth._fetch_jwks()
    auth._fetch_jwks()
    assert len(calls) == 1


def test_fetch_jwks_refreshes_after_ttl(monkeypatch) -> None:
    """After TTL elapses, the next call must refetch."""
    calls = []

    def stub_urlopen(url, timeout=5):
        calls.append(url)
        return _FakeJWKSResp({"keys": [{"kid": f"K{len(calls)}"}]})

    monkeypatch.setattr(auth.urllib.request, "urlopen", stub_urlopen)

    # Make time.monotonic return values that simulate clock advance.
    t = [1000.0]

    def fake_monotonic():
        return t[0]

    monkeypatch.setattr(auth.time, "monotonic", fake_monotonic)

    auth._fetch_jwks()                                  # call 1
    t[0] += auth._JWKS_TTL_SECONDS / 2
    auth._fetch_jwks()                                  # still cached
    t[0] += auth._JWKS_TTL_SECONDS                       # past TTL
    auth._fetch_jwks()                                  # refetch

    assert len(calls) == 2


def test_fetch_jwks_force_refetches_regardless_of_ttl(monkeypatch) -> None:
    """``force=True`` ignores the TTL and refetches every time."""
    calls = []

    def stub_urlopen(url, timeout=5):
        calls.append(url)
        return _FakeJWKSResp({"keys": []})

    monkeypatch.setattr(auth.urllib.request, "urlopen", stub_urlopen)

    auth._fetch_jwks()
    auth._fetch_jwks(force=True)
    auth._fetch_jwks(force=True)
    assert len(calls) == 3


def test_verify_id_token_force_refreshes_on_kid_miss(monkeypatch) -> None:
    """A token referencing an unknown kid must trigger a single
    JWKS re-fetch before raising — that handles the genuine
    case of Cognito rotating signing keys (security fix D3)."""
    calls = []

    def stub_urlopen(url, timeout=5):
        calls.append(url)
        # First fetch: only OLD-KID. Second fetch (after force):
        # adds NEW-KID so the second lookup succeeds.
        if len(calls) == 1:
            payload = {"keys": [{"kid": "OLD-KID"}]}
        else:
            payload = {"keys": [{"kid": "NEW-KID", "kty": "RSA"}]}
        return _FakeJWKSResp(payload)

    monkeypatch.setattr(auth.urllib.request, "urlopen", stub_urlopen)

    # Stub jose.jwt so we don't need a real signed token.
    monkeypatch.setattr(auth.jwt, "get_unverified_headers",
                        lambda token: {"kid": "NEW-KID"})
    monkeypatch.setattr(auth.jwt, "decode",
                        lambda *a, **k: {"token_use": "id", "sub": "u1"})

    claims = auth.verify_id_token("fake-token")
    assert claims["sub"] == "u1"
    # Exactly two JWKS fetches: initial + forced after kid miss.
    assert len(calls) == 2


def test_verify_id_token_raises_when_kid_still_missing(monkeypatch) -> None:
    """If even the forced re-fetch doesn't surface the kid, raise.
    No silent fail-open."""
    calls = []

    def stub_urlopen(url, timeout=5):
        calls.append(url)
        return _FakeJWKSResp({"keys": [{"kid": "OTHER-KID"}]})

    monkeypatch.setattr(auth.urllib.request, "urlopen", stub_urlopen)
    monkeypatch.setattr(auth.jwt, "get_unverified_headers",
                        lambda token: {"kid": "MISSING-KID"})

    with pytest.raises(ValueError, match="kid not in JWKS"):
        auth.verify_id_token("fake-token")
    # 2 fetches: cached lookup + forced refresh, both miss.
    assert len(calls) == 2
