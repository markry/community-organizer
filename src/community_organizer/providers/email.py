"""EmailProvider — abstraction over outbound mail.

v1 ships SesProvider. M365GraphProvider lands when a deployer needs to
send through their existing tenant's outbox.
"""
from __future__ import annotations

import os
import re
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Protocol

import boto3
from botocore.exceptions import ClientError

from ..core import db
from ..core.models import EmailKind, EmailLog

_RESERVED_TLDS = {"invalid", "test", "example", "localhost"}

# Control characters that must never appear in an RFC 5322 header
# value. CR/LF in any header injects a new header (e.g. Bcc:) and
# could be used to exfiltrate outbound mail.
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _tld(email: str) -> str:
    return email.rsplit("@", 1)[-1].rsplit(".", 1)[-1].lower() if "@" in email else ""


def _excerpt(body: str, n: int = 500) -> str:
    return body if len(body) <= n else body[:n] + "..."


class EmailProvider(Protocol):
    name: str

    def send(
        self,
        *,
        community_id: str,
        from_addr: str,
        to_addr: str,
        subject: str,
        body_text: str,
        body_html: str | None = None,
        kind: EmailKind,
        related_user_id: str | None = None,
        related_app_id: str | None = None,
        related_slot_id: str | None = None,
        related_yyyy_mm: str | None = None,
        ics_content: str | None = None,
        ics_attachment_only: bool = False,
        to_addrs: list[str] | None = None,
    ) -> EmailLog:
        ...


class SesProvider:
    name = "ses"

    def __init__(self, region: str | None = None):
        self._client = boto3.client(
            "sesv2",
            region_name=region or os.environ.get("AWS_REGION", "us-east-1"),
        )

    def send(
        self,
        *,
        community_id: str,
        from_addr: str,
        to_addr: str,
        subject: str,
        body_text: str,
        body_html: str | None = None,
        kind: EmailKind,
        related_user_id: str | None = None,
        related_app_id: str | None = None,
        related_slot_id: str | None = None,
        related_yyyy_mm: str | None = None,
        ics_content: str | None = None,
        ics_attachment_only: bool = False,
        to_addrs: list[str] | None = None,
    ) -> EmailLog:
        recipients = to_addrs if to_addrs else [to_addr]
        log_to = "; ".join(recipients)
        # Check every recipient's TLD, not just the first — a reserved
        # TLD in any position would cause SES to reject the whole
        # message (security fix M6).
        bad_tlds = [r for r in recipients if _tld(r) in _RESERVED_TLDS]
        if bad_tlds:
            log = EmailLog(
                community_id=community_id, direction="outbound",
                from_addr=from_addr, to_addr=to_addr, subject=subject,
                provider=self.name, kind=kind, outcome="rejected_allowlist",
                related_user_id=related_user_id,
                related_app_id=related_app_id,
                related_slot_id=related_slot_id,
                related_yyyy_mm=related_yyyy_mm,
                body_excerpt=_excerpt(body_text),
                error_detail=("refused to send to reserved TLD: "
                              + ", ".join(sorted({_tld(r) for r in bad_tlds}))),
            )
            db.put_email_log(log)
            return log

        log_kwargs = dict(
            community_id=community_id, direction="outbound",
            from_addr=from_addr, to_addr=log_to, subject=subject,
            provider=self.name, kind=kind,
            related_user_id=related_user_id,
            related_app_id=related_app_id,
            related_slot_id=related_slot_id,
            related_yyyy_mm=related_yyyy_mm,
            body_excerpt=_excerpt(body_text),
        )
        try:
            if ics_content:
                resp = self._send_raw(from_addr, recipients, subject,
                                      body_text, ics_content,
                                      ics_attachment_only=ics_attachment_only,
                                      body_html=body_html)
            else:
                body = {"Text": {"Data": body_text, "Charset": "UTF-8"}}
                if body_html:
                    body["Html"] = {"Data": body_html, "Charset": "UTF-8"}
                resp = self._client.send_email(
                    FromEmailAddress=from_addr,
                    Destination={"ToAddresses": recipients},
                    Content={"Simple": {
                        "Subject": {"Data": subject, "Charset": "UTF-8"},
                        "Body": body,
                    }},
                )
            log = EmailLog(
                **log_kwargs,
                provider_message_id=resp.get("MessageId"),
                outcome="accepted",
            )
        except ClientError as e:
            log = EmailLog(
                **log_kwargs,
                outcome="error",
                error_detail=f"{e.response.get('Error', {}).get('Code')}: "
                             f"{e.response.get('Error', {}).get('Message')}",
            )
        db.put_email_log(log)
        return log

    def _send_raw(self, from_addr: str, to_addrs, subject: str,
                  body_text: str, ics_content: str,
                  ics_attachment_only: bool = False,
                  body_html: str | None = None) -> dict:
        if isinstance(to_addrs, str):
            to_addrs = [to_addrs]
        # Strip control characters from every header value — CR/LF in
        # any of these injects a new header (Bcc:, Reply-To:, etc.) on
        # SES SendRawEmail. The web Lambda already sanitizes at the
        # source for known sinks, but defense in depth here keeps every
        # provider call safe (security fix H4).
        _strip = lambda s: _CTRL_RE.sub(" ", s or "")[:998]
        msg = MIMEMultipart("mixed")
        msg["From"] = _strip(from_addr)
        msg["To"] = ", ".join(_strip(a) for a in to_addrs)
        msg["Subject"] = _strip(subject)

        method = "CANCEL" if "METHOD:CANCEL" in ics_content else "REQUEST"

        if ics_attachment_only:
            if body_html:
                alt = MIMEMultipart("alternative")
                alt.attach(MIMEText(body_text, "plain", "utf-8"))
                alt.attach(MIMEText(body_html, "html", "utf-8"))
                msg.attach(alt)
            else:
                msg.attach(MIMEText(body_text, "plain", "utf-8"))
        else:
            alt = MIMEMultipart("alternative")
            alt.attach(MIMEText(body_text, "plain", "utf-8"))
            if body_html:
                alt.attach(MIMEText(body_html, "html", "utf-8"))
            cal = MIMEText(ics_content, "calendar", "utf-8")
            cal.set_param("method", method)
            alt.attach(cal)
            msg.attach(alt)

        attachment = MIMEText(ics_content, "calendar", "utf-8")
        attachment.set_param("method", method)
        attachment.add_header("Content-Disposition", "attachment",
                              filename="event.ics")
        msg.attach(attachment)

        return self._client.send_email(
            FromEmailAddress=from_addr,
            Destination={"ToAddresses": to_addrs},
            Content={"Raw": {"Data": msg.as_string()}},
        )


def get_email_provider() -> EmailProvider:
    name = os.environ.get("EMAIL_PROVIDER", "ses").lower()
    if name == "ses":
        return SesProvider()
    raise ValueError(f"unknown EMAIL_PROVIDER={name!r} (only 'ses' implemented)")
