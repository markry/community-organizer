"""Tests for Sign in with Apple integration (#213).

Two flavors:
* The helper `auth.is_apple_private_relay_email` — pure function,
  case-insensitive, handles None / "" / whitespace.
* `auth.apple_signin_url` — formats the authorize URL with the
  correct identity_provider value Cognito's federation expects.
"""
from __future__ import annotations

import urllib.parse

from community_organizer import auth


# ---- is_apple_private_relay_email ----------------------------------------

def test_relay_email_detected() -> None:
    assert auth.is_apple_private_relay_email(
        "abc123def@privaterelay.appleid.com") is True


def test_relay_email_detected_case_insensitive() -> None:
    assert auth.is_apple_private_relay_email(
        "ABC123DEF@PrivateRelay.AppleID.com") is True


def test_relay_email_detected_with_whitespace() -> None:
    assert auth.is_apple_private_relay_email(
        "  abc@privaterelay.appleid.com  ") is True


def test_real_apple_email_not_flagged() -> None:
    assert auth.is_apple_private_relay_email("user@me.com") is False


def test_gmail_not_flagged() -> None:
    assert auth.is_apple_private_relay_email("user@gmail.com") is False


def test_empty_inputs_not_flagged() -> None:
    assert auth.is_apple_private_relay_email("") is False
    assert auth.is_apple_private_relay_email(None) is False  # type: ignore


def test_substring_not_a_false_positive() -> None:
    """A spoof that puts the relay domain mid-string shouldn't pass,
    but endswith() also shouldn't false-trigger on a sub-portion."""
    # A user genuinely at evil.com whose username contains the relay
    # domain string isn't a relay address.
    assert auth.is_apple_private_relay_email(
        "fake-privaterelay.appleid.com@evil.com") is False


# ---- apple_signin_url ----------------------------------------------------

def test_apple_signin_url_uses_identity_provider_signinwithapple(
        monkeypatch) -> None:
    url = auth.apple_signin_url("state-token-xyz")
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    assert qs.get("identity_provider") == ["SignInWithApple"]
    assert qs.get("state") == ["state-token-xyz"]
    # Should reuse the standard authorize endpoint + client_id.
    assert qs.get("response_type") == ["code"]
    assert qs.get("client_id") == [auth.USER_POOL_CLIENT_ID]
