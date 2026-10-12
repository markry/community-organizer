"""Marking replies by hand on flexible_event, and adopted events.

Replies reach an organizer by text, by phone and in person, so an AA can record
them on any event: per candidate date while a poll is open, coming / not coming
once the date is set. A member's own answer always wins — it is locked unless
the AA unlocks that row, and it replaces an AA's mark when the member answers
later.

An adopted event is one arranged outside the app: created straight into
`scheduled` with no poll, no links and no email, so every answer on it is the
AA's."""
from __future__ import annotations

import pytest

from community_organizer.core import db
from community_organizer.core.models import FlexibleRSVP
from community_organizer.lambdas import web

from .test_flexible_event import (
    _add_option, _get, _mk_event, _mk_token, _post, _seed,
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


def _load(evt) -> dict:
    """What a replies page built right now would carry: the table's shape and
    each member's row as stored. Keep one to post a stale form later."""
    evt = db.get_flexible_event("a1", evt.event_id)
    options = list(db.list_flexible_poll_options("a1", evt.event_id))
    rsvps = {r.user_id: r for r in db.list_flexible_rsvps("a1", evt.event_id)}
    return {"sig": web._flex_roster_sig(evt, options),
            "base": {m.user_id: web._flex_rsvp_stamp(rsvps.get(m.user_id))
                     for m in db.list_memberships_for_app("a1")}}


def _mark(cid, app, aa, aa_mem, evt, answers: dict, *, sizes: dict | None = None,
          override: tuple = (), page: dict | None = None,
          dirty: list | None = None):
    """Post the replies form. `answers` maps user_id -> an answer: a plain
    "yes" / "no" / "" for a scheduled event, or {option_id: vote} for a poll.
    Users in `override` are posted as unlocked rows, the rest as editable.

    `page` is the page the form came from (default: one loaded this instant);
    `dirty` the rows the organizer touched (default: every row in `answers`;
    pass [] for a browser without JavaScript, which sends no markers)."""
    page = page or _load(evt)
    form: dict = {"event": evt.event_id, "sig": page["sig"],
                  "member": [u for u in answers if u not in override],
                  "override": list(override),
                  "dirty": list(answers) if dirty is None else dirty}
    for uid, a in answers.items():
        for key, val in (a.items() if isinstance(a, dict) else [("att", a)]):
            form[f"v_{uid}_{key}"] = val
        form[f"size_{uid}"] = str((sizes or {}).get(uid, ""))
        form[f"base_{uid}"] = page["base"].get(uid, "")
    return web._api_flex_event_save_attendance(
        _post("/api/flex/event/attendance", form),
        aa, db.get_community(cid), app, aa_mem)


def _vote_online(cid, evt, user, votes: dict, token: str, **extra):
    _mk_token(cid, evt, user.user_id, token=token)
    web._api_flex_token_vote(_post("/api/e/vote", {
        "token": token, **{f"vote_{k}": v for k, v in votes.items()}, **extra}))


def _rsvp(evt, user):
    return db.get_flexible_rsvp("a1", evt.event_id, user.user_id)


# ---- adopting --------------------------------------------------------------

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


def test_adopted_page_has_the_table_and_no_poll_machinery(
        ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    _adopt(cid, app, aa, aa_mem)
    body = _results(cid, app, aa, aa_mem, _the_event())
    assert "id='roster-form'" in body and "Save replies" in body
    assert "Arranged outside the app" in body
    for u in (bob, sue, joe):
        assert f"name='v_{u.user_id}_att'" in body
    assert "Date votes" not in body
    assert "/api/flex/event/send-poll" not in body
    assert "/api/flex/event/ask-host" not in body
    assert "Cancel this event" in body
    # No links exist, so there is nothing to override and no warning to give.
    assert "class='rs-unlock'" not in body
    assert "warn=false" in body


def test_marking_an_adopted_event(ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    _adopt(cid, app, aa, aa_mem)
    evt = _the_event()
    resp = _mark(cid, app, aa, aa_mem, evt,
                 {bob.user_id: "yes", sue.user_id: "yes", joe.user_id: "no"})
    assert resp["statusCode"] == 302
    assert _rsvp(evt, joe).confirmed_response == "no"
    assert _rsvp(evt, joe).marked_by == aa.user_id
    # Bob + Sue share a household: both marked, no count given -> 2 expected.
    assert _rsvp(evt, bob).party_size == 2
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
    # A defaulted count is shown blank, so it keeps tracking who is marked.
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


def test_blank_forgets_the_reply(ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    _adopt(cid, app, aa, aa_mem)
    evt = _the_event()
    _mark(cid, app, aa, aa_mem, evt, {joe.user_id: "no"})
    _mark(cid, app, aa, aa_mem, evt, {joe.user_id: ""})
    assert _rsvp(evt, joe) is None


def test_rows_not_on_the_form_are_left_alone(ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    _adopt(cid, app, aa, aa_mem)
    evt = _the_event()
    _mark(cid, app, aa, aa_mem, evt, {joe.user_id: "yes"})
    _mark(cid, app, aa, aa_mem, evt, {bob.user_id: "no"})    # Joe not posted
    assert _rsvp(evt, joe).confirmed_response == "yes"


def test_marking_is_admin_only(ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    _adopt(cid, app, aa, aa_mem)
    evt = _the_event()
    resp = _mark(cid, app, bob, db.get_membership("a1", bob.user_id), evt,
                 {bob.user_id: "yes"})
    assert resp["statusCode"] == 403
    assert _rsvp(evt, bob) is None


def test_non_members_cannot_be_marked(ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    _adopt(cid, app, aa, aa_mem)
    evt = _the_event()
    _mark(cid, app, aa, aa_mem, evt, {"stranger": "yes"})
    _mark(cid, app, aa, aa_mem, evt, {"stranger": "yes"}, override=("stranger",))
    assert list(db.list_flexible_rsvps("a1", evt.event_id)) == []


# ---- the same table on a poll ---------------------------------------------

def test_poll_table_has_a_column_per_date(ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, o1 = _mk_event(cid)
    o2 = _add_option(cid, evt, "2026-08-22")
    body = _results(cid, app, aa, aa_mem, evt)
    assert "id='roster-form'" in body and "their answer replaces yours" in body
    assert "warn=true" in body and "multi=true" in body
    # Who answered for themselves comes first, the unanswered last.
    order = [body.index(h) for h in ("Member direct feedback (", "Admin-created status (",
                                     "Haven't answered (") if h in body]
    assert order == sorted(order)
    for o in (o1, o2):
        assert f"name='v_{joe.user_id}_{o.option_id}'" in body
    assert "Haven't answered (4)" in body        # the AA is a member too


def test_marking_votes_on_a_several_date_poll(ddb_table, outbox) -> None:
    """A member texts which dates suit them: the AA records it per date, and
    it counts exactly as if they had answered online."""
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, o1 = _mk_event(cid)
    o2 = _add_option(cid, evt, "2026-08-22")
    _mark(cid, app, aa, aa_mem, evt, {
        joe.user_id: {o1.option_id: "no", o2.option_id: "maybe"},
        bob.user_id: {o1.option_id: "no", o2.option_id: "no"},
        sue.user_id: {o1.option_id: "", o2.option_id: ""}})
    j = _rsvp(evt, joe)
    assert j.votes == {o1.option_id: "no", o2.option_id: "maybe"}
    assert j.confirmed_response == "yes" and j.marked_by == aa.user_id
    assert _rsvp(evt, bob).confirmed_response == "no"       # no to every date
    assert _rsvp(evt, sue) is None
    body = _results(cid, app, aa, aa_mem, evt)
    assert "Admin-created status (2)" in body and "Haven't answered (2)" in body
    # Closing on the second date invites Joe and sends Bob the courtesy note.
    confirmed, couldnt, _ = web._flex_cohorts(
        app, db.get_community(cid), evt.event_id, o2.option_id)
    assert joe.user_id in {u.user_id for u in confirmed}
    assert bob.user_id in {u.user_id for u in couldnt}
    assert outbox == []


def test_a_members_own_answer_is_locked(ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    _vote_online(cid, evt, joe, {opt.option_id: "yes"}, "tj", bringing="Salad")
    before = _rsvp(evt, joe)
    body = _results(cid, app, aa, aa_mem, evt)
    assert "Member direct feedback (1)" in body and "class='rs-unlock'" in body
    assert f"name='override' value='{joe.user_id}' disabled" in body
    # Even a forged post of the row as an ordinary editable one changes nothing.
    _mark(cid, app, aa, aa_mem, evt, {joe.user_id: {opt.option_id: "no"}})
    assert _rsvp(evt, joe) == before


def test_unlocking_lets_the_aa_change_a_members_answer(ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    _vote_online(cid, evt, joe, {opt.option_id: "yes"}, "tj", bringing="Salad",
                 party_size="3")
    # The unlocked row posts what it showed, including their stated count.
    _mark(cid, app, aa, aa_mem, evt, {joe.user_id: {opt.option_id: "maybe"}},
          sizes={joe.user_id: 3}, override=(joe.user_id,))
    r = _rsvp(evt, joe)
    assert r.votes == {opt.option_id: "maybe"} and r.marked_by == aa.user_id
    assert r.bringing == "Salad" and r.party_size == 3      # the rest is kept
    assert "Admin-created status (1)" in _results(cid, app, aa, aa_mem, evt)


def test_unlocking_without_changing_anything_writes_nothing(
        ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    _vote_online(cid, evt, joe, {opt.option_id: "yes"}, "tj")
    before = _rsvp(evt, joe)
    _mark(cid, app, aa, aa_mem, evt, {joe.user_id: {opt.option_id: "yes"}},
          override=(joe.user_id,))
    assert _rsvp(evt, joe) == before and before.marked_by is None


def test_a_later_reply_from_the_member_replaces_the_mark(
        ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    _mark(cid, app, aa, aa_mem, evt, {joe.user_id: {opt.option_id: "yes"}})
    _mk_token(cid, evt, joe.user_id, token="tj")
    page = web._flex_token_page(_get("/e/tj"))["body"]
    assert "The organizer recorded the answer below for you" in page
    web._api_flex_token_vote(_post("/api/e/vote", {
        "token": "tj", f"vote_{opt.option_id}": "no"}))
    r = _rsvp(evt, joe)
    assert r.confirmed_response == "no" and r.marked_by is None
    assert "Member direct feedback (1)" in _results(cid, app, aa, aa_mem, evt)
    assert "organizer recorded" not in web._flex_token_page(_get("/e/tj"))["body"]


def test_marked_members_are_not_chased_as_unanswered(ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    _mark(cid, app, aa, aa_mem, evt, {joe.user_id: {opt.option_id: "yes"}})
    covered = web._flex_covered_or_responded_ids(
        list(db.list_memberships_for_app("a1")),
        {u.user_id: u for u in db.list_users(cid)},
        list(db.list_flexible_rsvps("a1", evt.event_id)))
    assert joe.user_id in covered and bob.user_id not in covered


# ---- and once the date is set ---------------------------------------------

def test_scheduled_event_from_a_poll_collapses_to_coming_or_not(
        ddb_table, outbox) -> None:
    """After the date is chosen the table is a single coming / not coming.
    Someone who had declined online tells the AA they can come after all:
    unlocking and marking them moves them into the people coming."""
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    _add_option(cid, evt, "2026-08-22")
    db.put_flexible_rsvp(FlexibleRSVP(
        community_id=cid, app_id="a1", event_id=evt.event_id,
        user_id=joe.user_id, votes={opt.option_id: "no"},
        confirmed_response="no"))
    evt.state, evt.winning_date = "scheduled", opt.iso_date
    db.put_flexible_event(evt)
    body = _results(cid, app, aa, aa_mem, evt)
    assert f"name='v_{bob.user_id}_att'" in body
    assert f"name='v_{bob.user_id}_{opt.option_id}'" not in body
    _mark(cid, app, aa, aa_mem, evt, {joe.user_id: "yes", bob.user_id: "yes"},
          override=(joe.user_id,))
    r = _rsvp(evt, joe)
    assert r.confirmed_response == "yes" and r.votes[opt.option_id] == "yes"
    ev = {"requestContext": {"http": {"method": "GET"}}}
    page = web._send_email_page(ev, aa, db.get_community(cid), app, aa_mem)
    assert "+ coming (2)</button>" in page["body"]
    assert outbox == []


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
    assert "+ coming (2)</button>" in body
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


def test_maybe_on_a_scheduled_event(ddb_table, outbox) -> None:
    """Plans stay uncertain after the date is set. A maybe counts toward the
    expected headcount but is not one of "the people coming"."""
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    _adopt(cid, app, aa, aa_mem)
    evt = _the_event()
    assert ">Maybe</option>" in _results(cid, app, aa, aa_mem, evt)
    _mark(cid, app, aa, aa_mem, evt, {bob.user_id: "yes", joe.user_id: "maybe"})
    assert _rsvp(evt, joe).confirmed_response == "maybe"
    assert _rsvp(evt, joe).party_size == 1
    body = _results(cid, app, aa, aa_mem, evt)
    assert "<b>Expected headcount:</b> 2 " in body and "<b>Maybe:</b> 1" in body
    assert f"<option value='maybe' selected>" in body
    ev = {"requestContext": {"http": {"method": "GET"}}}
    page = web._send_email_page(ev, aa, db.get_community(cid), app, aa_mem)["body"]
    assert "+ coming (1)</button>" in page
    assert "+ coming plus maybes (2)</button>" in page
    # Back to a plain yes: the second button has nothing to add, so it goes.
    _mark(cid, app, aa, aa_mem, evt, {joe.user_id: "yes"})
    page = web._send_email_page(ev, aa, db.get_community(cid), app, aa_mem)["body"]
    assert "coming plus maybes" not in page


def test_groups_list_coming_then_maybe_then_no(ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    _adopt(cid, app, aa, aa_mem)
    evt = _the_event()
    # Alphabetical would be Bob, Joe, Sue; by answer it is Sue, Joe, Bob.
    _mark(cid, app, aa, aa_mem, evt,
          {bob.user_id: "no", joe.user_id: "maybe", sue.user_id: "yes"})
    body = _results(cid, app, aa, aa_mem, evt)
    section = body.split("Admin-created status (3)")[1].split("Haven't answered")[0]
    order = [section.index(f"value='{u.user_id}'") for u in (sue, joe, bob)]
    assert order == sorted(order)


def test_send_email_names_the_event_and_opens_on_select(ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    _adopt(cid, app, aa, aa_mem)
    evt = _the_event()
    assert f"/admin/send-email?event={evt.event_id}" in _results(
        cid, app, aa, aa_mem, evt)
    ev = {"requestContext": {"http": {"method": "GET"}},
          "queryStringParameters": {"event": evt.event_id}}
    body = web._send_email_page(ev, aa, db.get_community(cid), app, aa_mem)["body"]
    assert "About: <b>Autumn Dinner</b> &middot; Saturday, November 14" in body
    assert "value='Book Club: Autumn Dinner -- '" in body
    assert "id='mode-select' checked>" in body and "id='mode-all'>" in body


def test_send_email_default_stays_all_after_a_poll_closes(ddb_table, outbox) -> None:
    """An event that came through a poll and has its date: everyone was asked
    and invited, so the page still opens on All members."""
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    ev = {"requestContext": {"http": {"method": "GET"}}}
    body = web._send_email_page(ev, aa, db.get_community(cid), app, aa_mem)["body"]
    assert "id='mode-select' checked>" in body          # poll still open
    evt.state, evt.winning_date = "scheduled", opt.iso_date
    db.put_flexible_event(evt)
    body = web._send_email_page(ev, aa, db.get_community(cid), app, aa_mem)["body"]
    assert "id='mode-all' checked>" in body and "About: <b>August Book Club</b>" in body


# ---- a stale page must not destroy newer replies ---------------------------
# Found in review: the form used to post every row and treat a blank as "no
# answer", so a page loaded earlier would erase whatever had been recorded
# since. Each test here saves from a page that is out of date.

def test_a_stale_page_does_not_erase_a_reply_recorded_elsewhere(
        ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    _adopt(cid, app, aa, aa_mem)
    evt = _the_event()
    old_page = _load(evt)                                    # first tab
    _mark(cid, app, aa, aa_mem, evt, {joe.user_id: "yes"})   # second tab
    # Back in the first tab the organizer changes only Bob. The browser also
    # posts Joe's row, still blank as that page drew it.
    _mark(cid, app, aa, aa_mem, evt, {bob.user_id: "no", joe.user_id: ""},
          page=old_page, dirty=[bob.user_id])
    assert _rsvp(evt, joe).confirmed_response == "yes"
    assert _rsvp(evt, bob).confirmed_response == "no"


def test_a_stale_row_is_refused_even_when_touched(ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    _adopt(cid, app, aa, aa_mem)
    evt = _the_event()
    old_page = _load(evt)
    _mark(cid, app, aa, aa_mem, evt, {joe.user_id: "yes"})
    resp = _mark(cid, app, aa, aa_mem, evt, {joe.user_id: "no"}, page=old_page)
    assert _rsvp(evt, joe).confirmed_response == "yes"       # left alone
    assert "error=" in resp["headers"]["Location"]           # and said so


def test_without_javascript_a_stale_page_still_cannot_erase(
        ddb_table, outbox) -> None:
    """No dirty markers: every posted row is considered, so the per-row check
    is all that stands between an old page and newer data."""
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    _adopt(cid, app, aa, aa_mem)
    evt = _the_event()
    old_page = _load(evt)
    _mark(cid, app, aa, aa_mem, evt, {joe.user_id: "yes"})
    _mark(cid, app, aa, aa_mem, evt, {bob.user_id: "no", joe.user_id: ""},
          page=old_page, dirty=[])
    assert _rsvp(evt, joe).confirmed_response == "yes"
    assert _rsvp(evt, bob).confirmed_response == "no"


def test_a_page_from_before_a_date_was_added_is_refused(ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, o1 = _mk_event(cid)
    old_page = _load(evt)
    o2 = _add_option(cid, evt, "2026-08-22")
    _mark(cid, app, aa, aa_mem, evt,
          {joe.user_id: {o1.option_id: "no", o2.option_id: "yes"}})
    resp = _mark(cid, app, aa, aa_mem, evt, {joe.user_id: {o1.option_id: "yes"}},
                 page=old_page)
    assert _rsvp(evt, joe).votes == {o1.option_id: "no", o2.option_id: "yes"}
    assert "error=" in resp["headers"]["Location"]


def test_a_poll_page_left_open_cannot_wipe_replies_after_it_closes(
        ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    _mark(cid, app, aa, aa_mem, evt, {joe.user_id: {opt.option_id: "yes"},
                                      bob.user_id: {opt.option_id: "yes"}})
    old_page = _load(evt)
    evt = db.get_flexible_event("a1", evt.event_id)
    evt.state, evt.winning_date = "scheduled", opt.iso_date
    db.put_flexible_event(evt)
    # The old page posts per-date fields; the event now wants a single answer.
    _mark(cid, app, aa, aa_mem, evt,
          {joe.user_id: {opt.option_id: "yes"}, bob.user_id: {opt.option_id: "no"}},
          page=old_page)
    assert _rsvp(evt, joe).confirmed_response == "yes"
    assert _rsvp(evt, bob).confirmed_response == "yes"


def test_a_member_answering_mid_save_is_not_overwritten(
        ddb_table, outbox, monkeypatch) -> None:
    """The member's answer lands after the save has read the row and before it
    writes. The conditional write must lose, not the member."""
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    _mark(cid, app, aa, aa_mem, evt, {joe.user_id: {opt.option_id: "yes"}})
    _mk_token(cid, evt, joe.user_id, token="tj")
    real_put = db.put_flexible_rsvp
    fired = []

    def put_after_member(rsvp, **kw):
        if not fired and rsvp.user_id == joe.user_id and rsvp.marked_by:
            fired.append(1)
            monkeypatch.setattr(db, "put_flexible_rsvp", real_put)
            web._api_flex_token_vote(_post("/api/e/vote", {
                "token": "tj", f"vote_{opt.option_id}": "no"}))
        return real_put(rsvp, **kw)

    monkeypatch.setattr(db, "put_flexible_rsvp", put_after_member)
    resp = _mark(cid, app, aa, aa_mem, evt, {joe.user_id: {opt.option_id: "maybe"}})
    r = _rsvp(evt, joe)
    assert fired and r.votes == {opt.option_id: "no"} and r.marked_by is None
    assert "error=" in resp["headers"]["Location"]


def test_clearing_a_stated_count_restores_the_default(ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    _adopt(cid, app, aa, aa_mem)
    evt = _the_event()
    _mark(cid, app, aa, aa_mem, evt, {joe.user_id: "yes"}, sizes={joe.user_id: 4})
    assert _rsvp(evt, joe).party_size == 4
    _mark(cid, app, aa, aa_mem, evt, {joe.user_id: "yes"})   # box blanked
    r = _rsvp(evt, joe)
    assert r.party_size == 1 and r.party_size_auto


def test_a_household_default_count_drops_when_one_of_them_drops_out(
        ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    _adopt(cid, app, aa, aa_mem)
    evt = _the_event()
    _mark(cid, app, aa, aa_mem, evt, {bob.user_id: "yes", sue.user_id: "yes"})
    _mark(cid, app, aa, aa_mem, evt, {sue.user_id: "no"})    # only Sue touched
    assert _rsvp(evt, bob).party_size == 1
    assert "<b>Expected headcount:</b> 1 " in _results(cid, app, aa, aa_mem, evt)


# ---- which option won, and what the member says afterwards -----------------

def _close(cid, app, aa, aa_mem, evt, opt):
    return web._api_flex_event_close(
        _post("/api/flex/event/close",
              {"event": evt.event_id, "winning_option": opt.option_id}),
        aa, db.get_community(cid), app, aa_mem)


def test_two_times_on_one_date_use_the_time_that_won(ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, early = _mk_event(cid)                              # 18:30
    late = _add_option(cid, evt, early.iso_date, start_time="20:00")
    _vote_online(cid, evt, joe, {early.option_id: "no", late.option_id: "yes"}, "tj")
    _close(cid, app, aa, aa_mem, evt, late)
    evt = db.get_flexible_event("a1", evt.event_id)
    assert evt.winning_option_id == late.option_id
    options = list(db.list_flexible_poll_options("a1", evt.event_id))
    won = web._flex_roster_option(evt, options)
    assert won.option_id == late.option_id
    assert web._flex_roster_answer(evt, _rsvp(evt, joe), won) == "yes"
    # An event closed before the winner's id was stored: matched by time.
    evt.winning_option_id = None
    assert web._flex_roster_option(evt, options).option_id == late.option_id


def test_rejoining_after_an_organizers_no_shows_as_coming(ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    _mk_token(cid, evt, joe.user_id, token="tj")
    _close(cid, app, aa, aa_mem, evt, opt)
    evt = db.get_flexible_event("a1", evt.event_id)
    _mark(cid, app, aa, aa_mem, evt, {joe.user_id: "no"})
    web._api_flex_token_join(_post("/api/e/join", {"token": "tj"}))
    r = _rsvp(evt, joe)
    assert r.confirmed_response == "yes" and r.votes[opt.option_id] == "yes"
    assert r.marked_by is None
    ev = {"requestContext": {"http": {"method": "GET"}}}
    page = web._send_email_page(ev, aa, db.get_community(cid), app, aa_mem)["body"]
    assert joe.user_id in page.split("var comingTo=")[1].split(";")[0]


def test_a_member_can_settle_an_organizers_maybe(ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    _mk_token(cid, evt, joe.user_id, token="tj")
    _close(cid, app, aa, aa_mem, evt, opt)
    evt = db.get_flexible_event("a1", evt.event_id)
    _mark(cid, app, aa, aa_mem, evt, {joe.user_id: "maybe"})
    page = web._flex_token_page(_get("/e/tj"))["body"]
    assert "We have you down as a maybe" in page and "name='attend'" in page
    # Saving only a dish says nothing about attendance: still the organizer's.
    web._api_flex_token_vote(_post("/api/e/vote", {
        "token": "tj", "bringing": "Salad"}))
    r = _rsvp(evt, joe)
    assert r.confirmed_response == "maybe" and r.marked_by == aa.user_id
    assert r.bringing == "Salad"
    # "Still not sure" is their own answer, and sends nothing.
    outbox.clear()
    web._api_flex_token_vote(_post("/api/e/vote", {
        "token": "tj", "attend": "maybe", "bringing": "Salad"}))
    assert _rsvp(evt, joe).marked_by is None
    assert not [m for m in outbox if m.get("ics_content")]
    # Saying yes is theirs too, and earns the calendar invite — once.
    resp = web._api_flex_token_vote(_post("/api/e/vote", {
        "token": "tj", "attend": "yes", "bringing": "Salad"}))
    r = _rsvp(evt, joe)
    assert r.confirmed_response == "yes" and r.marked_by is None
    assert r.votes[opt.option_id] == "yes"
    invites = [m for m in outbox if m.get("ics_content")]
    assert [m["to_addr"] for m in invites] == ["joe@example.com"]
    assert "sent you the calendar invite" in resp["body"]
    web._api_flex_token_vote(_post("/api/e/vote", {
        "token": "tj", "bringing": "Bread"}))               # just the dish
    assert len([m for m in outbox if m.get("ics_content")]) == 1
    assert "name='attend'" not in web._flex_token_page(_get("/e/tj"))["body"]


# ---- the shared recipient picker ------------------------------------------

def test_recipient_names_are_never_parsed_as_markup(ddb_table, outbox) -> None:
    """The picker adds names with DOM calls. innerHTML there would run a
    name containing markup in an admin's browser — on every app type."""
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    ev = {"requestContext": {"http": {"method": "GET"}}}
    body = web._send_email_page(ev, aa, db.get_community(cid), app, aa_mem)["body"]
    add = body.split("function addRecipient(uid,name){")[1].split(
        "picker.onchange")[0]
    assert "innerHTML" not in add and "createTextNode(name" in add


def test_a_blank_email_matches_nobody(ddb_table) -> None:
    from community_organizer.core.models import User
    cid, *_ = _seed(ddb_table)
    db.put_user(User(community_id=cid, email="", name="Guest", channel="none"))
    assert db.get_user_by_email(cid, "") is None
    assert db.get_user_by_email(cid, "   ") is None


def test_send_email_is_always_about_one_event(ddb_table, outbox) -> None:
    """With several live events the page picks the soonest, names it in a
    switcher at the top, and the quick-picks are for that event alone — so
    they never need to say which event they mean."""
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    import datetime as dt
    ahead = lambda days: (dt.date.today() + dt.timedelta(days=days)).isoformat()
    _adopt(cid, app, aa, aa_mem, title="Winter Dinner", date0=ahead(60))
    winter = _the_event()
    _adopt(cid, app, aa, aa_mem, date0=ahead(30))      # Autumn: the sooner one
    autumn = next(e for e in db.list_flexible_events("a1")
                  if e.event_id != winter.event_id)
    _mark(cid, app, aa, aa_mem, autumn, {bob.user_id: "yes"})
    _mark(cid, app, aa, aa_mem, winter, {joe.user_id: "yes", sue.user_id: "yes"})
    ev = {"requestContext": {"http": {"method": "GET"}}}

    def page():
        return web._send_email_page(ev, aa, db.get_community(cid), app,
                                    aa_mem)["body"]

    body = page()                                      # none chosen: soonest
    assert f"<option value='{autumn.event_id}' selected>Autumn Dinner" in body
    assert f"<option value='{winter.event_id}'>Winter Dinner" in body
    assert "+ coming (1)</button>" in body and "coming to" not in body
    assert "value='Book Club: Autumn Dinner -- '" in body
    ev["queryStringParameters"] = {"event": winter.event_id}
    body = page()
    assert f"<option value='{winter.event_id}' selected>Winter Dinner" in body
    assert "+ coming (2)</button>" in body and "coming to" not in body


def test_send_email_opens_on_the_next_upcoming_event(ddb_table, outbox) -> None:
    """An event stays live for a while after its date. Opened from the menu,
    the page should be about the next one coming up, not last week's — and
    fall back to the most recent past one only when nothing is ahead."""
    import datetime as dt
    today = dt.date.today()
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    for title, days in (("Long Ago", -30), ("Last Week", -7), ("Next Month", 30)):
        _adopt(cid, app, aa, aa_mem, title=title,
               date0=(today + dt.timedelta(days=days)).isoformat())
    ev = {"requestContext": {"http": {"method": "GET"}}}

    def about() -> str:
        body = web._send_email_page(ev, aa, db.get_community(cid), app,
                                    aa_mem)["body"]
        return body.split("' selected>")[1].split("</option>")[0]

    assert about() == "Next Month"
    nxt = next(e for e in db.list_flexible_events("a1") if e.title == "Next Month")
    db.delete_flexible_event("a1", nxt.event_id)
    assert about() == "Last Week"
    # A poll with no date yet counts as ahead of anything already past.
    _mk_event(cid)
    assert about() == "August Book Club"


# ---- round 2 review ---------------------------------------------------------

def test_havent_responded_is_for_the_event_the_page_is_about(
        ddb_table, outbox) -> None:
    """Two open polls, each answered by a different member. The shortcut on
    the older poll's page must list who has not answered THAT poll — it used
    to take whichever poll was newest."""
    import json
    from community_organizer.core.models import FlexibleEvent, FlexiblePollOption
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    older, o1 = _mk_event(cid)
    newer = FlexibleEvent(community_id=cid, app_id="a1", title="Later Poll",
                          state="poll", created_at="2099-01-01T00:00:00+00:00")
    db.put_flexible_event(newer)
    o2 = FlexiblePollOption(community_id=cid, app_id="a1",
                            event_id=newer.event_id, iso_date="2026-09-01")
    db.put_flexible_poll_option(o2)
    _mark(cid, app, aa, aa_mem, older, {joe.user_id: {o1.option_id: "yes"}})
    _mark(cid, app, aa, aa_mem, newer, {bob.user_id: {o2.option_id: "yes"}})

    def not_responded(event_id) -> set:
        ev = {"requestContext": {"http": {"method": "GET"}},
              "queryStringParameters": {"event": event_id}}
        body = web._send_email_page(ev, aa, db.get_community(cid), app,
                                    aa_mem)["body"]
        blob = body.split("var notResponded=")[1].split(";")[0]
        return {u["uid"] for u in json.loads(blob)}

    assert joe.user_id not in not_responded(older.event_id)
    assert bob.user_id in not_responded(older.event_id)
    assert bob.user_id not in not_responded(newer.event_id)
    assert joe.user_id in not_responded(newer.event_id)


def test_the_maybe_form_as_a_browser_posts_it_leaves_the_mark_alone(
        ddb_table, outbox) -> None:
    """Post exactly what the rendered form would send when the member only
    types a dish: every named control with its current value. No attendance
    choice may be preselected, or the save would count as their answer."""
    import re
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    _mk_token(cid, evt, joe.user_id, token="tj")
    _close(cid, app, aa, aa_mem, evt, opt)
    evt = db.get_flexible_event("a1", evt.event_id)
    _mark(cid, app, aa, aa_mem, evt, {joe.user_id: "maybe"})
    page = web._flex_token_page(_get("/e/tj"))["body"]
    form = page.split("action='/api/e/vote'")[1].split("</form>")[0]
    posted = {}
    for tag in re.findall(r"<input[^>]*>", form):
        name = re.search(r"name='([^']*)'", tag)
        if not name:
            continue
        kind = re.search(r"type='([^']*)'", tag)
        if kind and kind.group(1) in ("radio", "checkbox") and " checked" not in tag:
            continue                                # unchecked: not sent
        value = re.search(r"value='([^']*)'", tag)
        posted[name.group(1)] = value.group(1) if value else ""
    assert "attend" not in posted
    posted["bringing"] = "Salad"
    web._api_flex_token_vote(_post("/api/e/vote", posted))
    r = _rsvp(evt, joe)
    assert r.bringing == "Salad" and r.confirmed_response == "maybe"
    assert r.marked_by == aa.user_id


def test_a_refused_edit_does_not_change_a_household_mates_count(
        ddb_table, outbox, monkeypatch) -> None:
    """Both of a household are coming, count 2 by default. The organizer
    marks one of them No, but that row changes underneath and the edit is
    refused. The other's count must stay 2: nobody actually dropped out."""
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    _adopt(cid, app, aa, aa_mem)
    evt = _the_event()
    _mark(cid, app, aa, aa_mem, evt, {bob.user_id: "yes", sue.user_id: "yes"})
    assert _rsvp(evt, bob).party_size == 2
    real_put = db.put_flexible_rsvp
    fired = []

    def put(rsvp, **kw):
        if not fired and rsvp.user_id == sue.user_id:
            fired.append(1)
            monkeypatch.setattr(db, "put_flexible_rsvp", real_put)
            other = db.get_flexible_rsvp("a1", evt.event_id, sue.user_id)
            other.host_note = "changed meanwhile"
            real_put(other, expected_version=other.version)
        return real_put(rsvp, **kw)
    monkeypatch.setattr(db, "put_flexible_rsvp", put)
    resp = _mark(cid, app, aa, aa_mem, evt, {sue.user_id: "no"})
    assert fired and "error=" in resp["headers"]["Location"]
    assert _rsvp(evt, sue).confirmed_response == "yes"
    assert _rsvp(evt, bob).party_size == 2


def test_a_reply_cleared_and_recorded_again_is_a_different_row(
        ddb_table, outbox) -> None:
    """Same member, same version, same second: the old page's stamp must
    still not match the new reply."""
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    _adopt(cid, app, aa, aa_mem)
    evt = _the_event()
    _mark(cid, app, aa, aa_mem, evt, {joe.user_id: "yes"})
    first = _rsvp(evt, joe)
    old_page = _load(evt)
    _mark(cid, app, aa, aa_mem, evt, {joe.user_id: ""})
    _mark(cid, app, aa, aa_mem, evt, {joe.user_id: "no"})
    second = _rsvp(evt, joe)
    # Force the coincidence the review described.
    second.version, second.updated_at = first.version, first.updated_at
    db.put_flexible_rsvp(second)
    assert web._flex_rsvp_stamp(second) != web._flex_rsvp_stamp(first)
    _mark(cid, app, aa, aa_mem, evt, {joe.user_id: "yes"}, page=old_page,
          dirty=[])
    assert _rsvp(evt, joe).confirmed_response == "no"


def test_an_update_does_not_bring_back_a_reply_cleared_meanwhile(
        ddb_table, outbox, monkeypatch) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    _adopt(cid, app, aa, aa_mem)
    evt = _the_event()
    _mark(cid, app, aa, aa_mem, evt, {joe.user_id: "yes"})
    real_put = db.put_flexible_rsvp
    fired = []

    def put(rsvp, **kw):
        if not fired and rsvp.user_id == joe.user_id:
            fired.append(1)
            monkeypatch.setattr(db, "put_flexible_rsvp", real_put)
            db.delete_flexible_rsvp("a1", evt.event_id, joe.user_id)
        return real_put(rsvp, **kw)
    monkeypatch.setattr(db, "put_flexible_rsvp", put)
    resp = _mark(cid, app, aa, aa_mem, evt, {joe.user_id: "no"})
    assert fired and "error=" in resp["headers"]["Location"]
    assert _rsvp(evt, joe) is None


def test_without_javascript_a_members_new_answer_is_reported_not_ignored(
        ddb_table, outbox) -> None:
    """The organizer's page showed the member as unanswered. They answer
    online; the organizer then submits a different answer from a form with no
    change markers. The member's answer stands, and the organizer is told."""
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    old_page = _load(evt)
    _vote_online(cid, evt, joe, {opt.option_id: "yes"}, "tj")
    resp = _mark(cid, app, aa, aa_mem, evt, {joe.user_id: {opt.option_id: "no"}},
                 page=old_page, dirty=[])
    assert _rsvp(evt, joe).votes == {opt.option_id: "yes"}
    assert "error=" in resp["headers"]["Location"]
