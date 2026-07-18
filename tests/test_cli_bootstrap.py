"""Tests for the ``apps bootstrap-adoration`` CLI command.

This is the one-shot seeder for a Eucharistic-adoration
recurring_commitments app: creates the Application plus N
SlotTemplates covering Wed → Thu morning. The tests pin the
template count, day distribution, and the auto_reminders=False
convention so a regression in the bootstrap defaults gets caught
before the production seed runs.
"""
from __future__ import annotations

from click.testing import CliRunner

from community_organizer.cli import _hour_label, main
from community_organizer.core import db
from community_organizer.core.models import Community


def test_hour_label_formats_match_parish_style() -> None:
    """The auto-generated template names should read naturally:
    "Wed 12:45 PM", "Wed 1 PM", "Thu 12 AM" (midnight), etc."""
    assert _hour_label(2, "12:45") == "Wed 12:45 PM"
    assert _hour_label(2, "13:00") == "Wed 1 PM"
    assert _hour_label(2, "23:00") == "Wed 11 PM"
    assert _hour_label(3, "00:00") == "Thu 12 AM"
    assert _hour_label(3, "07:00") == "Thu 7 AM"
    assert _hour_label(3, "12:00") == "Thu 12 PM"


def test_bootstrap_adoration_creates_app_and_default_templates(ddb_table) -> None:
    """With the documented defaults — 12:45 opener + Wed 13..23 hourly
    + Thu 0..7 hourly — we expect 20 templates: 1 opener + 11 Wed + 8 Thu.
    auto_reminders is False on every one (weekly recurrence is on
    the user's calendar; auto reminders would be noise)."""
    cid = "c1"
    db.put_community(Community(community_id=cid, name="Test"))

    runner = CliRunner()
    result = runner.invoke(main, ["--community-id", cid,
                                  "apps", "bootstrap-adoration",
                                  "--name", "Adoration"])
    assert result.exit_code == 0, result.output

    apps = list(db.list_applications(cid))
    assert len(apps) == 1
    app = apps[0]
    assert app.name == "Adoration"
    assert app.app_type == "recurring_commitments"
    assert app.period_type == "weekly"

    tpls = list(db.list_templates(app.app_id))
    assert len(tpls) == 20

    by_day = {dow: [t for t in tpls if t.day_of_week == dow]
              for dow in (2, 3)}
    assert len(by_day[2]) == 12, "1 opener + 11 hourly Wed"
    assert len(by_day[3]) == 8, "Thu 0..7 hourly"

    assert all(not t.auto_reminders for t in tpls)
    assert all(t.required_volunteers == 1 for t in tpls)
    # Adoration slots are uncapped — extra people praying are welcome.
    assert all(t.max_volunteers is None for t in tpls)

    # Opener slot specifically: 12:45, 45-minute, friendly name.
    opener = next(t for t in tpls
                  if t.day_of_week == 2 and t.start_time == "12:45")
    assert opener.duration_minutes == 45
    assert opener.name == "Wed 12:45 PM"


def test_bootstrap_adoration_respects_flag_overrides(ddb_table) -> None:
    """If the parish doesn't run a 12:45 opener and ends at Thu 5 AM,
    the flags should let us bootstrap that shape too."""
    cid = "c1"
    db.put_community(Community(community_id=cid, name="Test"))

    runner = CliRunner()
    result = runner.invoke(main, [
        "--community-id", cid,
        "apps", "bootstrap-adoration",
        "--name", "Short Adoration",
        "--first-slot", "13:00",
        "--opener-duration", "60",
        "--hourly-start-hour", "14",
        "--last-thu-hour", "4",
    ])
    assert result.exit_code == 0, result.output

    app = next(iter(db.list_applications(cid)))
    tpls = sorted(db.list_templates(app.app_id),
                  key=lambda t: (t.day_of_week, t.start_time))
    # Wed 13:00 opener (60min) + Wed 14..23 (10 hourly) +
    # Thu 0..4 (5 hourly) = 16 total.
    assert len(tpls) == 1 + 10 + 5

    opener = next(t for t in tpls if t.start_time == "13:00")
    assert opener.duration_minutes == 60
    # Confirm the last Thu slot is 4 AM, not 7 AM.
    thu = sorted(t.start_time for t in tpls if t.day_of_week == 3)
    assert thu[-1] == "04:00"
