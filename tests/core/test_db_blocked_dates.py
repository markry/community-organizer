"""Tests for the BlockedDate helpers in ``community_organizer.core.db``.

These cover the I/O surface used by the My-Availability page and the
admin cohort-picker filter.
"""
from __future__ import annotations

from community_organizer.core import db
from community_organizer.core.models import Assignment, BlockedDate


def test_put_get_delete_round_trip(ddb_table) -> None:
    b = BlockedDate(community_id="c1", app_id="a1", user_id="u1",
                    local_date="2026-07-11")
    db.put_blocked_date(b)

    got = db.get_blocked_date("a1", "u1", "2026-07-11")
    assert got is not None
    assert got.user_id == "u1"
    assert got.local_date == "2026-07-11"

    db.delete_blocked_date("a1", "u1", "2026-07-11")
    assert db.get_blocked_date("a1", "u1", "2026-07-11") is None


def test_list_blocked_users_on_date(ddb_table) -> None:
    db.put_blocked_date(BlockedDate(
        community_id="c1", app_id="a1", user_id="u1", local_date="2026-07-11"))
    db.put_blocked_date(BlockedDate(
        community_id="c1", app_id="a1", user_id="u2", local_date="2026-07-11"))
    db.put_blocked_date(BlockedDate(
        community_id="c1", app_id="a1", user_id="u3", local_date="2026-07-12"))
    db.put_blocked_date(BlockedDate(
        community_id="c1", app_id="a2", user_id="u1", local_date="2026-07-11"))

    blocked = db.list_blocked_users_on_date("a1", "2026-07-11")
    assert blocked == {"u1", "u2"}    # u3 different date, a2's u1 different app


def test_list_blocked_dates_for_user_per_app(ddb_table) -> None:
    """GSI returns only this (user, app) pair's blocks, sorted by date."""
    for d in ["2026-07-11", "2026-07-12", "2026-08-05"]:
        db.put_blocked_date(BlockedDate(
            community_id="c1", app_id="a1", user_id="u1", local_date=d))
    db.put_blocked_date(BlockedDate(
        community_id="c1", app_id="a2", user_id="u1", local_date="2026-07-11"))
    db.put_blocked_date(BlockedDate(
        community_id="c1", app_id="a1", user_id="u2", local_date="2026-07-11"))

    dates = [b.local_date for b in db.list_blocked_dates_for_user("a1", "u1")]
    assert dates == ["2026-07-11", "2026-07-12", "2026-08-05"]


def test_list_blocked_dates_for_user_since_date_filters(ddb_table) -> None:
    """``since_date`` trims past blocks at the GSI range, not in Python."""
    for d in ["2026-01-01", "2026-06-15", "2026-12-25"]:
        db.put_blocked_date(BlockedDate(
            community_id="c1", app_id="a1", user_id="u1", local_date=d))

    future = list(db.list_blocked_dates_for_user(
        "a1", "u1", since_date="2026-06-01"))
    assert [b.local_date for b in future] == ["2026-06-15", "2026-12-25"]


def test_is_user_assigned_on_date_true_when_assignment_exists(ddb_table) -> None:
    db.put_assignment(Assignment(
        community_id="c1", app_id="a1", yyyy_mm="2026-07",
        slot_id="s1", user_id="u1", local_date="2026-07-11",
    ))
    assert db.is_user_assigned_on_date("a1", "u1", "2026-07-11") is True
    assert db.is_user_assigned_on_date("a1", "u1", "2026-07-12") is False


def test_is_user_assigned_on_date_scoped_to_app(ddb_table) -> None:
    """An assignment in a different app does not block this app's date."""
    db.put_assignment(Assignment(
        community_id="c1", app_id="other-app", yyyy_mm="2026-07",
        slot_id="s1", user_id="u1", local_date="2026-07-11",
    ))
    assert db.is_user_assigned_on_date("a1", "u1", "2026-07-11") is False


def test_put_blocked_date_overwrites_existing(ddb_table) -> None:
    """Re-blocking the same date is idempotent (same composite SK)."""
    b1 = BlockedDate(community_id="c1", app_id="a1", user_id="u1",
                     local_date="2026-07-11")
    db.put_blocked_date(b1)
    b2 = BlockedDate(community_id="c1", app_id="a1", user_id="u1",
                     local_date="2026-07-11")
    db.put_blocked_date(b2)

    assert list(db.list_blocked_dates_for_user("a1", "u1")) != []
    assert len(list(db.list_blocked_dates_for_user("a1", "u1"))) == 1
