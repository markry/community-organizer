"""Trade-request emails name the slot and the person; no singular they/their."""
from __future__ import annotations

import re

import pytest

from community_organizer.core import db
from community_organizer.core.models import (
    Application, Assignment, Community, Membership, Schedule, Slot,
    SwapRequest, User,
)
from community_organizer.lambdas import web

CID = "c1"


class _Outbox:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(self, **kw):
        self.sent.append(kw)


def _slot(app, sid, day, date):
    db.put_slot(Slot(community_id=CID, app_id=app.app_id, yyyy_mm="2026-05",
                     slot_id=sid, template_id="one-off", name=f"Sun {day}",
                     day_of_week=6, start_time="08:00",
                     arrival_offset_minutes=0, duration_minutes=60,
                     required_volunteers=1, min_volunteers=1,
                     concrete_date=f"{date}T12:00", local_date=date))


@pytest.mark.parametrize("released", [True, False])
def test_admin_trade_email_wording(ddb_table, monkeypatch, released) -> None:
    outbox = _Outbox()
    monkeypatch.setattr(
        "community_organizer.providers.email.get_email_provider",
        lambda: outbox)
    community = Community(community_id=CID, name="Parish",
                          default_timezone="America/New_York")
    db.put_community(community)
    app = Application(community_id=CID, name="Ushers", app_type="coverage")
    db.put_application(app)
    users = {}
    for name, role in (("Pat", "member"), ("Admin", "aa"), ("Sue", "member")):
        u = User(community_id=CID, email=f"{name}@example.com", name=name)
        db.put_user(u)
        db.put_membership(Membership(community_id=CID, app_id=app.app_id,
                                     user_id=u.user_id, app_role=role))
        users[name] = u
    db.put_schedule(Schedule(community_id=CID, app_id=app.app_id,
                             yyyy_mm="2026-05"))
    _slot(app, "give", "A", "2026-05-03")
    _slot(app, "take", "B", "2026-05-10")
    db.put_assignment(Assignment(community_id=CID, app_id=app.app_id,
                                 yyyy_mm="2026-05", slot_id="take",
                                 user_id=users["Sue"].user_id,
                                 local_date="2026-05-10"))
    swap = SwapRequest(community_id=CID, app_id=app.app_id, yyyy_mm="2026-05",
                       requester_user_id=users["Pat"].user_id,
                       release_slot_id="give", preferred_slot_ids=["take"],
                       released=released)
    web._notify_swap_request(users["Pat"], community, app, swap)

    by_to = {m["to_addr"]: m["body_text"] for m in outbox.sent}
    aa_body = by_to["Admin@example.com"]
    sue_body = by_to["Sue@example.com"]
    if released:
        assert "Pat released this slot and is looking to trade:" in aa_body
    else:
        assert "Pat is looking to trade this slot:" in aa_body
        assert "Pat is keeping the slot until someone accepts" in aa_body
    assert "Pat would like to trade this slot:" in sue_body
    for body in (aa_body, sue_body):
        assert "Any of these dates would work instead:" in body
        assert not re.search(r"\b(they|their|them|themselves)\b", body, re.I)
