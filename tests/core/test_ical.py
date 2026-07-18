"""Tests for ``community_organizer.core.ical`` — .ics calendar payload generation.

The most important guarantee these tests pin is **UID stability**: the
same (slot_id, user_id, domain) triple must produce the same iCal UID
across all three generators (``make_event_ics``, ``make_batch_ics``,
``make_cancel_ics``). If that ever broke, a republish would create
duplicate events on members' calendars instead of updating the
originals, and a cancel would orphan the original event.

Other things checked here:
    - DTSTART / DTEND are rendered in UTC with Z-suffix (interop)
    - VALARM appears iff ``alarm_minutes`` is given
    - METHOD:REQUEST vs METHOD:CANCEL is correctly set per generator
    - SEQUENCE:0 on request, SEQUENCE:1 on cancel (RFC 5545 monotonic)
    - DESCRIPTION line longer than 75 octets is RFC 5545 folded
"""
from __future__ import annotations

from community_organizer.core.ical import (
    _cohort_uid,
    _esc_text,
    _fold,
    _safe_addr,
    _uid,
    make_batch_ics,
    make_cancel_ics,
    make_event_ics,
    make_flexible_event_ics,
    make_recurring_cancel_ics,
    make_recurring_event_ics,
)
from community_organizer.core.models import Slot


# ---- Test fixtures --------------------------------------------------------

DOMAIN = "test.example.com"
TZ = "America/New_York"


def _slot(
    *, slot_id: str = "slot-123", name: str = "Sun 8:00 AM",
    date: str = "2026-05-31", start: str = "08:00",
    arrival_offset: int = 10, duration: int = 60,
) -> Slot:
    return Slot(
        community_id="c1", app_id="a1", yyyy_mm="2026-05",
        template_id="t1", name=name, day_of_week=6,
        start_time=start, arrival_offset_minutes=arrival_offset,
        duration_minutes=duration, required_volunteers=1, min_volunteers=1,
        concrete_date=date, local_date=date, slot_id=slot_id,
    )


# ---- UID stability — the contract that makes republish/cancel work --------

def test_uid_format_is_stable() -> None:
    """``_uid`` produces ``slot-<sid>-<uid>@<domain>`` — pin the format."""
    assert _uid("S", "U", "d.example") == "slot-S-U@d.example"


def test_same_slot_user_gives_same_uid_across_generators() -> None:
    """A republish + cancel cycle must address the same event.

    All three generators feed the UID line from ``_uid(slot_id,
    user_id, domain)``. If any one of them drifted (e.g. someone added
    a timestamp to the UID), republish would duplicate calendar events
    and cancel would orphan the original. This test pins that.
    """
    slot = _slot()
    req = make_event_ics(slot, "u1", "u@example.com",
                         domain=DOMAIN, community_name="Test")
    batch = make_batch_ics([slot], "u1", "u@example.com",
                           domain=DOMAIN, community_name="Test")
    cancel = make_cancel_ics(slot, "u1", "u@example.com", domain=DOMAIN)

    expected_uid = f"UID:slot-{slot.slot_id}-u1@{DOMAIN}"
    assert expected_uid in req
    assert expected_uid in batch
    assert expected_uid in cancel


def test_uid_suffix_distinguishes_reinvite() -> None:
    """The ``uid_suffix`` arg lets us send a NEW event for the same slot
    (e.g. after a tentative-decline nudge) without colliding with the
    original calendar event."""
    slot = _slot()
    ics = make_event_ics(slot, "u1", "u@example.com",
                         domain=DOMAIN, community_name="Test",
                         uid_suffix="-tentative-nudge")
    assert f"slot-{slot.slot_id}-u1@{DOMAIN}-tentative-nudge" in ics


# ---- METHOD + SEQUENCE — RFC 5545 update/cancel semantics -----------------

def test_event_method_is_request() -> None:
    slot = _slot()
    ics = make_event_ics(slot, "u", "u@example.com",
                         domain=DOMAIN, community_name="Test")
    assert "METHOD:REQUEST" in ics
    assert "SEQUENCE:0" in ics
    assert "STATUS:CONFIRMED" in ics


def test_cancel_method_and_sequence() -> None:
    """Cancel must use METHOD:CANCEL + SEQUENCE:1 to update the existing
    calendar event (SEQUENCE must strictly increase)."""
    slot = _slot()
    ics = make_cancel_ics(slot, "u", "u@example.com", domain=DOMAIN)
    assert "METHOD:CANCEL" in ics
    assert "SEQUENCE:1" in ics
    assert "STATUS:CANCELLED" in ics


# ---- DTSTART / DTEND — UTC with Z suffix ---------------------------------

def test_dtstart_dtend_rendered_utc() -> None:
    """8:00 AM EDT (UTC-4) on May 31 → 12:00 UTC. Z-suffix means client
    doesn't need to know about VTIMEZONE blocks."""
    slot = _slot(date="2026-05-31", start="08:00", duration=60)
    ics = make_event_ics(slot, "u", "u@example.com",
                         domain=DOMAIN, community_name="Test",
                         timezone="America/New_York")
    # May is EDT (UTC-4): 08:00 local → 12:00 UTC
    assert "DTSTART:20260531T120000Z" in ics
    assert "DTEND:20260531T130000Z" in ics


# ---- VALARM ---------------------------------------------------------------

def test_valarm_emitted_when_alarm_minutes_set() -> None:
    slot = _slot()
    ics = make_event_ics(slot, "u", "u@example.com",
                         domain=DOMAIN, community_name="Test",
                         alarm_minutes=15)
    assert "BEGIN:VALARM" in ics
    assert "TRIGGER:-PT15M" in ics
    assert "END:VALARM" in ics


def test_valarm_omitted_by_default() -> None:
    slot = _slot()
    ics = make_event_ics(slot, "u", "u@example.com",
                         domain=DOMAIN, community_name="Test")
    assert "VALARM" not in ics


def test_valarm_omitted_for_negative_minutes() -> None:
    """Negative alarm_minutes == "user disabled calendar alarms"."""
    slot = _slot()
    ics = make_event_ics(slot, "u", "u@example.com",
                         domain=DOMAIN, community_name="Test",
                         alarm_minutes=-1)
    assert "VALARM" not in ics


# ---- DESCRIPTION + arrival text + line folding ---------------------------

def test_description_includes_arrival_text() -> None:
    slot = _slot()
    ics = make_event_ics(slot, "u", "u@example.com",
                         domain=DOMAIN, community_name="Test Ushers",
                         arrival_text="please arrive by 7:50 AM")
    assert "Test Ushers" in ics
    assert "please arrive by 7:50 AM" in ics


def test_fold_short_line_unchanged() -> None:
    assert _fold("short line") == "short line"


def test_fold_long_line_splits_with_crlf_space() -> None:
    """A line longer than 75 octets is broken with CRLF + space prefix
    per RFC 5545 §3.1. Reassembling = strip(CRLF + space) — and that
    must round-trip to the original."""
    long_line = "DESCRIPTION:" + "x" * 200
    folded = _fold(long_line)
    assert "\r\n " in folded            # continuation marker present
    assert folded.replace("\r\n ", "") == long_line   # round-trips


def test_fold_preserves_multibyte_utf8_round_trip() -> None:
    """Em-dashes, accented characters, etc. must round-trip through
    fold without byte-boundary drops (security fix D19). The pre-fix
    implementation used errors="ignore" which silently lost bytes
    mid-multibyte."""
    # Em-dash is 3 bytes in UTF-8; a line of "x — y" repeated pushes
    # well past the 75-byte cap with em-dashes scattered through.
    line = "DESCRIPTION:" + "abcdef — ghijklm — opqrstu — vwxyz" * 3
    folded = _fold(line)
    assert folded.replace("\r\n ", "") == line
    # And no replacement / question characters appeared (which would
    # indicate boundary-drop).
    assert "�" not in folded


# ---- make_batch_ics: multiple VEVENTs, one VCALENDAR ----------------------

def test_batch_one_calendar_many_events() -> None:
    slots = [_slot(slot_id=f"s{i}", date=f"2026-05-{30+i:02d}")
             for i in range(2)]
    ics = make_batch_ics(slots, "u", "u@example.com",
                         domain=DOMAIN, community_name="Test")
    assert ics.count("BEGIN:VCALENDAR") == 1
    assert ics.count("END:VCALENDAR") == 1
    assert ics.count("BEGIN:VEVENT") == 2
    assert "slot-s0-u@" in ics
    assert "slot-s1-u@" in ics


def test_batch_empty_slots_returns_empty_string() -> None:
    """Callers check truthiness of the result before attaching."""
    assert make_batch_ics([], "u", "u@example.com",
                          domain=DOMAIN, community_name="Test") == ""


# ---- Security: TEXT escaping + address sanitization ---------------------

def test_esc_text_handles_specials() -> None:
    """Backslash, semicolon, comma, and newlines must be escaped;
    carriage return stripped (RFC 5545 §3.3.11)."""
    assert _esc_text("a;b,c\\d") == "a\\;b\\,c\\\\d"
    assert _esc_text("line1\nline2") == "line1\\nline2"
    assert _esc_text("with\rCR") == "withCR"


def test_safe_addr_passes_normal_email() -> None:
    assert _safe_addr("user@example.com") == "user@example.com"


def test_safe_addr_rejects_crlf() -> None:
    """A CRLF in the address must not flow into the mailto: line."""
    out = _safe_addr("user@example.com\r\nBcc: attacker@evil.com")
    assert "\r" not in out and "\n" not in out
    assert out == "invalid@invalid.invalid"


def test_event_ics_escapes_slot_name_with_specials() -> None:
    """A slot name containing CRLF + a forged property must NOT inject
    a new line into the VEVENT — the embedded "ATTENDEE:" must stay
    inside the SUMMARY value, escaped, not become a new property
    line. (security fix C2)"""
    slot = _slot()
    slot.name = "Mass\r\nATTENDEE:mailto:attacker@evil.com"
    ics = make_event_ics(slot, "u", "u@example.com",
                         domain=DOMAIN, community_name="Test")
    attendee_lines = [l for l in ics.split("\r\n") if l.startswith("ATTENDEE:")]
    # Only the real attendee — the injected one was escaped into SUMMARY.
    assert attendee_lines == ["ATTENDEE:mailto:u@example.com"]
    # The slot name CRLF is escaped to literal "\n" (TEXT escape).
    assert "SUMMARY:Mass\\nATTENDEE:mailto:attacker@evil.com" in ics


def test_event_ics_escapes_community_name_specials() -> None:
    """Same iCal-injection guarantee for community_name. The "DTSTART:"
    embedded in the name must stay inside the DESCRIPTION value, not
    become a second DTSTART property line."""
    slot = _slot()
    ics = make_event_ics(slot, "u", "u@example.com",
                         domain=DOMAIN,
                         community_name="Test, with; specials\\and \r\nDTSTART:19700101T000000Z")
    dtstart_lines = [l for l in ics.split("\r\n") if l.startswith("DTSTART:")]
    assert len(dtstart_lines) == 1
    assert "19700101T000000Z" not in dtstart_lines[0]


def test_event_ics_substitutes_safe_addr_for_bad_attendee() -> None:
    """A user email with CRLF must produce an invalid placeholder
    rather than inject a header."""
    slot = _slot()
    ics = make_event_ics(slot, "u",
                        "victim@example.com\r\nBcc: attacker@evil.com",
                        domain=DOMAIN, community_name="Test")
    assert "attacker@evil.com" not in ics
    assert "invalid@invalid.invalid" in ics


def test_batch_ics_escapes_all_events() -> None:
    """Multi-event invite escapes per-slot too, not just the first."""
    a = _slot(slot_id="a", date="2026-05-31")
    b = _slot(slot_id="b", date="2026-06-01")
    b.name = "Mass\r\nATTENDEE:mailto:attacker@evil.com"
    ics = make_batch_ics([a, b], "u", "u@example.com",
                         domain=DOMAIN, community_name="Test")
    # Only the real attendee line, repeated per VEVENT (2 events).
    attendee_lines = [l for l in ics.split("\r\n") if l.startswith("ATTENDEE:")]
    assert attendee_lines == ["ATTENDEE:mailto:u@example.com",
                              "ATTENDEE:mailto:u@example.com"]


def test_uid_strips_control_chars() -> None:
    """A control char in any UID component must not produce a header
    injection on the UID line."""
    out = _uid("ab\r\nINJECT", "user-1", "test.example")
    assert "\r" not in out and "\n" not in out


def test_batch_arrival_text_fn_per_slot() -> None:
    """``arrival_text_fn`` is invoked per slot; returning None omits the suffix."""
    s1 = _slot(slot_id="with", arrival_offset=10)
    s2 = _slot(slot_id="without", arrival_offset=0)
    seen = []

    def fn(s):
        seen.append(s.slot_id)
        return "arrive by X" if s.arrival_offset_minutes else None

    ics = make_batch_ics([s1, s2], "u", "u@example.com",
                         domain=DOMAIN, community_name="Test",
                         arrival_text_fn=fn)
    assert seen == ["with", "without"]
    assert "arrive by X" in ics



# ---- Recurring (cohort-join) invite ----------------------------------------

import datetime as _dt


def _rrule_kwargs(**overrides):
    base = dict(
        cohort_id="cohort-abc", user_id="user-xyz",
        user_email="m@example.com",
        summary="Wed 2 PM Adoration",
        description="St. Test Parish",
        day_of_week=2,        # Wed
        start_time="14:00",
        duration_minutes=60,
        first_date=_dt.date(2026, 5, 27),
        until_date=_dt.date(2026, 11, 30),
        domain=DOMAIN, timezone=TZ,
    )
    base.update(overrides)
    return base


def test_recurring_event_has_rrule_with_byday_and_until() -> None:
    body = make_recurring_event_ics(**_rrule_kwargs())
    assert "BEGIN:VCALENDAR" in body
    assert "METHOD:REQUEST" in body
    # Wed → WE.
    assert "RRULE:FREQ=WEEKLY;BYDAY=WE;UNTIL=" in body
    # UNTIL ends with Z and matches the configured cutoff month.
    assert "UNTIL=20261130" in body and "Z" in body
    # Stable cohort-namespaced UID.
    expected_uid = _cohort_uid("cohort-abc", "user-xyz", DOMAIN)
    assert f"UID:{expected_uid}" in body
    # SEQUENCE:0 on the initial REQUEST.
    assert "SEQUENCE:0" in body


def test_recurring_event_uid_namespace_distinct_from_per_slot_uid() -> None:
    """A per-slot one-off invite and a cohort recurring invite for the
    same user must have different UIDs so a CANCEL on one doesn't
    nuke the other."""
    slot_uid = _uid("slot-abc", "user-xyz", DOMAIN)
    cohort_uid = _cohort_uid("slot-abc", "user-xyz", DOMAIN)
    assert slot_uid != cohort_uid


def test_recurring_cancel_uses_same_uid_and_sequence_1() -> None:
    """The cancel for the cohort series must match the original UID
    and bump SEQUENCE so calendar apps update rather than ignore."""
    req = make_recurring_event_ics(**_rrule_kwargs())
    cancel = make_recurring_cancel_ics(
        cohort_id="cohort-abc", user_id="user-xyz",
        user_email="m@example.com", summary="Wed 2 PM Adoration",
        day_of_week=2, start_time="14:00", duration_minutes=60,
        first_date=_dt.date(2026, 5, 27),
        domain=DOMAIN, timezone=TZ,
    )
    # Same UID in both.
    expected_uid = _cohort_uid("cohort-abc", "user-xyz", DOMAIN)
    assert f"UID:{expected_uid}" in req
    assert f"UID:{expected_uid}" in cancel
    # Cancel has METHOD:CANCEL, SEQUENCE:1, STATUS:CANCELLED.
    assert "METHOD:CANCEL" in cancel
    assert "SEQUENCE:1" in cancel
    assert "STATUS:CANCELLED" in cancel


def test_recurring_event_byday_per_python_weekday() -> None:
    """All 7 day_of_week values map to the right iCalendar BYDAY token."""
    pairs = [(0, "MO"), (1, "TU"), (2, "WE"), (3, "TH"),
             (4, "FR"), (5, "SA"), (6, "SU")]
    for py_dow, ical in pairs:
        body = make_recurring_event_ics(**_rrule_kwargs(day_of_week=py_dow))
        assert f"BYDAY={ical}" in body, f"py {py_dow} should map to {ical}"


# ---- make_flexible_event_ics (date-poll / book club) ----------------------

def _flex_ics(**over) -> str:
    kw = dict(
        event_id="evt1", iso_date="2026-08-15", start_time="18:30",
        duration_minutes=120, summary="August Book Club",
        user_id="u1", user_email="reader@example.com",
        domain="community.example.org", community_name="Book Club",
        location="Vic's house", bringing="garden salad",
        timezone="America/New_York",
    )
    kw.update(over)
    return make_flexible_event_ics(**kw)


def test_flexible_event_ics_request_with_location_and_bringing() -> None:
    body = _flex_ics()
    assert "METHOD:REQUEST" in body
    assert "BEGIN:VEVENT" in body and "STATUS:CONFIRMED" in body
    assert "SUMMARY:August Book Club" in body
    assert "LOCATION:Vic's house" in body
    assert "You're bringing: garden salad" in body
    # UID is per (event, user) in the shared slot- namespace so inbound
    # reply parsing + dedupe-on-resend keep working.
    assert "UID:slot-evt1-u1@community.example.org" in body
    # 18:30 ET in August (EDT, UTC-4) -> 22:30Z start, +120min -> 00:30Z next day.
    assert "DTSTART:20260815T223000Z" in body
    assert "DTEND:20260816T003000Z" in body


def test_flexible_event_ics_omits_optional_fields() -> None:
    body = _flex_ics(location=None, bringing=None)
    assert "LOCATION:" not in body
    assert "You're bringing:" not in body
    assert "SUMMARY:August Book Club" in body


def test_flexible_event_ics_resend_uid_is_stable() -> None:
    a = _flex_ics(bringing="salad")
    b = _flex_ics(bringing="bread")          # same (event,user) -> same UID
    uid = "UID:slot-evt1-u1@community.example.org"
    assert uid in a and uid in b
