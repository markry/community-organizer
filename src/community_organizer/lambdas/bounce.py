"""Bounce Lambda — SES bounce + complaint events via SNS.

Parses the SES notification, looks up users by email, marks
email_undeliverable on permanent bounces and complaints, writes EmailLog.
"""
from __future__ import annotations

import json
import logging
import os

from community_organizer.core import db
from community_organizer.core.models import EmailLog

log = logging.getLogger()
log.setLevel(logging.INFO)

COMMUNITY_ID = os.environ.get("COMMUNITY_ID", "")
# The SNS topic we expect feedback to come from. Set in template.yaml
# from the BounceTopic resource's ARN. SAM's SNS event source already
# scopes IAM so only this topic can invoke us, but checking here is
# defense in depth (security fix D16) — if someone later manually
# subscribes this Lambda to a second topic, we still reject those
# messages.
EXPECTED_SNS_TOPIC_ARN = os.environ.get("EXPECTED_SNS_TOPIC_ARN", "")
# Number of distinct complaints before we silence a user's email
# (security fix D17). Permanent bounces still silence immediately —
# those are infrastructure-level signals. Complaints are user
# self-reports and can be mis-clicks; we want some hysteresis before
# disabling delivery.
COMPLAINT_THRESHOLD = int(os.environ.get("COMPLAINT_THRESHOLD", "2"))


def lambda_handler(event: dict, context) -> dict:  # noqa: ARG001
    records = event.get("Records") or []
    log.info("bounce invoked, records=%d", len(records))
    handled = 0
    for record in records:
        # Defense in depth: verify the event source + topic ARN before
        # parsing anything. If the Lambda is ever subscribed to an
        # unexpected SNS topic (manually, in a future refactor, etc.),
        # those messages are rejected (security fix D16).
        if record.get("EventSource") != "aws:sns":
            log.warning("bounce: rejecting non-SNS record (EventSource=%r)",
                        record.get("EventSource"))
            continue
        sns = record.get("Sns") or {}
        topic_arn = sns.get("TopicArn") or ""
        if EXPECTED_SNS_TOPIC_ARN and topic_arn != EXPECTED_SNS_TOPIC_ARN:
            log.warning("bounce: rejecting record from unexpected topic %s "
                        "(expected %s)", topic_arn, EXPECTED_SNS_TOPIC_ARN)
            continue
        message_str = sns.get("Message") or "{}"
        try:
            msg = json.loads(message_str)
        except json.JSONDecodeError:
            log.exception("could not parse SNS Message")
            continue
        handled += _handle(msg)
    return {"ok": True, "handled": handled}


def _handle(msg: dict) -> int:
    ntype = msg.get("notificationType") or msg.get("eventType")
    mail = msg.get("mail") or {}
    from_addr = (mail.get("source") or "").strip()
    headers = mail.get("commonHeaders") or {}
    subject = headers.get("subject", "") if isinstance(headers, dict) else ""

    if ntype == "Bounce":
        return _handle_bounce(msg, mail, from_addr, subject)
    if ntype == "Complaint":
        return _handle_complaint(msg, mail, from_addr, subject)
    if ntype == "Delivery":
        log.info("delivery for %s", mail.get("messageId"))
        return 0
    log.warning("unrecognized SES notification type=%r", ntype)
    return 0


def _handle_bounce(msg: dict, mail: dict, from_addr: str, subject: str) -> int:
    bounce = msg.get("bounce") or {}
    btype = bounce.get("bounceType")
    bsubtype = bounce.get("bounceSubType")
    recipients = bounce.get("bouncedRecipients") or []
    n = 0
    for r in recipients:
        to_addr = (r.get("emailAddress") or "").strip()
        if not to_addr:
            continue
        permanent = btype == "Permanent"
        related_user = db.get_user_by_email(COMMUNITY_ID, to_addr) if COMMUNITY_ID else None
        if permanent and related_user and not related_user.email_undeliverable:
            related_user.email_undeliverable = True
            db.put_user(related_user)
            log.info("marked user %s email_undeliverable=True", related_user.user_id)
        diag = r.get("diagnosticCode") or ""
        db.put_email_log(EmailLog(
            community_id=COMMUNITY_ID,
            direction="inbound",
            from_addr=from_addr,
            to_addr=to_addr,
            subject=subject,
            provider="ses",
            provider_message_id=mail.get("messageId"),
            kind="bounce",
            outcome="bounced",
            related_user_id=(related_user.user_id if related_user else None),
            body_excerpt=f"{btype}/{bsubtype}: {diag}"[:500],
            error_detail=diag or f"{btype}/{bsubtype}",
        ))
        n += 1
    return n


def _handle_complaint(msg: dict, mail: dict, from_addr: str, subject: str) -> int:
    complaint = msg.get("complaint") or {}
    recipients = complaint.get("complainedRecipients") or []
    feedback_type = complaint.get("complaintFeedbackType") or "abuse"
    n = 0
    for r in recipients:
        to_addr = (r.get("emailAddress") or "").strip()
        if not to_addr:
            continue
        related_user = db.get_user_by_email(COMMUNITY_ID, to_addr) if COMMUNITY_ID else None
        if related_user and not related_user.email_undeliverable:
            # Increment the counter; only silence once we've crossed
            # the threshold (security fix D17). Spam/abuse classifications
            # at mail providers can be noisy (forgetful user clicks
            # "this is spam" on a reminder once, etc.) — requiring
            # multiple complaints before silencing reduces false
            # positives. Permanent bounces (handled elsewhere) still
            # silence immediately because they're infrastructure-level
            # signals, not user judgments.
            related_user.complaint_count = (related_user.complaint_count or 0) + 1
            if related_user.complaint_count >= COMPLAINT_THRESHOLD:
                related_user.email_undeliverable = True
                log.info("complaint: user %s reached threshold (%d) — "
                         "marking email_undeliverable=True",
                         related_user.user_id, related_user.complaint_count)
            else:
                log.info("complaint: user %s now at %d/%d complaints — "
                         "below threshold, delivery continues",
                         related_user.user_id, related_user.complaint_count,
                         COMPLAINT_THRESHOLD)
            db.put_user(related_user)
        db.put_email_log(EmailLog(
            community_id=COMMUNITY_ID,
            direction="inbound",
            from_addr=from_addr,
            to_addr=to_addr,
            subject=subject,
            provider="ses",
            provider_message_id=mail.get("messageId"),
            kind="bounce",
            outcome="bounced",
            related_user_id=(related_user.user_id if related_user else None),
            body_excerpt=f"complaint feedback={feedback_type}"[:500],
            error_detail=f"complaint:{feedback_type}",
        ))
        n += 1
    return n
