"""Adopted events on flexible_event, and the hand-marked roster.

A gathering arranged outside the app is created straight into `scheduled` — no
poll, no links, no email. From there it is an ordinary scheduled event whose
roster the AA keeps by hand. The same roster is on every event with a known
date, because replies also arrive by text, by phone and in person."""
from __future__ import annotations

import pytest

from community_organizer.core import db
from community_organizer.core.models import FlexibleRSVP
from community_organizer.lambdas import web

from .test_flexible_event import (
    _add_option, _mk_event, _mk_token, _post, _seed,
)


@pytest.fixture
def outbox(monkeypatch):
    sent: list[dict] = []
    monkeypatch.setattr("community_organizer.providers.email.get_email_provider",
        lambda: type("S", (), {"send": lambda self, **kw: sent.append(kw)})())
    return sent


def _adopt(cid, app, aa, aa_mem, **fields):
    form = {"title": "Autumn Dinner", "location": "Parish hall",
            "duration": "120", "adopted": "1", "date0": "2026-11-14",
            "time0": "18:30"} | fields
    return web._api_flex_event_create(
        _post("/api/flex/event/create", form),
        aa, db.get_community(cid), app, aa_mem)


def _the_event():
    (evt,) = list(db.list_flexible_events("a1"))
    return evt


def _results(cid, app, aa, aa_mem, evt) -> str:
    ev = {"requestContext": {"http": {"method": "GET"}},
          "queryStringParameters": {"event": evt.event_id}}
    return web._flex_event_results_page(
        ev, aa, db.get_community(cid), app, aa_mem)["body"]


def _mark(cid, app, aa, aa_mem, evt, answers: dict, sizes: dict | None = None):
    """Post the attendance form: `answers` maps user_id -> "yes" / "no" / ""."""
    form: dict = {"event": evt.event_id, "member": list(answers)}
    for uid, a in answers.items():
        form[f"att_{uid}"] = a
        form[f"size_{uid}"] = str((sizes or {}).get(uid, ""))
    return web._api_flex_event_save_attendance(
        _post("/api/flex/event/attendance", form),
        aa, db.get_community(cid), app, aa_mem)


def test_adopting_creates_a_scheduled_event_and_emails_nobody(
        ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, *_ = _seed(ddb_table)
    resp = _adopt(cid, app, aa, aa_mem)
    assert resp["statusCode"] == 302
    evt = _the_event()
    assert evt.adopted and evt.state == "scheduled"
    assert (evt.winning_date, evt.winning_start_time) == ("2026-11-14", "18:30")
    assert list(db.list_flexible_poll_options("a1", evt.event_id)) == []
    assert list(db.list_event_tokens("a1", evt.event_id)) == []
    assert outbox == []


def test_adopting_needs_exactly_one_date(ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, *_ = _seed(ddb_table)
    _adopt(cid, app, aa, aa_mem, date1="2026-11-21")
    _adopt(cid, app, aa, aa_mem, date0="")
    assert list(db.list_flexible_events("a1")) == []


def test_a_poll_is_still_the_default(ddb_table) -> None:
    cid, app, aa, aa_mem, *_ = _seed(ddb_table)
    _adopt(cid, app, aa, aa_mem, adopted="")
    evt = _the_event()
    assert evt.state == "poll" and not evt.adopted
    assert len(list(db.list_flexible_poll_options("a1", evt.event_id))) == 1


def test_results_page_offers_a_roster_not_a_poll(ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    _adopt(cid, app, aa, aa_mem)
    body = _results(cid, app, aa, aa_mem, _the_event())
    assert "Mark replies by hand" in body and "Save replies" in body
    assert "arranged outside the app" in body
    for u in (bob, sue, joe):
        assert f"name='att_{u.user_id}'" in body
    # None of the poll machinery: no votes table, no send, no host question.
    assert "Date votes" not in body
    assert "/api/flex/event/send-poll" not in body
    assert "/api/flex/event/ask-host" not in body
    assert "Cancel this event" in body


def test_marking_attendance_by_hand(ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    _adopt(cid, app, aa, aa_mem)
    evt = _the_event()
    resp = _mark(cid, app, aa, aa_mem, evt,
          {bob.user_id: "yes", sue.user_id: "yes", joe.user_id: "no"})
    assert resp["statusCode"] == 302
    assert db.get_flexible_rsvp("a1", evt.event_id, joe.user_id
                                ).confirmed_response == "no"
    # Bob + Sue share a household: both ticked, no count given -> 2 expected.
    assert db.get_flexible_rsvp("a1", evt.event_id, bob.user_id).party_size == 2
    body = _results(cid, app, aa, aa_mem, evt)
    assert "<b>Expected headcount:</b> 2 " in body
    assert "<b>Declined:</b> 1" in body
    assert outbox == []


def test_a_given_count_overrides_the_household_default(ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    _adopt(cid, app, aa, aa_mem)
    evt = _the_event()
    _mark(cid, app, aa, aa_mem, evt,
          {bob.user_id: "yes", sue.user_id: "yes", joe.user_id: "yes"},
          sizes={bob.user_id: 4})
    body = _results(cid, app, aa, aa_mem, evt)
    assert "<b>Expected headcount:</b> 5 " in body          # 4 + Joe
    assert f"name='size_{bob.user_id}' min='1' value='4'" in body
    # A count that is just the default is shown blank, so it keeps tracking.
    assert f"name='size_{joe.user_id}' min='1' value=''" in body


def test_default_count_follows_later_changes(ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    _adopt(cid, app, aa, aa_mem)
    evt = _the_event()
    _mark(cid, app, aa, aa_mem, evt,
          {bob.user_id: "yes", sue.user_id: "", joe.user_id: ""})
    assert "<b>Expected headcount:</b> 1 " in _results(cid, app, aa, aa_mem, evt)
    _mark(cid, app, aa, aa_mem, evt,
          {bob.user_id: "yes", sue.user_id: "yes", joe.user_id: ""})
    assert "<b>Expected headcount:</b> 2 " in _results(cid, app, aa, aa_mem, evt)


def test_no_answer_forgets_the_response(ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    _adopt(cid, app, aa, aa_mem)
    evt = _the_event()
    _mark(cid, app, aa, aa_mem, evt, {joe.user_id: "no"})
    _mark(cid, app, aa, aa_mem, evt, {joe.user_id: ""})
    assert db.get_flexible_rsvp("a1", evt.event_id, joe.user_id) is None


def test_rows_not_on_the_form_are_left_alone(ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    _adopt(cid, app, aa, aa_mem)
    evt = _the_event()
    _mark(cid, app, aa, aa_mem, evt, {joe.user_id: "yes"})
    _mark(cid, app, aa, aa_mem, evt, {bob.user_id: "no"})            # Joe not posted
    assert db.get_flexible_rsvp("a1", evt.event_id, joe.user_id
                                ).confirmed_response == "yes"


def test_marking_is_admin_only(ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    _adopt(cid, app, aa, aa_mem)
    evt = _the_event()
    resp = _mark(cid, app, bob, db.get_membership("a1", bob.user_id), evt,
                 {bob.user_id: "yes"})
    assert resp["statusCode"] == 403
    assert db.get_flexible_rsvp("a1", evt.event_id, bob.user_id) is None


# ---- the same roster on ordinary events -----------------------------------

def test_one_date_invitation_can_be_marked_by_hand(ddb_table, outbox) -> None:
    """A reply that came by text is recorded as that member's answer to the
    one date, so the tallies and the eventual invite list agree with it."""
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    body = _results(cid, app, aa, aa_mem, evt)
    assert "Mark replies by hand" in body and ">Maybe</th>" in body
    assert "arranged outside the app" not in body
    _mark(cid, app, aa, aa_mem, evt,
          {joe.user_id: "maybe", bob.user_id: "no", sue.user_id: ""})
    r = db.get_flexible_rsvp("a1", evt.event_id, joe.user_id)
    assert r.votes == {opt.option_id: "maybe"} and r.confirmed_response == "yes"
    assert db.get_flexible_rsvp("a1", evt.event_id, bob.user_id
                                ).confirmed_response == "no"
    confirmed, couldnt, _ = web._flex_cohorts(
        app, db.get_community(cid), evt.event_id, opt.option_id)
    assert bob.user_id in {u.user_id for u in couldnt}
    assert joe.user_id in {u.user_id for u in confirmed}
    assert outbox == []


def test_several_dates_cannot_be_marked_yet(ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, _opt = _mk_event(cid)
    _add_option(cid, evt, "2026-08-22")
    assert "Mark replies by hand" not in _results(cid, app, aa, aa_mem, evt)
    _mark(cid, app, aa, aa_mem, evt, {joe.user_id: "yes"})
    assert db.get_flexible_rsvp("a1", evt.event_id, joe.user_id) is None


def test_saving_leaves_a_members_own_answer_alone(ddb_table, outbox) -> None:
    """The form posts every row. A member who answered online — count, dish
    and all — must come through a save untouched unless the AA changed them."""
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    _mk_token(cid, evt, joe.user_id, token="tj")
    web._api_flex_token_vote(_post("/api/e/vote", {
        "token": "tj", f"vote_{opt.option_id}": "yes", "bringing": "Salad"}))
    before = db.get_flexible_rsvp("a1", evt.event_id, joe.user_id)
    _mark(cid, app, aa, aa_mem, evt, {joe.user_id: "yes", bob.user_id: "yes"})
    after = db.get_flexible_rsvp("a1", evt.event_id, joe.user_id)
    assert after == before and after.bringing == "Salad"


def test_scheduled_event_from_a_poll_can_be_marked(ddb_table, outbox) -> None:
    """After the date is chosen, someone who had said no tells the AA they
    can come after all: marking them moves them into the people coming."""
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    db.put_flexible_rsvp(FlexibleRSVP(
        community_id=cid, app_id="a1", event_id=evt.event_id,
        user_id=joe.user_id, votes={opt.option_id: "no"},
        confirmed_response="no"))
    evt.state, evt.winning_date = "scheduled", opt.iso_date
    db.put_flexible_event(evt)
    body = _results(cid, app, aa, aa_mem, evt)
    assert "Mark replies by hand" in body and ">Maybe</th>" not in body
    _mark(cid, app, aa, aa_mem, evt, {joe.user_id: "yes"})
    r = db.get_flexible_rsvp("a1", evt.event_id, joe.user_id)
    assert r.confirmed_response == "yes" and r.votes[opt.option_id] == "yes"
    ev = {"requestContext": {"http": {"method": "GET"}}}
    page = web._send_email_page(ev, aa, db.get_community(cid), app, aa_mem)
    assert "+ coming to August Book Club (1)</button>" in page["body"]
    assert outbox == []


def test_non_members_cannot_be_marked(ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    _adopt(cid, app, aa, aa_mem)
    evt = _the_event()
    web._api_flex_event_save_attendance(
        _post("/api/flex/event/attendance",
              {"event": evt.event_id, "member": ["stranger"],
               "att_stranger": "yes"}),
        aa, db.get_community(cid), app, aa_mem)
    assert list(db.list_flexible_rsvps("a1", evt.event_id)) == []


def test_send_email_offers_the_people_coming(ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    ev = {"requestContext": {"http": {"method": "GET"}}}

    def page() -> str:
        return web._send_email_page(ev, aa, db.get_community(cid), app,
                                    aa_mem)["body"]

    _adopt(cid, app, aa, aa_mem)
    evt = _the_event()
    assert "qp-coming" not in page().split("<script")[0]     # nobody marked yet
    _mark(cid, app, aa, aa_mem, evt,
          {bob.user_id: "yes", sue.user_id: "no", joe.user_id: "yes"})
    body = page()
    assert "+ coming to Autumn Dinner (2)</button>" in body
    picked = body.split("var comingTo=")[1].split(";")[0]
    assert bob.user_id in picked and joe.user_id in picked
    assert sue.user_id not in picked


def test_home_lists_an_adopted_event_as_scheduled(ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    _adopt(cid, app, aa, aa_mem)
    evt = _the_event()
    db.put_flexible_rsvp(FlexibleRSVP(
        community_id=cid, app_id="a1", event_id=evt.event_id,
        user_id=joe.user_id, confirmed_response="yes", party_size=3))
    body = web._flexible_home(event={}, user=aa, community=db.get_community(cid),
                              app=app, membership=aa_mem,
                              org_name="Book Club")["body"]
    assert "Scheduled" in body and "Autumn Dinner" in body
    assert "3 attending" in body and "Open polls" not in body
