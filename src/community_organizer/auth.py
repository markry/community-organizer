"""Cognito OAuth + JWT helpers for the web Lambda."""
from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import time
import urllib.parse
import urllib.request
from http.cookies import SimpleCookie
from typing import Any

from jose import jwt
from jose.utils import base64url_decode

log = logging.getLogger()

USER_POOL_ID = os.environ["USER_POOL_ID"]
USER_POOL_CLIENT_ID = os.environ["USER_POOL_CLIENT_ID"]
COGNITO_DOMAIN = os.environ["COGNITO_DOMAIN"]
REGION = os.environ.get("AWS_REGION", "us-east-1")
DOMAIN_NAME = os.environ["DOMAIN_NAME"]

ISSUER = f"https://cognito-idp.{REGION}.amazonaws.com/{USER_POOL_ID}"
JWKS_URL = f"{ISSUER}/.well-known/jwks.json"
TOKEN_URL = f"https://{COGNITO_DOMAIN}/oauth2/token"
LOGOUT_URL = f"https://{COGNITO_DOMAIN}/logout"
LOGIN_URL = f"https://{COGNITO_DOMAIN}/login"
REDIRECT_URI = f"https://{DOMAIN_NAME}/auth/callback"
POST_LOGOUT_URI = f"https://{DOMAIN_NAME}/"

AUTHORIZE_URL = f"https://{COGNITO_DOMAIN}/oauth2/authorize"

ID_COOKIE = "scheduler_id"
REFRESH_COOKIE = "scheduler_refresh"
# Short-lived cookie used to bind an OAuth authorize -> callback round
# trip to the original browser session. See `new_oauth_state`,
# `validate_oauth_state`, and security fix D1.
OAUTH_STATE_COOKIE = "scheduler_oauth_state"

_jwks_cache: dict[str, Any] | None = None
_jwks_cache_at: float = 0.0
# 1h TTL. Cognito doesn't publish a rotation cadence, but a re-fetch
# every hour bounds the worst-case "verify failures after rotation"
# window without putting meaningful load on the JWKS endpoint
# (one request per warm container per hour). See security fix D3.
_JWKS_TTL_SECONDS = 3600


def _fetch_jwks(*, force: bool = False) -> dict[str, Any]:
    """Fetch (or return the cached) Cognito JWKS document.

    Cache TTL is ``_JWKS_TTL_SECONDS`` (default 1h). Pass
    ``force=True`` from ``verify_id_token`` when a token references
    a ``kid`` not in the cached JWKS — that's the signal that
    Cognito may have rotated keys since our last fetch.
    """
    global _jwks_cache, _jwks_cache_at
    now = time.monotonic()
    if (force
            or _jwks_cache is None
            or (now - _jwks_cache_at) > _JWKS_TTL_SECONDS):
        with urllib.request.urlopen(JWKS_URL, timeout=5) as resp:
            _jwks_cache = json.loads(resp.read())
        _jwks_cache_at = now
    return _jwks_cache


def new_oauth_state() -> str:
    """Generate a random URL-safe state token for OAuth CSRF protection.

    Per RFC 6749 §10.12, the OAuth ``state`` parameter binds the
    authorize request to its callback so an attacker can't trick a
    victim into completing a login flow the attacker started. We
    embed this value in the authorize URL AND stash it in a short-
    lived HttpOnly cookie; on callback we require them to match.
    Without this, an attacker initiates OAuth on their browser,
    obtains a code, then tricks the victim into loading
    ``/auth/callback?code=<attacker_code>`` — the victim is silently
    logged into the **attacker's** account (security fix D1).
    """
    return secrets.token_urlsafe(32)


def validate_oauth_state(state_qs: str | None, state_cookie: str | None) -> bool:
    """Constant-time comparison of the state from the callback URL
    against the value we wrote to the user's cookie.

    Returns False if either is missing/empty. Uses
    ``hmac.compare_digest`` rather than ``==`` to avoid timing side
    channels (very small risk here given the values' entropy, but
    free defense in depth).
    """
    if not state_qs or not state_cookie:
        return False
    return hmac.compare_digest(state_qs, state_cookie)


def state_cookie_value(state: str, *, max_age: int = 600) -> str:
    """Build the Set-Cookie header for the OAuth state cookie.

    ``max_age`` defaults to 10 minutes — enough for slow human sign-in
    but short enough to expire any abandoned state. The cookie shares
    the standard auth-cookie attributes (HttpOnly, Secure, SameSite=Lax).
    """
    return (
        f"{OAUTH_STATE_COOKIE}={state}; Path=/; HttpOnly; Secure; "
        f"SameSite=Lax; Max-Age={max_age}"
    )


def login_redirect_url(state: str) -> str:
    qs = urllib.parse.urlencode({
        "client_id": USER_POOL_CLIENT_ID,
        "response_type": "code",
        "scope": "openid email profile",
        "redirect_uri": REDIRECT_URI,
        "state": state,
    })
    return f"{LOGIN_URL}?{qs}"


def google_signin_url(state: str) -> str:
    qs = urllib.parse.urlencode({
        "client_id": USER_POOL_CLIENT_ID,
        "response_type": "code",
        "scope": "openid email profile",
        "redirect_uri": REDIRECT_URI,
        "identity_provider": "Google",
        "state": state,
    })
    return f"{AUTHORIZE_URL}?{qs}"


def apple_signin_url(state: str) -> str:
    """Build the authorize URL that bounces to Cognito's SignInWithApple
    federation. Mirrors google_signin_url; only the identity_provider
    query parameter differs.

    Apple's flow has one quirk we don't try to handle here: on first
    sign-in Apple POSTs the user's name to the redirect_uri as form
    data (not in the ID token). Cognito's Apple IdP swallows it; we
    don't see it. Users land in the app with their email as their
    display name until they edit their profile. Future improvement:
    pre-sign-up Lambda trigger that splits the name from Apple's
    user-info form post.
    """
    # `scope` must match the UserPoolClient's allowed-o-auth-scopes
    # (openid email profile). Apple's "name" claim arrives via the
    # form_post first-sign-in data, not via an OAuth scope, so we
    # don't include "name" here — Cognito would reject it as
    # invalid_scope.
    qs = urllib.parse.urlencode({
        "client_id": USER_POOL_CLIENT_ID,
        "response_type": "code",
        "scope": "openid email profile",
        "redirect_uri": REDIRECT_URI,
        "identity_provider": "SignInWithApple",
        "state": state,
    })
    return f"{AUTHORIZE_URL}?{qs}"


# Apple's Hide My Email feature relays mail through a per-user random
# alias like xyz@privaterelay.appleid.com. Two-way relay works only for
# clients that support it (Apple Mail on iOS/macOS); for Outlook /
# Gmail / Thunderbolt etc. the user's reply leaves their real account
# with their real From address, which we have no record of, so calendar
# decline / accept replies go silently unprocessed. We reject signup
# with a relay address rather than let users hit that footgun.
APPLE_PRIVATE_RELAY_DOMAIN = "@privaterelay.appleid.com"


def is_apple_private_relay_email(email: str) -> bool:
    """True when an email is one of Apple's Hide My Email aliases."""
    return (email or "").strip().lower().endswith(APPLE_PRIVATE_RELAY_DOMAIN)


def logout_redirect_url() -> str:
    qs = urllib.parse.urlencode({
        "client_id": USER_POOL_CLIENT_ID,
        "logout_uri": POST_LOGOUT_URI,
    })
    return f"{LOGOUT_URL}?{qs}"


def exchange_code(code: str) -> dict[str, Any]:
    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "client_id": USER_POOL_CLIENT_ID,
        "code": code,
        "redirect_uri": REDIRECT_URI,
    }).encode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def verify_id_token(token: str) -> dict[str, Any]:
    headers = jwt.get_unverified_headers(token)
    kid = headers["kid"]
    key = next((k for k in _fetch_jwks()["keys"] if k["kid"] == kid), None)
    if key is None:
        # The cached JWKS doesn't know about this kid. Could be a
        # legitimate Cognito key rotation since we last fetched —
        # force a refresh and try once more before failing closed.
        # See security fix D3.
        log.info("kid %s not in cached JWKS — forcing JWKS refresh", kid)
        key = next((k for k in _fetch_jwks(force=True)["keys"]
                    if k["kid"] == kid), None)
        if key is None:
            raise ValueError("kid not in JWKS")
    claims = jwt.decode(
        token,
        key,
        algorithms=["RS256"],
        audience=USER_POOL_CLIENT_ID,
        issuer=ISSUER,
        options={"verify_at_hash": False},
    )
    if claims.get("token_use") != "id":
        raise ValueError("not an id token")
    return claims


def refresh_tokens(refresh_token: str) -> dict[str, Any] | None:
    """Exchange a refresh_token for new id_token + access_token.

    Returns the token response dict on success, None on failure.
    Cognito refresh grants do not return a new refresh_token.
    """
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "client_id": USER_POOL_CLIENT_ID,
        "refresh_token": refresh_token,
    }).encode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log.warning("refresh_token exchange failed: %s", e)
        return None


def parse_cookies(event: dict) -> dict[str, str]:
    raw = event.get("cookies") or []
    if not raw and "headers" in event:
        cookie_header = event["headers"].get("cookie") or event["headers"].get("Cookie")
        if cookie_header:
            raw = cookie_header.split("; ")
    out: dict[str, str] = {}
    jar: SimpleCookie = SimpleCookie()
    for line in raw:
        try:
            jar.load(line)
        except Exception:
            continue
    for k, morsel in jar.items():
        out[k] = morsel.value
    return out


def _cookie_domain_attr() -> str:
    """Render the ``Domain=…;`` cookie attribute fragment for auth +
    active-app cookies, or an empty string when host-only is desired.

    Read from the ``COOKIE_DOMAIN`` env var. When set (e.g.
    ``community.example.org``), browsers scope the cookie to that host
    AND all subdomains — so a session created at
    ``community.example.org`` flows seamlessly to
    ``beta.community.example.org`` and vice versa. When unset, cookies
    are host-only (the pre-2026-06-04 behavior). Single-host
    deployments leave the env var unset and see no change.
    """
    dom = os.environ.get("COOKIE_DOMAIN", "").strip()
    return f" Domain={dom};" if dom else ""


def set_cookie(name: str, value: str, *, max_age: int) -> str:
    return (
        f"{name}={value}; Path=/;{_cookie_domain_attr()} HttpOnly; "
        f"Secure; SameSite=Lax; Max-Age={max_age}"
    )


def clear_cookie(name: str) -> str:
    return (
        f"{name}=; Path=/;{_cookie_domain_attr()} HttpOnly; Secure; "
        f"SameSite=Lax; Max-Age=0"
    )


def clear_cookie_variants(name: str) -> list[str]:
    """Set-Cookie deletions covering BOTH the domain-scoped and the
    host-only identity of a cookie.

    A cookie stored with ``Domain=community.example.org`` and one stored
    host-only (no Domain attr) are *distinct* entries in the browser's
    jar — a deletion only matches the variant whose Domain attribute
    matches. ``COOKIE_DOMAIN`` was introduced 2026-06-04 to make sessions
    flow across subdomains, so sessions are now domain-scoped; but a
    browser that signed in *before* that change still holds host-only
    ``scheduler_id`` / ``scheduler_refresh`` cookies (the refresh cookie
    lives 30 days). Clearing only the current domain-scoped variant leaves
    the stale host-only cookie behind, the next request re-mints a session
    from it, and logout silently fails — while a fresh private window (no
    legacy cookie) logs out fine. Emitting both deletions removes the
    cookie no matter which identity it was stored under.
    """
    out = [
        f"{name}=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0"
    ]
    dom = os.environ.get("COOKIE_DOMAIN", "").strip()
    if dom:
        out.append(
            f"{name}=; Path=/; Domain={dom}; HttpOnly; Secure; "
            f"SameSite=Lax; Max-Age=0"
        )
    return out
