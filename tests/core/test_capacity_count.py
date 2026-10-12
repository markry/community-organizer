"""How many people hold a slot, and the capacity check that depends on it.

Found by independent review (2026-10-11), in two rounds.

First: the count was only maintained by the atomic self-signup, so an
admin-made assignment was invisible to it and a member could then sign up to
a slot that was already full; two releases of one assignment both decremented
it; and on the live table it was wrong on nearly every slot.

Second: repairing it with a follow-up write was not enough. With the row and
the count changed in separate steps, a delayed step could land after a repair
and reopen a full slot, or close one with room in it.

So the count now lives in its own row and changes only inside the transaction
that adds or removes an assignment; a repair is fenced by a generation number.
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


def _count(slot: Slot) -> int | None:
    return db.get_slot_count(slot.app_id, slot.yyyy_mm, slot.slot_id)


def _rows(slot: Slot) -> int:
    return sum(1 for _ in db.list_assignments_for_slot(
        slot.app_id, slot.yyyy_mm, slot.slot_id))


def _signup(slot: Slot, user_id: str):
    return db.atomic_signup_assignment(slot, user_id=user_id, community_id="c1")


def _release(slot: Slot, user_id: str) -> None:
    db.delete_assignment(slot.app_id, slot.yyyy_mm, slot.slot_id, user_id)


def _legacy_row(slot: Slot, user_id: str) -> None:
    """An assignment written the way the live table's were: no count."""
    db._table().put_item(Item={
        "PK": db._app_month_pk(slot.app_id, slot.yyyy_mm),
        "SK": db._asgn_sk(slot.slot_id, user_id),
        "community_id": "c1", "app_id": slot.app_id,
        "yyyy_mm": slot.yyyy_mm, "slot_id": slot.slot_id, "user_id": user_id,
        "local_date": slot.local_date, "assignment_id": user_id,
        "created_at": "2030-01-01T00:00:00+00:00", "version": 0})


# ---- the sequential failures -----------------------------------------------

def test_member_cannot_sign_up_to_a_slot_an_admin_already_filled(
        ddb_table) -> None:
    slot = _slot(max_vol=1)
    _admin_assign(slot, "u1")
    with pytest.raises(db.CapacityExceeded):
        _signup(slot, "u2")
    assert _rows(slot) == 1


def test_capacity_holds_for_a_slot_that_predates_the_count(ddb_table) -> None:
    slot = _slot(max_vol=2)
    _legacy_row(slot, "u1"); _legacy_row(slot, "u2")
    assert _count(slot) is None and _rows(slot) == 2
    with pytest.raises(db.CapacityExceeded):
        _signup(slot, "u3")
    assert _count(slot) == 2                       # built from the real rows


def test_a_released_place_can_be_taken(ddb_table) -> None:
    slot = _slot(max_vol=1)
    _admin_assign(slot, "u1")
    _release(slot, "u1")
    _signup(slot, "u2")
    assert _rows(slot) == 1 and _count(slot) == 1


def test_admin_assignment_counts_once_however_often_it_is_saved(
        ddb_table) -> None:
    slot = _slot(max_vol=5)
    _admin_assign(slot, "u1")
    _admin_assign(slot, "u1")                      # re-save, not a new person
    _admin_assign(slot, "u2")
    assert _count(slot) == 2 and _rows(slot) == 2


def test_an_admin_may_still_go_over_the_maximum(ddb_table) -> None:
    """Deliberate: the cap is for self-signup. The count stays true."""
    slot = _slot(max_vol=1)
    _admin_assign(slot, "u1")
    _admin_assign(slot, "u2")
    assert _rows(slot) == 2 and _count(slot) == 2
    with pytest.raises(db.CapacityExceeded):
        _signup(slot, "u3")


def test_releasing_twice_counts_once(ddb_table) -> None:
    slot = _slot(max_vol=5)
    _signup(slot, "u1"); _signup(slot, "u2")
    _release(slot, "u1"); _release(slot, "u1")
    assert _count(slot) == 1 and _rows(slot) == 1


def test_releasing_something_that_was_never_assigned_changes_nothing(
        ddb_table) -> None:
    slot = _slot(max_vol=5)
    _signup(slot, "u1")
    _release(slot, "nobody")
    assert _count(slot) == 1


def test_uncapped_slot_still_takes_everyone(ddb_table) -> None:
    slot = _slot(max_vol=None)
    for i in range(12):
        _admin_assign(slot, f"a{i}") if i % 2 else _signup(slot, f"m{i}")
    assert _rows(slot) == 12 and _count(slot) == 12


# ---- the count cannot be knocked out of step -------------------------------

def test_saving_the_slot_does_not_touch_the_count(ddb_table) -> None:
    """Second-round reproduction B. An admin edits the slot from a copy read
    before it filled. The count is not part of the slot row, so the save has
    nothing stale to write back."""
    slot = _slot(max_vol=1)
    stale = db.find_slot_in_month(slot.app_id, slot.yyyy_mm, slot.slot_id)
    _signup(slot, "u1")
    stale.name = "Renamed"
    db.put_slot(stale)
    assert _count(slot) == 1
    with pytest.raises(db.CapacityExceeded):
        _signup(slot, "u2")


def test_row_and_count_change_together_or_not_at_all(
        ddb_table, monkeypatch) -> None:
    """Second-round reproductions A and C both needed a moment where the row
    had changed and the count had not. Make every transaction fail: neither
    may have moved."""
    import boto3
    slot = _slot(max_vol=2)
    _signup(slot, "u1")

    class _Down:
        def transact_write_items(self, **_kw):
            raise RuntimeError("store unavailable")
    real = boto3.client
    monkeypatch.setattr(db.boto3, "client", lambda *_a, **_k: _Down())
    for attempt in (lambda: _signup(slot, "u2"),
                    lambda: _admin_assign(slot, "u3"),
                    lambda: _release(slot, "u1")):
        with pytest.raises(RuntimeError):
            attempt()
    monkeypatch.setattr(db.boto3, "client", real)
    assert _rows(slot) == 1 and _count(slot) == 1


def test_no_step_is_left_for_later(ddb_table, monkeypatch) -> None:
    """A release used to delete the row and decrement afterwards. Nothing but
    a transaction may write the count now."""
    slot = _slot(max_vol=2)
    _signup(slot, "u1")
    writes = []
    real_table = db._table

    class _Spy:
        def __getattr__(self, name):
            target = getattr(real_table(), name)
            if name in ("update_item", "put_item", "delete_item"):
                return lambda **kw: (writes.append(name), target(**kw))[1]
            return target
    monkeypatch.setattr(db, "_table", lambda: _Spy())
    _release(slot, "u1")
    _signup(slot, "u2")
    assert writes == []                            # transactions only


# ---- repair ----------------------------------------------------------------

def test_repair_sets_the_real_number(ddb_table) -> None:
    slot = _slot(max_vol=5)
    for u in ("u1", "u2", "u3"):
        _legacy_row(slot, u)
    assert db.reconcile_slot_count(slot.app_id, slot.yyyy_mm, slot.slot_id) == 3
    assert _count(slot) == 3


def _interleave(monkeypatch, slot, action):
    """Run ``action`` once, in the gap between a repair counting the rows and
    writing its answer."""
    real_query = db._paginate_query
    fired = []

    def query(**kw):
        rows = list(real_query(**kw))
        if not fired and kw.get("ProjectionExpression") == "SK":
            fired.append(1)
            monkeypatch.setattr(db, "_paginate_query", real_query)
            action()
        return iter(rows)
    monkeypatch.setattr(db, "_paginate_query", query)
    return fired


def test_repair_loses_to_a_signup_that_lands_while_it_counts(
        ddb_table, monkeypatch) -> None:
    slot = _slot(max_vol=5)
    _legacy_row(slot, "u1")
    fired = _interleave(monkeypatch, slot, lambda: _signup(slot, "u2"))
    db.reconcile_slot_count(slot.app_id, slot.yyyy_mm, slot.slot_id)
    assert fired and _rows(slot) == 2 and _count(slot) == 2


def test_repair_is_not_fooled_by_one_in_one_out(ddb_table, monkeypatch) -> None:
    """The count reads the same before and after, but the roster changed
    while it was being counted. Comparing the number alone would accept a
    count taken in the middle."""
    slot = _slot(max_vol=5)
    _signup(slot, "u1")
    # Knock the stored number wrong so the repair wants to write.
    db._table().update_item(
        Key={"PK": db._app_month_pk(slot.app_id, slot.yyyy_mm),
             "SK": db._slot_count_sk(slot.slot_id)},
        UpdateExpression="SET n = :n", ExpressionAttributeValues={":n": 3})

    def churn():
        _signup(slot, "u2")                        # 3 -> 4
        _release(slot, "u1")                       # 4 -> 3 again

    fired = _interleave(monkeypatch, slot, churn)
    db.reconcile_slot_count(slot.app_id, slot.yyyy_mm, slot.slot_id)
    assert fired and _rows(slot) == 1 and _count(slot) == 1


def test_regenerating_a_month_clears_its_counts(ddb_table) -> None:
    slot = _slot(max_vol=1)
    _signup(slot, "u1")
    db.delete_assignments_for_month(slot.app_id, slot.yyyy_mm)
    assert _count(slot) is None and _rows(slot) == 0
    _signup(slot, "u2")                            # not refused as "full"
    assert _count(slot) == 1


def test_repair_on_a_slot_with_no_assignments(ddb_table) -> None:
    assert db.reconcile_slot_count("a1", "2030-06", "no-such-slot") == 0
