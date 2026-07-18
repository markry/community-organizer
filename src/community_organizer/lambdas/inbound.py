"""Inbound Lambda — processes calendar DECLINE replies via SES inbound.

When a user declines a calendar invite from their email client, SES
receives the iTIP REPLY, stores it in S3, and invokes this Lambda.
We parse the DECLINE, extract the slot/user from the UID, delete the
assignment, and fire the standard withdrawal notification chain.
"""
from __future__ import annotations

import email
import logging
import os
import re

import boto3

from community_organizer.core import db
from community_organizer.providers.email import get_email_provider

log = logging.getLogger()
log.setLevel(logging.INFO)

COMMUNITY_ID = os.environ.get("COMMUNITY_ID", "")


def _resolve_community_for_user_id(user_id: str) -> str:
    """Find which community owns this user_id when the inbound Lambda
    serves multiple stacks via a shared queue (e.g. prod + beta both
    point their SES receipt rules at this Lambda's prod home).

    Falls back to the env ``COMMUNITY_ID`` when nothing matches — the
    handler will then fail closed with its normal "user not found"
    path, identical to the single-community behavior.
    """
    if not user_id:
        return COMMUNITY_ID
    for community in db.list_communities():
        if db.get_user(community.community_id, user_id):
            return community.community_id
    return COMMUNITY_ID


def _resolve_community_for_email(email: str) -> str:
    """Pick a community for an inbound reply when the sender's email
    is the only routing signal we have.

    Returns:
      - The unique community when exactly one stack knows this email.
      - Env ``COMMUNITY_ID`` (= prod) when the email is in multiple
        communities (e.g. the same address in both). This trade-off is intentional
        — replies from dual-community users route to prod data, and he
        knows to check the other stack manually.
      - Env ``COMMUNITY_ID`` when the email is unknown — caller's
        downstream lookup then rejects, identical to pre-change behavior.
    """
    if not email:
        return COMMUNITY_ID
    hits = db.find_users_by_email_anywhere(email)
    if len(hits) == 1:
        return hits[0][0]
    return COMMUNITY_ID
DOMAIN_NAME = os.environ.get("DOMAIN_NAME", "community.example.org")


def _community_host(community) -> str:
    """Public hostname for email-body URLs scoped to this community.

    Mirrors notifier._community_host. Necessary because inbound runs
    in the prod stack but may handle replies from any community via
    the shared queue — so the prod DOMAIN_NAME env is wrong for a
    another community's tentative re-invite or decline confirmation.

    Falls back to env DOMAIN_NAME when the Community row predates
    the public_url field, so single-community deployments need no
    change.
    """
    if community is not None and getattr(community, "public_url", None):
        return community.public_url
    return DOMAIN_NAME
INBOUND_BUCKET = os.environ.get("INBOUND_BUCKET", "")
FROM_ADDR = f"organizer@{DOMAIN_NAME}"

_s3 = None

def _get_s3():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3")
    return _s3


_UID_RE = re.compile(r"slot-([a-f0-9]+)-([a-f0-9]+)@")

# Hard caps on inbound message parsing — see security fix D15. SES
# inbound is capped at 40 MB by SES itself, but our legitimate
# traffic is tiny (calendar replies, plain-text replies); anything
# above these caps is suspicious or hostile and dropped.
_MAX_INBOUND_BYTES = 1_000_000     # 1 MB
_MAX_INBOUND_PARTS = 50


def lambda_handler(event: dict, context) -> dict:
    records = event.get("Records") or []
    log.info("inbound invoked, records=%d", len(records))
    processed = 0
    for rec in records:
        ses_event = rec.get("ses") or {}
        mail = ses_event.get("mail") or {}
        receipt = ses_event.get("receipt") or {}
        message_id = mail.get("messageId")
        if not message_id:
            log.warning("no messageId in record")
            continue
        # Security: enforce SES SPF/DKIM/DMARC + spam/virus verdicts on
        # any inbound that drives a destructive action (DECLINE deletes
        # an assignment). If any verdict is not PASS, we still forward
        # to admins (low-trust handler) but skip the calendar-action
        # path. (security fix H5).
        verdicts_pass = _ses_verdicts_pass(receipt)
        try:
            if _process_message(message_id, verdicts_pass=verdicts_pass):
                processed += 1
        except Exception:
            log.exception("error processing message %s", message_id)
    return {"ok": True, "processed": processed}


def _ses_verdicts_pass(receipt: dict) -> bool:
    """True iff every SES verdict on this receipt is the literal PASS.

    SES delivers ``receipt.{spfVerdict,dkimVerdict,dmarcVerdict,spamVerdict,virusVerdict}``
    with one of ``PASS|FAIL|GRAY|PROCESSING_FAILED``. Any non-PASS
    value indicates the inbound is forged, suspicious, or unverified,
    and we must not act on it for destructive operations.

    DMARC may be ``GRAY`` if the sending domain doesn't publish a
    record — that's NOT a pass, so we conservatively reject. Operators
    who need to accept GRAY can override here, but the default is
    closed.
    """
    keys = ("spfVerdict", "dkimVerdict", "dmarcVerdict",
            "spamVerdict", "virusVerdict")
    for k in keys:
        status = (receipt.get(k) or {}).get("status")
        if status != "PASS":
            log.warning("inbound: verdict %s=%s, rejecting destructive actions", k, status)
            return False
    return True


def _process_message(message_id: str, *, verdicts_pass: bool = True) -> bool:
    if not INBOUND_BUCKET:
        log.warning("INBOUND_BUCKET not set, skipping")
        return False

    # Defense: messageId comes from the SES event and should be a
    # short alphanumeric. Reject anything that could escape the
    # incoming/ prefix in the S3 key.
    if not re.match(r"^[A-Za-z0-9._-]{1,200}$", message_id or ""):
        log.warning("inbound: rejecting suspicious messageId=%r", message_id)
        return False

    key = f"incoming/{message_id}"
    try:
        resp = _get_s3().get_object(Bucket=INBOUND_BUCKET, Key=key)
        raw = resp["Body"].read()
    except Exception:
        log.exception("failed to read s3://%s/%s", INBOUND_BUCKET, key)
        return False

    # Reject MIME bombs early. SES limits inbound to 40 MB but our
    # legitimate inbound is small (calendar replies, plain-text
    # replies). Anything > _MAX_INBOUND_BYTES is suspicious —
    # truncating the parse window protects against deeply nested
    # multiparts that could DoS the Lambda (security fix D15).
    if len(raw) > _MAX_INBOUND_BYTES:
        log.warning("inbound: %s exceeds %d byte cap (got %d) — dropping",
                    message_id, _MAX_INBOUND_BYTES, len(raw))
        return False

    msg = email.message_from_bytes(raw)
    from_addr = msg.get("From", "")
    log.info("processing inbound from=%s subject=%s", from_addr, msg.get("Subject", ""))

    # Cap the part-walk too. A pathological message could in theory
    # produce a very long ``msg.walk()`` chain; bound it as
    # belt-and-suspenders (D15).
    parts_seen = 0
    for part in msg.walk():
        parts_seen += 1
        if parts_seen > _MAX_INBOUND_PARTS:
            log.warning("inbound: %s exceeded part cap (%d); truncating",
                        message_id, _MAX_INBOUND_PARTS)
            break
        ct = part.get_content_type()
        if ct != "text/calendar":
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        cal_text = payload.decode("utf-8", errors="replace")
        if "METHOD:REPLY" not in cal_text.upper():
            continue
        uid_match = _UID_RE.search(cal_text)
        if not uid_match:
            log.warning("calendar reply but UID doesn't match our format")
            continue

        slot_id = uid_match.group(1)
        user_id = uid_match.group(2)
        cal_upper = cal_text.upper()

        # Destructive actions require: (a) SES verdicts pass, AND
        # (b) the From: header's email matches the user_id from the
        # UID. Otherwise anyone who learns a UID can forge a DECLINE
        # and remove a victim's assignment (security fix H5).
        if not verdicts_pass:
            log.warning("calendar reply rejected: SES verdicts not all PASS "
                        "(slot_id=%s user_id=%s)", slot_id, user_id)
            continue
        if not _from_matches_user(from_addr, user_id):
            log.warning("calendar reply rejected: From=%r does not match user_id=%s",
                        from_addr, user_id)
            continue

        if "PARTSTAT=DECLINED" in cal_upper:
            log.info("calendar DECLINE: slot_id=%s user_id=%s", slot_id, user_id)
            return _handle_decline(slot_id, user_id)
        elif "PARTSTAT=TENTATIVE" in cal_upper:
            log.info("calendar TENTATIVE: slot_id=%s user_id=%s", slot_id, user_id)
            return _handle_tentative(slot_id, user_id)
        elif "PARTSTAT=ACCEPTED" in cal_upper:
            log.info("calendar ACCEPTED: slot_id=%s user_id=%s", slot_id, user_id)
            return _handle_accepted(slot_id, user_id)
        else:
            log.info("calendar reply PARTSTAT not DECLINED/TENTATIVE/"
                     "ACCEPTED, ignoring")
            continue

    log.info("no actionable calendar part found, considering admin forward")
    return _forward_to_admins(msg, from_addr, verdicts_pass=verdicts_pass)


def _from_matches_user(from_header: str, user_id: str) -> bool:
    """Verify the inbound ``From:`` email belongs to the user named in
    the iCal UID.

    The UID embeds ``user_id``; the calendar reply must come from the
    address we have on file for that user. Otherwise an attacker who
    learns or guesses a UID could send a forged DECLINE/TENTATIVE and
    delete a victim's assignment (security fix H5).

    Returns False on any mismatch, missing user, or unparseable
    address — fail closed.
    """
    from email.utils import parseaddr
    if not from_header or not user_id:
        return False
    _, sender_addr = parseaddr(from_header)
    if not sender_addr or "@" not in sender_addr:
        return False
    community_id = _resolve_community_for_user_id(user_id)
    target = db.get_user(community_id, user_id)
    if not target or not target.email:
        return False
    return sender_addr.strip().lower() == target.email.strip().lower()


def _handle_accepted(slot_id: str, user_id: str) -> bool:
    """A calendar reply with PARTSTAT=ACCEPTED arrived for one of our
    slot+user UIDs. Stamp the Assignment as confirmed via ical_reply.

    No email is sent back to the user — acceptance is the "happy path"
    they already see in their own calendar app. Cohort + admin awareness
    flows through the in-app confirmed-status indicator added in #217.
    """
    community_id = _resolve_community_for_user_id(user_id)
    apps = db.list_applications(community_id)
    for a in apps:
        for sch in db.list_schedules(a.app_id):
            slot = db.find_slot_in_month(a.app_id, sch.yyyy_mm, slot_id)
            if slot is None:
                continue
            confirmed = db.confirm_assignment(
                a.app_id, sch.yyyy_mm, slot_id, user_id,
                via="ical_reply",
            )
            if confirmed:
                log.info(
                    "calendar ACCEPTED confirmed assignment "
                    "app=%s yyyy_mm=%s slot=%s user=%s",
                    a.app_id, sch.yyyy_mm, slot_id, user_id)
            else:
                log.info(
                    "calendar ACCEPTED for slot=%s user=%s found no "
                    "matching assignment row (probably already swapped)",
                    slot_id, user_id)
            return True
    log.info("calendar ACCEPTED: no slot %s found for user %s",
             slot_id, user_id)
    return False


def _handle_tentative(slot_id: str, user_id: str) -> bool:
    community_id = _resolve_community_for_user_id(user_id)
    user = db.get_user(community_id, user_id)
    if not user or not user.email or user.email_undeliverable:
        return False

    apps = db.list_applications(community_id)
    slot = None
    app = None
    yyyy_mm = None
    for a in apps:
        for sch in db.list_schedules(a.app_id):
            s = db.find_slot_in_month(a.app_id, sch.yyyy_mm, slot_id)
            if s:
                slot = s
                app = a
                yyyy_mm = sch.yyyy_mm
                break
        if slot:
            break

    if not slot or not app or not yyyy_mm:
        return False

    community = db.get_community(community_id)
    if not community:
        return False

    when = _fmt_date(slot.local_date)
    tz_name = (app.default_timezone or community.default_timezone
               or "America/New_York")
    from community_organizer.core.ical import make_event_ics
    arrival_text = None
    if slot.arrival_offset_minutes:
        arrival_text = (f"{app.arrival_label or 'please arrive by'} "
                        f"{_fmt_time(_arrival_hhmm(slot))}")
    import time
    event_ics = make_event_ics(
        slot, user.user_id, user.email,
        domain=_community_host(community), community_name=app.name,
        timezone=tz_name, arrival_text=arrival_text,
        uid_suffix=f"-reinvite-{int(time.time())}",
        alarm_minutes=user.calendar_alarm_minutes,
    )
    provider = get_email_provider()
    body = (
        f"Hi {user.name},\n\n"
        f"You responded \"tentative\" to a calendar request, but our "
        f"scheduling system doesn't support tentative responses. Please "
        f"either accept or decline. A new calendar invite is attached "
        f"for your convenience. Our system is not able to update a "
        f"\"tentative\" response with a new request so you may have to "
        f"delete the \"tentative\" calendar item manually.\n\n"
        f"  {slot.name}\n"
        f"  {when} -- {_fmt_time(slot.start_time)}\n\n"
        f"If you'd like to trade for a different slot, please accept "
        f"first, then visit the schedule to request a trade:\n"
        f"  https://{_community_host(community)}/your-schedule\n\n"
        f"If you need to withdraw entirely, you can decline the "
        f"calendar invite or use the withdraw option on the website.\n\n"
        f"-- {app.name if app else community.name}\n"
    )
    provider.send(
        community_id=community.community_id,
        from_addr=FROM_ADDR, to_addr=user.email,
        subject=f"{app.name} -- {slot.name} on {when}",
        body_text=body, kind="change_notification",
        related_user_id=user.user_id,
        related_app_id=app.app_id,
        related_slot_id=slot.slot_id,
        related_yyyy_mm=yyyy_mm,
        ics_content=event_ics,
        ics_attachment_only=True,
    )
    log.info("sent tentative nudge to %s for slot %s", user.email, slot_id)
    return True


def _arrival_hhmm(slot) -> str:
    import datetime as dt
    h, m = (int(x) for x in slot.start_time.split(":"))
    base = dt.datetime(2000, 1, 1, h, m)
    arrival = base - dt.timedelta(minutes=slot.arrival_offset_minutes)
    return f"{arrival.hour:02d}:{arrival.minute:02d}"


# Max characters we'll echo from an inbound body into the forwarded
# admin email. Anything longer is truncated with [...] — protects
# admin inboxes from MIME-bomb amplification and keeps the forwarded
# email a sensible read.
_MAX_FWD_BODY_CHARS = 2000


def _find_thread_origin(msg):
    """Resolve the original outbound email_log row a reply is in
    response to, via the inbound message's In-Reply-To / References
    headers.

    RFC 5322 §3.6.4: a reply MAY include In-Reply-To and References,
    each containing one or more ``<message-id>`` tokens. Outlook,
    Apple Mail, Gmail, and SES outbound all set In-Reply-To when the
    reply is generated by the standard client.

    We check In-Reply-To first (the most specific parent), then walk
    References left-to-right looking for any token whose bare
    message-id matches an outbound row's stored
    `provider_message_id`. The first match wins.

    Returns the matching EmailLog or None.
    """
    candidates: list[str] = []
    irt = (msg.get("In-Reply-To") or "").strip()
    if irt:
        candidates.append(irt)
    refs = (msg.get("References") or "").strip()
    if refs:
        # References is whitespace-separated tokens, each
        # ``<message-id>``. Walk in document order — the leftmost
        # references are the root of the thread; the rightmost is
        # the most recent ancestor. Either form is acceptable for
        # routing, so we accept the first match in either direction.
        candidates.extend(refs.split())

    for raw in candidates:
        token = raw.strip().strip("<>").strip()
        if not token:
            continue
        origin = db.find_email_log_by_provider_message_id(token)
        if origin is not None:
            return origin
    return None


def _is_auto_response(msg) -> bool:
    """True if this inbound looks machine-generated (out-of-office, mailing
    list, bounce, vacation autoresponder). We must never auto-reply to one —
    that's how mail loops start. RFC 3834 (`Auto-Submitted`) plus the common
    de-facto headers cover essentially every autoresponder in the wild.
    """
    auto_sub = (msg.get("Auto-Submitted") or "").strip().lower()
    if auto_sub and auto_sub != "no":
        return True
    prec = (msg.get("Precedence") or "").strip().lower()
    if prec in ("bulk", "list", "junk", "auto_reply"):
        return True
    for h in ("X-Autoreply", "X-Autorespond", "X-Auto-Response-Suppress",
              "List-Id", "List-Unsubscribe"):
        if msg.get(h):
            return True
    return False


def _poll_reply_marker(title: str) -> str:
    """The tail of a poll-invite subject: ``vote on dates for <title>``.
    A genuine reply to the invite carries this in its (Re:) subject, so we
    use it to confirm an inbound reply is really about this poll before we
    auto-nudge — avoids nudging on an unrelated coverage reply."""
    return f"vote on dates for {title}".lower()


def _maybe_nudge_poll_reply(msg, member, community, community_id: str):
    """If a member REPLIED to a poll invite instead of using their link,
    email them back their personal link. Returns the event title we nudged
    about, or None.

    Keyed off (sender is a member) + (member has an open poll + token) +
    (the reply subject matches that poll's invite) — NOT the In-Reply-To
    chain, because SES stores a bare Message-ID that doesn't match the
    ``<id@region.amazonses.com>`` a client quotes back, so header threading
    is unreliable for these simple sends. Bounded to once per (event, user)
    via ``reply_nudged_at`` and skipped for auto-responders, so no loop.
    """
    if not member.email or member.email_undeliverable:
        return None
    if _is_auto_response(msg):
        log.info("inbound: skipping poll nudge — message looks auto-generated")
        return None
    subject = (msg.get("Subject") or "").lower()

    for m in db.list_memberships_for_user(member.user_id):
        app = db.get_application(community_id, m.app_id)
        if app is None or app.app_type != "flexible_event":
            continue
        for evt in db.list_flexible_events(app.app_id):
            if evt.state != "poll":
                continue
            if _poll_reply_marker(evt.title) not in subject:
                continue
            tok = db.get_event_token(app.app_id, evt.event_id, member.user_id)
            if tok is None or tok.revoked or tok.reply_nudged_at:
                continue
            link = f"https://{_community_host(community)}/e/{tok.token}"
            org_name = app.name or (community.name if community else "")
            body = (
                f"Hi {member.name},\n\n"
                f"Thanks for your reply! Just so you know, answers sent by "
                f"email aren't recorded automatically. To make sure your "
                f"dates (and what you're bringing) are counted for "
                f"{evt.title}, please use your personal link:\n\n"
                f"  {link}\n\n"
                f"It only takes a minute — no login needed.\n\n"
                f"Thanks!\n-- {org_name}\n")
            reply_subject = msg.get("Subject") or f"{org_name}: {evt.title}"
            if not reply_subject.lower().startswith("re:"):
                reply_subject = "Re: " + reply_subject
            get_email_provider().send(
                community_id=community_id, from_addr=FROM_ADDR,
                to_addr=member.email, subject=reply_subject,
                body_text=body, kind="event_reply_nudge",
                related_user_id=member.user_id, related_app_id=app.app_id)
            import datetime as _dt
            tok.reply_nudged_at = _dt.datetime.now(
                _dt.timezone.utc).isoformat(timespec="seconds")
            db.put_event_token(tok)
            log.info("inbound: auto-nudged %s to use poll link for event %s",
                     member.email, evt.event_id)
            return evt.title
    return None


def _send_forward_to_app_admins(*, msg, sender_member, apps, community,
                                community_id: str) -> bool:
    """Build the forward body once and fan out to AAs of the given
    apps. Shared by the thread-routed path (single-app) and the
    legacy sender-membership path (one or more apps).

    Returns True iff at least one forward was actually sent.
    """
    safe_sender_name = sender_member.name or sender_member.email
    safe_sender_email = sender_member.email

    # If this is a member replying to a poll invite instead of using their
    # link, auto-remind them (once) with the link. We STILL forward to the
    # AA below so nothing is lost — just add a note that we nudged them.
    nudged_event = _maybe_nudge_poll_reply(
        msg, sender_member, community, community_id)

    subject = msg.get("Subject", "(no subject)")
    if len(subject) > 200:
        subject = subject[:200] + "..."

    body_text = ""
    parts_seen = 0
    for part in msg.walk():
        parts_seen += 1
        if parts_seen > _MAX_INBOUND_PARTS:
            break
        if part.get_content_type() == "text/plain":
            payload = part.get_payload(decode=True)
            if payload:
                body_text = payload.decode("utf-8", errors="replace")
                break
    if not body_text:
        body_text = "(no text content)"
    body_text = body_text.strip()
    if len(body_text) > _MAX_FWD_BODY_CHARS:
        body_text = body_text[:_MAX_FWD_BODY_CHARS] + "\n[...truncated]"

    provider = get_email_provider()
    forwarded = 0
    users_by_id = {u.user_id: u for u in db.list_users(community_id)}
    for app in apps:
        aa_ids = {m.user_id for m in db.list_memberships_for_app(app.app_id)
                  if m.app_role == "aa"}
        for aa_id in aa_ids:
            aa = users_by_id.get(aa_id)
            if not aa or not aa.email or aa.email_undeliverable:
                continue
            if aa.email.lower() == safe_sender_email.lower():
                continue
            nudge_note = (
                f"(We've automatically emailed {safe_sender_name} their "
                f"personal voting link and let them know email replies "
                f"aren't recorded as poll answers.)\n\n"
                if nudged_event else "")
            fwd_body = (
                f"Hi {aa.name},\n\n"
                f"[Forwarded from a member's reply — the body content "
                f"below is from {safe_sender_name}, not from the "
                f"system itself.]\n\n"
                f"{nudge_note}"
                f"{safe_sender_name} replied to a scheduling email:\n\n"
                f"--- Original message ---\n"
                f"From: {safe_sender_name} <{safe_sender_email}>\n"
                f"Subject: {subject}\n\n"
                f"{body_text}\n"
                f"--- End of message ---\n\n"
                f"You can reply directly to {safe_sender_name} "
                f"at {safe_sender_email}.\n\n"
                f"-- {app.name if app else community.name}\n"
            )
            provider.send(
                community_id=community_id,
                from_addr=FROM_ADDR, to_addr=aa.email,
                subject=f"{app.name} -- reply from {safe_sender_name}: {subject}",
                body_text=fwd_body, kind="other",
                related_user_id=aa_id,
                related_app_id=app.app_id,
            )
            forwarded += 1
    log.info("forwarded reply from %s to %d admins", safe_sender_email, forwarded)
    return forwarded > 0


def _forward_to_admins(msg, from_addr: str, *,
                       verdicts_pass: bool = True) -> bool:
    """Forward an inbound reply to App Admins — gated on SES verdicts
    + community-member allowlist (security fix D14).

    Inbound replies to ``organizer@<domain>`` that don't carry a
    calendar action used to be forwarded verbatim to every App Admin.
    That was a fan-out amplifier and a phishing aid: an attacker who
    can mail the address could spoof ``From:`` / display name and pipe
    untrusted text into admin inboxes via a trusted sender (us).

    Now we require:

        1. SES PASS on all of spf / dkim / dmarc / spam / virus. Calls
           with ``verdicts_pass=False`` are dropped at the top.
        2. The parsed ``From`` email matches a community user we
           recognise — i.e. it's a reply from someone already
           registered in this community. We don't trust unknown
           senders. Lookup is case-insensitive.

    For the forward itself:

        - The display name we present to admins is the matched user's
          stored ``name`` field (NOT ``parseaddr(from)[0]``, which is
          attacker-controlled).
        - The body is truncated to ``_MAX_FWD_BODY_CHARS`` and stripped
          of any HTML — we only echo the ``text/plain`` part.
        - A short banner reminds admins that the body content is from
          a member, not from the system itself.

    Returns True if any forward was actually sent, False otherwise.
    """
    if not verdicts_pass:
        log.warning("inbound: forward-to-admins skipped — SES verdicts not all PASS")
        return False

    from email.utils import parseaddr
    _, sender_email_raw = parseaddr(from_addr)
    sender_email = (sender_email_raw or "").strip().lower()
    if not sender_email:
        log.warning("inbound: forward skipped — no parseable sender email")
        return False

    # Thread-aware routing — try to identify the *specific* app this
    # reply is about by following the In-Reply-To / References chain
    # back to the original outbound's email_log row. The row carries
    # `related_app_id` (the app the original message was about) and
    # `community_id` (which stack issued it). When the chain is
    # missing or the lookup fails, we fall back to sender-membership
    # scoping below — the pre-#202 behavior.
    thread_origin = _find_thread_origin(msg)
    if thread_origin is not None and thread_origin.related_app_id:
        community_id = thread_origin.community_id
        community = db.get_community(community_id)
        if not community:
            return False
        target_app = db.get_application(community_id,
                                        thread_origin.related_app_id)
        if target_app is None:
            log.info("inbound: thread origin references missing app %s, "
                     "falling back to membership scoping",
                     thread_origin.related_app_id)
        else:
            # Allowlist still gates: must be a community member.
            member = db.get_user_by_email(community_id, sender_email)
            if member is None:
                log.warning(
                    "inbound: forward skipped — sender %s not a "
                    "member of %s (thread origin's community)",
                    sender_email, community_id)
                return False
            log.info("inbound: routing via thread origin to app %s "
                     "(community=%s)", target_app.name, community_id)
            apps = [target_app]
            all_apps = apps  # narrow downstream usage
            return _send_forward_to_app_admins(
                msg=msg, sender_member=member, apps=apps,
                community=community, community_id=community_id)

    # Cross-community sender resolution — one inbound Lambda may serve
    # multiple stacks (prod + beta share one SES rule). Pick the
    # community whose user list contains this email; if it's in more
    # than one (the dual-community case), env COMMUNITY_ID wins.
    community_id = _resolve_community_for_email(sender_email)
    all_apps = list(db.list_applications(community_id))
    if not all_apps:
        return False
    community = db.get_community(community_id)
    if not community:
        return False

    # Sender allowlist: must be a registered community user. Look up
    # by email; if not found, drop the forward. This is the gate that
    # closes anonymous fan-out.
    member = db.get_user_by_email(community_id, sender_email)
    if member is None:
        log.warning("inbound: forward skipped — sender %s not a community member",
                    sender_email)
        return False

    # Scope the forward to apps the sender is actually a member of.
    # The pre-fix code looped over EVERY app in the community and sent a
    # forward per app — so a reply to a single scheduling email fanned
    # out to all apps' AAs, and any admin who happened to AA multiple
    # apps got the same reply N times (This was observed on 2026-06-03
    # when he and Casey each accepted one invite). Scoping by the
    # sender's memberships ties the forward back to the apps the
    # message could plausibly be about. If they only belong to one app
    # (the common case) that's a clean 1-to-1 routing.
    sender_app_ids = {m.app_id for m in
                      db.list_memberships_for_user(member.user_id)}
    apps = [a for a in all_apps if a.app_id in sender_app_ids]
    if not apps:
        log.info("inbound: forward skipped — sender %s has no app memberships",
                 sender_email)
        return False

    return _send_forward_to_app_admins(
        msg=msg, sender_member=member, apps=apps,
        community=community, community_id=community_id)


def _handle_decline(slot_id: str, user_id: str) -> bool:
    community_id = _resolve_community_for_user_id(user_id)
    user = db.get_user(community_id, user_id)
    if not user:
        log.warning("user %s not found", user_id)
        return False

    apps = db.list_applications(community_id)
    slot = None
    app = None
    yyyy_mm = None
    for a in apps:
        for sch in db.list_schedules(a.app_id):
            s = db.find_slot_in_month(a.app_id, sch.yyyy_mm, slot_id)
            if s:
                slot = s
                app = a
                yyyy_mm = sch.yyyy_mm
                break
        if slot:
            break

    if not slot or not app or not yyyy_mm:
        log.warning("slot %s not found in any schedule", slot_id)
        return False

    asgns = list(db.list_assignments_for_slot(app.app_id, yyyy_mm, slot_id))
    if not any(a.user_id == user_id for a in asgns):
        log.info("user %s not assigned to slot %s, nothing to do", user_id, slot_id)
        return False

    sch = db.get_schedule(app.app_id, yyyy_mm)
    if not sch or sch.state != "published":
        log.info("schedule %s not published, ignoring decline", yyyy_mm)
        return False

    db.delete_assignment(app.app_id, yyyy_mm, slot_id, user_id)
    log.info("deleted assignment: user=%s slot=%s month=%s (calendar decline)",
             user.name, slot_id, yyyy_mm)

    community = db.get_community(community_id)
    if not community:
        return True

    _send_decline_confirmation(user, community, app, slot, yyyy_mm)
    _trigger_withdrawal_notifications(user, community, app, slot, yyyy_mm)
    return True


def _send_decline_confirmation(user, community, app, slot, yyyy_mm):
    if not user.email or user.email_undeliverable:
        return
    provider = get_email_provider()
    when = _fmt_date(slot.local_date)
    body = (
        f"Hi {user.name},\n\n"
        f"You declined the calendar invite, so we've withdrawn you from:\n\n"
        f"  {slot.name}\n"
        f"  {when} -- {_fmt_time(slot.start_time)}\n\n"
        f"If this was accidental, you can sign up again at:\n"
        f"  https://{_community_host(community)}/your-schedule\n\n"
        f"-- {app.name if app else community.name}\n"
    )
    provider.send(
        community_id=community.community_id,
        from_addr=FROM_ADDR, to_addr=user.email,
        subject=f"{app.name} -- withdrawn via calendar: {slot.name} on {when}",
        body_text=body, kind="change_notification",
        related_user_id=user.user_id,
        related_app_id=app.app_id,
        related_slot_id=slot.slot_id,
        related_yyyy_mm=yyyy_mm,
    )


def _trigger_withdrawal_notifications(user, community, app, slot, yyyy_mm):
    remaining = sum(1 for _ in db.list_assignments_for_slot(
        app.app_id, yyyy_mm, slot.slot_id))

    if remaining >= slot.required_volunteers:
        return

    provider = get_email_provider()
    when = _fmt_date(slot.local_date)
    event_type = app.event_noun or "event"
    coverage = _coverage_message(slot, remaining)

    aa_ids = {m.user_id for m in db.list_memberships_for_app(app.app_id)
              if m.app_role == "aa" and m.user_id != user.user_id}
    users_by_id = {u.user_id: u for u in db.list_users(community.community_id)}
    for aa_id in aa_ids:
        aa = users_by_id.get(aa_id)
        if not aa or not aa.email or aa.email_undeliverable:
            continue
        body = (
            f"Hi {aa.name},\n\n"
            f"{user.name} withdrew (via calendar decline):\n\n"
            f"  {slot.name}\n"
            f"  {when} -- {_fmt_time(slot.start_time)}\n\n"
            f"{coverage}\n\n"
            f"View and manage the schedule at:\n"
            f"  https://{_community_host(community)}/schedules/{yyyy_mm}\n\n"
            f"-- {app.name if app else community.name}\n"
        )
        provider.send(
            community_id=community.community_id,
            from_addr=FROM_ADDR, to_addr=aa.email,
            subject=f"{app.name} -- {user.name} withdrew: {slot.name}",
            body_text=body, kind="change_notification",
            related_user_id=aa_id, related_app_id=app.app_id,
            related_slot_id=slot.slot_id, related_yyyy_mm=yyyy_mm,
        )

    cohort = (db.get_cohort_by_template(app.app_id, slot.template_id)
              if slot.template_id != "one-off" else None)
    if not cohort:
        return
    cohort_members = db.list_cohort_members(cohort.cohort_id)
    assigned_ids = {a.user_id for a in db.list_assignments_for_slot(
        app.app_id, yyyy_mm, slot.slot_id)}
    for cm in cohort_members:
        if cm.user_id == user.user_id:
            continue
        vol = users_by_id.get(cm.user_id)
        if not vol or not vol.email or vol.email_undeliverable or vol.channel == "none":
            continue
        in_slot = cm.user_id in assigned_ids
        if in_slot:
            body = (
                f"Hi {vol.name},\n\n"
                f"{user.name} has withdrawn from your upcoming {event_type}:\n\n"
                f"  {slot.name}\n"
                f"  {when} -- {_fmt_time(slot.start_time)}\n\n"
                f"{coverage}\n\n"
                f"-- {app.name if app else community.name}\n"
            )
            subj = f"{app.name} -- {user.name} withdrew from your {event_type}"
        else:
            body = (
                f"Hi {vol.name},\n\n"
                f"A slot has opened up at your usual time:\n\n"
                f"  {slot.name}\n"
                f"  {when} -- {_fmt_time(slot.start_time)}\n\n"
                f"{coverage}\n\n"
                f"Can you help? Sign up at:\n"
                f"  https://{_community_host(community)}/your-schedule\n\n"
                f"-- {app.name if app else community.name}\n"
            )
            subj = f"{app.name} -- opening: {slot.name} on {when}"
        provider.send(
            community_id=community.community_id,
            from_addr=FROM_ADDR, to_addr=vol.email,
            subject=subj, body_text=body, kind="change_notification",
            related_user_id=vol.user_id, related_app_id=app.app_id,
            related_slot_id=slot.slot_id, related_yyyy_mm=yyyy_mm,
        )


def _fmt_time(hhmm: str) -> str:
    h, m = (int(x) for x in hhmm.split(":"))
    suffix = "AM" if h < 12 else "PM"
    h12 = h if 1 <= h <= 12 else (12 if h == 0 else h - 12)
    return f"{h12}:{m:02d} {suffix}"


def _fmt_date(iso_date: str) -> str:
    import datetime as dt
    _MONTH_LABEL = {1: "January", 2: "February", 3: "March", 4: "April",
                    5: "May", 6: "June", 7: "July", 8: "August",
                    9: "September", 10: "October", 11: "November", 12: "December"}
    _DAY_LABEL = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri", 5: "Sat", 6: "Sun"}
    y, mo, d = (int(x) for x in iso_date.split("-"))
    date = dt.date(y, mo, d)
    return f"{_DAY_LABEL[date.weekday()]}, {_MONTH_LABEL[mo]} {d}"


def _coverage_message(slot, remaining: int) -> str:
    if remaining >= slot.required_volunteers:
        return "This event still has full coverage."
    elif remaining >= slot.min_volunteers:
        return (f"Coverage is now {remaining}/{slot.required_volunteers} "
                f"(below the desired level of {slot.required_volunteers}).")
    elif remaining > 0:
        return (f"WARNING: Coverage is now {remaining}/{slot.required_volunteers} "
                f"(at minimum level).")
    else:
        return "URGENT: This event has NO coverage!"
