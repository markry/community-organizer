"""Test for the inbound iCal PARTSTAT=ACCEPTED handler (#217).

The DECLINE/TENTATIVE paths existed and we extended the same parser
with an ACCEPTED branch that stamps the Assignment as confirmed via
ical_reply. The unit-level check is: given an unconfirmed assignment
row, ``_handle_accepted(slot_id, user_id)`` upgrades it.
"""
from __future__ import annotations

from community_organizer.core import db
from community_organizer.core.models import (
    Application, Assignment, Community, Schedule, Slot, User,
)
from community_organizer.lambdas import inbound


def _seed_one_assignment(ddb_table) -> tuple[str, str]:
    """Stand up a community + app + slot + unconfirmed admin assignment.
    Returns (slot_id, user_id) so the test can drive the handler."""
    cid = "c1"
    db.put_community(Community(community_id=cid, name="Test"))
    db.put_user(User(community_id=cid, email="m@example.com",
                     name="Member", user_id="u1"))
    app = Application(community_id=cid, name="App", app_type="coverage",
                      period_type="monthly")
    db.put_application(app)
    sch = Schedule(community_id=cid, app_id=app.app_id, yyyy_mm="2026-07",
                   state="published")
    db.put_schedule(sch)
    db.put_slot(Slot(
        community_id=cid, app_id=app.app_id, yyyy_mm="2026-07",
        template_id="tpl", name="Sunday 8am", day_of_week=6,
        start_time="08:00", arrival_offset_minutes=0,
        duration_minutes=60, required_volunteers=1, min_volunteers=1,
        concrete_date="2026-07-12T08:00:00+00:00",
        local_date="2026-07-12", slot_id="s1", max_volunteers=1,
    ))
    db.put_assignment(Assignment(
        community_id=cid, app_id=app.app_id, yyyy_mm="2026-07",
        slot_id="s1", user_id="u1", local_date="2026-07-12",
    ))
    return "s1", "u1"


def test_handle_accepted_marks_assignment_confirmed(
        ddb_table, monkeypatch) -> None:
    slot_id, user_id = _seed_one_assignment(ddb_table)

    # The inbound handler uses _resolve_community_for_user_id which
    # reads community via the User row's GSI. Force it to c1 for this
    # test rather than walking the actual lookup path.
    monkeypatch.setattr(inbound, "_resolve_community_for_user_id",
                        lambda _uid: "c1")

    result = inbound._handle_accepted(slot_id, user_id)
    assert result is True

    after = list(db.list_assignments_for_slot("a1", "2026-07", "s1")
                 if False else  # noqa: B015 placeholder for app_id discovery
                 db.list_assignments_for_month(
                     next(db.list_applications("c1")).app_id, "2026-07"))
    assert any(a.confirmed_at and a.confirmed_via == "ical_reply"
               for a in after)


def test_handle_accepted_missing_slot_returns_false(
        ddb_table, monkeypatch) -> None:
    """ACCEPTED for a slot we don't have (already deleted, swap completed,
    cross-community noise) returns False and doesn't blow up."""
    db.put_community(Community(community_id="c1", name="Test"))
    db.put_user(User(community_id="c1", email="m@example.com",
                     name="Member", user_id="u1"))
    monkeypatch.setattr(inbound, "_resolve_community_for_user_id",
                        lambda _uid: "c1")

    result = inbound._handle_accepted("nonexistent-slot", "u1")
    assert result is False
