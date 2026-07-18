"""Tests for standing_event recurrence expansion.

The optimized arithmetic in ``recurrence.py`` is cross-checked against an
independent reference built on ``calendar.monthcalendar`` (the nth occurrence
of a weekday is the nth non-zero entry in that weekday's column).
"""
from __future__ import annotations

import calendar
import datetime as dt

import pytest

from community_organizer.core import recurrence as rec

_WD = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_ORD = {"1st": 1, "2nd": 2, "3rd": 3, "4th": 4}


def _ref_date(year: int, month: int, weekday: int, ordinal) -> dt.date | None:
    """Independent reference using calendar.monthcalendar (Mon-first)."""
    cols = [w[weekday] for w in calendar.monthcalendar(year, month) if w[weekday]]
    if ordinal == "last":
        return dt.date(year, month, cols[-1])
    return dt.date(year, month, cols[ordinal - 1]) if ordinal <= len(cols) else None


@pytest.mark.parametrize("ord_tok,ordinal", [("1st", 1), ("2nd", 2), ("3rd", 3), ("4th", 4), ("last", "last")])
@pytest.mark.parametrize("wd_tok,wd", list(_WD.items()))
def test_matches_reference_over_two_years(ord_tok, ordinal, wd_tok, wd):
    if ord_tok == "last" and wd_tok in ("sat", "sun"):
        return  # enum only defines monthly_last_mon..fri; skip undefined rules
    rule = f"monthly_{ord_tok}_{wd_tok}"
    start, end = dt.date(2026, 1, 1), dt.date(2027, 12, 31)
    got = rec.occurrence_dates(rule, start, end)
    # build the reference set month-by-month
    expected = []
    y, m = 2026, 1
    while dt.date(y, m, 1) <= end:
        d = _ref_date(y, m, wd, ordinal)
        if d is not None:
            expected.append(d)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    assert got == expected
    # every result is the right weekday
    assert all(d.weekday() == wd for d in got)


def test_range_is_inclusive_and_clipped():
    # 2nd Tuesday of each month, but only within a tight window
    rule = "monthly_2nd_tue"
    full = rec.occurrence_dates(rule, dt.date(2026, 1, 1), dt.date(2026, 12, 31))
    assert len(full) == 12 and all(d.weekday() == 1 for d in full)
    # clip to a single month
    one = rec.occurrence_dates(rule, dt.date(2026, 7, 1), dt.date(2026, 7, 31))
    assert one == [full[6]]
    # start mid-month AFTER the occurrence -> excluded
    after = rec.occurrence_dates(rule, full[6] + dt.timedelta(days=1), dt.date(2026, 7, 31))
    assert after == []


def test_known_anchor_dates():
    # independently verifiable: 1st Monday of June 2026 is 2026-06-01
    assert rec.occurrence_dates("monthly_1st_mon", dt.date(2026, 6, 1), dt.date(2026, 6, 30)) == [dt.date(2026, 6, 1)]
    # last Friday of May 2026 is 2026-05-29
    assert rec.occurrence_dates("monthly_last_fri", dt.date(2026, 5, 1), dt.date(2026, 5, 31)) == [dt.date(2026, 5, 29)]


def test_supports_and_unsupported():
    assert rec.supports("monthly_2nd_tue")
    assert rec.supports("monthly_last_wed")
    assert not rec.supports("rrule")
    assert not rec.supports("weekly")
    with pytest.raises(NotImplementedError):
        rec.occurrence_dates("rrule", dt.date(2026, 1, 1), dt.date(2026, 12, 31))
