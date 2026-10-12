"""Reminders across the life of an assignment.

Found by independent review (2026-10-11):

  - a send that failed was recorded as sent, or left claimed for good, and
    never retried;
  - someone who signed up after the month was published got no reminders;
  - a member who unticked every reminder got the defaults anyway (and an
    admin saving an edit form without reminder boxes silently unticked them).
"""
from __future__ import annotations

import datetime as dt
import urllib.parse

import pytest
from boto3.dynamodb.types import TypeSerializer

from community_organizer.core import db, publishing
from community_organizer.core.models import (
    Application, Assignment, Community, EmailLog, Membership, Notification,
    Schedule, Slot, SlotTemplate, User,
)
from community_organizer.lambdas import notifier, web

CID, APP = "c1", "a1"


def _utc(**delta) -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc) + dt.timedelta(**delta)


class _Mail:
    """Email provider stub. `fail` = how many sends report an error first."""
    def __init__(self, fail: int = 0, raises: bool = False):
        self.calls, self.fail, self.raises = [], fail, raises

    def send(self, **kw):
        self.calls.append(kw)
        if self.raises:
            raise RuntimeError("transport down")
        outcome = "accepted"
        if self.fail > 0:
            self.fail -= 1
            outcome = "error"
        return EmailLog(community_id=kw.get("community_id", ""),
                        direction="outbound", from_addr="x@example.com",
                        to_addr=kw.get("to_addr", ""), subject="s",
                        provider="fake", kind="reminder", outcome=outcome)


@pytest.fixture
def world(ddb_table, monkeypatch):
    """A coverage app with one slot three days out, in a published month."""
    monkeypatch.setattr(notifier, "COMMUNITY_ID", CID, raising=False)
    monkeypatch.setattr(notifier, "_get_sms", lambda: None)
    community = Community(community_id=CID, name="Parish",
                          default_timezone="UTC")
    db.put_community(community)
    app = Application(community_id=CID, name="Ushers", app_type="coverage",
                      app_id=APP, default_timezone="UTC")
    db.put_application(app)
    when = _utc(days=3).replace(minute=0, second=0, microsecond=0)
    ym = when.strftime("%Y-%m")
    db.put_template(SlotTemplate(community_id=CID, app_id=APP, name="Mass",
                                 day_of_week=when.weekday(),
                                 start_time=when.strftime("%H:%M"),
                                 duration_minutes=60, template_id="t1"))
    slot = Slot(community_id=CID, app_id=APP, yyyy_mm=ym, template_id="t1",
                name="Usher", day_of_week=when.weekday(),
                start_time=when.strftime("%H:%M"), arrival_offset_minutes=0,
                duration_minutes=60, required_volunteers=2, min_volunteers=1,
                max_volunteers=5, concrete_date=when.strftime("%Y-%m-%d"),
                local_date=when.strftime("%Y-%m-%d"), slot_id="s1")
    db.put_slot(slot)
    db.put_schedule(Schedule(community_id=CID, app_id=APP, yyyy_mm=ym,
                             state="published"))

    def member(name: str, leads=..., channel="email") -> User:
        u = User(community_id=CID, email=f"{name}@example.com", name=name,
                 user_id=name, channel=channel)
        if leads is not ...:
            u.lead_times_minutes = leads
        db.put_user(u)
        db.put_membership(Membership(community_id=CID, app_id=APP,
                                     user_id=name))
        return u

    return community, app, slot, member


def _assign(slot: Slot, user_id: str) -> Assignment:
    a = Assignment(community_id=CID, app_id=APP, yyyy_mm=slot.yyyy_mm,
                   slot_id=slot.slot_id, user_id=user_id,
                   local_date=slot.local_date, created_by=user_id)
    db.put_assignment(a)
    return a


def _stream(event_name: str, a: Assignment) -> dict:
    """The stream record DynamoDB would deliver for this assignment row."""
    ts = TypeSerializer()
    image = {k: ts.serialize(v) for k, v in {
        "community_id": a.community_id, "app_id": a.app_id,
        "yyyy_mm": a.yyyy_mm, "slot_id": a.slot_id, "user_id": a.user_id,
        "created_by": a.created_by}.items() if v is not None}
    return {"eventName": event_name, "dynamodb": {
        "Keys": {"SK": {"S": f"ASGN#{a.slot_id}#{a.user_id}"}},
        "NewImage" if event_name != "REMOVE" else "OldImage": image}}


def _reminders(slot: Slot, user_id: str) -> list[Notification]:
    return sorted(db.list_notifications_for_slot_user(APP, slot.slot_id, user_id),
                  key=lambda n: n.lead_minutes)


def _due(slot: Slot, user_id: str, lead: int = 60) -> Notification:
    """A reminder for `user_id` that came due a minute ago."""
    ntf = Notification(
        community_id=CID, app_id=APP, user_id=user_id, slot_id=slot.slot_id,
        yyyy_mm=slot.yyyy_mm, lead_minutes=lead,
        send_at=_utc(minutes=-1).isoformat(timespec="seconds"))
    db.put_notification(ntf)
    return ntf


def _stored(ntf: Notification) -> Notification:
    return next(n for n in db.list_notifications_for_slot_user(
        APP, ntf.slot_id, ntf.user_id)
        if n.notification_id == ntf.notification_id)


# ---- a failed send is retried, not recorded as sent ------------------------

def test_a_send_the_provider_reports_as_failed_is_retried(
        world, monkeypatch) -> None:
    _c, _a, slot, member = world
    member("ann"); _assign(slot, "ann")
    mail = _Mail(fail=1)
    monkeypatch.setattr(notifier, "_get_provider", lambda: mail)
    ntf = _due(slot, "ann")

    assert notifier._handle_scheduled()["sent"] == 0
    after = _stored(ntf)
    assert after.state == "pending" and after.attempts == 1
    assert not after.email_done

    assert notifier._handle_scheduled()["sent"] == 1         # next poll
    after = _stored(ntf)
    assert after.state == "sent" and after.email_done and after.attempts == 2
    assert len(mail.calls) == 2


def test_a_provider_that_raises_does_not_strand_the_reminder(
        world, monkeypatch) -> None:
    _c, _a, slot, member = world
    member("ann"); _assign(slot, "ann")
    monkeypatch.setattr(notifier, "_get_provider", lambda: _Mail(raises=True))
    ntf = _due(slot, "ann")
    notifier._handle_scheduled()
    assert _stored(ntf).state == "pending"                   # not in_flight
    good = _Mail()
    monkeypatch.setattr(notifier, "_get_provider", lambda: good)
    assert notifier._handle_scheduled()["sent"] == 1
    assert _stored(ntf).state == "sent"


def test_retrying_stops_when_the_window_closes(world, monkeypatch) -> None:
    _c, _a, slot, member = world
    member("ann"); _assign(slot, "ann")
    monkeypatch.setattr(notifier, "_get_provider", lambda: _Mail(fail=99))
    ntf = _due(slot, "ann", lead=1440)
    notifier._handle_scheduled()
    assert _stored(ntf).state == "pending"
    monkeypatch.setattr(notifier, "_RETRY_WINDOW", dt.timedelta(seconds=0))
    notifier._handle_scheduled()
    after = _stored(ntf)
    assert after.state == "failed" and after.attempts == 2
    assert notifier._handle_scheduled()["polled"] == 0       # left alone now


def test_nothing_is_sent_once_the_event_has_started(world, monkeypatch) -> None:
    """Due two hours ago for something that began an hour ago: the notifier
    was down. Sending now would be noise."""
    _c, _a, slot, member = world
    member("ann"); _assign(slot, "ann")
    mail = _Mail()
    monkeypatch.setattr(notifier, "_get_provider", lambda: mail)
    ntf = Notification(
        community_id=CID, app_id=APP, user_id="ann", slot_id=slot.slot_id,
        yyyy_mm=slot.yyyy_mm, lead_minutes=60,
        send_at=_utc(hours=-2).isoformat(timespec="seconds"))
    db.put_notification(ntf)
    assert notifier._handle_scheduled()["sent"] == 0
    assert mail.calls == [] and _stored(ntf).state == "cancelled"


def test_a_late_reminder_is_still_sent_while_it_is_useful(
        world, monkeypatch) -> None:
    """A day-before reminder running five hours late still helps."""
    _c, _a, slot, member = world
    member("ann"); _assign(slot, "ann")
    mail = _Mail()
    monkeypatch.setattr(notifier, "_get_provider", lambda: mail)
    ntf = Notification(
        community_id=CID, app_id=APP, user_id="ann", slot_id=slot.slot_id,
        yyyy_mm=slot.yyyy_mm, lead_minutes=1440,
        send_at=_utc(hours=-5).isoformat(timespec="seconds"))
    db.put_notification(ntf)
    assert notifier._handle_scheduled()["sent"] == 1
    assert _stored(ntf).state == "sent"


def test_a_channel_that_got_through_is_not_sent_again(world, monkeypatch) -> None:
    """Email went out, the text did not: the retry is for the text alone."""
    _c, _a, slot, member = world
    u = member("ann", channel="both"); u.phone = "+15555550100"
    db.put_user(u); _assign(slot, "ann")
    mail = _Mail()
    texts = []
    monkeypatch.setattr(notifier, "_get_provider", lambda: mail)
    monkeypatch.setattr(notifier, "_channels_for", lambda _u: (True, True))
    monkeypatch.setattr(notifier, "_send_sms_reminder",
                        lambda *a, **k: (texts.append(1), len(texts) > 1)[1])
    ntf = _due(slot, "ann")
    notifier._handle_scheduled()
    after = _stored(ntf)
    assert after.state == "pending" and after.email_done and not after.sms_done
    notifier._handle_scheduled()
    assert _stored(ntf).state == "sent"
    assert len(mail.calls) == 1 and len(texts) == 2


def test_a_text_only_member_falls_back_to_email_when_the_text_fails(
        world, monkeypatch) -> None:
    _c, _a, slot, member = world
    member("ann", channel="sms"); _assign(slot, "ann")
    mail = _Mail()
    monkeypatch.setattr(notifier, "_get_provider", lambda: mail)
    monkeypatch.setattr(notifier, "_channels_for", lambda _u: (False, True))
    monkeypatch.setattr(notifier, "_send_sms_reminder", lambda *a, **k: False)
    ntf = _due(slot, "ann")
    notifier._handle_scheduled()
    assert len(mail.calls) == 1 and _stored(ntf).email_done


def test_an_abandoned_claim_is_recovered(world, monkeypatch) -> None:
    """A run died between claiming a reminder and recording the outcome."""
    _c, _a, slot, member = world
    member("ann"); _assign(slot, "ann")
    mail = _Mail()
    monkeypatch.setattr(notifier, "_get_provider", lambda: mail)
    ntf = _due(slot, "ann")
    assert db.claim_notification(ntf.notification_id, APP, ntf.send_at)
    assert notifier._handle_scheduled()["polled"] == 0       # claim is fresh
    monkeypatch.setattr(notifier, "_STALE_CLAIM", dt.timedelta(seconds=-1))
    out = notifier._handle_scheduled()
    assert out["recovered"] == 1 and out["sent"] == 1
    assert _stored(ntf).state == "sent"


# ---- joining after the month is published ----------------------------------

def test_a_late_sign_up_gets_reminders(world, monkeypatch) -> None:
    _c, _a, slot, member = world
    monkeypatch.setattr(notifier, "_get_provider", lambda: _Mail())
    member("ann")
    a = _assign(slot, "ann")
    assert _reminders(slot, "ann") == []                     # nothing yet
    notifier._process(_stream("INSERT", a))
    got = _reminders(slot, "ann")
    assert [n.lead_minutes for n in got] == [120, 1440]
    assert all(n.state == "pending" for n in got)


def test_a_redelivered_stream_record_does_not_double_the_reminders(
        world, monkeypatch) -> None:
    _c, _a, slot, member = world
    monkeypatch.setattr(notifier, "_get_provider", lambda: _Mail())
    member("ann")
    a = _assign(slot, "ann")
    for _ in range(3):
        notifier._process(_stream("INSERT", a))
    assert len(_reminders(slot, "ann")) == 2


def test_no_reminders_are_queued_in_an_unpublished_month(
        world, monkeypatch) -> None:
    _c, _a, slot, member = world
    monkeypatch.setattr(notifier, "_get_provider", lambda: _Mail())
    sch = db.get_schedule(APP, slot.yyyy_mm)
    sch.state = "draft"
    db.put_schedule(sch)
    member("ann")
    notifier._process(_stream("INSERT", _assign(slot, "ann")))
    assert _reminders(slot, "ann") == []


def test_releasing_cancels_and_taking_it_again_requeues(
        world, monkeypatch) -> None:
    _c, _a, slot, member = world
    monkeypatch.setattr(notifier, "_get_provider", lambda: _Mail())
    member("ann")
    a = _assign(slot, "ann")
    notifier._process(_stream("INSERT", a))
    db.delete_assignment(APP, slot.yyyy_mm, slot.slot_id, "ann")
    notifier._process(_stream("REMOVE", a))
    assert {n.state for n in _reminders(slot, "ann")} == {"cancelled"}
    a = _assign(slot, "ann")
    notifier._process(_stream("INSERT", a))
    got = _reminders(slot, "ann")
    assert len(got) == 2 and {n.state for n in got} == {"pending"}


def test_publishing_and_a_late_sign_up_share_one_set_of_reminders(
        world, monkeypatch) -> None:
    """Publish queues them; a stream record for the same assignment arriving
    afterwards must land on the same rows, not add a second set."""
    community, app, slot, member = world
    monkeypatch.setattr(notifier, "_get_provider", lambda: _Mail())
    member("ann")
    a = _assign(slot, "ann")
    assert publishing._materialize_reminders(community, app, slot.yyyy_mm) == 2
    notifier._process(_stream("INSERT", a))
    assert len(_reminders(slot, "ann")) == 2


# ---- "none ticked" means none ----------------------------------------------

def test_a_member_with_reminders_off_gets_none(world, monkeypatch) -> None:
    community, app, slot, member = world
    monkeypatch.setattr(notifier, "_get_provider", lambda: _Mail())
    member("quiet", leads=[])
    member("usual")
    _assign(slot, "quiet"); _assign(slot, "usual")
    assert publishing._materialize_reminders(community, app, slot.yyyy_mm) == 2
    assert _reminders(slot, "quiet") == []
    assert len(_reminders(slot, "usual")) == 2
    notifier._process(_stream("INSERT", _assign(slot, "quiet")))
    assert _reminders(slot, "quiet") == []


def test_effective_lead_times_distinguishes_off_from_unset() -> None:
    u = User(community_id=CID, email="e@example.com", name="n")
    assert publishing.effective_lead_times(u) == [1440, 120]   # the default
    u.lead_times_minutes = []
    assert publishing.effective_lead_times(u) == []
    u.lead_times_minutes = None
    assert publishing.effective_lead_times(u) == [1440, 120]
    u.lead_times_minutes = [60]
    assert publishing.effective_lead_times(u) == [60]


def _edit(actor, app, target, **fields):
    form = {"user_id": target.user_id, "version": str(target.version or 0),
            "name": target.name, "email": target.email} | fields
    return web._api_user_edit(
        {"rawPath": "/api/users/edit",
         "requestContext": {"http": {"method": "POST"}},
         "body": urllib.parse.urlencode(form, doseq=True),
         "isBase64Encoded": False},
        actor, db.get_community(CID), app, db.get_membership(APP, actor.user_id))


def test_an_admin_save_without_reminder_boxes_leaves_reminders_alone(
        world) -> None:
    """The edit forms for event apps and community users carry no reminder
    boxes. Saving one used to blank the member's reminders."""
    _c, app, _slot, member = world
    admin = member("boss"); admin.community_role = "ca"; db.put_user(admin)
    ann = member("ann", leads=[1440])
    _edit(admin, app, db.get_user(CID, "ann"), name="Ann B")
    after = db.get_user(CID, "ann")
    assert after.name == "Ann B" and after.lead_times_minutes == [1440]


def test_an_admin_can_still_set_or_clear_reminders_from_the_form_with_boxes(
        world) -> None:
    _c, app, _slot, member = world
    admin = member("boss"); admin.community_role = "ca"; db.put_user(admin)
    member("ann", leads=[1440, 120])
    _edit(admin, app, db.get_user(CID, "ann"), has_leads="1", lead=["120"])
    assert db.get_user(CID, "ann").lead_times_minutes == [120]
    _edit(admin, app, db.get_user(CID, "ann"), has_leads="1")   # none ticked
    assert db.get_user(CID, "ann").lead_times_minutes == []


def test_settings_page_shows_reminders_off_as_off(world) -> None:
    _c, app, _slot, member = world
    quiet = member("quiet", leads=[])
    ev = {"requestContext": {"http": {"method": "GET"}}}
    body = web._settings_page(ev, quiet, db.get_community(CID), app,
                              db.get_membership(APP, "quiet"))["body"]
    boxes = body.split("name='lead'")[1:]
    assert boxes and not any(" checked" in b.split(">")[0] for b in boxes)
