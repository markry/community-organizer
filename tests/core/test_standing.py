"""Tests for standing_event occurrence materialization (slice 3)."""
from __future__ import annotations

import datetime as dt

from community_organizer.core import db, standing
from community_organizer.core.models import StandingOccurrence, StandingSeries


def _series(**kw) -> StandingSeries:
    base = dict(community_id="c1", app_id="a1", recurrence="monthly_2nd_tue")
    base.update(kw)
    s = StandingSeries(**base)
    db.put_standing_series(s)
    return s


def test_materialize_creates_occurrences_on_rule_dates(ddb_table) -> None:
    s = _series()
    occs = standing.materialize_occurrences(s, dt.date(2026, 7, 1), dt.date(2026, 9, 30))
    # 2nd Tuesday of Jul/Aug/Sep 2026
    assert [o.iso_date for o in occs] == ["2026-07-14", "2026-08-11", "2026-09-08"]
    assert all(o.state == "scheduled" for o in occs)
    assert all(o.series_id == s.series_id for o in occs)
    # persisted
    stored = list(db.list_standing_occurrences("a1"))
    assert {o.iso_date for o in stored} == {"2026-07-14", "2026-08-11", "2026-09-08"}


def test_materialize_is_idempotent(ddb_table) -> None:
    s = _series()
    first = standing.materialize_occurrences(s, dt.date(2026, 7, 1), dt.date(2026, 9, 30))
    second = standing.materialize_occurrences(s, dt.date(2026, 7, 1), dt.date(2026, 9, 30))
    assert [o.occurrence_id for o in first] == [o.occurrence_id for o in second]
    # no duplicate rows
    assert len(list(db.list_standing_occurrences("a1"))) == 3


def test_materialize_preserves_existing_exceptions(ddb_table) -> None:
    s = _series()
    # Pre-existing cancelled occurrence on a rule date.
    cancelled = StandingOccurrence(
        community_id="c1", app_id="a1", series_id=s.series_id,
        iso_date="2026-08-11", state="cancelled",
        occurrence_id=standing.occurrence_id(s.series_id, "2026-08-11"),
    )
    db.put_standing_occurrence(cancelled)
    occs = standing.materialize_occurrences(s, dt.date(2026, 7, 1), dt.date(2026, 9, 30))
    by_date = {o.iso_date: o for o in occs}
    assert by_date["2026-08-11"].state == "cancelled"  # not clobbered
    assert by_date["2026-07-14"].state == "scheduled"


def test_materialize_clips_to_range(ddb_table) -> None:
    s = _series()
    occs = standing.materialize_occurrences(s, dt.date(2026, 7, 15), dt.date(2026, 8, 31))
    # Jul 14 is before the window start -> excluded; only Aug 11.
    assert [o.iso_date for o in occs] == ["2026-08-11"]
