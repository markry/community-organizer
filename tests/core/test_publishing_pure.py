"""Pure-function tests for ``community_organizer.core.publishing``.

These tests exercise the helpers that have no DynamoDB or boto3
dependency, so they run in milliseconds with no fixtures:

    - _fmt_time         (HH:MM 24h -> "H:MM AM/PM")
    - _fmt_date         (YYYY-MM-DD -> "Day, Month D")
    - _arrival_hhmm     (slot + offset -> HH:MM, with rollover)
    - _month_human      (YYYY-MM -> "Month YYYY")
    - _lead_time_desc   (list[int] -> human phrase)
    - _build_email      (renders subject/text/html for one user)

For DDB-backed tests (plan_publish, publish_schedule with its
idempotency contract, _materialize_reminders, unpublish_schedule) see
``test_publishing_flow.py``.
"""
from __future__ import annotations

import pytest

from community_organizer.core.models import Application, Community, Slot, User
from community_organizer.core.publishing import (
    _arrival_hhmm,
    _build_email,
    _fmt_date,
    _fmt_time,
    _lead_time_desc,
    _month_human,
)


# ---- _fmt_time -----------------------------------------------------------

@pytest.mark.parametrize(
    "hhmm, expected",
    [
        ("08:00", "8:00 AM"),
        ("14:30", "2:30 PM"),
        ("00:15", "12:15 AM"),   # midnight bucket
        ("12:00", "12:00 PM"),   # noon bucket
        ("23:59", "11:59 PM"),
    ],
)
def test_fmt_time_24h_to_12h(hhmm: str, expected: str) -> None:
    """24h -> 12h with AM/PM, including the midnight/noon edge cases."""
    assert _fmt_time(hhmm) == expected


@pytest.mark.parametrize("bad", ["25:00", "24:00", "08:60", "-1:00"])
def test_fmt_time_rejects_out_of_range(bad: str) -> None:
    """Hours must be 0..23 and minutes 0..59 — the pre-fix code
    silently rendered "25:00" as "13:00 PM" (security fix D20)."""
    with pytest.raises(ValueError, match="out-of-range"):
        _fmt_time(bad)


# ---- _fmt_date -----------------------------------------------------------

def test_fmt_date_renders_weekday_and_month() -> None:
    # 2026-05-31 was a Sunday.
    assert _fmt_date("2026-05-31") == "Sun, May 31"


def test_fmt_date_no_year() -> None:
    # Subject line carries the year; body lines must not repeat it.
    assert "2026" not in _fmt_date("2026-05-31")


# ---- _arrival_hhmm -------------------------------------------------------

def _slot(start_time: str, arrival_offset: int) -> Slot:
    """Minimal Slot factory — just the fields _arrival_hhmm reads."""
    return Slot(
        community_id="c", app_id="a", yyyy_mm="2026-05",
        template_id="t", name="x", day_of_week=0,
        start_time=start_time, arrival_offset_minutes=arrival_offset,
        duration_minutes=60, required_volunteers=1, min_volunteers=1,
        concrete_date="2026-05-31", local_date="2026-05-31",
    )


@pytest.mark.parametrize(
    "start, offset, expected",
    [
        ("08:00", 10, "07:50"),     # normal subtraction
        ("08:05", 10, "07:55"),     # minute borrow
        ("00:05", 10, "23:55"),     # day rollover (handled by timedelta)
        ("08:00",  0, "08:00"),     # zero offset = no change
    ],
)
def test_arrival_hhmm(start: str, offset: int, expected: str) -> None:
    """Arrival = start - offset, with proper minute/hour/day rollover."""
    assert _arrival_hhmm(_slot(start, offset)) == expected


# ---- _month_human --------------------------------------------------------

def test_month_human() -> None:
    assert _month_human("2026-05") == "May 2026"
    assert _month_human("2026-12") == "December 2026"


# ---- _lead_time_desc -----------------------------------------------------

@pytest.mark.parametrize(
    "minutes, expected",
    [
        ([1440, 120], "1 day and 2 hours"),       # standard default
        ([2880], "2 days"),                        # plural day
        ([60], "1 hour"),
        ([180], "3 hours"),
        ([30], "30 minutes"),
        ([1], "1 minute"),                         # singular minute
        ([], "2 hours"),                           # empty -> safety default
        ([120, 1440], "1 day and 2 hours"),       # unsorted input still sorts desc
    ],
)
def test_lead_time_desc(minutes: list[int], expected: str) -> None:
    """Sorted largest-first, units folded into the largest natural unit."""
    assert _lead_time_desc(minutes) == expected


# ---- _build_email --------------------------------------------------------

def _community() -> Community:
    return Community(community_id="c1", name="Test Parish")


def _app(name: str = "Test Ushers") -> Application:
    return Application(community_id="c1", name=name,
                       event_noun="Mass", arrival_label="please arrive by", app_type="coverage")


def _user(name: str = "Jane Doe") -> User:
    return User(community_id="c1", email="jane@example.com", name=name,
                lead_times_minutes=[1440, 120])


def test_build_email_no_slots_uses_app_name() -> None:
    """Org name in subject + signature comes from ``app.name``, not community.

    This was the templatization decision made when the app/community
    split landed (see project history). Test pins that invariant.
    """
    subject, body_text, body_html = _build_email(
        user=_user(), community=_community(), app=_app("Test Ushers"),
        yyyy_mm="2026-05", slots=[],
    )
    assert "Test Ushers" in subject
    assert "May 2026" in subject
    assert "Test Ushers" in body_text
    assert "Test Ushers" in body_html
    # Community name must NOT leak into the broadcast.
    assert "Test Parish" not in subject
    assert "Test Parish" not in body_text


def test_build_email_no_slots_mentions_no_assignments() -> None:
    """Users without assignments get a courtesy email pointing at open slots."""
    _, body_text, _ = _build_email(
        user=_user(), community=_community(), app=_app(),
        yyyy_mm="2026-05", slots=[],
    )
    assert "don't have any assignments" in body_text
    assert "your-schedule" in body_text


def test_build_email_with_slots_lists_each() -> None:
    """One bullet per assignment with date, name, and start/arrival times."""
    slots = [
        _slot("08:00", 10),
        _slot("10:30", 10),
    ]
    slots[0].name = "Sun 8:00 AM"
    slots[1].name = "Sun 10:30 AM"
    _, body_text, body_html = _build_email(
        user=_user(), community=_community(), app=_app(),
        yyyy_mm="2026-05", slots=slots,
    )
    assert "Sun 8:00 AM" in body_text
    assert "Sun 10:30 AM" in body_text
    assert "8:00 AM" in body_text       # start time rendered 12h
    assert "7:50 AM" in body_text       # arrival = start - 10min
    assert "please arrive by" in body_text
    # HTML body has one <li> per slot.
    assert body_html.count("<li>") == 2


def test_build_email_pluralizes_slot_count() -> None:
    """Subject says "the following 1 slot:" vs "the following 2 slots:".

    Small thing, but if it ever broke it would read as "the following 1 slots:"
    which looks like a bug to members.
    """
    one = [_slot("08:00", 10)]
    two = [_slot("08:00", 10), _slot("10:00", 10)]
    _, b1, _ = _build_email(user=_user(), community=_community(), app=_app(),
                            yyyy_mm="2026-05", slots=one)
    _, b2, _ = _build_email(user=_user(), community=_community(), app=_app(),
                            yyyy_mm="2026-05", slots=two)
    assert "1 slot:" in b1 and "slots:" not in b1.split("1 slot:")[0]
    assert "2 slots:" in b2


def test_build_email_uses_user_lead_times_in_body() -> None:
    """Reminder lead times are rendered into the body so members know when
    to expect the next ping."""
    user = User(community_id="c1", email="j@example.com", name="J",
                lead_times_minutes=[60, 30])
    _, body_text, _ = _build_email(
        user=user, community=_community(), app=_app(),
        yyyy_mm="2026-05", slots=[_slot("08:00", 10)],
    )
    assert "1 hour and 30 minutes" in body_text
