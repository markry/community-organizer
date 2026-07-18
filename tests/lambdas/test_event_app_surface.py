"""Slice 3.5 — event apps (standing/flexible) hide the coverage surface.

Pure tests for the nav-bar app-type gating + the coverage-only path matcher
(no DB / auth needed).
"""
from __future__ import annotations

from community_organizer.lambdas import web
from community_organizer.core.models import Application


def _app(app_type: str) -> Application:
    return Application(community_id="c1", name="X", app_type=app_type, app_id="a1")


def test_coverage_only_path_matcher() -> None:
    for p in ["/schedules", "/your-schedule", "/my-availability",
              "/admin/templates", "/admin/cohorts",
              "/swap/new", "/swap/abc/accept", "/api/templates/add",
              "/api/slots/cancel", "/api/swap/create", "/api/cohorts/add-member",
              "/api/settings/save", "/api/assignments/x", "/api/schedules/publish"]:
        assert web._is_coverage_only_path(p), p
    # /admin/settings is NOT coverage-only anymore — event apps use it too
    # for the consolidated public-page controls.
    for p in ["/", "/admin/users", "/admin/send-email", "/admin/emails",
              "/admin/settings", "/standing/setup", "/api/standing/setup",
              "/launcher"]:
        assert not web._is_coverage_only_path(p), p


def test_nav_hides_coverage_links_for_standing() -> None:
    nav = web._admin_nav_bar("home", app=_app("standing_event"))
    # coverage links gone
    for href in ["/schedules", "/your-schedule", "/my-availability",
                 "/admin/templates", "/admin/cohorts"]:
        assert href not in nav, href
    # kept links present — Settings now stays (hosts the public-page block)
    for href in ["/admin/users", "/admin/send-email", "/admin/emails",
                 "/admin/settings"]:
        assert href in nav, href
    # added link present
    assert "/standing/setup" in nav
    assert "Meeting schedule" in nav


def test_nav_flexible_hides_coverage_but_no_standing_setup() -> None:
    nav = web._admin_nav_bar("home", app=_app("flexible_event"))
    assert "/admin/templates" not in nav
    assert "/admin/cohorts" not in nav
    assert "/standing/setup" not in nav  # standing-only link
    assert "/admin/settings" in nav      # Settings shown (public-page block)


def test_nav_coverage_app_unchanged() -> None:
    nav = web._admin_nav_bar("home", app=_app("coverage"))
    # coverage app still shows the full surface
    for href in ["/schedules", "/admin/templates", "/admin/cohorts",
                 "/admin/settings", "/my-availability"]:
        assert href in nav, href


def test_schedule_action_offers_archive_for_published() -> None:
    from community_organizer.core.models import Schedule
    sch = Schedule(community_id="c1", app_id="a1", yyyy_mm="2026-06",
                   state="published")
    html_ = web._schedule_action(sch)
    assert "/api/schedules/archive" in html_ and ">Archive<" in html_
    assert "/api/schedules/unpublish" in html_      # both still offered


def test_schedule_action_offers_reactivate_for_archived() -> None:
    from community_organizer.core.models import Schedule
    sch = Schedule(community_id="c1", app_id="a1", yyyy_mm="2026-06",
                   state="archived")
    html_ = web._schedule_action(sch)
    assert "/api/schedules/reactivate" in html_ and ">Reactivate<" in html_
    assert "/api/schedules/archive" not in html_
