"""Tests for the Twilio SMS provider + phone normalization."""
from __future__ import annotations

import io
import json

import pytest

from community_organizer.providers import sms


# ---- to_e164 --------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("555-555-0100", "+15555550100"),
    ("(555) 555 0100", "+15555550100"),
    ("5555550100", "+15555550100"),
    ("1-555-555-0100", "+15555550100"),
    ("+15555550100", "+15555550100"),
    ("+44 20 7946 0958", "+442079460958"),
])
def test_to_e164_normalizes(raw, expected):
    assert sms.to_e164(raw) == expected


@pytest.mark.parametrize("bad", [None, "", "12345", "not-a-phone", "555-1212"])
def test_to_e164_rejects_unparseable(bad):
    assert sms.to_e164(bad) is None


# ---- TwilioProvider.send --------------------------------------------------

class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _seed_ssm():
    import boto3
    ssm = boto3.client("ssm", region_name="us-east-1")
    ssm.put_parameter(Name="/community-organizer/twilio_account_sid",
                      Value="ACtest", Type="String")
    ssm.put_parameter(Name="/community-organizer/twilio_auth_token",
                      Value="tok-secret", Type="SecureString")


def test_twilio_send_success(ddb_table, monkeypatch):
    from community_organizer.core import db
    _seed_ssm()
    monkeypatch.setattr(sms, "_SENDER", "+18005550199")

    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["data"] = req.data.decode()
        captured["auth"] = req.headers.get("Authorization")
        return _FakeResp(json.dumps({"sid": "SM123", "status": "queued"}).encode())

    monkeypatch.setattr(sms.urllib.request, "urlopen", fake_urlopen)

    prov = sms.TwilioProvider()
    prov._sender = "+18005550199"
    row = prov.send(community_id="example-community", to_phone="555-555-0100",
                    body="Reminder: ushers Sunday 8 AM", related_user_id="u1")

    assert row.outcome == "accepted"
    assert row.provider_message_id == "SM123"
    assert row.provider == "twilio"
    assert "ACtest/Messages.json" in captured["url"]
    assert "To=%2B15555550100" in captured["data"]      # normalized + URL-encoded
    assert "From=%2B18005550199" in captured["data"]
    assert captured["auth"].startswith("Basic ")


def test_twilio_send_unparseable_phone_no_http(ddb_table, monkeypatch):
    _seed_ssm()
    called = {"n": 0}
    monkeypatch.setattr(sms.urllib.request, "urlopen",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    prov = sms.TwilioProvider()
    row = prov.send(community_id="c", to_phone="bogus", body="x")
    assert row.outcome == "error"
    assert "unparseable" in row.error_detail
    assert called["n"] == 0      # never hit the network


def test_twilio_send_http_error(ddb_table, monkeypatch):
    import urllib.error
    _seed_ssm()
    monkeypatch.setattr(sms, "_SENDER", "+18005550199")

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401,
                                     "Unauthorized", {},
                                     io.BytesIO(b'{"message":"bad creds"}'))

    monkeypatch.setattr(sms.urllib.request, "urlopen", boom)
    prov = sms.TwilioProvider()
    prov._sender = "+18005550199"
    row = prov.send(community_id="c", to_phone="5555550100", body="x")
    assert row.outcome == "error"
    assert "HTTP 401" in row.error_detail


def test_get_sms_provider_off_by_default(monkeypatch):
    monkeypatch.delenv("SMS_PROVIDER", raising=False)
    assert sms.get_sms_provider() is None
    monkeypatch.setenv("SMS_PROVIDER", "none")
    assert sms.get_sms_provider() is None
    monkeypatch.setenv("SMS_PROVIDER", "twilio")
    assert isinstance(sms.get_sms_provider(), sms.TwilioProvider)
