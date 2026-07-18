"""Business logic for standing_event apps.

Lazily materializes a series' recurrence rule into concrete
``StandingOccurrence`` rows (the design's "materialized lazily on AA action or
member view"). Pairs with ``recurrence.py`` (computes the dates) and ``db.py``
(persistence).

Idempotent + race-safe: an occurrence's id is derived deterministically from
``(series_id, date)``, so two concurrent first-views of the same month write
the same DynamoDB key and converge on one row rather than creating duplicates.

Tested by: tests/core/test_standing.py
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from . import db, recurrence
from .models import (
    Application, Community, Notification, StandingOccurrence, StandingSeries,
)


def occurrence_id(series_id: str, iso_date: str) -> str:
    """Deterministic, stable id for the occurrence of ``series`` on ``iso_date``."""
    return f"{series_id}-{iso_date}"


def materialize_occurrences(
    series: StandingSeries, from_date: dt.date, to_date: dt.date
) -> list[StandingOccurrence]:
    """Ensure a ``StandingOccurrence`` exists for every date the series'
    recurrence produces in ``[from_date, to_date]`` (inclusive), then return all
    occurrences in that range in date order.

    Idempotent: existing dates (including AA exceptions like cancelled/moved)
    are left untouched; only missing dates get a fresh ``scheduled`` row.
    """
    existing = list(db.list_standing_occurrences(
        series.app_id,
        from_date=from_date.isoformat(),
        to_date=to_date.isoformat(),
    ))
    have_dates = {o.iso_date for o in existing}
    for d in recurrence.occurrence_dates(series.recurrence, from_date, to_date):
        iso = d.isoformat()
        if iso in have_dates:
            continue
        occ = StandingOccurrence(
            community_id=series.community_id,
            app_id=series.app_id,
            series_id=series.series_id,
            iso_date=iso,
            occurrence_id=occurrence_id(series.series_id, iso),
        )
        db.put_standing_occurrence(occ)
        existing.append(occ)
        have_dates.add(iso)
    existing.sort(key=lambda o: o.iso_date)
    return existing


# --- reminders --------------------------------------------------------------


def _add_months_date(d: dt.date, n: int) -> dt.date:
    idx = (d.month - 1) + n
    return d.replace(year=d.year + idx // 12, month=idx % 12 + 1, day=1)


def _occurrence_datetime(occ: StandingOccurrence, series: StandingSeries,
                         tz: ZoneInfo) -> dt.datetime:
    """Local start datetime of an occurrence (its start_time, else the series
    default, else noon as a safe fallback)."""
    time_str = occ.start_time or series.default_start_time or "12:00"
    try:
        hh, mm = (int(x) for x in time_str.split(":")[:2])
    except (ValueError, AttributeError):
        hh, mm = 12, 0
    d = dt.date.fromisoformat(occ.iso_date)
    return dt.datetime(d.year, d.month, d.day, hh, mm, tzinfo=tz)


def materialize_occurrence_reminders(
    community: Community | None, app: Application, series: StandingSeries,
    *, horizon_months: int = 12,
) -> int:
    """(Re)queue reminder Notifications for this series' upcoming occurrences.

    Deletes the app's existing reminders, then inserts one per (future
    occurrence × eligible member) at ``reminder_lead_days`` before the meeting.
    Called on setup-save. Returns the count inserted.

    A standing app has no slots, so EVERY Notification for it is an occurrence
    reminder — ``delete_notifications_for_app`` is a safe full reset. send_at is
    formatted identically to coverage (UTC isoformat, seconds) so the notifier's
    string-compared ``list_pending_notifications`` picks them up.
    """
    db.delete_notifications_for_app(app.app_id)
    lead_days = series.reminder_lead_days or 0
    if lead_days <= 0:
        return 0

    tz_name = (app.default_timezone
               or (community.default_timezone if community else "America/New_York"))
    tz = ZoneInfo(tz_name)
    now_utc = dt.datetime.now(dt.timezone.utc)
    today = now_utc.astimezone(tz).date()
    horizon_end = _add_months_date(today.replace(day=1), horizon_months)

    community_id = community.community_id if community else app.community_id
    member_ids = {m.user_id for m in db.list_memberships_for_app(app.app_id)}
    users = {u.user_id: u for u in db.list_users(community_id)}
    eligible = [
        u for uid in member_ids
        if (u := users.get(uid)) is not None
        and u.email and not u.email_undeliverable and u.channel != "none"
    ]
    if not eligible:
        return 0

    lead_minutes = lead_days * 1440
    ntfs: list[Notification] = []
    for occ in db.list_standing_occurrences(
            app.app_id, from_date=today.isoformat(),
            to_date=horizon_end.isoformat()):
        if occ.state == "cancelled":
            continue
        send_at_utc = (_occurrence_datetime(occ, series, tz)
                       .astimezone(dt.timezone.utc)
                       - dt.timedelta(days=lead_days))
        if send_at_utc <= now_utc:
            continue  # the lead window has already passed
        yyyy_mm = occ.iso_date[:7]
        for u in eligible:
            ntfs.append(Notification(
                community_id=u.community_id, app_id=app.app_id,
                user_id=u.user_id, slot_id=occ.occurrence_id,
                yyyy_mm=yyyy_mm, source="occurrence",
                send_at=send_at_utc.isoformat(timespec="seconds"),
                lead_minutes=lead_minutes,
                notification_id=f"occ-{occ.occurrence_id}-{u.user_id}"))
    if ntfs:
        db.put_notifications(ntfs)
    return len(ntfs)
