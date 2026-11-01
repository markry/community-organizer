"""Regression tests for the independent review of event capacity: each one
replays a reported failure (concurrency, notices, close, clearing the cap,
big events) and checks the corrected outcome."""
from __future__ import annotations

import datetime as dt
from unittest.mock import patch

import pytest

from community_organizer.core import capacity as seats
from community_organizer.core import db
from community_organizer.core.models import (
    FlexibleEvent, FlexiblePollOption, FlexibleRSVP,
)
from community_organizer.lambdas import web

from .test_flexible_event import _mk_token, _post, _seed

DATE = "2099-03-15"


@pytest.fixture
def outbox(monkeypatch):
    sent: list[dict] = []
    monkeypatch.setattr(
        "community_organizer.providers.email.get_email_provider",
        lambda: type("S", (), {"send": lambda self, **kw: sent.append(kw)})())
    return sent


def _scheduled(cid, cap=1, adopted=False):
    e = FlexibleEvent(community_id=cid, app_id="a1", title="Supper",
                      state="scheduled", capacity=cap, adopted=adopted,
                      winning_date=DATE, winning_start_time="18:30")
    o = FlexiblePollOption(community_id=cid, app_id="a1", event_id=e.event_id,
                           iso_date=DATE, start_time="18:30")
    e.winning_option_id = o.option_id
    db.put_flexible_event(e)
    db.put_flexible_poll_option(o)
    return e, o


def _world(ddb_table, cap=1, adopted=False):
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    e, o = _scheduled(cid, cap, adopted)
    for u, t in ((aa, "ta"), (bob, "tb"), (sue, "ts"), (joe, "tj")):
        _mk_token(cid, e, u.user_id, t)
    return dict(cid=cid, app=app, aa=aa, mem=aa_mem, bob=bob, sue=sue,
                joe=joe, e=e, o=o)


def _attend(tok, ans, size=None):
    f = {"token": tok, "attend": ans}
    if size is not None:
        f["party_size"] = str(size)
    return web._api_flex_token_vote(_post("/api/e/vote", f))


def _seat(w, u):
    r = db.get_flexible_rsvp("a1", w["e"].event_id, u.user_id)
    return r.seat if r else None


def _invites(outbox, u):
    return [m for m in outbox if m["kind"] == "event_confirmed"
            and m["related_user_id"] == u.user_id]


def _taken(w):
    rs = {r.user_id: r for r in db.list_flexible_rsvps("a1", w["e"].event_id)}
    users = {u.user_id: u for u in db.list_users(w["cid"])}
    return seats.seats_taken(rs, web._flex_hh_keyer(users), w["o"].option_id,
                             "scheduled")


def test_party_growth_cannot_race_past_capacity(ddb_table, outbox) -> None:
    w = _world(ddb_table, cap=3)
    _attend("tj", "yes", 1)
    original = web._flex_party_refusal
    once = [False]

    def check(*a):
        out = original(*a)
        if not once[0]:
            once[0] = True
            _attend("ta", "yes", 2)       # takes the last two seats meanwhile
        return out
    with patch.object(web, "_flex_party_refusal", side_effect=check):
        body = _attend("tj", "yes", 2)["body"]
    assert _taken(w) <= 3
    assert "nothing was saved" in body


def test_plan_is_not_committed_over_a_changed_unchanged_row(
        ddb_table, outbox) -> None:
    w = _world(ddb_table, cap=2)
    _attend("ta", "yes", 1)
    with patch.object(web, "_flex_reseat", return_value=None):
        _attend("tj", "yes")
    original = db.commit_seat_plan
    grew = [False]

    def commit(*a, **kw):
        if not grew[0]:
            grew[0] = True
            r = db.get_flexible_rsvp("a1", w["e"].event_id, w["aa"].user_id)
            r.party_size = 2
            web._flex_write_rsvp(w["e"], r)
        return original(*a, **kw)
    with patch.object(db, "commit_seat_plan", side_effect=commit):
        web._flex_reseat("a1", w["e"].event_id)
    assert _seat(w, w["joe"]) == "wait_yes"
    assert _taken(w) == 2


def test_override_of_an_existing_no_force_seats(ddb_table, outbox) -> None:
    w = _world(ddb_table, cap=1)
    _attend("ta", "yes")
    _attend("tj", "no")
    r = db.get_flexible_rsvp("a1", w["e"].event_id, w["joe"].user_id)
    uid = w["joe"].user_id
    resp = web._api_flex_event_save_attendance(
        _post("/api/flex/event/attendance", {
            "event": w["e"].event_id,
            "sig": web._flex_roster_sig(w["e"], [w["o"]]),
            "member": uid, "dirty": uid, "override": uid,
            f"base_{uid}": web._flex_rsvp_stamp(r), f"v_{uid}_att": "yes"}),
        w["aa"], db.get_community(w["cid"]), w["app"], w["mem"])
    assert _seat(w, w["joe"]) == "seated"
    assert db.get_flexible_rsvp("a1", w["e"].event_id, uid).seat_forced
    assert "over capacity" in resp["headers"]["Location"].replace("%20", " ")


def test_adopted_event_never_emails_members(ddb_table, outbox) -> None:
    _world(ddb_table, cap=3, adopted=True)
    _attend("tj", "yes")
    assert not [m for m in outbox if m["kind"] == "event_confirmed"]


def test_promotion_sends_one_invite(ddb_table, outbox) -> None:
    w = _world(ddb_table, cap=1)
    _attend("ta", "yes")
    _attend("tj", "yes")
    with patch.object(web, "_flex_reseat", return_value=None):
        _attend("ta", "no")             # saved; its re-seat is late
    outbox.clear()
    _attend("tj", "yes")
    assert _seat(w, w["joe"]) == "seated"
    assert len(_invites(outbox, w["joe"])) == 1


def test_sweep_seating_a_saved_reply_sends_its_invite(ddb_table,
                                                      outbox) -> None:
    w = _world(ddb_table, cap=1)
    with patch.object(db, "commit_seat_plan",
                      side_effect=db.ConcurrencyConflict("test")):
        _attend("tj", "yes")
    assert _seat(w, w["joe"]) is None
    outbox.clear()
    web._seat_sweep()
    assert _seat(w, w["joe"]) == "seated"
    assert len(_invites(outbox, w["joe"])) == 1
    outbox.clear()
    web._seat_sweep()
    assert not _invites(outbox, w["joe"])


def test_clearing_capacity_admits_the_yes_waitlist(ddb_table, outbox) -> None:
    w = _world(ddb_table, cap=1)
    _attend("ta", "yes")
    _attend("tj", "yes")
    assert _seat(w, w["joe"]) == "wait_yes"
    outbox.clear()
    web._api_flex_event_save_capacity(
        _post("/api/flex/event/capacity", {"event": w["e"].event_id,
                                           "capacity": ""}),
        w["aa"], db.get_community(w["cid"]), w["app"], w["mem"])
    assert _seat(w, w["joe"]) is None
    assert len(_invites(outbox, w["joe"])) == 1
    assert "no longer a limit" in _invites(outbox, w["joe"])[0]["body_text"]


def test_clearing_capacity_quietly_ends_a_maybe_offer(ddb_table,
                                                      outbox) -> None:
    w = _world(ddb_table, cap=1)
    _attend("ta", "yes")
    _attend("tj", "maybe")
    _attend("ta", "no")
    assert _seat(w, w["joe"]) == "offered"
    outbox.clear()
    web._api_flex_event_save_capacity(
        _post("/api/flex/event/capacity", {"event": w["e"].event_id,
                                           "capacity": ""}),
        w["aa"], db.get_community(w["cid"]), w["app"], w["mem"])
    r = db.get_flexible_rsvp("a1", w["e"].event_id, w["joe"].user_id)
    assert r.seat is None and r.offer_expires_at is None
    assert not [m for m in outbox if m["related_user_id"] == w["joe"].user_id]


def test_first_answer_keeps_its_place_in_the_same_second(ddb_table,
                                                         outbox) -> None:
    """Two answers in the same second, both saved before either is seated:
    the one given first gets the seat (the time is stored with the answer,
    to the microsecond, and compared as a time, not as text)."""
    w = _world(ddb_table, cap=1)
    with patch.object(web, "_flex_reseat", return_value=None):
        _attend("tj", "yes")
        _attend("ta", "yes")
    web._seat_sweep()
    assert _seat(w, w["joe"]) == "seated"
    assert _seat(w, w["aa"]) == "wait_yes"
    assert seats.when("2098-01-01T12:00:00+00:00") < seats.when(
        "2098-01-01T12:00:00.100000+00:00")


def test_a_hundred_replies_are_seated_in_parts(ddb_table, outbox) -> None:
    w = _world(ddb_table, cap=200)
    for i in range(120):
        db.put_flexible_rsvp(FlexibleRSVP(
            community_id=w["cid"], app_id="a1", event_id=w["e"].event_id,
            user_id=f"role-{i:03d}", votes={w["o"].option_id: "yes"},
            confirmed_response="yes"))
    web._flex_reseat("a1", w["e"].event_id)
    rs = list(db.list_flexible_rsvps("a1", w["e"].event_id))
    assert sum(r.seat == "seated" for r in rs) == 120


def test_close_does_not_seat_a_yes_for_another_date(ddb_table,
                                                    outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    e = FlexibleEvent(community_id=cid, app_id="a1", title="Supper",
                      state="poll", capacity=5)
    db.put_flexible_event(e)
    a = FlexiblePollOption(community_id=cid, app_id="a1", event_id=e.event_id,
                           iso_date=DATE, start_time="18:30", sort_key=0)
    b = FlexiblePollOption(community_id=cid, app_id="a1", event_id=e.event_id,
                           iso_date="2099-03-22", start_time="18:30",
                           sort_key=1)
    db.put_flexible_poll_option(a)
    db.put_flexible_poll_option(b)
    _mk_token(cid, e, joe.user_id, "tj")
    web._api_flex_token_vote(_post("/api/e/vote", {
        "token": "tj", f"vote_{b.option_id}": "yes"}))
    outbox.clear()
    web._api_flex_event_close(
        _post("/api/flex/event/close", {"event": e.event_id,
                                        "winning_option": a.option_id}),
        aa, db.get_community(cid), app, aa_mem)
    assert db.get_flexible_rsvp("a1", e.event_id, joe.user_id).seat is None
    kinds = [m["kind"] for m in outbox if m["related_user_id"] == joe.user_id]
    assert kinds == ["event_claim_seat"]


def test_close_invites_a_maybe_in_a_seated_household(ddb_table,
                                                     outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    e = FlexibleEvent(community_id=cid, app_id="a1", title="Supper",
                      state="poll", capacity=4)
    db.put_flexible_event(e)
    a = FlexiblePollOption(community_id=cid, app_id="a1", event_id=e.event_id,
                           iso_date=DATE, start_time="18:30", sort_key=0)
    b = FlexiblePollOption(community_id=cid, app_id="a1", event_id=e.event_id,
                           iso_date="2099-03-22", start_time="18:30",
                           sort_key=1)
    db.put_flexible_poll_option(a)
    db.put_flexible_poll_option(b)
    _mk_token(cid, e, bob.user_id, "tb")
    _mk_token(cid, e, sue.user_id, "ts")
    web._api_flex_token_vote(_post("/api/e/vote", {
        "token": "tb", f"vote_{a.option_id}": "yes", "party_size": "2"}))
    web._api_flex_token_vote(_post("/api/e/vote", {
        "token": "ts", f"vote_{a.option_id}": "maybe"}))
    outbox.clear()
    web._api_flex_event_close(
        _post("/api/flex/event/close", {"event": e.event_id,
                                        "winning_option": a.option_id}),
        aa, db.get_community(cid), app, aa_mem)
    invited = {m["to_addr"] for m in outbox if m["kind"] == "event_confirmed"}
    assert {"bob@example.com", "sue@example.com"} <= invited
    # And exactly one each, though the close also re-seats afterwards.
    assert sum(m["to_addr"] == "sue@example.com" for m in outbox
               if m["kind"] == "event_confirmed") == 1


def test_a_turned_away_yes_gets_the_next_seats_that_fit(ddb_table,
                                                        outbox) -> None:
    w = _world(ddb_table, cap=4)
    for u, t in ((w["aa"], "ta"),):
        _attend(t, "yes", 3)
    _attend("tb", "yes", 2)             # household of 2: only 1 seat left
    assert _seat(w, w["bob"]) == "wait_yes"
    web._api_flex_event_save_capacity(
        _post("/api/flex/event/capacity", {"event": w["e"].event_id,
                                           "capacity": "5"}),
        w["aa"], db.get_community(w["cid"]), w["app"], w["mem"])
    assert _seat(w, w["bob"]) == "seated"
