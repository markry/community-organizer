"""Tests for the per-community URL routing in notifier emails."""
from __future__ import annotations

import pytest

from community_organizer.core.models import Community
from community_organizer.lambdas import notifier


def test_community_host_returns_public_url_when_set() -> None:
    c = Community(community_id="c_other", name="Beta",
                  public_url="beta.community.example.org")
    assert notifier._community_host(c) == "beta.community.example.org"


def test_community_host_falls_back_when_public_url_unset(monkeypatch) -> None:
    monkeypatch.setattr(notifier, "DOMAIN_NAME", "community.example.org")
    c = Community(community_id="example-community", name="Prod")
    assert notifier._community_host(c) == "community.example.org"


def test_community_host_handles_none_community(monkeypatch) -> None:
    monkeypatch.setattr(notifier, "DOMAIN_NAME", "community.example.org")
    assert notifier._community_host(None) == "community.example.org"


def test_assigned_email_uses_provided_host() -> None:
    from community_organizer.core.models import Slot, User
    user = User(community_id="x", email="u@example.com", name="U")
    slot = Slot(community_id="x", app_id="a", yyyy_mm="2026-06",
                template_id="t1", name="8 AM", day_of_week=6,
                start_time="08:00", arrival_offset_minutes=15,
                duration_minutes=60, required_volunteers=1,
                min_volunteers=1, concrete_date="2026-06-15",
                local_date="2026-06-15", slot_id="s1")
    subject, body = notifier._assigned_email(
        user, slot, "Beta Volunteers", host="beta.community.example.org")
    assert "https://beta.community.example.org/your-schedule" in body
    assert "community.example.org" in body
    assert "https://community.example.org/your-schedule" not in body


def test_reminder_email_uses_provided_host() -> None:
    from community_organizer.core.models import Slot, User
    user = User(community_id="x", email="u@example.com", name="U")
    slot = Slot(community_id="x", app_id="a", yyyy_mm="2026-06",
                template_id="t1", name="8 AM", day_of_week=6,
                start_time="08:00", arrival_offset_minutes=15,
                duration_minutes=60, required_volunteers=1,
                min_volunteers=1, concrete_date="2026-06-15",
                local_date="2026-06-15", slot_id="s1")
    subject, body = notifier._reminder_email(
        user, slot, "Beta Volunteers", lead_minutes=60,
        host="beta.community.example.org")
    assert "https://beta.community.example.org/your-schedule" in body
