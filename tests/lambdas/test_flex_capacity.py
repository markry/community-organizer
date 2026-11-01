"""Capacity and the waitlist on flexible events, through the real handlers:
seats first come first served, the yes and maybe tiers, hand-on when a seat
frees, offers to maybes, the cutoff, closing a poll, organizer overrides."""
from __future__ import annotations

import datetime as dt

import pytest

from community_organizer.core import db
from community_organizer.core.models import FlexibleEvent, FlexiblePollOption
from community_organizer.lambdas import web

from .test_flexible_event import _mk_token, _post, _seed

FUTURE = "2027-03-15"


@pytest.fixture
def outbox(monkeypatch):
    sent: list[dict] = []
    monkeypatch.setattr("community_organizer.providers.email.get_email_provider",
        lambda: type("S", (), {"send": lambda self, **kw: sent.append(kw)})())
    return sent


def _event(cid, *, cap, state="poll", dates=(FUTURE,), created_by=None):
    evt = FlexibleEvent(community_id=cid, app_id="a1", title="Supper",
                        state=state, capacity=cap, created_by=created_by)
    opts = []
    for i, d in enumerate(dates):
        o = FlexiblePollOption(community_id=cid, app_id="a1",
                               event_id=evt.event_id, iso_date=d,
                               start_time="18:30", sort_key=i)
        opts.append(o)
    if state == "scheduled":
        evt.winning_date, evt.winning_start_time = dates[0], "18:30"
        evt.winning_option_id = opts[0].option_id
    db.put_flexible_event(evt)
    for o in opts:
        db.put_flexible_poll_option(o)
    return evt, opts


def _vote(token, opt, answer, size=None):
    f = {"token": token, f"vote_{opt.option_id}": answer}
    if size is not None:
        f["party_size"] = str(size)
    return web._api_flex_token_vote(_post("/api/e/vote", f))["body"]


def _attend(token, answer):
    return web._api_flex_token_vote(_post("/api/e/vote", {
        "token": token, "attend": answer}))["body"]


def _seat(evt, user):
    return db.get_flexible_rsvp("a1", evt.event_id, user.user_id).seat


def _kinds(outbox, to=None):
    return [m["kind"] for m in outbox if to is None or m["to_addr"] == to]


def test_first_come_first_served_then_waitlist(ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, (opt,) = _event(cid, cap=2)
    _mk_token(cid, evt, joe.user_id, "tj")
    _mk_token(cid, evt, bob.user_id, "tb")
    _vote("tj", opt, "yes", 1)
    body = _vote("tb", opt, "yes", 2)
    assert _seat(evt, joe) == "seated"
    assert _seat(evt, bob) == "wait_yes"
    assert "on the waitlist (#1 in line)" in body
    # The page they see says so; no extra email for their own answer.
    assert "event_waitlisted" not in _kinds(outbox)
    page = web._flex_token_page({"rawPath": "/e/tb"})["body"]
    assert "#1</b> on the waitlist" in page


def test_cancellation_hands_the_seat_on(ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, (opt,) = _event(cid, cap=1)
    _mk_token(cid, evt, joe.user_id, "tj")
    _mk_token(cid, evt, bob.user_id, "tb")
    _vote("tj", opt, "yes")
    _vote("tb", opt, "yes")
    assert _seat(evt, bob) == "wait_yes"
    _vote("tj", opt, "no")
    assert _seat(evt, bob) == "seated" and _seat(evt, joe) is None
    assert "event_seat_promoted" in _kinds(outbox, "bob@example.com")


def test_maybe_waits_and_is_not_contacted_while_not_full(ddb_table,
                                                         outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, (opt,) = _event(cid, cap=3)
    _mk_token(cid, evt, joe.user_id, "tj")
    _mk_token(cid, evt, bob.user_id, "tb")
    _vote("tj", opt, "yes")
    body = _vote("tb", opt, "maybe")
    assert _seat(evt, bob) == "wait_maybe" and "waitlist for maybes" in body
    _vote("tj", opt, "no")
    assert _seat(evt, bob) == "wait_maybe"
    assert "event_seat_offered" not in _kinds(outbox)


def test_offer_to_maybe_then_accept_gets_invite(ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, (opt,) = _event(cid, cap=1, state="scheduled")
    _mk_token(cid, evt, joe.user_id, "tj")
    _mk_token(cid, evt, bob.user_id, "tb")
    _attend("tj", "yes")
    _attend("tb", "maybe")
    assert _seat(evt, joe) == "seated" and _seat(evt, bob) == "wait_maybe"
    _attend("tj", "no")
    assert _seat(evt, bob) == "offered"
    assert "event_seat_offered" in _kinds(outbox, "bob@example.com")
    page = web._flex_token_page({"rawPath": "/e/tb"})["body"]
    assert "Yes, I'll take it" in page
    outbox.clear()
    body = _attend("tb", "yes")
    assert _seat(evt, bob) == "seated"
    assert "Wonderful" in body
    assert [m for m in outbox if m["kind"] == "event_confirmed"
            and m["to_addr"] == "bob@example.com" and m.get("ics_content")]


def test_yes_tier_first_before_any_maybe(ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, (opt,) = _event(cid, cap=1, state="scheduled")
    _mk_token(cid, evt, aa.user_id, "ta")
    _mk_token(cid, evt, bob.user_id, "tb")
    _mk_token(cid, evt, joe.user_id, "tj")
    _attend("ta", "yes")
    _attend("tb", "maybe")                # earlier, but a maybe
    _attend("tj", "yes")
    _attend("ta", "no")
    assert _seat(evt, joe) == "seated" and _seat(evt, bob) == "wait_maybe"


def test_offer_lapses_on_sweep_and_passes_on(ddb_table, outbox,
                                             monkeypatch) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, (opt,) = _event(cid, cap=1, state="scheduled")
    for u, t in ((aa, "ta"), (joe, "tj"), (bob, "tb")):
        _mk_token(cid, evt, u.user_id, t)
    _attend("ta", "yes")
    _attend("tj", "maybe")
    _attend("tb", "maybe")
    _attend("ta", "no")
    assert _seat(evt, joe) == "offered"
    r = db.get_flexible_rsvp("a1", evt.event_id, joe.user_id)
    r.offer_expires_at = "2020-01-01T00:00:00+00:00"
    db.put_flexible_rsvp(r)
    web._seat_sweep()
    assert _seat(evt, joe) is None
    assert db.get_flexible_rsvp("a1", evt.event_id, joe.user_id).offer_passed
    assert _seat(evt, bob) == "offered"
    assert "event_seat_lapsed" in _kinds(outbox, "joe@example.com")


def test_cutoff_tells_organizer_instead(ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    soon = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=6)
            ).date().isoformat()
    evt, (opt,) = _event(cid, cap=1, state="scheduled", dates=(soon,))
    _mk_token(cid, evt, joe.user_id, "tj")
    _mk_token(cid, evt, bob.user_id, "tb")
    # Place them while the event is still far off, then move it close.
    evt.winning_date = FUTURE
    db.put_flexible_event(evt)
    _attend("tj", "yes")
    _attend("tb", "yes")
    evt = db.get_flexible_event("a1", evt.event_id)
    evt.winning_date = soon
    evt.winning_start_time = "23:59"
    db.put_flexible_event(evt)
    outbox.clear()
    _attend("tj", "no")
    assert _seat(evt, bob) == "wait_yes"
    cut = [m for m in outbox if m["kind"] == "event_seat_cutoff"]
    assert cut and cut[0]["to_addr"] == "aa@example.com"
    assert "Bob" in cut[0]["body_text"]
    # Told once, not on every re-plan.
    web._seat_sweep()
    assert len([m for m in outbox if m["kind"] == "event_seat_cutoff"]) == 1


def test_seated_household_cannot_grow_past_full(ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, (opt,) = _event(cid, cap=3)
    _mk_token(cid, evt, joe.user_id, "tj")
    _mk_token(cid, evt, bob.user_id, "tb")
    _vote("tb", opt, "yes", 2)
    _vote("tj", opt, "yes", 1)
    body = _vote("tb", opt, "yes", 4)
    assert "Only 2 seats are held for you" in body
    assert db.get_flexible_rsvp("a1", evt.event_id, bob.user_id).party_size == 2


def test_household_holds_seats_as_one(ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, (opt,) = _event(cid, cap=2)
    for u, t in ((bob, "tb"), (sue, "ts"), (joe, "tj")):
        _mk_token(cid, evt, u.user_id, t)
    _vote("tb", opt, "yes", 2)
    _vote("tj", opt, "yes", 1)
    _vote("ts", opt, "yes", 2)               # the household-mate, later
    assert _seat(evt, bob) == _seat(evt, sue) == "seated"
    assert _seat(evt, joe) == "wait_yes"


def test_close_seats_in_order_counts_creator_and_asks_nonresponders(
        ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, (o1, o2) = _event(cid, cap=2, dates=(FUTURE, "2027-03-22"),
                           created_by=aa.user_id)
    _mk_token(cid, evt, joe.user_id, "tj")
    _mk_token(cid, evt, bob.user_id, "tb")
    _mk_token(cid, evt, sue.user_id, "ts")
    web._api_flex_token_vote(_post("/api/e/vote", {
        "token": "tj", f"vote_{o1.option_id}": "yes",
        f"vote_{o2.option_id}": "no"}))
    web._api_flex_token_vote(_post("/api/e/vote", {
        "token": "tb", f"vote_{o1.option_id}": "yes",
        f"vote_{o2.option_id}": "yes", "party_size": "2"}))
    # No seats while the poll is choosing between dates.
    assert _seat(evt, joe) is None
    outbox.clear()
    web._api_flex_event_close(
        _post("/api/flex/event/close", {"event": evt.event_id,
                                        "winning_option": o1.option_id}),
        aa, db.get_community(cid), app, aa_mem)
    assert _seat(evt, aa) == "seated"        # the creator counts as a yes
    assert _seat(evt, joe) == "seated"
    assert _seat(evt, bob) == "wait_yes"
    invited = {m["to_addr"] for m in outbox if m["kind"] == "event_confirmed"}
    assert invited == {"aa@example.com", "joe@example.com"}
    assert "event_waitlisted" in _kinds(outbox, "bob@example.com")
    # Sue never answered; the household answered for her, so she's in line
    # with them rather than asked to claim a seat.
    assert "event_claim_seat" not in _kinds(outbox, "sue@example.com")


def test_close_nonresponder_asked_to_claim(ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, (o1, o2) = _event(cid, cap=5, dates=(FUTURE, "2027-03-22"))
    _mk_token(cid, evt, joe.user_id, "tj")
    web._api_flex_event_close(
        _post("/api/flex/event/close", {"event": evt.event_id,
                                        "winning_option": o1.option_id}),
        aa, db.get_community(cid), app, aa_mem)
    assert "event_claim_seat" in _kinds(outbox, "joe@example.com")
    assert "event_confirmed" not in _kinds(outbox, "joe@example.com")


def test_admin_can_seat_over_capacity_with_warning(ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, (opt,) = _event(cid, cap=1, state="scheduled")
    _mk_token(cid, evt, bob.user_id, "tb")
    _attend("tb", "yes")
    options = list(db.list_flexible_poll_options("a1", evt.event_id))
    resp = web._api_flex_event_save_attendance(
        _post("/api/flex/event/attendance", {
            "event": evt.event_id,
            "sig": web._flex_roster_sig(evt, options),
            "member": joe.user_id, "dirty": joe.user_id,
            f"base_{joe.user_id}": "", f"v_{joe.user_id}_att": "yes"}),
        aa, db.get_community(cid), app, aa_mem)
    assert _seat(evt, joe) == "seated"
    assert db.get_flexible_rsvp("a1", evt.event_id, joe.user_id).seat_forced
    assert "over capacity" in resp["headers"]["Location"].replace("%20", " ")


def test_raising_capacity_promotes(ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, (opt,) = _event(cid, cap=1)
    _mk_token(cid, evt, joe.user_id, "tj")
    _mk_token(cid, evt, bob.user_id, "tb")
    _vote("tj", opt, "yes")
    _vote("tb", opt, "yes")
    web._api_flex_event_save_capacity(
        _post("/api/flex/event/capacity", {"event": evt.event_id,
                                           "capacity": "5"}),
        aa, db.get_community(cid), app, aa_mem)
    assert _seat(evt, bob) == "seated"


def test_no_capacity_changes_nothing(ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, (opt,) = _event(cid, cap=None)
    _mk_token(cid, evt, joe.user_id, "tj")
    _vote("tj", opt, "yes")
    assert _seat(evt, joe) is None


def test_results_page_shows_seats_and_waitlist(ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, (opt,) = _event(cid, cap=1)
    _mk_token(cid, evt, joe.user_id, "tj")
    _mk_token(cid, evt, bob.user_id, "tb")
    _vote("tj", opt, "yes")
    _vote("tb", opt, "maybe")
    ev = {"requestContext": {"http": {"method": "GET"}},
          "queryStringParameters": {"event": evt.event_id}}
    body = web._flex_event_results_page(ev, aa, db.get_community(cid), app,
                                        aa_mem)["body"]
    assert "1 of 1 seats" in body and "1 waiting (0 yes, 1 maybe)" in body
    assert "Waitlist: said maybe" in body
