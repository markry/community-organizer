"""Hosting volunteers on flexible_event: an AA can ask people who are coming
whether they'd host. The question is only put once the date is known (one-date
event, or after the poll closes); offers are AA-only; the AA picks the host, who
alone gets a private thank-you."""
from __future__ import annotations

import pytest

from community_organizer.core import db
from community_organizer.core.models import FlexibleRSVP
from community_organizer.lambdas import web

from .test_flexible_event import _add_option, _get, _mk_event, _mk_token, _post, _seed


@pytest.fixture
def outbox(monkeypatch):
    sent: list[dict] = []
    monkeypatch.setattr("community_organizer.providers.email.get_email_provider",
        lambda: type("S", (), {"send": lambda self, **kw: sent.append(kw)})())
    return sent


def _ask_host(evt, *, prompt=None):
    evt.ask_for_host = True
    evt.host_prompt = prompt
    db.put_flexible_event(evt)


def _token_page(token: str) -> str:
    return web._flex_token_page(_get(f"/e/{token}"))["body"]


def _results(cid, app, aa, aa_mem, evt) -> str:
    ev = {"requestContext": {"http": {"method": "GET"}},
          "queryStringParameters": {"event": evt.event_id}}
    return web._flex_event_results_page(
        ev, aa, db.get_community(cid), app, aa_mem)["body"]


def _volunteer(cid, evt, opt, user, token, note="Big kitchen, parks 6"):
    _mk_token(cid, evt, user.user_id, token=token)
    web._api_flex_token_vote(_post("/api/e/vote", {
        "token": token, f"vote_{opt.option_id}": "yes", "party_size": "2",
        "willing_to_host": "1", "host_note": note}))


# ---- the member side ------------------------------------------------------

def test_one_date_event_asks_about_hosting_right_away(ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)                                   # one date
    _ask_host(evt, prompt="Could you host us?")
    _mk_token(cid, evt, joe.user_id, token="tj")
    body = _token_page("tj")
    assert "Could you host us?" in body and "name='willing_to_host'" in body
    assert "Only the organizers see this." in body

    _volunteer(cid, evt, opt, joe, "tj")
    r = db.get_flexible_rsvp("a1", evt.event_id, joe.user_id)
    assert r.willing_to_host and r.host_note == "Big kitchen, parks 6"


def test_no_hosting_question_unless_the_aa_asked(ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    _mk_token(cid, evt, joe.user_id, token="tj")
    assert "willing_to_host" not in _token_page("tj")
    _volunteer(cid, evt, opt, joe, "tj")                        # forged field
    assert not db.get_flexible_rsvp("a1", evt.event_id, joe.user_id).willing_to_host


def test_date_poll_only_promises_the_question(ddb_table, outbox) -> None:
    """Several candidate dates: nobody can offer a home for an unknown date,
    so the poll shows a heads-up and ignores any posted offer."""
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    _add_option(cid, evt, "2026-08-22")
    _ask_host(evt)
    _mk_token(cid, evt, joe.user_id, token="tj")
    body = _token_page("tj")
    assert "we'll ask who'd be willing to host" in body
    assert "name='willing_to_host'" not in body
    _volunteer(cid, evt, opt, joe, "tj")
    assert not db.get_flexible_rsvp("a1", evt.event_id, joe.user_id).willing_to_host


def test_saying_no_withdraws_a_hosting_offer(ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    _ask_host(evt)
    _volunteer(cid, evt, opt, joe, "tj")
    web._api_flex_token_vote(_post("/api/e/vote", {
        "token": "tj", f"vote_{opt.option_id}": "no", "willing_to_host": "1"}))
    assert not db.get_flexible_rsvp("a1", evt.event_id, joe.user_id).willing_to_host


def test_offers_are_hidden_from_household_mates(ddb_table, outbox) -> None:
    """Household-mates see each other's answers as guidance — but a hosting
    offer is for the organizers only."""
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    _ask_host(evt)
    _volunteer(cid, evt, opt, bob, "tb", note="Use the side door")
    _mk_token(cid, evt, sue.user_id, token="ts")
    body = _token_page("ts")
    assert "Bob" in body                                  # household guidance
    assert "Use the side door" not in body
    assert "checked" not in body.split("name='willing_to_host'")[1][:20]


def test_new_offer_emails_the_aas_even_with_notify_off(ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    _ask_host(evt)
    _volunteer(cid, evt, opt, joe, "tj")
    offers = [m for m in outbox if m["kind"] == "event_host_offer"]
    assert len(offers) == 1 and offers[0]["to_addr"] == "aa@example.com"
    assert "Big kitchen, parks 6" in offers[0]["body_text"]
    # Re-saving the same offer doesn't re-notify.
    _volunteer(cid, evt, opt, joe, "tj")
    assert len([m for m in outbox if m["kind"] == "event_host_offer"]) == 1


# ---- the AA side ----------------------------------------------------------

def test_results_page_lists_volunteers_for_admins(ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    _ask_host(evt)
    _volunteer(cid, evt, opt, joe, "tj")
    body = _results(cid, app, aa, aa_mem, evt)
    assert "Hosting" in body and "(visible to admins only)" in body
    assert "Big kitchen, parks 6" in body and "Choose as host" in body


def test_results_page_is_still_admin_only(ddb_table) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, _ = _mk_event(cid)
    ev = {"requestContext": {"http": {"method": "GET"}},
          "queryStringParameters": {"event": evt.event_id}}
    resp = web._flex_event_results_page(
        ev, joe, db.get_community(cid), app, db.get_membership("a1", joe.user_id))
    assert resp["statusCode"] == 403


def test_choosing_a_host_tells_only_the_host(ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    _ask_host(evt)
    _volunteer(cid, evt, opt, joe, "tj")
    outbox.clear()
    resp = web._api_flex_event_choose_host(
        _post("/api/flex/event/host", {"event": evt.event_id,
                                       "user_id": joe.user_id}),
        aa, db.get_community(cid), app, aa_mem)
    assert resp["statusCode"] == 302
    assert db.get_flexible_event("a1", evt.event_id).host_user_id == joe.user_id
    assert [m["to_addr"] for m in outbox] == ["joe@example.com"]
    assert outbox[0]["kind"] == "event_host_chosen"
    assert "you're our host" in outbox[0]["body_text"]
    # The question closes for everyone else; the host gets a thank-you.
    _mk_token(cid, evt, sue.user_id, token="ts")
    assert "willing_to_host" not in _token_page("ts")
    assert "You're hosting" in _token_page("tj")


def test_cannot_choose_someone_who_did_not_offer(ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    _ask_host(evt)
    web._api_flex_event_choose_host(
        _post("/api/flex/event/host", {"event": evt.event_id,
                                       "user_id": joe.user_id}),
        aa, db.get_community(cid), app, aa_mem)
    assert db.get_flexible_event("a1", evt.event_id).host_user_id is None
    assert outbox == []


def test_clearing_the_host_reopens_without_email(ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    _ask_host(evt)
    _volunteer(cid, evt, opt, joe, "tj")
    web._api_flex_event_choose_host(
        _post("/api/flex/event/host", {"event": evt.event_id,
                                       "user_id": joe.user_id}),
        aa, db.get_community(cid), app, aa_mem)
    outbox.clear()
    web._api_flex_event_choose_host(
        _post("/api/flex/event/host", {"event": evt.event_id, "user_id": ""}),
        aa, db.get_community(cid), app, aa_mem)
    assert db.get_flexible_event("a1", evt.event_id).host_user_id is None
    assert outbox == []
    assert "name='willing_to_host'" in _token_page("tj")


def test_date_poll_asks_in_the_confirmation_email(ddb_table, outbox) -> None:
    """Step two: closing a multi-date poll puts the question, with the
    person's own link, in each confirmation — then the link page asks it."""
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    _add_option(cid, evt, "2026-08-22")
    _ask_host(evt, prompt="Could you host us?")
    _mk_token(cid, evt, joe.user_id, token="tj")
    web._api_flex_token_vote(_post("/api/e/vote", {
        "token": "tj", f"vote_{opt.option_id}": "yes", "party_size": "1"}))
    web._api_flex_event_close(
        _post("/api/flex/event/close",
              {"event": evt.event_id, "winning_option": opt.option_id}),
        aa, db.get_community(cid), app, aa_mem)
    confirm = next(m for m in outbox if m["kind"] == "event_confirmed"
                   and m["to_addr"] == "joe@example.com")
    assert "Could you host us?" in confirm["body_text"]
    assert "/e/tj" in confirm["body_text"]
    assert "name='willing_to_host'" in _token_page("tj")       # now scheduled
    web._api_flex_token_vote(_post("/api/e/vote", {
        "token": "tj", "willing_to_host": "1", "host_note": "Any Saturday"}))
    assert db.get_flexible_rsvp("a1", evt.event_id, joe.user_id).willing_to_host


def test_one_date_confirmation_does_not_ask_again(ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    _ask_host(evt)
    _mk_token(cid, evt, joe.user_id, token="tj")
    web._api_flex_token_vote(_post("/api/e/vote", {
        "token": "tj", f"vote_{opt.option_id}": "yes", "party_size": "1"}))
    web._api_flex_event_close(
        _post("/api/flex/event/close",
              {"event": evt.event_id, "winning_option": opt.option_id}),
        aa, db.get_community(cid), app, aa_mem)
    confirm = next(m for m in outbox if m["kind"] == "event_confirmed")
    assert "host" not in confirm["body_text"].lower()


def test_cannot_choose_a_host_before_the_date_is_known(ddb_table, outbox) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    _add_option(cid, evt, "2026-08-22")
    _ask_host(evt)
    db.put_flexible_rsvp(FlexibleRSVP(
        community_id=cid, app_id="a1", event_id=evt.event_id,
        user_id=joe.user_id, willing_to_host=True, confirmed_response="yes"))
    web._api_flex_event_choose_host(
        _post("/api/flex/event/host", {"event": evt.event_id,
                                       "user_id": joe.user_id}),
        aa, db.get_community(cid), app, aa_mem)
    assert db.get_flexible_event("a1", evt.event_id).host_user_id is None


def test_ask_host_setting_and_create_flag(ddb_table) -> None:
    cid, app, aa, aa_mem, *_ = _seed(ddb_table)
    web._api_flex_event_create(
        _post("/api/flex/event/create", {"title": "Dinner", "date0": "2026-09-05",
                                         "ask_for_host": "1"}),
        aa, db.get_community(cid), app, aa_mem)
    evt = next(iter(db.list_flexible_events("a1")))
    assert evt.ask_for_host
    web._api_flex_event_save_ask_host(
        _post("/api/flex/event/ask-host", {"event": evt.event_id,
                                           "ask_for_host": "1",
                                           "host_prompt": "Got room for 20?"}),
        aa, db.get_community(cid), app, aa_mem)
    assert db.get_flexible_event("a1", evt.event_id).host_prompt == "Got room for 20?"
    web._api_flex_event_save_ask_host(
        _post("/api/flex/event/ask-host", {"event": evt.event_id}),
        aa, db.get_community(cid), app, aa_mem)
    assert not db.get_flexible_event("a1", evt.event_id).ask_for_host
