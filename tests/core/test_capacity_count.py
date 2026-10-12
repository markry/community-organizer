"""A slot's ``assignment_count`` and the capacity check that reads it.

Found by independent review (2026-10-11): the tally was only maintained by the
atomic self-signup, so an admin-made assignment was invisible to it and a
member could then sign up to a slot that was already full; and two releases of
one assignment both decremented it. On the live table it was wrong on nearly
every slot. The capacity check now reconciles the tally against the real rows
first, and the other write paths keep it close in between.
"""
from __future__ import annotations

import pytest

from community_organizer.core import db
from community_organizer.core.models import Assignment, Slot


def _slot(*, max_vol: int | None = 1, required: int = 1) -> Slot:
    slot = Slot(
        community_id="c1", app_id="a1", yyyy_mm="2030-06",
        template_id="t1", name="Sun 8 AM", day_of_week=6,
        start_time="08:00", arrival_offset_minutes=0,
        duration_minutes=60, required_volunteers=required,
        min_volunteers=1, max_volunteers=max_vol,
        concrete_date="2030-06-02", local_date="2030-06-02",
    )
    db.put_slot(slot)
    return slot


def _admin_assign(slot: Slot, user_id: str) -> None:
    db.put_assignment(Assignment(
        community_id="c1", app_id=slot.app_id, yyyy_mm=slot.yyyy_mm,
        slot_id=slot.slot_id, user_id=user_id, local_date=slot.local_date,
        created_by="admin"))


def _count(slot: Slot) -> int:
    return db.find_slot_in_month(slot.app_id, slot.yyyy_mm,
                                 slot.slot_id).assignment_count


def _rows(slot: Slot) -> int:
    return sum(1 for _ in db.list_assignments_for_slot(
        slot.app_id, slot.yyyy_mm, slot.slot_id))


def _signup(slot: Slot, user_id: str):
    return db.atomic_signup_assignment(slot, user_id=user_id, community_id="c1")


# ---- the reported failure ---------------------------------------------------

def test_member_cannot_sign_up_to_a_slot_an_admin_already_filled(
        ddb_table) -> None:
    slot = _slot(max_vol=1)
    _admin_assign(slot, "u1")
    with pytest.raises(db.CapacityExceeded):
        _signup(slot, "u2")
    assert _rows(slot) == 1


def test_capacity_holds_even_when_the_tally_was_never_kept(ddb_table) -> None:
    """Rows written the way the live table's were: present, tally untouched."""
    slot = _slot(max_vol=2)
    for uid in ("u1", "u2"):
        db._table().put_item(Item={
            "PK": db._app_month_pk(slot.app_id, slot.yyyy_mm),
            "SK": db._asgn_sk(slot.slot_id, uid),
            "community_id": "c1", "app_id": slot.app_id,
            "yyyy_mm": slot.yyyy_mm, "slot_id": slot.slot_id, "user_id": uid,
            "local_date": slot.local_date, "assignment_id": uid,
            "created_at": "2030-01-01T00:00:00+00:00", "version": 0})
    assert _count(slot) == 0 and _rows(slot) == 2
    with pytest.raises(db.CapacityExceeded):
        _signup(slot, "u3")
    assert _count(slot) == 2                       # and it healed the tally


def test_a_stale_whole_slot_save_does_not_reopen_a_full_slot(ddb_table) -> None:
    """An admin edits the slot from a copy read before it filled; saving it
    writes the old tally back."""
    slot = _slot(max_vol=1)
    stale = db.find_slot_in_month(slot.app_id, slot.yyyy_mm, slot.slot_id)
    _signup(slot, "u1")
    stale.name = "Renamed"
    db.put_slot(stale)                             # tally back to 0
    assert _count(slot) == 0
    with pytest.raises(db.CapacityExceeded):
        _signup(slot, "u2")


def test_a_released_place_can_be_taken(ddb_table) -> None:
    slot = _slot(max_vol=1)
    _admin_assign(slot, "u1")
    db.delete_assignment(slot.app_id, slot.yyyy_mm, slot.slot_id, "u1")
    _signup(slot, "u2")
    assert _rows(slot) == 1 and _count(slot) == 1


# ---- the tally is kept by every path ---------------------------------------

def test_admin_assignment_counts_once_however_often_it_is_saved(
        ddb_table) -> None:
    slot = _slot(max_vol=5)
    _admin_assign(slot, "u1")
    _admin_assign(slot, "u1")                      # re-save, not a new person
    _admin_assign(slot, "u2")
    assert _count(slot) == 2


def test_releasing_twice_decrements_once(ddb_table) -> None:
    """Two releases of the same assignment racing: only the one that removed
    the row may take a place off the tally."""
    slot = _slot(max_vol=5)
    _signup(slot, "u1")
    _signup(slot, "u2")
    db.delete_assignment(slot.app_id, slot.yyyy_mm, slot.slot_id, "u1")
    db.delete_assignment(slot.app_id, slot.yyyy_mm, slot.slot_id, "u1")
    assert _count(slot) == 1 and _rows(slot) == 1


def test_releasing_something_that_was_never_assigned_changes_nothing(
        ddb_table) -> None:
    slot = _slot(max_vol=5)
    _signup(slot, "u1")
    db.delete_assignment(slot.app_id, slot.yyyy_mm, slot.slot_id, "nobody")
    assert _count(slot) == 1


# ---- reconcile -------------------------------------------------------------

def test_reconcile_reports_and_stores_the_real_number(ddb_table) -> None:
    slot = _slot(max_vol=5)
    _signup(slot, "u1")
    _signup(slot, "u2")
    stale = db.find_slot_in_month(slot.app_id, slot.yyyy_mm, slot.slot_id)
    stale.assignment_count = 9
    db.put_slot(stale)
    assert db.reconcile_assignment_count(
        slot.app_id, slot.yyyy_mm, slot.local_date, slot.slot_id) == 2
    assert _count(slot) == 2


def test_reconcile_does_not_overwrite_a_signup_that_lands_meanwhile(
        ddb_table, monkeypatch) -> None:
    """A signup increments the tally after reconcile has read it and before
    reconcile writes. Its conditional write must lose."""
    slot = _slot(max_vol=5)
    _signup(slot, "u1")
    real_query = db._paginate_query
    fired = []

    def query_then_signup(**kw):
        rows = list(real_query(**kw))
        if not fired and kw.get("ProjectionExpression") == "SK":
            fired.append(1)
            monkeypatch.setattr(db, "_paginate_query", real_query)
            # Lands between reconcile's row count and its write.
            db._table().update_item(
                Key={"PK": db._app_month_pk(slot.app_id, slot.yyyy_mm),
                     "SK": db._slot_sk(slot.local_date, slot.slot_id)},
                UpdateExpression="ADD assignment_count :one",
                ExpressionAttributeValues={":one": 1})
        return iter(rows)

    # Make the tally wrong so reconcile wants to write.
    stale = db.find_slot_in_month(slot.app_id, slot.yyyy_mm, slot.slot_id)
    stale.assignment_count = 0
    db.put_slot(stale)
    monkeypatch.setattr(db, "_paginate_query", query_then_signup)
    db.reconcile_assignment_count(slot.app_id, slot.yyyy_mm, slot.local_date,
                                  slot.slot_id)
    assert fired and _count(slot) == 1             # the increment survived


def test_reconcile_on_a_missing_slot_is_harmless(ddb_table) -> None:
    assert db.reconcile_assignment_count("a1", "2030-06", "2030-06-02",
                                         "no-such-slot") == 0


def test_uncapped_slot_still_takes_everyone(ddb_table) -> None:
    slot = _slot(max_vol=None)
    for i in range(12):
        _admin_assign(slot, f"a{i}") if i % 2 else _signup(slot, f"m{i}")
    assert _rows(slot) == 12 and _count(slot) == 12
