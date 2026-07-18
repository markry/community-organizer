"""Tests for slice 1 — date-centric event app data layer (#206).

Covers the new db.py helpers for the two new app types
(standing_event + flexible_event). Pure data-layer tests; no web
or notifier code involved yet.
"""
from __future__ import annotations

import pytest

from community_organizer.core import db
from community_organizer.core.models import (
    FlexibleEvent, FlexiblePollOption, FlexibleRSVP, FlexibleSeries,
    StandingOccurrence, StandingRSVP, StandingSeries,
)


# ---- StandingSeries --------------------------------------------------------

def test_put_and_get_standing_series(ddb_table) -> None:
    s = StandingSeries(community_id="c1", app_id="a1",
                       recurrence="monthly_2nd_tue",
                       default_location="Parish hall")
    db.put_standing_series(s)
    got = db.get_standing_series_for_app("a1")
    assert got is not None
    assert got.recurrence == "monthly_2nd_tue"
    assert got.default_location == "Parish hall"
    # Defaults preserved.
    assert got.attendance_tracking is False
    assert got.send_calendar_invites is False
    assert got.reminder_lead_days == 1


def test_get_standing_series_for_unknown_app_is_none(ddb_table) -> None:
    assert db.get_standing_series_for_app("never-existed") is None


def test_put_standing_series_stale_version_raises(ddb_table) -> None:
    s = StandingSeries(community_id="c1", app_id="a1",
                       recurrence="monthly_2nd_tue")
    db.put_standing_series(s)
    # A wins.
    a_view = db.get_standing_series_for_app("a1")
    a_view.default_location = "From A"
    db.put_standing_series(a_view, expected_version=0)
    # B still holds v0.
    b_view = StandingSeries(community_id="c1", app_id="a1",
                            recurrence="monthly_2nd_tue",
                            default_location="From B",
                            series_id=a_view.series_id, version=0)
    with pytest.raises(db.ConcurrencyConflict):
        db.put_standing_series(b_view, expected_version=0)
    final = db.get_standing_series_for_app("a1")
    assert final.default_location == "From A"


# ---- StandingOccurrence ----------------------------------------------------

def test_list_standing_occurrences_in_order(ddb_table) -> None:
    for d in ("2026-07-14", "2026-06-09", "2026-08-11"):
        db.put_standing_occurrence(StandingOccurrence(
            community_id="c1", app_id="a1", series_id="s1",
            iso_date=d))
    got = [o.iso_date for o in db.list_standing_occurrences("a1")]
    # SK sorts naturally by iso_date → June, July, August.
    assert got == ["2026-06-09", "2026-07-14", "2026-08-11"]


def test_list_standing_occurrences_in_range(ddb_table) -> None:
    for d in ("2026-05-12", "2026-07-14", "2026-09-08"):
        db.put_standing_occurrence(StandingOccurrence(
            community_id="c1", app_id="a1", series_id="s1",
            iso_date=d))
    got = [o.iso_date for o in db.list_standing_occurrences(
        "a1", from_date="2026-06-01", to_date="2026-08-31")]
    assert got == ["2026-07-14"]


def test_list_standing_occurrences_excludes_rsvp_rows(ddb_table) -> None:
    """RSVP rows share the OCC# SK prefix; the list must filter them
    out so callers only see the occurrence meta rows."""
    occ = StandingOccurrence(community_id="c1", app_id="a1",
                             series_id="s1", iso_date="2026-07-14")
    db.put_standing_occurrence(occ)
    db.put_standing_rsvp(StandingRSVP(
        community_id="c1", app_id="a1",
        occurrence_id=occ.occurrence_id,
        user_id="u1", response="yes"))
    occs = list(db.list_standing_occurrences("a1"))
    assert len(occs) == 1
    assert occs[0].occurrence_id == occ.occurrence_id


def test_get_standing_occurrence_round_trip(ddb_table) -> None:
    occ = StandingOccurrence(community_id="c1", app_id="a1",
                             series_id="s1", iso_date="2026-07-14",
                             notes="Guest speaker")
    db.put_standing_occurrence(occ)
    got = db.get_standing_occurrence("a1", "2026-07-14",
                                     occ.occurrence_id)
    assert got is not None
    assert got.notes == "Guest speaker"
    assert got.state == "scheduled"


# ---- StandingRSVP ----------------------------------------------------------

def test_standing_rsvp_round_trip_per_occurrence(ddb_table) -> None:
    db.put_standing_rsvp(StandingRSVP(
        community_id="c1", app_id="a1", occurrence_id="o1",
        user_id="alice", response="yes"))
    db.put_standing_rsvp(StandingRSVP(
        community_id="c1", app_id="a1", occurrence_id="o1",
        user_id="bob", response="no"))
    db.put_standing_rsvp(StandingRSVP(
        community_id="c1", app_id="a1", occurrence_id="o2",
        user_id="alice", response="maybe"))
    o1 = sorted((r.user_id, r.response)
                for r in db.list_standing_rsvps_for_occurrence("a1", "o1"))
    assert o1 == [("alice", "yes"), ("bob", "no")]


def test_standing_rsvp_re_vote_is_upsert(ddb_table) -> None:
    """Same (occurrence, user) overwrites — no duplicate rows."""
    db.put_standing_rsvp(StandingRSVP(
        community_id="c1", app_id="a1", occurrence_id="o1",
        user_id="alice", response="yes"))
    db.put_standing_rsvp(StandingRSVP(
        community_id="c1", app_id="a1", occurrence_id="o1",
        user_id="alice", response="no"))
    rsvps = list(db.list_standing_rsvps_for_occurrence("a1", "o1"))
    assert len(rsvps) == 1
    assert rsvps[0].response == "no"


# ---- FlexibleSeries --------------------------------------------------------

def test_put_and_get_flexible_series(ddb_table) -> None:
    s = FlexibleSeries(community_id="c1", app_id="a2",
                       default_location="Vic's house",
                       bring_prompt="Please bring something to share")
    db.put_flexible_series(s)
    got = db.get_flexible_series_for_app("a2")
    assert got is not None
    assert got.default_location == "Vic's house"
    assert got.bring_prompt == "Please bring something to share"


# ---- FlexibleEvent ---------------------------------------------------------

def test_flexible_event_round_trip(ddb_table) -> None:
    evt = FlexibleEvent(community_id="c1", app_id="a2",
                        title="Book club — July")
    db.put_flexible_event(evt)
    got = db.get_flexible_event("a2", evt.event_id)
    assert got is not None
    assert got.title == "Book club — July"
    assert got.state == "poll"


def test_list_flexible_events_excludes_opt_and_rsvp_rows(ddb_table) -> None:
    evt = FlexibleEvent(community_id="c1", app_id="a2", title="Picnic")
    db.put_flexible_event(evt)
    db.put_flexible_poll_option(FlexiblePollOption(
        community_id="c1", app_id="a2", event_id=evt.event_id,
        iso_date="2026-07-04", sort_key=0))
    db.put_flexible_rsvp(FlexibleRSVP(
        community_id="c1", app_id="a2", event_id=evt.event_id,
        user_id="u1"))
    events = list(db.list_flexible_events("a2"))
    assert len(events) == 1
    assert events[0].event_id == evt.event_id


def test_flexible_event_stale_version_raises(ddb_table) -> None:
    evt = FlexibleEvent(community_id="c1", app_id="a2", title="Hike")
    db.put_flexible_event(evt)
    a = db.get_flexible_event("a2", evt.event_id)
    a.title = "Hike (A)"
    db.put_flexible_event(a, expected_version=0)
    b = FlexibleEvent(community_id="c1", app_id="a2",
                      title="Hike (B)", event_id=evt.event_id, version=0)
    with pytest.raises(db.ConcurrencyConflict):
        db.put_flexible_event(b, expected_version=0)


# ---- FlexiblePollOption ----------------------------------------------------

def test_poll_options_listed_in_sort_key_order(ddb_table) -> None:
    for i, d in enumerate(["2026-07-25", "2026-07-04", "2026-07-11"]):
        db.put_flexible_poll_option(FlexiblePollOption(
            community_id="c1", app_id="a2", event_id="e1",
            iso_date=d, sort_key=i))
    dates = [o.iso_date for o in db.list_flexible_poll_options("a2", "e1")]
    # sort_key drives SK ordering (the AA's intent of "first proposed
    # = first shown") regardless of date.
    assert dates == ["2026-07-25", "2026-07-04", "2026-07-11"]


def test_delete_flexible_poll_options_wipes_all(ddb_table) -> None:
    for i in range(3):
        db.put_flexible_poll_option(FlexiblePollOption(
            community_id="c1", app_id="a2", event_id="e1",
            iso_date=f"2026-07-{i+1:02d}", sort_key=i))
    n = db.delete_flexible_poll_options("a2", "e1")
    assert n == 3
    assert list(db.list_flexible_poll_options("a2", "e1")) == []


# ---- FlexibleRSVP ----------------------------------------------------------

def test_flexible_rsvp_re_vote_is_upsert(ddb_table) -> None:
    db.put_flexible_rsvp(FlexibleRSVP(
        community_id="c1", app_id="a2", event_id="e1",
        user_id="alice",
        votes={"opt-1": "yes", "opt-2": "no"}))
    db.put_flexible_rsvp(FlexibleRSVP(
        community_id="c1", app_id="a2", event_id="e1",
        user_id="alice",
        votes={"opt-1": "maybe", "opt-2": "yes"}))
    got = db.get_flexible_rsvp("a2", "e1", "alice")
    assert got is not None
    assert got.votes == {"opt-1": "maybe", "opt-2": "yes"}


def test_flexible_rsvp_post_confirmation_bringing(ddb_table) -> None:
    """After AA closes the poll, the same RSVP row stores the
    confirmed response + bringing field."""
    db.put_flexible_rsvp(FlexibleRSVP(
        community_id="c1", app_id="a2", event_id="e1",
        user_id="alice",
        confirmed_response="yes",
        bringing="garden salad"))
    got = db.get_flexible_rsvp("a2", "e1", "alice")
    assert got.confirmed_response == "yes"
    assert got.bringing == "garden salad"


def test_list_flexible_rsvps(ddb_table) -> None:
    for u in ("alice", "bob", "charlie"):
        db.put_flexible_rsvp(FlexibleRSVP(
            community_id="c1", app_id="a2", event_id="e1",
            user_id=u, confirmed_response="yes"))
    users = sorted(r.user_id
                   for r in db.list_flexible_rsvps("a2", "e1"))
    assert users == ["alice", "bob", "charlie"]
