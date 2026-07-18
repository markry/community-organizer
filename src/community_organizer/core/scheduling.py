"""Materialize SlotTemplates into concrete Slot instances for a period.

A *period* is the unit a Schedule covers — either a calendar month
(monthly apps, the original Ushers flow) or an ISO week (weekly apps,
recurring_commitments). materialize() expands templates into Slots
for whichever period the caller passes.

Conceptual flow, monthly::

    SlotTemplate("Sun 8:00 AM")  +  period_id="2026-05"
                                |
                                v
    Slot(2026-05-03), Slot(2026-05-10), Slot(2026-05-17),
    Slot(2026-05-24), Slot(2026-05-31)

Conceptual flow, weekly::

    SlotTemplate("Wed 2:00 PM")  +  period_id="2026-W22"
                                |
                                v
    Slot(2026-05-27)             # the single Wed in that ISO week

Each Slot's ``local_date`` is the ISO date string; its
``concrete_date`` is the start time on that date converted to UTC
using the application's IANA timezone (so DST transitions are
handled correctly).

For monthly periods, only ``recurrence="weekly"`` is supported. The
other ``SlotTemplate.recurrence`` values (biweekly_*, monthly_*,
rrule) are reserved for future work and will raise
``NotImplementedError``.

Storage note: the period_id is written to the existing ``yyyy_mm``
attribute (Slot, Assignment, Schedule). For monthly that's
"2026-05"; for weekly "2026-W22". Keeping the attribute name avoids
a DDB migration.

Tested by:
    tests/core/test_scheduling.py
"""
from __future__ import annotations

import calendar
import datetime as dt
from zoneinfo import ZoneInfo

from .models import PeriodType, Slot, SlotTemplate


def _parse_yyyy_mm(yyyy_mm: str) -> tuple[int, int]:
    """Parse ``"YYYY-MM"`` into (year, month) with validation.

    Centralized so every callsite gets the same error messages. The
    web Lambda's form parsing also passes through this — any
    malformed month from a URL gets a clean ValueError, not a
    silent off-by-month bug.
    """
    parts = yyyy_mm.split("-")
    if len(parts) != 2:
        raise ValueError(f"yyyy_mm must be 'YYYY-MM', got {yyyy_mm!r}")
    year, month = int(parts[0]), int(parts[1])
    if not (1 <= month <= 12):
        raise ValueError(f"month out of range: {month}")
    return year, month


def _parse_iso_week(period_id: str) -> tuple[int, int]:
    """Parse ``"YYYY-Www"`` into (iso_year, iso_week) with validation.

    ISO 8601 weeks: week 1 is the week containing the year's first
    Thursday; weeks run Monday→Sunday. The "iso_year" is NOT always
    the calendar year — e.g. 2026-01-01 falls in week 1 of ISO year
    2026, but 2024-12-30 falls in week 1 of ISO year 2025. We trust
    the year part of the string and only validate the week range.
    """
    if len(period_id) != 8 or period_id[4:6] != "-W":
        raise ValueError(
            f"weekly period_id must be 'YYYY-Www', got {period_id!r}"
        )
    try:
        year = int(period_id[:4])
        week = int(period_id[6:])
    except ValueError as e:
        raise ValueError(
            f"weekly period_id must be 'YYYY-Www', got {period_id!r}"
        ) from e
    if not (1 <= week <= 53):
        raise ValueError(f"ISO week out of range: {week}")
    return year, week


def _dates_for_weekday(year: int, month: int, day_of_week: int) -> list[dt.date]:
    """Return every date in (year, month) that falls on ``day_of_week``.

    ``day_of_week`` follows Python's ``date.weekday()`` convention:
    Monday=0, Sunday=6. A typical month has 4–5 occurrences of each
    weekday; this helper enumerates them in order.
    """
    _, last = calendar.monthrange(year, month)
    out = []
    for d in range(1, last + 1):
        date = dt.date(year, month, d)
        if date.weekday() == day_of_week:
            out.append(date)
    return out


def _date_in_iso_week(iso_year: int, iso_week: int,
                      day_of_week: int) -> dt.date:
    """Return the single date in (iso_year, iso_week) matching day_of_week.

    Uses Python's ``date.fromisocalendar(year, week, weekday)`` where
    weekday is 1..7 with Monday=1 — converted from the project's
    Monday=0 convention by adding 1.
    """
    return dt.date.fromisocalendar(iso_year, iso_week, day_of_week + 1)


def materialize(
    community_id: str,
    app_id: str,
    yyyy_mm: str,
    timezone: str,
    templates: list[SlotTemplate],
    period_type: PeriodType = "monthly",
) -> list[Slot]:
    """Expand a list of templates into concrete Slots for one period.

    For ``period_type="monthly"``, each weekly template fans out
    into N slots (one per matching weekday in the month). For
    ``period_type="weekly"``, each weekly template emits exactly one
    slot — the single occurrence of that weekday in the ISO week.

    Args:
        community_id, app_id: stamped onto every generated Slot.
        yyyy_mm: the period_id. ``"YYYY-MM"`` for monthly,
            ``"YYYY-Www"`` for weekly. (Field stays named ``yyyy_mm``
            because that's what Slot/Assignment store it as.)
        timezone: IANA zone name — used to convert each local start
            time into UTC for the Slot's ``concrete_date``. Must come
            from the Application's effective timezone
            (``app.default_timezone or community.default_timezone``);
            don't default here, that would mask config bugs.
        templates: the active SlotTemplates from
            ``db.list_templates(app_id)``.
        period_type: "monthly" (default, original behavior) or
            "weekly" (recurring_commitments). Passed in by the caller
            from ``Application.period_type``.

    Returns slots sorted by (local_date, start_time) — the canonical
    order the schedule edit page and the broadcast email both render
    in.

    Raises:
        ValueError: malformed period_id for the given period_type.
        NotImplementedError: any template using a recurrence other
            than ``"weekly"``.

    Side effects: none. Pure transformation — caller persists via
    ``db.put_slots``.
    """
    tz = ZoneInfo(timezone)
    slots: list[Slot] = []

    if period_type == "monthly":
        year, month = _parse_yyyy_mm(yyyy_mm)
        # For monthly periods, ``tpl.ordinal`` (if set) restricts to
        # the Nth occurrence of day_of_week in the month — that's how
        # "First Friday" templates work. ordinal=None preserves the
        # legacy fan-out (one slot per matching weekday in the month,
        # which is how Ushers schedules have always worked).
        def date_fn(tpl):
            dates = _dates_for_weekday(year, month, tpl.day_of_week)
            if tpl.ordinal is None:
                return dates
            if tpl.ordinal == -1:
                return dates[-1:] if dates else []
            idx = tpl.ordinal - 1
            if 0 <= idx < len(dates):
                return [dates[idx]]
            return []   # e.g. "fifth Wednesday" in a month with only 4
    elif period_type == "weekly":
        iso_year, iso_week = _parse_iso_week(yyyy_mm)
        date_fn = lambda tpl: [_date_in_iso_week(
            iso_year, iso_week, tpl.day_of_week)]
    else:
        raise ValueError(f"unknown period_type: {period_type!r}")

    for tpl in templates:
        if tpl.recurrence != "weekly":
            raise NotImplementedError(
                f"recurrence {tpl.recurrence!r} not yet supported "
                f"(template_id={tpl.template_id})"
            )
        h, m = (int(x) for x in tpl.start_time.split(":"))
        for date in date_fn(tpl):
            local = dt.datetime(date.year, date.month, date.day, h, m, tzinfo=tz)
            utc = local.astimezone(dt.timezone.utc)
            slots.append(Slot(
                community_id=community_id,
                app_id=app_id,
                yyyy_mm=yyyy_mm,
                template_id=tpl.template_id,
                name=tpl.name,
                day_of_week=tpl.day_of_week,
                start_time=tpl.start_time,
                arrival_offset_minutes=tpl.arrival_offset_minutes,
                duration_minutes=tpl.duration_minutes,
                required_volunteers=tpl.required_volunteers,
                min_volunteers=tpl.min_volunteers,
                max_volunteers=tpl.max_volunteers,
                tags=list(tpl.tags),
                local_date=date.isoformat(),
                concrete_date=utc.isoformat(),
            ))

    slots.sort(key=lambda s: (s.local_date, s.start_time))
    return slots
