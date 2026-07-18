"""Tests for ``community_organizer.core.schedule_email`` — the monthly schedule table.

Validates the table that gets embedded in the publish broadcast (every
recipient sees the whole month, not just their own row) and that backs
the admin "Send schedule to me by email" link.

Key invariants pinned here:
    - Subject uses ``app.name`` + role + month/year
    - Third column header is pluralized via ``terminology_plural`` (or
      derived if blank)
    - Cancelled slots are omitted
    - Open slots render as "(open)"
    - DATE / TIME cells use ``rowspan`` for the per-slot grouping
    - ``generate_schedule_table_html`` extracts just the ``<table>``
"""
from __future__ import annotations

import pytest

from community_organizer.core import db, schedule_email
from community_organizer.core.models import (
    Application, Assignment, Community, Schedule, Slot, User,
)


def _seed(ddb_table, *, terminology_plural: str = "") -> tuple[Community, Application, list[User]]:
    """Seed a small dataset: 2 slots on one Sunday with 2+1 assignees."""
    community = Community(community_id="c1", name="Test Parish")
    db.put_community(community)
    app = Application(
        community_id="c1", name="Test Ushers",
        terminology="usher", terminology_plural=terminology_plural,
        event_noun="Mass", default_timezone="America/New_York", app_type="coverage")
    db.put_application(app)

    users = [
        User(community_id="c1", email="a@example.com", name="Alice"),
        User(community_id="c1", email="b@example.com", name="Bob"),
        User(community_id="c1", email="c@example.com", name="Carol"),
    ]
    for u in users:
        db.put_user(u)

    db.put_schedule(Schedule(community_id="c1", app_id=app.app_id,
                             yyyy_mm="2026-05"))

    # Sunday May 31, 2026: 8 AM (Alice + Bob), 10:30 AM (Carol).
    s1 = Slot(community_id="c1", app_id=app.app_id, yyyy_mm="2026-05",
              template_id="t1", name="Sun 8:00 AM", day_of_week=6,
              start_time="08:00", arrival_offset_minutes=10,
              duration_minutes=60, required_volunteers=2, min_volunteers=1,
              concrete_date="2026-05-31", local_date="2026-05-31",
              slot_id="slot-8am")
    s2 = Slot(community_id="c1", app_id=app.app_id, yyyy_mm="2026-05",
              template_id="t1", name="Sun 10:30 AM", day_of_week=6,
              start_time="10:30", arrival_offset_minutes=10,
              duration_minutes=60, required_volunteers=2, min_volunteers=1,
              concrete_date="2026-05-31", local_date="2026-05-31",
              slot_id="slot-1030")
    db.put_slot(s1)
    db.put_slot(s2)

    for u, slot in [(users[0], s1), (users[1], s1), (users[2], s2)]:
        db.put_assignment(Assignment(
            community_id="c1", app_id=app.app_id, yyyy_mm="2026-05",
            slot_id=slot.slot_id, user_id=u.user_id, local_date="2026-05-31",
        ))
    return community, app, users


def test_subject_uses_app_name_and_role(ddb_table) -> None:
    community, app, _ = _seed(ddb_table)
    subj, _, _ = schedule_email.generate_schedule_email(community, app, "2026-05")
    assert "Test Ushers" in subj
    assert "Usher" in subj         # capitalized role
    assert "May 2026" in subj


def test_column_header_uses_explicit_plural_when_set(ddb_table) -> None:
    """If ``terminology_plural`` is set, that wins over the derived form.

    Useful for irregulars: an app with ``terminology="child"``,
    ``terminology_plural="children"`` should NOT show "CHILDS" in the
    table header.
    """
    community, app, _ = _seed(ddb_table, terminology_plural="Ushers")
    _, _, html = schedule_email.generate_schedule_email(community, app, "2026-05")
    assert "<b>USHERS</b>" in html


def test_column_header_falls_back_to_derived_plural(ddb_table) -> None:
    """No explicit plural set -> ``_pluralize`` derives ("usher" -> "ushers")."""
    community, app, _ = _seed(ddb_table, terminology_plural="")
    _, _, html = schedule_email.generate_schedule_email(community, app, "2026-05")
    assert "<b>USHERS</b>" in html


def test_open_slot_renders_open(ddb_table) -> None:
    """A slot with no assignments shows "(open)" in the names cell."""
    community = Community(community_id="c1", name="Test Parish")
    db.put_community(community)
    app = Application(community_id="c1", name="Test Ushers",
                      terminology="usher", app_type="coverage")
    db.put_application(app)
    db.put_schedule(Schedule(community_id="c1", app_id=app.app_id,
                             yyyy_mm="2026-05"))
    db.put_slot(Slot(
        community_id="c1", app_id=app.app_id, yyyy_mm="2026-05",
        template_id="t1", name="Sun 8 AM", day_of_week=6, start_time="08:00",
        arrival_offset_minutes=0, duration_minutes=60,
        required_volunteers=2, min_volunteers=1,
        concrete_date="2026-05-31", local_date="2026-05-31",
    ))

    _, body_text, body_html = schedule_email.generate_schedule_email(
        community, app, "2026-05",
    )
    assert "(open)" in body_text
    assert "(open)" in body_html


def test_cancelled_slot_omitted(ddb_table) -> None:
    """Cancelled slots do not appear in the table at all."""
    community, app, _ = _seed(ddb_table)
    # Cancel the 8 AM slot.
    slot_8am = next(s for s in db.list_slots(app.app_id, "2026-05")
                    if s.start_time == "08:00")
    slot_8am.cancelled = True
    db.put_slot(slot_8am)

    _, _, html = schedule_email.generate_schedule_email(community, app, "2026-05")
    # 10:30 row still present, 8:00 row gone.
    assert "10:30 AM" in html
    assert "8:00 AM" not in html


def test_rowspan_used_for_multi_assignee_slot(ddb_table) -> None:
    """Two assignees on the 8 AM slot → DATE/TIME cells rowspan="2"."""
    community, app, _ = _seed(ddb_table)
    _, _, html = schedule_email.generate_schedule_email(community, app, "2026-05")
    assert 'rowspan="2"' in html       # 8 AM slot has 2 assignees


def test_generate_table_html_extracts_table_only(ddb_table) -> None:
    """The table-only helper returns ``<table>...</table>``, suitable for
    embedding in another HTML body (e.g. the publish broadcast)."""
    community, app, _ = _seed(ddb_table)
    table = schedule_email.generate_schedule_table_html(community, app, "2026-05")
    assert table.startswith("<table")
    assert table.endswith("</table>")
    # Sanity: still contains the recipient names.
    assert "Alice" in table
    assert "Carol" in table


def test_html_escapes_member_name(ddb_table) -> None:
    """Member display name flows into the rendered table — must be
    HTML-escaped so a name like ``<img src=x onerror=alert(1)>`` is
    rendered as literal text, not executed (security fix C3)."""
    community = Community(community_id="c1", name="Test Parish")
    db.put_community(community)
    app = Application(community_id="c1", name="Test Ushers",
                      terminology="usher", app_type="coverage")
    db.put_application(app)
    db.put_schedule(Schedule(community_id="c1", app_id=app.app_id,
                             yyyy_mm="2026-05"))

    evil = User(community_id="c1", email="e@example.com",
                name="<img src=x onerror=alert(1)>")
    db.put_user(evil)

    slot = Slot(community_id="c1", app_id=app.app_id, yyyy_mm="2026-05",
                template_id="t1", name="Sun 8 AM", day_of_week=6,
                start_time="08:00", arrival_offset_minutes=10,
                duration_minutes=60, required_volunteers=1, min_volunteers=1,
                concrete_date="2026-05-31", local_date="2026-05-31")
    db.put_slot(slot)
    db.put_assignment(Assignment(community_id="c1", app_id=app.app_id,
                                 yyyy_mm="2026-05", slot_id=slot.slot_id,
                                 user_id=evil.user_id,
                                 local_date="2026-05-31"))

    _, _, body_html = schedule_email.generate_schedule_email(
        community, app, "2026-05")
    # The raw payload must NOT appear; the escaped form must.
    assert "<img src=x" not in body_html
    assert "&lt;img src=x" in body_html


def test_html_escapes_app_name(ddb_table) -> None:
    """App name surfaces in the signature line — must also be escaped."""
    community = Community(community_id="c1", name="Test Parish")
    db.put_community(community)
    app = Application(community_id="c1",
                      name="<script>alert(1)</script>",
                      terminology="usher",
                      app_type="coverage")
    db.put_application(app)
    db.put_schedule(Schedule(community_id="c1", app_id=app.app_id,
                             yyyy_mm="2026-05"))
    _, _, body_html = schedule_email.generate_schedule_email(
        community, app, "2026-05")
    assert "<script>alert" not in body_html
    assert "&lt;script&gt;alert" in body_html


def test_generate_table_html_headers_only_when_no_slots(ddb_table) -> None:
    """No slots → table with month + column headers but zero data rows.

    The function always renders the heading rows (MAY 2026 + DATE | TIME
    | USHERS), so an empty schedule still produces a non-empty
    ``<table>``. Pin that so callers can rely on a renderable table.
    """
    community = Community(community_id="c1", name="Test Parish")
    db.put_community(community)
    app = Application(community_id="c1", name="Test Ushers", app_type="coverage")
    db.put_application(app)
    db.put_schedule(Schedule(community_id="c1", app_id=app.app_id,
                             yyyy_mm="2026-05"))
    table = schedule_email.generate_schedule_table_html(community, app, "2026-05")
    assert table.startswith("<table")
    assert table.endswith("</table>")
    assert "MAY 2026" in table          # month banner present
    assert "<b>DATE</b>" in table       # column headers present
    # No data cells (no <td> with vertical-align except the header styles).
    assert "Alice" not in table         # no rows from the previous fixture


def _seed_two_dates(ddb_table):
    """Two Sundays, each one slot on template t1; plus a t2 slot for the
    filter test."""
    community = Community(community_id="c1", name="P")
    db.put_community(community)
    app = Application(community_id="c1", name="Ushers", terminology="usher",
                      app_type="coverage")
    db.put_application(app)
    u1 = User(community_id="c1", email="a@example.com", name="Alice"); db.put_user(u1)
    u2 = User(community_id="c1", email="b@example.com", name="Bob"); db.put_user(u2)
    db.put_schedule(Schedule(community_id="c1", app_id=app.app_id, yyyy_mm="2026-05"))
    rows = [("2026-05-03", "t1", u1, "s1"), ("2026-05-10", "t1", u1, "s2"),
            ("2026-05-10", "t2", u2, "s3")]
    for d, tid, u, sid in rows:
        db.put_slot(Slot(community_id="c1", app_id=app.app_id, yyyy_mm="2026-05",
                         template_id=tid, name=sid, day_of_week=6, start_time="08:00",
                         arrival_offset_minutes=0, duration_minutes=60,
                         required_volunteers=1, min_volunteers=1,
                         concrete_date=d, local_date=d, slot_id=sid))
        db.put_assignment(Assignment(community_id="c1", app_id=app.app_id,
                                     yyyy_mm="2026-05", slot_id=sid,
                                     user_id=u.user_id, local_date=d))
    return community, app


def test_no_blank_spacer_row_between_dates(ddb_table) -> None:
    """The empty bordered spacer row between dates was removed (it read as a
    formatting error). The only source of &nbsp; was that spacer."""
    community, app = _seed_two_dates(ddb_table)
    table = schedule_email.generate_schedule_table_html(community, app, "2026-05")
    assert "&nbsp;" not in table
    # Both dates still render.
    assert "3 May" in table and "10 May" in table


def test_table_template_filter_renders_only_that_cohort(ddb_table) -> None:
    community, app = _seed_two_dates(ddb_table)
    # Slice to t2 only -> Bob's slot shows, Alice's t1 slots don't.
    table = schedule_email.generate_schedule_table_html(
        community, app, "2026-05", template_ids={"t2"})
    assert "Bob" in table
    assert "Alice" not in table
