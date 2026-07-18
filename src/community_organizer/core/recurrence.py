"""Expand a standing_event ``Recurrence`` rule into concrete occurrence dates.

Standing events recur on an "ordinal weekday of the month" rule — e.g.
``monthly_2nd_tue`` (Knights of Columbus), ``monthly_last_fri`` (parish
council). These rules fully specify the date within any month, so expansion
needs only a date range, not a separate weekday anchor.

``scheduling.materialize()`` deliberately leaves these rules unimplemented
(they're for the coverage flow's reserved future work); standing events use
this module instead.

Tested by: tests/core/test_recurrence.py
"""
from __future__ import annotations

import calendar
import datetime as dt

from .models import Recurrence

# weekday token -> Python weekday index (Monday=0 .. Sunday=6)
_WEEKDAY: dict[str, int] = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}
# ordinal token -> 1-based position within the month ("last" handled separately)
_ORDINAL: dict[str, int] = {"1st": 1, "first": 1, "2nd": 2, "3rd": 3, "4th": 4}


def _parse(recurrence: str) -> tuple[str | int, int] | None:
    """Parse ``monthly_<ordinal>_<weekday>`` into ``(ordinal, weekday_idx)``.

    Returns ``("last", idx)`` for last-of-month rules, ``(1..4, idx)`` for
    ordinal rules, or ``None`` if the rule isn't a monthly ordinal-weekday
    rule this module handles.
    """
    if not recurrence.startswith("monthly_"):
        return None
    parts = recurrence[len("monthly_"):].split("_")
    if len(parts) != 2:
        return None
    ord_tok, wd_tok = parts
    wd = _WEEKDAY.get(wd_tok)
    if wd is None:
        return None
    if ord_tok == "last":
        return ("last", wd)
    o = _ORDINAL.get(ord_tok)
    return (o, wd) if o is not None else None


def supports(recurrence: Recurrence) -> bool:
    """True if this module can expand the given recurrence rule."""
    return _parse(recurrence) is not None


def _nth_weekday(year: int, month: int, weekday: int, ordinal: int) -> dt.date | None:
    """The ``ordinal``-th (1-based) ``weekday`` of the month, or ``None`` if the
    month has fewer than ``ordinal`` of that weekday (only possible for the 5th,
    which we don't expose; 1st–4th always exist)."""
    first_weekday, days_in_month = calendar.monthrange(year, month)  # first_weekday: Mon=0
    first_occurrence = 1 + (weekday - first_weekday) % 7
    day = first_occurrence + (ordinal - 1) * 7
    return dt.date(year, month, day) if day <= days_in_month else None


def _last_weekday(year: int, month: int, weekday: int) -> dt.date:
    """The last ``weekday`` of the month."""
    first_weekday, days_in_month = calendar.monthrange(year, month)
    last_dow = (first_weekday + days_in_month - 1) % 7
    return dt.date(year, month, days_in_month - (last_dow - weekday) % 7)


def occurrence_dates(
    recurrence: Recurrence, start: dt.date, end: dt.date
) -> list[dt.date]:
    """All occurrence dates in ``[start, end]`` (inclusive), ascending.

    Raises ``NotImplementedError`` for recurrence rules this module doesn't
    handle (e.g. ``rrule`` — reserved for a later slice).
    """
    parsed = _parse(recurrence)
    if parsed is None:
        raise NotImplementedError(
            f"recurrence {recurrence!r} is not a supported standing-event rule"
        )
    ordinal, weekday = parsed
    out: list[dt.date] = []
    year, month = start.year, start.month
    while dt.date(year, month, 1) <= end:
        if ordinal == "last":
            d: dt.date | None = _last_weekday(year, month, weekday)
        else:
            d = _nth_weekday(year, month, weekday, int(ordinal))
        if d is not None and start <= d <= end:
            out.append(d)
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out
