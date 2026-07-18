"""Tests for #203 — inbound's outbound paths use community.public_url
not the prod-stack DOMAIN_NAME env, so a beta user who Outlook-
tentatives or declines a beta calendar invite gets a re-invite /
confirmation with the beta hostname, not community.example.org."""
from __future__ import annotations

from community_organizer.core.models import Community
from community_organizer.lambdas import inbound


def test_community_host_returns_public_url_when_set() -> None:
    c = Community(community_id="c_other", name="Beta",
                  public_url="beta.community.example.org")
    assert inbound._community_host(c) == "beta.community.example.org"


def test_community_host_falls_back_when_public_url_unset(monkeypatch) -> None:
    monkeypatch.setattr(inbound, "DOMAIN_NAME", "community.example.org")
    c = Community(community_id="example-community", name="Prod")
    assert inbound._community_host(c) == "community.example.org"


def test_community_host_handles_none(monkeypatch) -> None:
    monkeypatch.setattr(inbound, "DOMAIN_NAME", "community.example.org")
    assert inbound._community_host(None) == "community.example.org"
