"""Tests for the shared wall-calendar renderer in
``community_organizer.lambdas.web._render_wall_calendar`` and
``_render_calendar_cell`` (event-apps slice 2).

Read-only renderer with no DB dependency — the tests drive it with
hand-built ``items_by_date`` dicts so they pin layout (no missing
weeks, the correct number of cells) and the chip-rendering shape
(time prefix, label, today-cell highlight).
"""
from __future__ import annotations

import datetime as dt

from community_organizer.lambdas import web


def test_render_wall_calendar_cell_count_july_2026() -> None:
    """July 2026 starts on a Wednesday. 31 days → 31 cells of in-month
    content, plus 3 leading filler (Sun-Tue) and 1 trailing (Sat=Aug 1).
    Total = 35 cells = 5 rows of 7 (a perfect rectangle, no partial
    final row)."""
    html = web._render_wall_calendar(
        month_first=dt.date(2026, 7, 1),
        items_by_date={},
        today=dt.date(2026, 6, 7),
    )
    # 35 <td>s = 35 cells. The renderer's table also has <th>s in the
    # header row; we count <td> specifically.
    assert html.count("<td") == 35
    # Five week rows.
    assert html.count("<tr>") == 6   # 1 header + 5 body
    assert "July 2026" in html


def test_render_wall_calendar_today_cell_highlighted() -> None:
    """The cell whose date matches ``today`` gets a green outline.
    Use a date inside the rendered month."""
    html = web._render_wall_calendar(
        month_first=dt.date(2026, 7, 1),
        items_by_date={},
        today=dt.date(2026, 7, 15),
    )
    # The today cell has border-color #2a7.
    assert "border:2px solid #2a7" in html


def test_render_wall_calendar_renders_event_chips() -> None:
    """Items for a date show up inside the matching cell with the
    time prefix and the label both visible."""
    items = {
        "2026-07-14": [{"label": "K of C Meeting", "time": "19:00"}],
    }
    html = web._render_wall_calendar(
        month_first=dt.date(2026, 7, 1),
        items_by_date=items,
        today=dt.date(2026, 6, 7),
    )
    assert "K of C Meeting" in html
    # Time formats via _fmt_time → "7:00 PM" or similar.
    assert "7:00" in html


def test_render_wall_calendar_chip_escapes_html() -> None:
    """An event title with HTML metacharacters must be escaped, not
    rendered. Otherwise an admin (or member, via flexible_event
    title) could inject markup into other users' calendars."""
    items = {
        "2026-07-14": [{"label": "<script>x()</script>", "time": None}],
    }
    html = web._render_wall_calendar(
        month_first=dt.date(2026, 7, 1),
        items_by_date=items,
        today=dt.date(2026, 6, 7),
    )
    assert "<script>x()</script>" not in html
    assert "&lt;script&gt;" in html


def test_render_wall_calendar_multiple_items_per_day_stack() -> None:
    """Two events on the same day → two chips in that cell, in input
    order (renderer doesn't re-sort; callers feed pre-ordered lists)."""
    items = {
        "2026-07-14": [
            {"label": "Morning prayer", "time": "07:00"},
            {"label": "Vespers", "time": "18:00"},
        ],
    }
    html = web._render_wall_calendar(
        month_first=dt.date(2026, 7, 1),
        items_by_date=items,
        today=dt.date(2026, 6, 7),
    )
    morning_pos = html.index("Morning prayer")
    vespers_pos = html.index("Vespers")
    assert morning_pos < vespers_pos
