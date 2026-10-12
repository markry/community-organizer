"""Notifier Lambda — DDB stream -> change-notification emails.

On Assignment INSERT/REMOVE in a published Schedule, email the affected
user. Draft schedules are silent. The EventSourceMapping filters on
SK prefix ASGN# so we only see assignment rows.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import re

from boto3.dynamodb.types import TypeDeserializer

from community_organizer.core import db, publishing
from community_organizer.core.ical import make_event_ics, make_occurrence_ics
from community_organizer.core.models import (
    Application, EmailLog, Notification, Slot, StandingOccurrence,
    StandingSeries, User,
)
from community_organizer.providers.email import SesProvider
from community_organizer.providers.sms import get_sms_provider, to_e164

log = logging.getLogger()
log.setLevel(logging.INFO)

COMMUNITY_ID = os.environ.get("COMMUNITY_ID", "")
DOMAIN_NAME = os.environ.get("DOMAIN_NAME", "community.example.org")

# SMS (reminders only). Disabled unless SMS_PROVIDER=twilio. SMS_ALLOWLIST is a
# defense-in-depth gate during rollout: a comma-separated set of user_ids that
# may receive SMS — when non-empty, NO other user gets a text even if their
# channel says "sms"/"both". Empty allowlist = no per-user restriction (rely on
# channel alone). Set to the operator's user_id for the initial single-user
# rollout so a stray channel flag can't text a real member.
SMS_PROVIDER = os.environ.get("SMS_PROVIDER", "none")
SMS_ALLOWLIST = {x.strip() for x in os.environ.get("SMS_ALLOWLIST", "").split(",")
                 if x.strip()}
_sms_provider = None


def _get_sms():
    """Lazily build the SMS provider (None when SMS is off)."""
    global _sms_provider
    if _sms_provider is None:
        _sms_provider = get_sms_provider()
    return _sms_provider


def _sms_allowed(user: User) -> bool:
    """Whether this user should receive an SMS reminder.

    Gated by: SMS turned on, the user opted into sms/both, a phone on file,
    AND (the allowlist is empty OR the user is on it). The allowlist is the
    hard guarantee that only intended recipients get texts during rollout."""
    if SMS_PROVIDER != "twilio":
        return False
    if user.channel not in ("sms", "both"):
        return False
    if not to_e164(user.phone):
        return False
    if SMS_ALLOWLIST and user.user_id not in SMS_ALLOWLIST:
        return False
    return True


def _channels_for(user: User) -> tuple[bool, bool]:
    """Return (want_email, want_sms) for a reminder to this user.

    - email/both: email (if deliverable); both also texts.
    - sms: text only — but fall back to email if SMS is gated/unavailable so a
      reminder is never silently dropped.
    None of this applies to non-reminder mail, which is always email."""
    sms_ok = _sms_allowed(user)
    email_deliverable = bool(user.email) and not user.email_undeliverable
    if user.channel == "sms":
        want_email = email_deliverable and not sms_ok   # fallback only
    else:  # "email" or "both"
        want_email = email_deliverable
    return want_email, sms_ok


def _send_sms_reminder(user: User, text: str, ntf: Notification,
                       slot_id: str | None) -> bool:
    """Best-effort SMS reminder. Returns True on accept. Never raises — an
    SMS failure must not crash the poll or block the email fallback."""
    prov = _get_sms()
    if prov is None:
        return False
    try:
        row = prov.send(
            community_id=ntf.community_id or COMMUNITY_ID,
            to_phone=user.phone,
            body=text,
            related_user_id=user.user_id,
            related_app_id=ntf.app_id,
            related_slot_id=slot_id,
            related_yyyy_mm=ntf.yyyy_mm,
        )
        return row.outcome == "accepted"
    except Exception:
        log.exception("sms reminder send failed for %s", user.user_id)
        return False
# FROM stays community-agnostic — one SES identity (community.example.org)
# serves every community sharing this notifier. Each community's URLs
# come from Community.public_url (see _community_host).
FROM_ADDR = f"organizer@{DOMAIN_NAME}"


def _community_host(community) -> str:
    """Public hostname to use in email links for this community.

    Falls back to the notifier's DOMAIN_NAME env var when the
    Community row predates the public_url field. With one notifier
    serving multiple communities (shared queue), this is what
    routes each user's "release your slot" link to the right stack.
    """
    if community is not None and getattr(community, "public_url", None):
        return community.public_url
    return DOMAIN_NAME

# Strip control characters before composing email headers — see
# providers/email.py _CTRL_RE.
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")

_DAY_LABEL = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri", 5: "Sat", 6: "Sun"}
_MONTH_LABEL = {1: "January", 2: "February", 3: "March", 4: "April",
                5: "May", 6: "June", 7: "July", 8: "August",
                9: "September", 10: "October", 11: "November", 12: "December"}

_deser = TypeDeserializer()
_provider: SesProvider | None = None


def _get_provider() -> SesProvider:
    global _provider
    if _provider is None:
        _provider = SesProvider()
    return _provider


def lambda_handler(event: dict, context) -> dict:  # noqa: ARG001
    source = event.get("source")
    if source == "aws.events":
        return _handle_scheduled()
    records = event.get("Records") or []
    log.info("notifier stream batch, records=%d", len(records))
    n = 0
    for rec in records:
        try:
            if _process(rec):
                n += 1
        except Exception:
            log.exception("error processing record %s", rec.get("eventID"))
    return {"ok": True, "notified": n}


# A reminder that fails to send is retried on the following polls (one a
# minute) for this long after it was due...
_RETRY_WINDOW = dt.timedelta(minutes=30)
# ...and a claim older than this was abandoned by a run that died mid-send.
_STALE_CLAIM = dt.timedelta(minutes=5)


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _event_time(ntf: Notification) -> dt.datetime:
    """When the thing being reminded about begins. Taken from ``event_at``
    (set when the reminder was queued, and refreshed from the slot at send
    time). Only rows queued before that field existed fall back to send_at +
    lead, which is right except across a daylight-saving change."""
    if ntf.event_at:
        return dt.datetime.fromisoformat(ntf.event_at)
    return (dt.datetime.fromisoformat(ntf.send_at)
            + dt.timedelta(minutes=ntf.lead_minutes))


def _settle(ntf: Notification) -> bool:
    """Record ``ntf``'s outcome. A row claimed by a poll is written only if
    that claim still stands (see db.settle_notification); if it does not,
    another run has taken the reminder over and this one's view is dropped.
    A Notification handed in without a claim (direct calls) is just saved."""
    if not ntf.claim_id:
        db.put_notification(ntf)
        return True
    if db.settle_notification(ntf):
        return True
    log.warning("reminder %s: claim no longer ours; outcome %s not recorded",
                ntf.notification_id, ntf.state)
    return False


def _retry_deadline(ntf: Notification) -> dt.datetime:
    """Stop retrying after the retry window, and never after the event has
    begun — a reminder that arrives once it is under way is just noise."""
    return min(dt.datetime.fromisoformat(ntf.send_at) + _RETRY_WINDOW,
               _event_time(ntf))


def _handle_scheduled() -> dict:
    now_dt = _utcnow()
    now = now_dt.isoformat(timespec="seconds")
    log.info("reminder poll, up_to=%s", now)
    # Recover claims abandoned by a run that died between claiming and
    # recording the outcome; they would otherwise sit in_flight for good.
    # The run may have died after the send went out, so this can repeat a
    # reminder: a duplicate is the lesser harm next to a missed one.
    recovered = 0
    for stale in list(db.list_in_flight_notifications()):
        claimed = dt.datetime.fromisoformat(stale.claimed_at or stale.send_at)
        if now_dt - claimed > _STALE_CLAIM and db.release_stale_claim(stale):
            recovered += 1
    if recovered:
        log.warning("released %d abandoned reminder claim(s)", recovered)
    pending = list(db.list_pending_notifications(up_to=now))
    log.info("found %d pending notifications", len(pending))
    sent = 0
    skipped = 0
    for listed in pending:
        # Atomic claim: pending -> in_flight. If another invocation
        # already grabbed this row (or the row moved on for any
        # reason), skip — at most one send per notification
        # (security fix D13).
        # Work from the row the claim returns, never the listed copy: the
        # listing can be a poll old, and would not show a channel that an
        # earlier attempt has already delivered.
        ntf = db.claim_notification_row(
            listed.notification_id, listed.app_id, listed.send_at)
        if ntf is None:
            log.info("notification %s already claimed; skipping",
                     listed.notification_id)
            skipped += 1
            continue
        # We own the row now. _send_reminder settles it: sent, cancelled,
        # failed, or back to pending for the next poll to retry.
        try:
            if _send_reminder(ntf):
                sent += 1
        except Exception:
            log.exception("error sending reminder %s", ntf.notification_id)
            # Whatever blew up, do not leave the claim hanging.
            try:
                _retry_or_give_up(ntf)
            except Exception:
                log.exception("could not settle reminder %s",
                              ntf.notification_id)
    reconciled = None
    if now_dt.minute % _RECONCILE_EVERY_MIN == 0:
        try:
            reconciled = _reconcile_published_months(now_dt)
        except Exception:
            log.exception("reminder reconcile sweep failed")
    return {"ok": True, "polled": len(pending), "sent": sent,
            "skipped": skipped, "recovered": recovered,
            "reconciled": reconciled}


# Every this-many minutes the poll also re-derives each published month's
# reminder queue from its assignments (publishing.reconcile_reminders). The
# stream handler does the same the moment an assignment changes; the sweep is
# what makes that reliable — a stream record that failed, was dropped or
# never came is caught here instead of leaving someone unreminded.
_RECONCILE_EVERY_MIN = 10


def _reconcile_published_months(now_dt: dt.datetime) -> dict:
    """Reconcile reminders for every published month that is not yet over."""
    this_month = now_dt.strftime("%Y-%m")
    totals = {"months": 0, "queued": 0, "cancelled": 0}
    community = db.get_community(COMMUNITY_ID) if COMMUNITY_ID else None
    if community is None:
        return totals
    for app in db.list_applications(COMMUNITY_ID):
        if app.app_type in ("standing_event", "flexible_event"):
            continue                    # no slot reminders
        for sch in db.list_schedules(app.app_id):
            if sch.state != "published" or sch.yyyy_mm < this_month:
                continue
            out = publishing.reconcile_reminders(community, app, sch.yyyy_mm)
            totals["months"] += 1
            totals["queued"] += out["queued"]
            totals["cancelled"] += out["cancelled"]
    if totals["queued"] or totals["cancelled"]:
        log.info("reminder reconcile: %s", totals)
    return totals


def _too_late(ntf: Notification) -> bool:
    """True (and the reminder is closed) if what it is about has begun: a
    long outage, or retries that ran out the clock. Sending now is noise."""
    if _utcnow() < _event_time(ntf):
        return False
    ntf.state = "failed" if (ntf.attempts or 0) else "cancelled"
    _settle(ntf)
    log.warning("reminder %s dropped: event already started",
                ntf.notification_id)
    return True


def _retry_or_give_up(ntf: Notification) -> None:
    """Settle a reminder whose send did not fully succeed."""
    ntf.attempts = (ntf.attempts or 0) + 1
    if _utcnow() < _retry_deadline(ntf):
        ntf.state = "pending"              # next poll retries what is left
        _settle(ntf)
        log.warning("reminder %s not fully sent (attempt %d); will retry",
                    ntf.notification_id, ntf.attempts)
        return
    if ntf.email_done or ntf.sms_done:
        # One channel got through; the other never did. That is a sent
        # reminder, not a failed one.
        ntf.state = "sent"
        ntf.sent_at = _utcnow().isoformat(timespec="seconds")
    else:
        ntf.state = "failed"
        log.error("reminder %s FAILED after %d attempt(s); giving up",
                  ntf.notification_id, ntf.attempts)
    _settle(ntf)


def _deliver(ntf: Notification, user: User, want_email: bool, want_sms: bool,
             send_email, send_sms) -> bool:
    """Send whichever of this reminder's channels are still owed, record what
    got through, and settle the notification. Returns True if it is now sent.

    ``send_email()`` returns the provider's EmailLog; ``send_sms()`` returns
    whether the text was accepted. A channel that has already gone out is
    never sent again on a retry. The email provider reports a failed send as
    an EmailLog with an outcome rather than raising, so the outcome is what
    counts — not merely getting a log back. Only "accepted" is a delivery.
    "error" is transient and retried; anything else (the provider refused
    the recipient outright) will not improve with retrying, so that channel
    is closed without being counted as delivered.
    """
    refused = []

    def _email() -> None:
        row = send_email()
        outcome = getattr(row, "outcome", None) if row is not None else None
        if outcome == "accepted":
            ntf.email_done = True
            ntf.email_id = row.email_id
        elif outcome is not None and outcome != "error":
            refused.append(outcome)

    if want_email and not ntf.email_done:
        _email()
    if want_sms and not ntf.sms_done:
        if send_sms():
            ntf.sms_done = True
        elif (not want_email and not ntf.email_done and user.email
              and not user.email_undeliverable):
            # A text-only member whose text did not go: fall back to email
            # now rather than risk the reminder not arriving at all.
            _email()
    if ((not want_email or ntf.email_done) and (not want_sms or ntf.sms_done)):
        ntf.attempts = (ntf.attempts or 0) + 1
        ntf.state = "sent"
        ntf.sent_at = _utcnow().isoformat(timespec="seconds")
        _settle(ntf)
        return True
    if refused and not (want_sms and not ntf.sms_done):
        # Email was the only thing still owed and the provider will not take
        # it. Retrying cannot help.
        ntf.attempts = (ntf.attempts or 0) + 1
        ntf.state = "sent" if ntf.sms_done else "failed"
        if ntf.state == "sent":
            ntf.sent_at = _utcnow().isoformat(timespec="seconds")
        else:
            log.error("reminder %s FAILED: email refused (%s)",
                      ntf.notification_id, refused[0])
        _settle(ntf)
        return ntf.state == "sent"
    _retry_or_give_up(ntf)
    return ntf.state == "sent"


def _send_reminder(ntf: Notification) -> bool:
    community_id = ntf.community_id or COMMUNITY_ID
    user = db.get_user(community_id, ntf.user_id)
    if not user or user.channel == "none":
        ntf.state = "cancelled"
        _settle(ntf)
        return False
    want_email, want_sms = _channels_for(user)
    if not want_email and not want_sms:
        ntf.state = "cancelled"
        _settle(ntf)
        return False
    if ntf.source == "occurrence":
        if _too_late(ntf):
            return False
        return _send_occurrence_reminder(ntf, user, community_id)
    slot = db.find_slot_in_month(ntf.app_id, ntf.yyyy_mm, ntf.slot_id)
    if not slot or slot.cancelled:
        ntf.state = "cancelled"
        _settle(ntf)
        return False
    # The member's settings as they are NOW decide: someone who has since
    # turned this reminder off must not get one queued before they did.
    if ntf.lead_minutes not in publishing.effective_lead_times(user):
        ntf.state = "cancelled"
        _settle(ntf)
        log.info("reminder %s cancelled: member no longer wants a %d-minute "
                 "reminder", ntf.notification_id, ntf.lead_minutes)
        return False
    # The user must still HOLD this slot. Release/trade/admin-removal all
    # delete the Assignment row but don't proactively cancel this pending
    # reminder, so re-validate at send time — otherwise someone who traded
    # out of their slot still gets the day-before reminder for it.
    if not any(a.user_id == ntf.user_id for a in
               db.list_assignments_for_slot(ntf.app_id, ntf.yyyy_mm, slot.slot_id)):
        ntf.state = "cancelled"
        _settle(ntf)
        log.info("reminder %s cancelled: user %s no longer assigned to slot %s",
                 ntf.notification_id, ntf.user_id, slot.slot_id)
        return False
    community = db.get_community(community_id)
    app = next((a for a in db.list_applications(community_id) if a.app_id == ntf.app_id), None)
    # The slot as it stands now says when the member is due — not the time
    # it had when the reminder was queued.
    if community is not None and app is not None:
        try:
            ntf.event_at = publishing.arrival_time_utc(
                community, app, slot).isoformat(timespec="seconds")
        except Exception:
            log.exception("could not compute arrival time for slot %s",
                          slot.slot_id)
    if _too_late(ntf):
        return False
    org_name = app.name if app else (community.name if community else community_id)
    event_type = app.event_noun if app else "event"
    co_names = _co_names_for_slot(community_id, ntf.app_id, ntf.yyyy_mm,
                                  slot.slot_id, user.user_id)
    subject, body = _reminder_email(user, slot, org_name, ntf.lead_minutes,
                                    event_type, co_names=co_names,
                                    host=_community_host(community))
    def send_email():
        return _get_provider().send(
            community_id=community_id,
            from_addr=FROM_ADDR,
            to_addr=user.email,
            subject=subject,
            body_text=body,
            kind="reminder",
            related_user_id=user.user_id,
            related_app_id=ntf.app_id,
            related_slot_id=slot.slot_id,
            related_yyyy_mm=ntf.yyyy_mm,
        )

    def send_sms():
        return _send_sms_reminder(
            user, _reminder_sms(slot, org_name, _community_host(community)),
            ntf, slot.slot_id)

    done = _deliver(ntf, user, want_email, want_sms, send_email, send_sms)
    log.info("reminder for slot %s (lead=%d min): state=%s email=%s sms=%s "
             "attempts=%d", slot.slot_id, ntf.lead_minutes, ntf.state,
             ntf.email_done, ntf.sms_done, ntf.attempts)
    return done


def _send_occurrence_reminder(ntf: Notification, user: User,
                              community_id: str) -> bool:
    """Send a standing_event meeting reminder (source='occurrence')."""
    # The occurrence id's trailing 10 chars are its ISO date (standing.py).
    iso_date = ntf.slot_id[-10:]
    occ = db.get_standing_occurrence(ntf.app_id, iso_date, ntf.slot_id)
    if occ is None or occ.state == "cancelled":
        ntf.state = "cancelled"
        _settle(ntf)
        return False
    series = db.get_standing_series_for_app(ntf.app_id)
    community = db.get_community(community_id)
    app = next((a for a in db.list_applications(community_id)
                if a.app_id == ntf.app_id), None)
    org_name = app.name if app else (community.name if community else community_id)
    subject, body = _occurrence_reminder_email(
        user, occ, series, app, org_name, ntf.lead_minutes)

    # Attach an .ics meeting invite when the series opted in. Best-effort:
    # a malformed time or generation error must not block the reminder email.
    ics_content: str | None = None
    if series is not None and series.send_calendar_invites:
        start_time = occ.start_time or series.default_start_time or "12:00"
        tz_name = ((app.default_timezone if app else None)
                   or (community.default_timezone if community else None)
                   or "America/New_York")
        try:
            ics_content = make_occurrence_ics(
                occurrence_id=occ.occurrence_id,
                iso_date=occ.iso_date,
                start_time=start_time,
                duration_minutes=series.default_duration_minutes or 60,
                summary=app.name if app else org_name,
                user_id=user.user_id,
                user_email=user.email,
                domain=_community_host(community),
                community_name=org_name,
                location=occ.location or series.default_location,
                timezone=tz_name,
                notes=occ.notes,
            )
        except Exception:
            log.exception("occurrence .ics generation failed for %s",
                          occ.occurrence_id)

    want_email, want_sms = _channels_for(user)

    def send_email():
        return _get_provider().send(
            community_id=community_id,
            from_addr=FROM_ADDR,
            to_addr=user.email,
            subject=subject,
            body_text=body,
            kind="reminder",
            related_user_id=user.user_id,
            related_app_id=ntf.app_id,
            related_slot_id=ntf.slot_id,
            related_yyyy_mm=ntf.yyyy_mm,
            ics_content=ics_content,
        )

    def send_sms():
        return _send_sms_reminder(
            user, _occurrence_reminder_sms(occ, series, app, org_name),
            ntf, ntf.slot_id)

    done = _deliver(ntf, user, want_email, want_sms, send_email, send_sms)
    log.info("occurrence reminder for %s on %s (lead=%d min): state=%s "
             "email=%s sms=%s attempts=%d", ntf.app_id, iso_date,
             ntf.lead_minutes, ntf.state, ntf.email_done, ntf.sms_done,
             ntf.attempts)
    return done


def _fmt_hhmm(t: str | None) -> str | None:
    """'19:00' -> '7:00 PM'; passthrough on anything unparseable."""
    if not t:
        return None
    try:
        hh, mm = (int(x) for x in t.split(":")[:2])
    except (ValueError, AttributeError):
        return t
    ap = "AM" if hh < 12 else "PM"
    return f"{hh % 12 or 12}:{mm:02d} {ap}"


def _lead_label(lead_minutes: int) -> str:
    if lead_minutes >= 1440:
        d = lead_minutes // 1440
        return f"in {d} day{'s' if d != 1 else ''}"
    if lead_minutes >= 60:
        h = lead_minutes // 60
        return f"in {h} hour{'s' if h != 1 else ''}"
    return f"in {lead_minutes} minutes"


def _occurrence_reminder_email(
    user: User, occ: StandingOccurrence, series: StandingSeries | None,
    app: Application | None, org_name: str, lead_minutes: int,
) -> tuple[str, str]:
    d = dt.date.fromisoformat(occ.iso_date)
    when = d.strftime("%A, %B ") + str(d.day)
    time_str = _fmt_hhmm(occ.start_time
                         or (series.default_start_time if series else None))
    location = occ.location or (series.default_location if series else None)
    meeting = app.name if app else org_name
    label = _lead_label(lead_minutes)
    subject = f"{org_name} -- reminder: {meeting} {label}"
    detail = when + (f" at {time_str}" if time_str else "")
    if location:
        detail += f" -- {location}"
    lines = [f"Hi {user.name},", "",
             f"Reminder: {meeting} is {label}.", detail + "."]
    if occ.notes:
        lines += ["", occ.notes]
    lines += ["", f"-- {org_name}"]
    return subject, "\n".join(lines)


def _occurrence_reminder_sms(occ: StandingOccurrence,
                             series: "StandingSeries | None",
                             app: "Application | None", org_name: str) -> str:
    """Tight one-line SMS for a standing_event occurrence reminder
    (meeting, date, time, location). ASCII/GSM-7; no self-service link
    (standing events don't expose a release link in the reminder)."""
    d = dt.date.fromisoformat(occ.iso_date)
    when = d.strftime("%a, %b ") + str(d.day)
    time_str = _fmt_hhmm(occ.start_time
                         or (series.default_start_time if series else None))
    location = occ.location or (series.default_location if series else None)
    meeting = app.name if app else org_name
    s = f"Reminder: {meeting}, {when}"
    if time_str:
        s += f", {time_str}"
    if location:
        s += f", {location}"
    return s + "."


def _reminder_email(user: User, slot: Slot, org_name: str,
                    lead_minutes: int, event_type: str = "event",
                    co_names: list[str] | None = None,
                    host: str = DOMAIN_NAME) -> tuple[str, str]:
    when = _fmt_date(slot.local_date)
    if lead_minutes >= 1440:
        time_label = f"in {lead_minutes // 1440} day{'s' if lead_minutes >= 2880 else ''}"
    elif lead_minutes >= 60:
        time_label = f"in {lead_minutes // 60} hour{'s' if lead_minutes >= 120 else ''}"
    else:
        time_label = f"in {lead_minutes} minutes"
    subject = f"{org_name} -- reminder: {slot.name} {time_label}"
    body = (
        f"Hi {user.name},\n\n"
        f"Reminder: you're scheduled for:\n\n"
        f"  {slot.name}\n"
        f"  {when} -- starts {_fmt_time(slot.start_time)}, "
        f"please arrive by {_fmt_time(_arrival_hhmm(slot))}\n"
        f"{_co_line(co_names or [])}\n"
        f"If you need to withdraw, you can decline the calendar event or "
        f"visit https://{host}/your-schedule to release your slot "
        f"or trade for a different one.\n\n"
        f"-- {org_name}\n"
    )
    return subject, body


def _reminder_sms(slot: Slot, org_name: str, host: str) -> str:
    """Tight one-line SMS for a coverage/recurring slot reminder.

    Purpose-built (not the email subject): carries date + start + arrive-by +
    the self-service link, the essentials a text needs. Kept to ASCII / GSM-7
    punctuation (no em-dash or smart quotes) so it stays a single 160-char
    segment whenever the org name is reasonable."""
    when = _fmt_date(slot.local_date)
    start = _fmt_time(slot.start_time)
    arrive = _fmt_time(_arrival_hhmm(slot))
    return (f"Reminder: {org_name}, {when}, {start}, "
            f"arrive {arrive}. https://{host}/your-schedule")


def _process(rec: dict) -> bool:
    event_name = rec.get("eventName")
    ddb = rec.get("dynamodb") or {}
    keys = _deserialize(ddb.get("Keys") or {})
    sk = keys.get("SK", "")
    if not sk.startswith("ASGN#"):
        return False
    image = (ddb.get("NewImage") if event_name != "REMOVE"
             else ddb.get("OldImage")) or {}
    asg = _deserialize(image)
    if not asg:
        return False
    community_id = asg.get("community_id") or COMMUNITY_ID
    app_id = asg.get("app_id")
    yyyy_mm = asg.get("yyyy_mm")
    slot_id = asg.get("slot_id")
    user_id = asg.get("user_id")
    if not (community_id and app_id and yyyy_mm and slot_id and user_id):
        log.warning("malformed assignment image keys=%s", keys)
        return False

    sch = db.get_schedule(app_id, yyyy_mm)
    if sch is None or sch.state != "published":
        log.info("skipping %s in non-published schedule %s/%s",
                 event_name, app_id, yyyy_mm)
        return False

    # Keep the reminder queue in step with the assignments. Publishing queued
    # reminders only for the assignments that existed then; every later
    # change arrives here, whichever screen or admin action made it. The
    # reconcile works from the month's current state, so it does not matter
    # which change this was, or whether records arrive late or twice — and
    # if this attempt fails, the poll's periodic sweep does it instead.
    if event_name in ("INSERT", "REMOVE"):
        try:
            _reconcile_month(community_id, app_id, yyyy_mm)
        except Exception:
            # Never let a reminder problem stop the sign-up emails below.
            log.exception("could not reconcile reminders %s/%s",
                          app_id, yyyy_mm)
    if event_name == "REMOVE":
        return False

    user = db.get_user(community_id, user_id)
    if user is None or user.email_undeliverable or not user.email or user.channel == "none":
        log.info("skipping notification for user_id=%s (missing/undeliverable)", user_id)
        return False

    slot = db.find_slot_in_month(app_id, yyyy_mm, slot_id)
    if slot is None:
        log.warning("slot %s not found in %s/%s", slot_id, app_id, yyyy_mm)
        return False

    community = db.get_community(community_id)
    app = next((a for a in db.list_applications(community_id) if a.app_id == app_id), None)
    org_name = app.name if app else (community.name if community else community_id)
    event_type = app.event_noun if app else "event"

    if event_name == "INSERT":
        created_by = asg.get("created_by")
        self_signup = (created_by is None or created_by == user_id)
        co_names = _co_names_for_slot(community_id, app_id, yyyy_mm,
                                      slot_id, user_id)
        subject, body = _assigned_email(user, slot, org_name, event_type,
                                        self_signup=self_signup,
                                        co_names=co_names,
                                        host=_community_host(community))
    else:
        return False

    tz_name = (community.default_timezone if community else
               os.environ.get("COMMUNITY_TIMEZONE", "America/New_York"))
    ics = None
    if event_name == "INSERT":
        arrival_text = (f"please arrive by {_fmt_time(_arrival_hhmm(slot))}"
                        if slot.arrival_offset_minutes else None)
        ics = make_event_ics(
            slot, user.user_id, user.email,
            domain=DOMAIN_NAME, community_name=org_name,
            timezone=tz_name, arrival_text=arrival_text,
            alarm_minutes=user.calendar_alarm_minutes,
        )

    from_addr = FROM_ADDR
    if not self_signup and created_by:
        admin = db.get_user(community_id, created_by)
        if admin:
            # Strip both quote (would break display-name quoting) AND
            # control chars (would inject a Bcc/Reply-To header on
            # SendRawEmail — security fix H4).
            safe_name = _CTRL_RE.sub(" ", admin.name).replace('"', '')[:100]
            safe_org = _CTRL_RE.sub(" ", org_name).replace('"', '')[:100]
            from_addr = f'"{safe_name} of {safe_org}" <{FROM_ADDR}>'

    log_row: EmailLog = _get_provider().send(
        community_id=community_id,
        from_addr=from_addr,
        to_addr=user.email,
        subject=subject,
        body_text=body,
        kind="change_notification",
        related_user_id=user.user_id,
        related_app_id=app_id,
        related_slot_id=slot.slot_id,
        related_yyyy_mm=yyyy_mm,
        ics_content=ics,
    )
    log.info("notified %s outcome=%s for event=%s slot=%s",
             user.email, log_row.outcome, event_name, slot.slot_id)
    if event_name == "INSERT":
        when = _fmt_date(slot.local_date)
        users_by_id = {u.user_id: u for u in db.list_users(community_id)}

        # Notify existing co-assignees of the new peer
        co_asgns = list(db.list_assignments_for_slot(app_id, yyyy_mm, slot_id))
        co_ids = {a.user_id for a in co_asgns} - {user.user_id}
        for co_id in co_ids:
            co = users_by_id.get(co_id)
            if not co or not co.email or co.email_undeliverable or co.channel == "none":
                continue
            co_body = (
                f"Hi {co.name},\n\n"
                f"{user.name} just signed up to join you for:\n\n"
                f"  {slot.name}\n"
                f"  {when} -- {_fmt_time(slot.start_time)}\n\n"
                f"-- {org_name}\n"
            )
            _get_provider().send(
                community_id=community_id,
                from_addr=FROM_ADDR, to_addr=co.email,
                subject=f"{org_name} -- {user.name} joined: {slot.name}",
                body_text=co_body,
                kind="change_notification",
                related_user_id=co_id,
                related_app_id=app_id,
                related_slot_id=slot.slot_id,
                related_yyyy_mm=yyyy_mm,
            )

        # Notify AAs
        aa_ids = {m.user_id for m in db.list_memberships_for_app(app_id)
                  if m.app_role == "aa" and m.user_id != user.user_id}
        for aa_id in aa_ids:
            aa = users_by_id.get(aa_id)
            if not aa or not aa.email or aa.email_undeliverable:
                continue
            aa_body = (
                f"Hi {aa.name},\n\n"
                f"{user.name} signed up for:\n\n"
                f"  {slot.name}\n"
                f"  {when} -- {_fmt_time(slot.start_time)}\n\n"
                f"-- {org_name}\n"
            )
            _get_provider().send(
                community_id=community_id,
                from_addr=FROM_ADDR, to_addr=aa.email,
                subject=f"{org_name} -- {user.name} signed up: {slot.name}",
                body_text=aa_body,
                kind="change_notification",
                related_user_id=aa_id,
                related_app_id=app_id,
                related_slot_id=slot.slot_id,
                related_yyyy_mm=yyyy_mm,
            )
    return True


def _reconcile_month(community_id: str, app_id: str, yyyy_mm: str) -> None:
    community = db.get_community(community_id)
    app = next((a for a in db.list_applications(community_id)
                if a.app_id == app_id), None)
    if community is None or app is None:
        return
    out = publishing.reconcile_reminders(community, app, yyyy_mm)
    if out["queued"] or out["cancelled"]:
        log.info("reminders reconciled for %s/%s: %s", app_id, yyyy_mm, out)


def _deserialize(image: dict) -> dict:
    return {k: _deser.deserialize(v) for k, v in image.items()}


def _fmt_time(hhmm: str) -> str:
    h, m = (int(x) for x in hhmm.split(":"))
    suffix = "AM" if h < 12 else "PM"
    h12 = h if 1 <= h <= 12 else (12 if h == 0 else h - 12)
    return f"{h12}:{m:02d} {suffix}"


def _fmt_date(iso_date: str) -> str:
    y, mo, d = (int(x) for x in iso_date.split("-"))
    date = dt.date(y, mo, d)
    return f"{_DAY_LABEL[date.weekday()]}, {_MONTH_LABEL[mo]} {d}"


def _arrival_hhmm(slot: Slot) -> str:
    h, m = (int(x) for x in slot.start_time.split(":"))
    base = dt.datetime(2000, 1, 1, h, m)
    arrival = base - dt.timedelta(minutes=slot.arrival_offset_minutes)
    return f"{arrival.hour:02d}:{arrival.minute:02d}"


def _co_names_for_slot(community_id: str, app_id: str, yyyy_mm: str,
                       slot_id: str, exclude_user_id: str) -> list[str]:
    """Return sorted display names of users co-assigned to a slot (excluding one)."""
    asgns = list(db.list_assignments_for_slot(app_id, yyyy_mm, slot_id))
    co_ids = [a.user_id for a in asgns if a.user_id != exclude_user_id]
    if not co_ids:
        return []
    users_by_id = {u.user_id: u for u in db.list_users(community_id)}
    names = [users_by_id[uid].name for uid in co_ids if uid in users_by_id]
    return sorted(names)


def _co_line(co_names: list[str]) -> str:
    if not co_names:
        return ""
    if len(co_names) == 1:
        return f"  Also assigned: {co_names[0]}\n"
    return f"  Also assigned: {', '.join(co_names)}\n"


def _assigned_email(user: User, slot: Slot, org_name: str,
                    event_type: str = "event",
                    self_signup: bool = False,
                    co_names: list[str] | None = None,
                    host: str = DOMAIN_NAME) -> tuple[str, str]:
    when = _fmt_date(slot.local_date)
    if self_signup:
        subject = f"{org_name} -- you've signed up: {slot.name} on {when}"
        intro = f"You've chosen to help at:"
    else:
        subject = f"{org_name} -- you're assigned: {slot.name} on {when}"
        intro = f"You've been assigned to:"
    body = (
        f"Hi {user.name},\n\n"
        f"{intro}\n\n"
        f"  {slot.name}\n"
        f"  {when} -- starts {_fmt_time(slot.start_time)}, "
        f"please arrive by {_fmt_time(_arrival_hhmm(slot))}\n"
        f"{_co_line(co_names or [])}\n"
        f"If you need to withdraw, you can simply decline this calendar "
        f"invitation. You can also visit https://{host}/your-schedule "
        f"to release your slot, take a slot, or trade an existing slot for "
        f"another. If you withdraw, others covering your {event_type} and "
        f"administrators will be notified, as well as others from your "
        f"cohort, encouraging someone to sign up in your place. Trading is "
        f"the best option, so please visit the website to start the "
        f"process.\n\n"
        f"-- {org_name}\n"
    )
    return subject, body


def _unassigned_email(user: User, slot: Slot, org_name: str) -> tuple[str, str]:
    when = _fmt_date(slot.local_date)
    subject = f"{org_name} -- assignment removed: {slot.name} on {when}"
    body = (
        f"Hi {user.name},\n\n"
        f"You've been removed from:\n\n"
        f"  {slot.name}\n"
        f"  {when} -- {_fmt_time(slot.start_time)}\n\n"
        f"No action needed.\n\n"
        f"-- {org_name}\n"
    )
    return subject, body
