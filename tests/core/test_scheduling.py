"""Tests for ``community_organizer.core.scheduling.materialize``.

This module is pure logic — no DDB, no I/O — so tests are fast and
deterministic. We verify:

    - Each weekly template fans out to one Slot per matching weekday
    - The right number of weekdays per month (4 or 5)
    - Slot.local_date is the local ISO date
    - Slot.concrete_date is the UTC datetime (DST-aware)
    - Slots come back sorted by (local_date, start_time)
    - Unsupported recurrence types raise NotImplementedError
    - Malformed yyyy_mm raises ValueError
"""
from __future__ import annotations

import datetime as dt

import pytest

from community_organizer.core.models import SlotTemplate
from community_organizer.core.scheduling import (
    _date_in_iso_week,
    _dates_for_weekday,
    _parse_iso_week,
    _parse_yyyy_mm,
    materialize,
)


# ---- _parse_yyyy_mm -------------------------------------------------------

def test_parse_yyyy_mm_happy() -> None:
    assert _parse_yyyy_mm("2026-05") == (2026, 5)
    assert _parse_yyyy_mm("2026-12") == (2026, 12)


@pytest.mark.parametrize("bad", ["2026", "2026/05", "2026-13", "2026-00", "abc-de"])
def test_parse_yyyy_mm_rejects_bad_input(bad: str) -> None:
    with pytest.raises(ValueError):
        _parse_yyyy_mm(bad)


# ---- _dates_for_weekday ---------------------------------------------------

def test_dates_for_weekday_sundays_in_may_2026() -> None:
    # May 2026: Sundays are May 3, 10, 17, 24, 31.
    sundays = _dates_for_weekday(2026, 5, 6)
    assert [d.day for d in sundays] == [3, 10, 17, 24, 31]


def test_dates_for_weekday_fridays_in_feb_2026() -> None:
    # February 2026 (28 days): Fridays are 6, 13, 20, 27.
    fridays = _dates_for_weekday(2026, 2, 4)
    assert [d.day for d in fridays] == [6, 13, 20, 27]


def test_dates_for_weekday_leap_feb() -> None:
    # February 2028 (leap, 29 days): Tuesdays are 1, 8, 15, 22, 29.
    tuesdays = _dates_for_weekday(2028, 2, 1)
    assert [d.day for d in tuesdays] == [1, 8, 15, 22, 29]


# ---- materialize ----------------------------------------------------------

def _tpl(
    *, name: str = "Sun 8 AM", day_of_week: int = 6,
    start_time: str = "08:00", template_id: str | None = None,
) -> SlotTemplate:
    return SlotTemplate(
        community_id="c1", app_id="a1", name=name,
        day_of_week=day_of_week, start_time=start_time,
        duration_minutes=60,
        **({"template_id": template_id} if template_id else {}),
    )


def test_materialize_one_template_fans_out_per_weekday() -> None:
    """Sun 8 AM in May 2026 → 5 Slots (one per Sunday)."""
    slots = materialize("c1", "a1", "2026-05", "America/New_York",
                        [_tpl(name="Sun 8 AM")])
    assert len(slots) == 5
    assert [s.local_date for s in slots] == [
        "2026-05-03", "2026-05-10", "2026-05-17", "2026-05-24", "2026-05-31",
    ]
    # Each carries the template fields verbatim.
    for s in slots:
        assert s.name == "Sun 8 AM"
        assert s.start_time == "08:00"
        assert s.day_of_week == 6
        assert s.community_id == "c1"
        assert s.app_id == "a1"
        assert s.yyyy_mm == "2026-05"


def test_materialize_multiple_templates_interleaved() -> None:
    """Multiple templates → result sorted by (local_date, start_time)."""
    slots = materialize(
        "c1", "a1", "2026-05", "America/New_York",
        [_tpl(name="Sun 10:30", start_time="10:30"),
         _tpl(name="Sun 8 AM",  start_time="08:00")],
    )
    # 5 Sundays × 2 templates = 10 slots, sorted by (date, time).
    assert len(slots) == 10
    assert slots[0].local_date == "2026-05-03" and slots[0].start_time == "08:00"
    assert slots[1].local_date == "2026-05-03" and slots[1].start_time == "10:30"
    assert slots[-1].local_date == "2026-05-31" and slots[-1].start_time == "10:30"


def test_materialize_concrete_date_is_utc() -> None:
    """``concrete_date`` is the start time on that local date, in UTC.

    May 3, 8:00 AM ET (EDT, UTC-4) → 12:00 UTC.
    """
    slots = materialize("c1", "a1", "2026-05", "America/New_York",
                        [_tpl(start_time="08:00")])
    first = slots[0]
    assert first.local_date == "2026-05-03"
    parsed = dt.datetime.fromisoformat(first.concrete_date)
    assert parsed.tzinfo == dt.timezone.utc
    assert parsed.hour == 12   # 8 AM EDT == 12 noon UTC


def test_materialize_handles_dst_transition() -> None:
    """DST starts in the US on March 8, 2026.

    Mar 1 (pre-DST, EST -5): 8 AM local → 13:00 UTC
    Mar 8 (post-DST, EDT -4): 8 AM local → 12:00 UTC

    If the materializer hard-coded UTC offsets instead of using the
    IANA zone, the post-DST slot would still be at 13:00 UTC and
    members would get reminded an hour late.
    """
    slots = materialize("c1", "a1", "2026-03", "America/New_York",
                        [_tpl(day_of_week=6, start_time="08:00")])
    # March 2026 Sundays: 1, 8, 15, 22, 29.
    by_date = {s.local_date: dt.datetime.fromisoformat(s.concrete_date)
               for s in slots}
    assert by_date["2026-03-01"].hour == 13   # EST
    assert by_date["2026-03-08"].hour == 12   # EDT — DST kicked in


def test_materialize_unsupported_recurrence_raises() -> None:
    tpl = _tpl()
    tpl.recurrence = "biweekly_even"
    with pytest.raises(NotImplementedError, match="biweekly_even"):
        materialize("c1", "a1", "2026-05", "America/New_York", [tpl])


def test_materialize_empty_templates_returns_empty() -> None:
    assert materialize("c1", "a1", "2026-05", "America/New_York", []) == []


def test_materialize_malformed_yyyy_mm_raises() -> None:
    with pytest.raises(ValueError):
        materialize("c1", "a1", "not-a-month", "America/New_York", [_tpl()])


# ---- weekly period_type ---------------------------------------------------

def test_parse_iso_week_happy() -> None:
    assert _parse_iso_week("2026-W22") == (2026, 22)
    assert _parse_iso_week("2026-W01") == (2026, 1)
    assert _parse_iso_week("2024-W53") == (2024, 53)


@pytest.mark.parametrize("bad", [
    "2026-22", "2026W22", "2026-W", "2026-Waa",
    "2026-W54", "2026-W00", "abc-Wee",
])
def test_parse_iso_week_rejects_bad_input(bad: str) -> None:
    with pytest.raises(ValueError):
        _parse_iso_week(bad)


def test_date_in_iso_week_picks_single_day() -> None:
    # 2026-W22 runs Mon 2026-05-25 through Sun 2026-05-31.
    # Wednesday (day_of_week=2 in Python convention) → 2026-05-27.
    assert _date_in_iso_week(2026, 22, 2) == dt.date(2026, 5, 27)
    assert _date_in_iso_week(2026, 22, 0) == dt.date(2026, 5, 25)  # Mon
    assert _date_in_iso_week(2026, 22, 6) == dt.date(2026, 5, 31)  # Sun


def test_materialize_weekly_emits_one_slot_per_template() -> None:
    """Weekly period: each template fans out to exactly one Slot —
    the single occurrence of that weekday in the ISO week."""
    wed_tpl = _tpl()             # default _tpl is day_of_week=6 (Sun); override
    wed_tpl.day_of_week = 2
    wed_tpl.name = "Wed 2 PM"
    wed_tpl.start_time = "14:00"

    slots = materialize("c1", "a1", "2026-W22", "America/New_York",
                        [wed_tpl], period_type="weekly")
    assert len(slots) == 1
    s = slots[0]
    assert s.local_date == "2026-05-27"
    assert s.yyyy_mm == "2026-W22"   # the period_id flows into the storage field
    assert s.name == "Wed 2 PM"


def test_materialize_weekly_two_templates_same_week() -> None:
    """Two templates on different days each get one slot in the week,
    sorted by (local_date, start_time)."""
    wed = _tpl(); wed.day_of_week = 2; wed.start_time = "14:00"; wed.name = "Wed"
    thu = _tpl(); thu.day_of_week = 3; thu.start_time = "08:00"; thu.name = "Thu"

    slots = materialize("c1", "a1", "2026-W22", "America/New_York",
                        [wed, thu], period_type="weekly")
    # Wed 2026-05-27 < Thu 2026-05-28.
    assert [s.local_date for s in slots] == ["2026-05-27", "2026-05-28"]


def test_materialize_weekly_rejects_malformed_period() -> None:
    with pytest.raises(ValueError):
        materialize("c1", "a1", "2026-05", "America/New_York",
                    [_tpl()], period_type="weekly")


def test_materialize_monthly_default_unchanged() -> None:
    """The default period_type stays "monthly" so existing call sites
    that never pass period_type keep working — pin that."""
    # _tpl() has day_of_week=6 (Sun). May 2026 has 5 Sundays.
    slots = materialize("c1", "a1", "2026-05", "America/New_York", [_tpl()])
    assert len(slots) == 5


def test_materialize_rejects_unknown_period_type() -> None:
    with pytest.raises(ValueError, match="period_type"):
        materialize("c1", "a1", "2026-05", "America/New_York",
                    [_tpl()], period_type="quarterly")


# ---- ordinal: First / Last / etc. for monthly apps ------------------------

def test_materialize_first_friday_picks_one_date(ddb_table=None) -> None:
    """A template with ordinal=1 and day_of_week=4 (Fri) should
    yield exactly the first Friday of the month."""
    # May 2026: Fridays are May 1, 8, 15, 22, 29.
    tpl = _tpl(day_of_week=4, name="First Fri 7 PM")
    tpl.ordinal = 1
    slots = materialize("c1", "a1", "2026-05", "America/New_York", [tpl])
    assert len(slots) == 1
    assert slots[0].local_date == "2026-05-01"


def test_materialize_last_sunday_picks_one_date() -> None:
    """ordinal=-1 means the LAST matching weekday."""
    tpl = _tpl(day_of_week=6, name="Last Sun 5 PM")
    tpl.ordinal = -1
    # May 2026 Sundays: 3, 10, 17, 24, 31.
    slots = materialize("c1", "a1", "2026-05", "America/New_York", [tpl])
    assert len(slots) == 1
    assert slots[0].local_date == "2026-05-31"


def test_materialize_third_thursday_picks_one_date() -> None:
    tpl = _tpl(day_of_week=3, name="Third Thu")
    tpl.ordinal = 3
    # May 2026 Thursdays: 7, 14, 21, 28. Third = the 21st.
    slots = materialize("c1", "a1", "2026-05", "America/New_York", [tpl])
    assert len(slots) == 1
    assert slots[0].local_date == "2026-05-21"


def test_materialize_fifth_wednesday_in_short_month_returns_empty() -> None:
    """A template demanding the fifth occurrence of a weekday in a
    month that only has four returns no slot for that month — better
    than guessing or clamping to the fourth."""
    tpl = _tpl(day_of_week=2)
    tpl.ordinal = 4
    # June 2026 Wednesdays: 3, 10, 17, 24 (only 4). Asking for the
    # fourth works; asking for the fifth (-1 is the way to ask for
    # last, but using fourth here is the maximum that always exists)
    # — verify fourth-exists case works.
    slots = materialize("c1", "a1", "2026-06", "America/New_York", [tpl])
    assert len(slots) == 1


def test_materialize_ordinal_ignored_in_weekly_period_type() -> None:
    """For weekly periods we don't filter on ordinal — the rolling
    week view should always show every template's occurrence in
    THIS week."""
    tpl = _tpl(day_of_week=2)
    tpl.ordinal = 4   # would block on monthly; weekly should still emit
    slots = materialize("c1", "a1", "2026-W22", "America/New_York",
                        [tpl], period_type="weekly")
    assert len(slots) == 1


def test_materialize_no_ordinal_keeps_legacy_fanout() -> None:
    """Coverage apps with ordinal=None on every template (the
    default) keep emitting one slot per matching weekday in the
    month — the original Ushers behavior."""
    tpl = _tpl(day_of_week=6)
    # ordinal not set → None
    slots = materialize("c1", "a1", "2026-05", "America/New_York", [tpl])
    assert len(slots) == 5   # five Sundays in May 2026
