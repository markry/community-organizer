"""Tests for the assignment-confirmation helpers in
``community_organizer.core.db`` (slice #217).

Covers:
- ``atomic_signup_assignment`` stamps confirmed_at + confirmed_via.
- ``confirm_assignment`` upgrades an unconfirmed row.
- ``confirm_assignment`` returns False when the row is missing.
- Re-confirming overwrites with the newer timestamp + via.
"""
from __future__ import annotations

from community_organizer.core import db
from community_organizer.core.models import Assignment, Slot


def _make_slot(app_id: str = "a1", yyyy_mm: str = "2026-07") -> Slot:
    return Slot(
        community_id="c1", app_id=app_id, yyyy_mm=yyyy_mm,
        template_id="tpl", name="Beta smoke 8 AM",
        day_of_week=6, start_time="08:00",
        arrival_offset_minutes=0, duration_minutes=60,
        required_volunteers=1, min_volunteers=1,
        concrete_date="2026-07-12T08:00:00+00:00",
        local_date="2026-07-12",
        slot_id="s1", max_volunteers=1,
    )


def test_self_signup_stamps_confirmed(ddb_table) -> None:
    """Self-signup is implicitly confirmed at creation time."""
    slot = _make_slot()
    db.put_slot(slot)

    asg = db.atomic_signup_assignment(
        slot, user_id="u1", community_id="c1")
    assert asg.confirmed_at is not None
    assert asg.confirmed_via == "self_signup"

    # And it round-trips from DDB.
    stored = list(db.list_assignments_for_slot("a1", "2026-07", "s1"))
    assert len(stored) == 1
    assert stored[0].confirmed_at is not None
    assert stored[0].confirmed_via == "self_signup"


def test_confirm_assignment_upgrades_unconfirmed(ddb_table) -> None:
    """An admin-created assignment (no confirmed_at) becomes confirmed
    when confirm_assignment is called."""
    db.put_assignment(Assignment(
        community_id="c1", app_id="a1", yyyy_mm="2026-07",
        slot_id="s1", user_id="u1", local_date="2026-07-12",
    ))
    before = list(db.list_assignments_for_slot("a1", "2026-07", "s1"))
    assert before[0].confirmed_at is None
    assert before[0].confirmed_via is None

    ok = db.confirm_assignment("a1", "2026-07", "s1", "u1",
                               via="member_login")
    assert ok is True

    after = list(db.list_assignments_for_slot("a1", "2026-07", "s1"))
    assert after[0].confirmed_at is not None
    assert after[0].confirmed_via == "member_login"


def test_confirm_assignment_missing_row_returns_false(ddb_table) -> None:
    """Confirming an assignment that doesn't exist (e.g. it was already
    swapped or released) is a silent no-op that returns False."""
    ok = db.confirm_assignment("a1", "2026-07", "s1", "ghost-uid",
                               via="ical_reply")
    assert ok is False


def test_confirm_assignment_overwrites_via(ddb_table) -> None:
    """Re-confirming via a different path updates the via field —
    useful when a member self-confirms first and then later accepts
    the calendar invite (ical_reply wins as the more recent signal)."""
    db.put_assignment(Assignment(
        community_id="c1", app_id="a1", yyyy_mm="2026-07",
        slot_id="s1", user_id="u1", local_date="2026-07-12",
    ))
    db.confirm_assignment("a1", "2026-07", "s1", "u1", via="member_login")
    after_first = list(db.list_assignments_for_slot("a1", "2026-07", "s1"))[0]
    assert after_first.confirmed_via == "member_login"

    db.confirm_assignment("a1", "2026-07", "s1", "u1", via="ical_reply")
    after_second = list(db.list_assignments_for_slot("a1", "2026-07", "s1"))[0]
    assert after_second.confirmed_via == "ical_reply"
