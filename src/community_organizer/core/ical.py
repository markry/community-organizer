"""Generate iCalendar (.ics) attachments for emails.

This module produces RFC 5545 calendar payloads that get attached to
broadcast / change / cancellation emails. There are three exported
generators:

    - ``make_event_ics``   : single event, ``METHOD:REQUEST``
    - ``make_batch_ics``   : multi-event invite (one per assignment),
                              ``METHOD:REQUEST``
    - ``make_cancel_ics``  : single event, ``METHOD:CANCEL`` + ``SEQUENCE:1``

Why three: SES has a per-message attachment limit, and most mail clients
treat a VCALENDAR with many VEVENTs as a single multi-day invite rather
than N separate prompts — so the publish broadcast uses ``make_batch_ics``
with all the user's slots, while a one-off "your trade was accepted"
email uses ``make_event_ics`` with the new slot only.

UID stability — the cross-cutting contract
-------------------------------------------

Every event's UID is derived from ``(slot_id, user_id, domain)`` via
``_uid``. This means:

    - **Republishing** a schedule re-emits invites with the **same UID**
      as the original. Most calendar apps treat the new invite as an
      update to the existing event (no duplicate calendar entries).
    - **Cancellation** uses the same UID with ``METHOD:CANCEL`` and
      ``SEQUENCE:1`` (RFC 5545 requires monotonic sequence numbers for
      updates / cancels of the same UID).

Don't change the UID format without a migration plan — old invites on
members' calendars are addressed by their original UID, and any new
``METHOD:CANCEL`` with a different UID would orphan the original event.

Tested by:
    tests/core/test_ical.py
"""
from __future__ import annotations

import datetime as dt
import re
from zoneinfo import ZoneInfo

from .models import Slot


# Strip control characters from anything that goes into a UID, mailto:,
# or DTSTART value — CRLF in any of those would inject new iCal properties.
_CTRL = re.compile(r"[\x00-\x1f\x7f]")

# RFC 5322 (loose) email check — used to keep CRLF/spaces out of
# ATTENDEE/ORGANIZER mailto: lines. Falls back to a sanitized placeholder
# if input is malformed; we never want a bad address to inject a header.
_EMAIL_OK = re.compile(r"^[^\s,;:<>()\[\]\\\"]+@[^\s,;:<>()\[\]\\\"]+$")


def _esc_text(s: str) -> str:
    """Escape a string for RFC 5545 §3.3.11 TEXT property values.

    iCalendar TEXT values can't contain unescaped ``,``, ``;``, ``\\``,
    or newlines — those characters are structural in the .ics grammar.
    Without escaping, a slot name containing CRLF + ``DTSTART:...`` could
    inject calendar properties.

    Escape order matters: backslashes first (so we don't double-escape
    ones we add for the others). Carriage returns are stripped entirely
    (RFC 5545 lines end CRLF; the unfolding step expects bare LF inside
    TEXT to be escaped as ``\\n``).
    """
    if not s:
        return ""
    return (s
            .replace("\\", "\\\\")
            .replace(";", "\\;")
            .replace(",", "\\,")
            .replace("\r", "")
            .replace("\n", "\\n"))


def _safe_addr(email: str) -> str:
    """Return an address safe to drop into a ``mailto:`` URI.

    If the input doesn't look like a normal address (contains CRLF,
    spaces, or RFC 5322 specials we don't allow), return a static
    placeholder rather than inject. Calendar invites with the
    placeholder will still render — they just won't be a working reply
    address. Better than a header-injected mailbomb.
    """
    if email and _EMAIL_OK.match(email) and not _CTRL.search(email):
        return email
    return "invalid@invalid.invalid"


def _uid(slot_id: str, user_id: str, domain: str) -> str:
    """Build the stable iCalendar UID for one (slot, user) pair.

    Format: ``slot-<slot_id>-<user_id>@<domain>``. The ``@<domain>``
    suffix is the RFC 5545 convention for ensuring UIDs are globally
    unique. The same (slot_id, user_id) must always produce the same
    UID — see the module docstring for why.

    Components are scrubbed of control characters defensively — slot_id
    and user_id come from our own UUID generator so should never
    contain them, but a future schema change shouldn't quietly turn
    UID strings into a header-injection sink.
    """
    return (f"slot-{_CTRL.sub('', slot_id)}-{_CTRL.sub('', user_id)}"
            f"@{_CTRL.sub('', domain)}")


def _cohort_uid(cohort_id: str, user_id: str, domain: str) -> str:
    """UID for the recurring "I'm in this cohort" calendar entry.

    Distinct namespace from slot-UIDs so:
      - Per-slot one-off invites (a one-week pickup) don't clash
        with the recurring commitment series.
      - Cancelling a cohort membership removes ONLY the recurring
        series and leaves any specific-week pickups untouched.

    Format: ``cohort-<cohort_id>-<user_id>@<domain>``.
    """
    return (f"cohort-{_CTRL.sub('', cohort_id)}-{_CTRL.sub('', user_id)}"
            f"@{_CTRL.sub('', domain)}")


def _ical_dt(date_str: str, time_str: str, tz: ZoneInfo) -> str:
    """Render a (date, local time, tz) triple as iCal UTC: ``YYYYMMDDTHHMMSSZ``.

    The Z-suffix form is the simplest interop: every calendar app
    understands UTC + a Z, so we don't need to ship a VTIMEZONE block
    for the application's local zone.

    Args:
        date_str: ``"YYYY-MM-DD"`` local date.
        time_str: ``"HH:MM"`` 24h local time.
        tz: zoneinfo for the application's local zone.
    """
    d = dt.date.fromisoformat(date_str)
    h, m = (int(x) for x in time_str.split(":"))
    local = dt.datetime(d.year, d.month, d.day, h, m, tzinfo=tz)
    utc = local.astimezone(dt.timezone.utc)
    return utc.strftime("%Y%m%dT%H%M%SZ")


def _fold(line: str) -> str:
    """Fold long lines per RFC 5545 §3.1 — max 75 octets per line.

    Calendar lines longer than 75 bytes must be split with CRLF + a
    single space prefix on each continuation.

    Cuts at 75 bytes for the first chunk and 74 for subsequent ones to
    account for the leading-space continuation byte. **Boundary-aware
    on UTF-8** (security fix D19): we walk codepoints and break at the
    last codepoint whose encoded bytes still fit. The earlier
    byte-then-``errors="ignore"`` approach silently dropped bytes
    mid-multibyte sequence — slot or community names containing
    em-dashes / accented chars would be visibly corrupted in the
    calendar entry.
    """
    if len(line.encode("utf-8")) <= 75:
        return line
    parts: list[str] = []
    remaining = line
    while remaining.encode("utf-8"):
        budget = 75 if not parts else 74
        # Take as many leading codepoints as fit within ``budget`` bytes.
        used = 0
        cut_idx = 0
        for i, ch in enumerate(remaining):
            ch_len = len(ch.encode("utf-8"))
            if used + ch_len > budget:
                break
            used += ch_len
            cut_idx = i + 1
        if cut_idx == 0:
            # A single codepoint exceeds the budget (e.g. some emoji
            # at ~4 bytes if the budget got tiny). Take it anyway —
            # better an oversized line than an infinite loop.
            cut_idx = 1
        parts.append(remaining[:cut_idx])
        remaining = remaining[cut_idx:]
        if not remaining:
            break
    return "\r\n ".join(parts)


def _valarm_lines(alarm_minutes: int | None, summary: str) -> list[str]:
    """Build the VALARM block — calendar-side display reminder.

    A VALARM tells the user's calendar app to pop a notification at
    a fixed lead time before the event starts. This is **independent**
    of our DDB-stored ``Notification`` rows (which drive our own
    reminder emails). Users who want both calendar pops AND email
    reminders get both; users who disable calendar alarms still get
    our emails.

    Returns the lines for a single VALARM, or [] if disabled
    (``alarm_minutes is None`` or negative). DESCRIPTION is TEXT-escaped
    because the summary comes from ``slot.name`` which is admin-controlled.
    """
    if alarm_minutes is None or alarm_minutes < 0:
        return []
    return [
        "BEGIN:VALARM",
        "ACTION:DISPLAY",
        f"DESCRIPTION:{_esc_text(summary)}",
        f"TRIGGER:-PT{int(alarm_minutes)}M",
        "END:VALARM",
    ]


def make_event_ics(
    slot: Slot,
    user_id: str,
    user_email: str,
    *,
    domain: str,
    community_name: str,
    timezone: str = "America/New_York",
    arrival_text: str | None = None,
    uid_suffix: str = "",
    alarm_minutes: int | None = None,
) -> str:
    """Build a single-event VCALENDAR (METHOD:REQUEST).

    Used for one-off emails like "you've been assigned" or "your trade
    was accepted." For the publish broadcast, use ``make_batch_ics``
    instead — it bundles all the user's month into one invite.

    Args:
        slot: the event being invited to.
        user_id, user_email: the attendee.
        domain: the app's public domain — flows into UID + ORGANIZER.
        community_name: shown in the calendar event's DESCRIPTION.
        timezone: IANA zone for the slot's local time.
        arrival_text: optional "please arrive by 7:50 AM" line appended
            to DESCRIPTION.
        uid_suffix: optional extra string appended to the UID. Used in
            the rare case where we need to send the SAME slot as a NEW
            invite (e.g., after a tentative-decline nudge) — the suffix
            makes the calendar app treat it as a separate event.
        alarm_minutes: VALARM trigger, or None to omit.

    Returns the full .ics body as a string (CRLF-terminated).
    """
    tz = ZoneInfo(timezone)
    dtstart = _ical_dt(slot.local_date, slot.start_time, tz)
    h, m = (int(x) for x in slot.start_time.split(":"))
    end_dt = dt.datetime(2000, 1, 1, h, m) + dt.timedelta(minutes=slot.duration_minutes)
    end_time = f"{end_dt.hour:02d}:{end_dt.minute:02d}"
    dtend = _ical_dt(slot.local_date, end_time, tz)

    description = community_name
    if arrival_text:
        description += f" — {arrival_text}"

    safe_domain = _CTRL.sub("", domain)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:-//Community Organizer//{safe_domain}//EN",
        "METHOD:REQUEST",
        "BEGIN:VEVENT",
        f"UID:{_uid(slot.slot_id, user_id, domain)}{_CTRL.sub('', uid_suffix)}",
        f"DTSTART:{dtstart}",
        f"DTEND:{dtend}",
        f"SUMMARY:{_esc_text(slot.name)}",
        _fold(f"DESCRIPTION:{_esc_text(description)}"),
        f"ORGANIZER:mailto:{_safe_addr(f'organizer@{safe_domain}')}",
        f"ATTENDEE:mailto:{_safe_addr(user_email)}",
        "SEQUENCE:0",
        "STATUS:CONFIRMED",
    ]
    lines += _valarm_lines(alarm_minutes, slot.name)
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(lines) + "\r\n"


def make_occurrence_ics(
    *,
    occurrence_id: str,
    iso_date: str,
    start_time: str,
    duration_minutes: int,
    summary: str,
    user_id: str,
    user_email: str,
    domain: str,
    community_name: str,
    location: str | None = None,
    timezone: str = "America/New_York",
    notes: str | None = None,
    alarm_minutes: int | None = None,
) -> str:
    """Build a single-event VCALENDAR (METHOD:REQUEST) for a standing_event
    occurrence. Like ``make_event_ics`` but driven by occurrence fields rather
    than a Slot, and carries a ``LOCATION`` (meetings have a place; slots
    don't). The UID is per-(occurrence, user, domain) so a re-sent invite
    updates the existing calendar entry instead of duplicating it.
    """
    tz = ZoneInfo(timezone)
    dtstart = _ical_dt(iso_date, start_time, tz)
    h, m = (int(x) for x in start_time.split(":"))
    end_dt = dt.datetime(2000, 1, 1, h, m) + dt.timedelta(minutes=duration_minutes)
    end_time = f"{end_dt.hour:02d}:{end_dt.minute:02d}"
    dtend = _ical_dt(iso_date, end_time, tz)

    description = community_name
    if notes:
        description += f" — {notes}"
    safe_domain = _CTRL.sub("", domain)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:-//Community Organizer//{safe_domain}//EN",
        "METHOD:REQUEST",
        "BEGIN:VEVENT",
        f"UID:{_uid(occurrence_id, user_id, domain)}",
        f"DTSTART:{dtstart}",
        f"DTEND:{dtend}",
        f"SUMMARY:{_esc_text(summary)}",
        _fold(f"DESCRIPTION:{_esc_text(description)}"),
    ]
    if location:
        lines.append(_fold(f"LOCATION:{_esc_text(location)}"))
    lines += [
        f"ORGANIZER:mailto:{_safe_addr(f'organizer@{safe_domain}')}",
        f"ATTENDEE:mailto:{_safe_addr(user_email)}",
        "SEQUENCE:0",
        "STATUS:CONFIRMED",
    ]
    lines += _valarm_lines(alarm_minutes, summary)
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(lines) + "\r\n"


def make_flexible_event_ics(
    *,
    event_id: str,
    iso_date: str,
    start_time: str,
    duration_minutes: int,
    summary: str,
    user_id: str,
    user_email: str,
    domain: str,
    community_name: str,
    location: str | None = None,
    bringing: str | None = None,
    timezone: str = "America/New_York",
    alarm_minutes: int | None = None,
) -> str:
    """Build a single-event VCALENDAR (METHOD:REQUEST) for a finalized
    flexible_event (date-poll / book club). Like ``make_occurrence_ics`` but
    driven by FlexibleEvent fields and carries the member's potluck
    ``bringing`` in the DESCRIPTION so the assignment rides along on their
    calendar. UID is per-(event, user, domain) so a re-send updates the
    existing entry instead of duplicating it (and keeps the inbound
    UID-parse / From-match reply handling working unchanged).
    """
    tz = ZoneInfo(timezone)
    dtstart = _ical_dt(iso_date, start_time, tz)
    h, m = (int(x) for x in start_time.split(":"))
    end_dt = dt.datetime(2000, 1, 1, h, m) + dt.timedelta(minutes=duration_minutes)
    end_time = f"{end_dt.hour:02d}:{end_dt.minute:02d}"
    dtend = _ical_dt(iso_date, end_time, tz)

    description = community_name
    if bringing:
        description += f" — You're bringing: {bringing}"
    safe_domain = _CTRL.sub("", domain)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:-//Community Organizer//{safe_domain}//EN",
        "METHOD:REQUEST",
        "BEGIN:VEVENT",
        f"UID:{_uid(event_id, user_id, domain)}",
        f"DTSTART:{dtstart}",
        f"DTEND:{dtend}",
        f"SUMMARY:{_esc_text(summary)}",
        _fold(f"DESCRIPTION:{_esc_text(description)}"),
    ]
    if location:
        lines.append(_fold(f"LOCATION:{_esc_text(location)}"))
    lines += [
        f"ORGANIZER:mailto:{_safe_addr(f'organizer@{safe_domain}')}",
        f"ATTENDEE:mailto:{_safe_addr(user_email)}",
        "SEQUENCE:0",
        "STATUS:CONFIRMED",
    ]
    lines += _valarm_lines(alarm_minutes, summary)
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(lines) + "\r\n"


def make_batch_ics(
    slots: list[Slot],
    user_id: str,
    user_email: str,
    *,
    domain: str,
    community_name: str,
    timezone: str = "America/New_York",
    arrival_text_fn=None,
    alarm_minutes: int | None = None,
) -> str:
    """Build a multi-event VCALENDAR with one VEVENT per slot.

    This is what attaches to the publish broadcast — every member with
    assignments gets one .ics file containing every slot they're on
    for the month. Calendar clients then show them as separate events
    on their respective dates.

    Args:
        slots: the user's assignments, in any order. UIDs are per-slot
            so order doesn't affect calendar identity.
        arrival_text_fn: optional callable ``(slot) -> str | None``
            invoked per slot to compute the "please arrive by …" suffix
            for that slot's DESCRIPTION. Passing a function (rather than
            pre-computing) lets the caller derive arrival text from the
            slot's offset without iterating twice.

    Returns "" if no slots, otherwise the full VCALENDAR body. Callers
    typically check truthiness to decide whether to attach.
    """
    if not slots:
        return ""
    tz = ZoneInfo(timezone)
    safe_domain = _CTRL.sub("", domain)
    safe_attendee = _safe_addr(user_email)
    safe_organizer = _safe_addr(f"organizer@{safe_domain}")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:-//Community Organizer//{safe_domain}//EN",
        "METHOD:REQUEST",
    ]
    for slot in slots:
        dtstart = _ical_dt(slot.local_date, slot.start_time, tz)
        h, m = (int(x) for x in slot.start_time.split(":"))
        end_dt = dt.datetime(2000, 1, 1, h, m) + dt.timedelta(minutes=slot.duration_minutes)
        end_time = f"{end_dt.hour:02d}:{end_dt.minute:02d}"
        dtend = _ical_dt(slot.local_date, end_time, tz)
        description = community_name
        if arrival_text_fn:
            at = arrival_text_fn(slot)
            if at:
                description += f" — {at}"
        lines += [
            "BEGIN:VEVENT",
            f"UID:{_uid(slot.slot_id, user_id, domain)}",
            f"DTSTART:{dtstart}",
            f"DTEND:{dtend}",
            f"SUMMARY:{_esc_text(slot.name)}",
            _fold(f"DESCRIPTION:{_esc_text(description)}"),
            f"ORGANIZER:mailto:{safe_organizer}",
            f"ATTENDEE:mailto:{safe_attendee}",
            "SEQUENCE:0",
            "STATUS:CONFIRMED",
        ]
        lines += _valarm_lines(alarm_minutes, slot.name)
        lines += ["END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


_PY_WEEKDAY_TO_ICAL = {0: "MO", 1: "TU", 2: "WE", 3: "TH",
                       4: "FR", 5: "SA", 6: "SU"}


def make_recurring_event_ics(
    *, cohort_id: str, user_id: str, user_email: str,
    summary: str, description: str,
    day_of_week: int, start_time: str, duration_minutes: int,
    first_date: dt.date, until_date: dt.date,
    domain: str, timezone: str = "America/New_York",
    alarm_minutes: int | None = None,
) -> str:
    """Build a single-VEVENT VCALENDAR with a weekly RRULE.

    Used when a member joins a cohort: one calendar entry covers
    every future occurrence of the slot. The UID is keyed on
    (cohort_id, user_id) so a later CANCEL with the same UID can
    remove the entire series.

    Args:
        cohort_id, user_id: keys for the stable UID.
        summary: VEVENT SUMMARY (the cohort/slot name).
        description: VEVENT DESCRIPTION (community name + arrival note).
        day_of_week: 0=Mon..6=Sun (Python convention) — converted to
            iCalendar's two-letter BYDAY value.
        start_time, duration_minutes: per-occurrence local time + length.
        first_date: the first occurrence date (DTSTART date part);
            should be the next upcoming occurrence at the time the
            invite is sent, in the app's local timezone.
        until_date: RRULE UNTIL — the last date on which an occurrence
            may start. The series ends here. Usually
            ``today + visible_horizon_months``.
        domain: app's public domain — flows into UID + ORGANIZER.
        timezone: IANA zone for local-to-UTC conversion.
        alarm_minutes: VALARM trigger, or None.

    Returns the full .ics body as a string (CRLF-terminated).
    """
    tz = ZoneInfo(timezone)
    dtstart = _ical_dt(first_date.isoformat(), start_time, tz)
    h, m = (int(x) for x in start_time.split(":"))
    end_dt = dt.datetime(2000, 1, 1, h, m) + dt.timedelta(minutes=duration_minutes)
    end_time = f"{end_dt.hour:02d}:{end_dt.minute:02d}"
    dtend = _ical_dt(first_date.isoformat(), end_time, tz)

    # RRULE UNTIL is rendered in UTC as YYYYMMDDTHHMMSSZ. We use
    # end-of-day on until_date *in UTC* (not converted from local) so
    # the date in the rendered string matches the caller's chosen
    # cutoff. This is generous by ~1 day relative to local-time
    # end-of-day, which is fine: a parishioner who renews around the
    # cutoff is better served by including the last occurrence than
    # by clipping it.
    until_str = (f"{until_date.year:04d}{until_date.month:02d}"
                 f"{until_date.day:02d}T235959Z")
    byday = _PY_WEEKDAY_TO_ICAL[day_of_week]

    safe_domain = _CTRL.sub("", domain)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:-//Community Organizer//{safe_domain}//EN",
        "METHOD:REQUEST",
        "BEGIN:VEVENT",
        f"UID:{_cohort_uid(cohort_id, user_id, domain)}",
        f"DTSTART:{dtstart}",
        f"DTEND:{dtend}",
        f"RRULE:FREQ=WEEKLY;BYDAY={byday};UNTIL={until_str}",
        f"SUMMARY:{_esc_text(summary)}",
        _fold(f"DESCRIPTION:{_esc_text(description)}"),
        f"ORGANIZER:mailto:{_safe_addr(f'organizer@{safe_domain}')}",
        f"ATTENDEE:mailto:{_safe_addr(user_email)}",
        "SEQUENCE:0",
        "STATUS:CONFIRMED",
    ]
    lines += _valarm_lines(alarm_minutes, summary)
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(lines) + "\r\n"


def make_recurring_cancel_ics(
    *, cohort_id: str, user_id: str, user_email: str,
    summary: str, day_of_week: int, start_time: str,
    duration_minutes: int, first_date: dt.date,
    domain: str, timezone: str = "America/New_York",
) -> str:
    """Build a METHOD:CANCEL VCALENDAR for a recurring cohort invite.

    Uses the same UID as ``make_recurring_event_ics`` so the user's
    calendar removes the entire series. SEQUENCE:1 satisfies RFC 5545's
    monotonic-update rule.

    Args mirror ``make_recurring_event_ics``; only the bits the
    calendar app needs to identify the original event are included.
    """
    tz = ZoneInfo(timezone)
    dtstart = _ical_dt(first_date.isoformat(), start_time, tz)
    h, m = (int(x) for x in start_time.split(":"))
    end_dt = dt.datetime(2000, 1, 1, h, m) + dt.timedelta(minutes=duration_minutes)
    end_time = f"{end_dt.hour:02d}:{end_dt.minute:02d}"
    dtend = _ical_dt(first_date.isoformat(), end_time, tz)

    safe_domain = _CTRL.sub("", domain)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:-//Community Organizer//{safe_domain}//EN",
        "METHOD:CANCEL",
        "BEGIN:VEVENT",
        f"UID:{_cohort_uid(cohort_id, user_id, domain)}",
        f"DTSTART:{dtstart}",
        f"DTEND:{dtend}",
        f"SUMMARY:{_esc_text(summary)}",
        f"ORGANIZER:mailto:{_safe_addr(f'organizer@{safe_domain}')}",
        f"ATTENDEE:mailto:{_safe_addr(user_email)}",
        "SEQUENCE:1",
        "STATUS:CANCELLED",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(lines) + "\r\n"


def make_cancel_ics(
    slot: Slot,
    user_id: str,
    user_email: str,
    *,
    domain: str,
    timezone: str = "America/New_York",
) -> str:
    """Build a CANCEL .ics that removes a previously-sent event from a calendar.

    Two RFC 5545 rules are key here:

        1. **Same UID** as the original invite — that's how the client
           knows which event to remove.
        2. **SEQUENCE:1** (vs the original's SEQUENCE:0) — sequence
           must strictly increase for any update / cancellation. If we
           ever cancel and then re-issue, the new invite needs
           SEQUENCE:2.

    Used when a user withdraws (manually or via calendar decline), and
    when an admin removes an assignment.
    """
    tz = ZoneInfo(timezone)
    dtstart = _ical_dt(slot.local_date, slot.start_time, tz)
    h, m = (int(x) for x in slot.start_time.split(":"))
    end_dt = dt.datetime(2000, 1, 1, h, m) + dt.timedelta(minutes=slot.duration_minutes)
    end_time = f"{end_dt.hour:02d}:{end_dt.minute:02d}"
    dtend = _ical_dt(slot.local_date, end_time, tz)

    safe_domain = _CTRL.sub("", domain)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:-//Community Organizer//{safe_domain}//EN",
        "METHOD:CANCEL",
        "BEGIN:VEVENT",
        f"UID:{_uid(slot.slot_id, user_id, domain)}",
        f"DTSTART:{dtstart}",
        f"DTEND:{dtend}",
        f"SUMMARY:{_esc_text(slot.name)}",
        f"ORGANIZER:mailto:{_safe_addr(f'organizer@{safe_domain}')}",
        f"ATTENDEE:mailto:{_safe_addr(user_email)}",
        "SEQUENCE:1",
        "STATUS:CANCELLED",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(lines) + "\r\n"
