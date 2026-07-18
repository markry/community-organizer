"""SmsProvider — abstraction over outbound SMS.

v1 ships TwilioProvider (toll-free, US/Canada). SMS is **reminders only** —
every scheduling/calendar message stays email because .ics doesn't work over
SMS (see the notifier dispatch). The Twilio REST call is a plain form-encoded
POST so we add no SDK / native dependency (stdlib urllib only).

Credentials live in SSM (the auth token as a SecureString); the notifier's
IAM role grants ssm:GetParameter on /community-organizer/twilio_* + a ViaService-scoped
kms:Decrypt. Account SID + sender are not secret but are co-located for one
place to rotate.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Protocol

import boto3

from ..core import db
from ..core.models import EmailLog

log = logging.getLogger(__name__)

_SSM_SID = os.environ.get("TWILIO_SID_PARAM", "/community-organizer/twilio_account_sid")
_SSM_TOKEN = os.environ.get("TWILIO_TOKEN_PARAM", "/community-organizer/twilio_auth_token")
_SSM_SENDER = os.environ.get("TWILIO_SENDER_PARAM", "/community-organizer/twilio_sender_phone")
# Optional env override for the sender; otherwise read from SSM at cold start.
_SENDER = os.environ.get("TWILIO_SENDER_PHONE", "")

_TWILIO_API = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"


def _excerpt(body: str, n: int = 500) -> str:
    return body if len(body) <= n else body[:n] + "..."


def to_e164(phone: str | None) -> str | None:
    """Normalize a US phone to E.164 (+1XXXXXXXXXX).

    Accepts '555-555-0100', '(555) 555 0100', '+15555550100', etc.
    Returns None if it can't be confidently normalized (a malformed number
    must not silently send to the wrong destination)."""
    if not phone:
        return None
    p = phone.strip()
    if p.startswith("+"):
        digits = re.sub(r"\D", "", p)
        return "+" + digits if 11 <= len(digits) <= 15 else None
    digits = re.sub(r"\D", "", p)
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return None


class SmsProvider(Protocol):
    name: str

    def send(
        self,
        *,
        community_id: str,
        to_phone: str,
        body: str,
        related_user_id: str | None = None,
        related_app_id: str | None = None,
        related_slot_id: str | None = None,
        related_yyyy_mm: str | None = None,
    ) -> EmailLog:
        ...


class TwilioProvider:
    """Send SMS via Twilio's REST API. Creds fetched once per cold start."""

    name = "twilio"

    def __init__(self) -> None:
        self._sid: str | None = None
        self._token: str | None = None
        self._sender: str = _SENDER

    def _creds(self) -> tuple[str, str]:
        if self._sid is None:
            ssm = boto3.client("ssm")
            self._sid = ssm.get_parameter(Name=_SSM_SID)["Parameter"]["Value"]
            self._token = ssm.get_parameter(
                Name=_SSM_TOKEN, WithDecryption=True)["Parameter"]["Value"]
            if not self._sender:
                self._sender = ssm.get_parameter(
                    Name=_SSM_SENDER)["Parameter"]["Value"]
        return self._sid, self._token  # type: ignore[return-value]

    def send(
        self,
        *,
        community_id: str,
        to_phone: str,
        body: str,
        related_user_id: str | None = None,
        related_app_id: str | None = None,
        related_slot_id: str | None = None,
        related_yyyy_mm: str | None = None,
    ) -> EmailLog:
        log_kwargs = dict(
            community_id=community_id, direction="outbound",
            from_addr=self._sender or "twilio", to_addr=to_phone,
            subject="(sms reminder)", provider=self.name, kind="reminder",
            related_user_id=related_user_id, related_app_id=related_app_id,
            related_slot_id=related_slot_id, related_yyyy_mm=related_yyyy_mm,
            body_excerpt=_excerpt(body),
        )
        e164 = to_e164(to_phone)
        if not e164:
            row = EmailLog(**log_kwargs, outcome="error",
                           error_detail=f"unparseable phone: {to_phone!r}")
            db.put_email_log(row)
            return row
        try:
            sid, token = self._creds()
        except Exception as e:  # SSM/creds failure
            row = EmailLog(**log_kwargs, outcome="error",
                           error_detail=f"creds error: {e}")
            db.put_email_log(row)
            return row
        data = urllib.parse.urlencode(
            {"From": self._sender, "To": e164, "Body": body}).encode()
        req = urllib.request.Request(_TWILIO_API.format(sid=sid), data=data)
        auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
        req.add_header("Authorization", f"Basic {auth}")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                payload = json.load(resp)
            row = EmailLog(**log_kwargs, outcome="accepted",
                           provider_message_id=payload.get("sid"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            row = EmailLog(**log_kwargs, outcome="error",
                           error_detail=f"HTTP {e.code}: {detail}")
        except Exception as e:
            row = EmailLog(**log_kwargs, outcome="error", error_detail=str(e))
        db.put_email_log(row)
        return row


def get_sms_provider() -> SmsProvider | None:
    """Return the configured SMS provider, or None when SMS is off.

    The notifier is the only caller — it treats None as 'don't send SMS'."""
    if os.environ.get("SMS_PROVIDER", "none") == "twilio":
        return TwilioProvider()
    return None
