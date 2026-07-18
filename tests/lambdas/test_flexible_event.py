"""Flow tests for the flexible_event date-poll / book-club feature:
AA create/send/close + the public passwordless token flow (vote, decline,
household warning, opt-out)."""
from __future__ import annotations

import datetime as dt
import urllib.parse

from community_organizer.core import db
from community_organizer.core.models import (
    Application, Community, EventToken, FlexibleEvent, FlexiblePollOption,
    FlexibleSeries, Membership, User,
)
from community_organizer.lambdas import web


def _seed(ddb_table):
    cid = "c1"
    db.put_community(Community(community_id=cid, name="Parish",
                              default_timezone="America/New_York"))
    app = Application(community_id=cid, name="Book Club",
                      app_type="flexible_event", app_id="a1")
    db.put_application(app)
    db.put_flexible_series(FlexibleSeries(
        community_id=cid, app_id="a1", default_location="Parish hall",
        bring_prompt="What will you bring?"))
    aa = User(community_id=cid, email="aa@example.com", name="Organizer")
    bob = User(community_id=cid, email="bob@example.com", name="Bob", household_id="h1")
    sue = User(community_id=cid, email="sue@example.com", name="Sue", household_id="h1")
    joe = User(community_id=cid, email="joe@example.com", name="Joe")
    for u in (aa, bob, sue, joe):
        db.put_user(u)
    db.put_membership(Membership(community_id=cid, app_id="a1",
                                 user_id=aa.user_id, app_role="aa"))
    for u in (bob, sue, joe):
        db.put_membership(Membership(community_id=cid, app_id="a1",
                                     user_id=u.user_id))
    return cid, app, aa, db.get_membership("a1", aa.user_id), bob, sue, joe


def _post(path: str, fields: dict) -> dict:
    # doseq so a list value becomes repeated keys (user_id=a&user_id=b) the way
    # a real checkbox group posts, not the repr of a list.
    return {"rawPath": path,
            "requestContext": {"http": {"method": "POST"}},
            "body": urllib.parse.urlencode(fields, doseq=True),
            "isBase64Encoded": False}


def _get(path: str) -> dict:
    return {"rawPath": path, "requestContext": {"http": {"method": "GET"}}}


def _mk_event(cid, *, state="poll", date="2026-08-15"):
    evt = FlexibleEvent(community_id=cid, app_id="a1", title="August Book Club",
                        state=state, location="Vic's house")
    db.put_flexible_event(evt)
    opt = FlexiblePollOption(community_id=cid, app_id="a1",
                             event_id=evt.event_id, iso_date=date,
                             start_time="18:30", sort_key=0)
    db.put_flexible_poll_option(opt)
    return evt, opt


def _add_option(cid, evt, date, *, sort_key=1, start_time="18:30"):
    """Add another candidate date to an event (turns a fixed-date event into a
    multi-date poll)."""
    opt = FlexiblePollOption(community_id=cid, app_id="a1",
                             event_id=evt.event_id, iso_date=date,
                             start_time=start_time, sort_key=sort_key)
    db.put_flexible_poll_option(opt)
    return opt


def _mk_token(cid, evt, user_id, token="tok-1"):
    db.put_event_token(EventToken(
        community_id=cid, app_id="a1", event_id=evt.event_id, user_id=user_id,
        token=token,
        expires_at=dt.datetime(2099, 1, 1, tzinfo=dt.timezone.utc).isoformat()))
    return token


# ---- AA flows -------------------------------------------------------------

def test_create_event_makes_poll_with_options(ddb_table) -> None:
    cid, app, aa, aa_mem, *_ = _seed(ddb_table)
    resp = web._api_flex_event_create(
        _post("/api/flex/event/create",
              {"title": "August Book Club", "location": "Vic's",
               "duration": "120", "date0": "2026-08-15", "time0": "18:30",
               "date1": "2026-08-22"}),
        aa, db.get_community(cid), app, aa_mem)
    assert resp["statusCode"] == 302
    evts = list(db.list_flexible_events("a1"))
    assert len(evts) == 1 and evts[0].state == "poll"
    assert evts[0].winning_duration_minutes == 120
    assert len(list(db.list_flexible_poll_options("a1", evts[0].event_id))) == 2


def test_results_page_lists_voter_names_per_date(ddb_table) -> None:
    """Per-date buckets count HOUSEHOLDS with the headcount implied ('N for M'),
    names grouped by household. Bob answers for his 2-person household (Yes),
    Joe declines solo (No), the Organizer is still pending."""
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    web._api_flex_token_vote(_post("/api/e/vote", {
        "token": _mk_token(cid, evt, bob.user_id, token="tb"),
        f"vote_{opt.option_id}": "yes", "party_size": "2"}))
    web._api_flex_token_vote(_post("/api/e/vote", {
        "token": _mk_token(cid, evt, joe.user_id, token="tj"),
        f"vote_{opt.option_id}": "no"}))
    ev = {"requestContext": {"http": {"method": "GET"}},
          "queryStringParameters": {"event": evt.event_id}}
    body = web._flex_event_results_page(
        ev, aa, db.get_community(cid), app, aa_mem)["body"]
    assert "Who voted" in body                       # two-column table header
    # 1 household / 2 people, spouses grouped under one name.
    assert "Yes (1 for 2):</b> Bob &amp; Sue" in body
    # Joe, solo, no headcount given -> falls back to his household size (1).
    assert "No (1 for 1):</b> Joe" in body
    # The Organizer never answered -> still pending, counted by household.
    assert "Pending (1 household)" in body and "Organizer" in body
    assert "/api/flex/event/cancel" in body
    assert "Cancel this event" in body               # tokens sent -> cancel
    # The bottom "App home" back-link must be a real link, not a disabled
    # span (regression: the page used to mark itself current="home").
    assert "<a href='/'" in body and "App home</a>" in body


def test_per_date_headcount_reflects_reported_party_size(ddb_table) -> None:
    """'N for M' uses the reported headcount, not the household member count:
    a 2-person household bringing a guest reads 'Yes (1 for 3)'."""
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    web._api_flex_token_vote(_post("/api/e/vote", {
        "token": _mk_token(cid, evt, bob.user_id, token="tb"),
        f"vote_{opt.option_id}": "yes", "party_size": "3"}))
    ev = {"requestContext": {"http": {"method": "GET"}},
          "queryStringParameters": {"event": evt.event_id}}
    body = web._flex_event_results_page(
        ev, aa, db.get_community(cid), app, aa_mem)["body"]
    assert "Yes (1 for 3):</b> Bob &amp; Sue" in body


def test_pending_count_is_household_aware(ddb_table) -> None:
    """Per-date 'Pending' counts households we're still waiting on — a covered
    spouse drops out, so it matches the household counts elsewhere."""
    from community_organizer.core.models import FlexibleRSVP
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    # Bob answers for his 2-person household (covers Sue).
    db.put_flexible_rsvp(FlexibleRSVP(
        community_id=cid, app_id="a1", event_id=evt.event_id,
        user_id=bob.user_id, votes={opt.option_id: "yes"}, party_size=2))
    ev = {"requestContext": {"http": {"method": "GET"}},
          "queryStringParameters": {"event": evt.event_id}}
    body = web._flex_event_results_page(
        ev, aa, db.get_community(cid), app, aa_mem)["body"]
    # Covered: Bob (voted) + Sue (household). Pending = aa + Joe = 2, not 3.
    assert "Pending (2 households)" in body


def test_home_counts_responded_by_household(ddb_table) -> None:
    """The home dashboard shows responded/total by HOUSEHOLD, not individuals."""
    from community_organizer.core.models import FlexibleRSVP
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    db.put_flexible_rsvp(FlexibleRSVP(
        community_id=cid, app_id="a1", event_id=evt.event_id,
        user_id=bob.user_id, votes={opt.option_id: "yes"}, party_size=2))
    body = web._flexible_home(
        event={"requestContext": {"http": {"method": "GET"}}},
        user=aa, community=db.get_community(cid), app=app,
        membership=aa_mem, org_name="Book Club")["body"]
    # 4 members in 3 households (aa solo, Joe solo, Bob+Sue). Bob's household
    # answered -> 1 of 3 responded.
    assert "1/3 households responded" in body


def test_cancel_unsent_poll_deletes_it(ddb_table) -> None:
    cid, app, aa, aa_mem, *_ = _seed(ddb_table)
    evt, opt = _mk_event(cid)                         # poll, nothing sent
    resp = web._api_flex_event_cancel(
        _post("/api/flex/event/cancel", {"event": evt.event_id}),
        aa, db.get_community(cid), app, aa_mem)
    assert resp["statusCode"] == 302
    assert db.get_flexible_event("a1", evt.event_id) is None      # gone entirely
    assert list(db.list_flexible_poll_options("a1", evt.event_id)) == []


def test_cancel_sent_poll_marks_cancelled(ddb_table) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    _mk_token(cid, evt, bob.user_id, token="tb")     # a link went out
    web._api_flex_event_cancel(
        _post("/api/flex/event/cancel", {"event": evt.event_id}),
        aa, db.get_community(cid), app, aa_mem)
    fresh = db.get_flexible_event("a1", evt.event_id)
    assert fresh is not None and fresh.state == "cancelled"       # kept + cancelled
    page = web._flex_token_page(_get("/e/tb"))                    # member's link
    assert "cancelled" in page["body"].lower()


def test_save_message_updates_event(ddb_table) -> None:
    cid, app, aa, aa_mem, *_ = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    web._api_flex_event_save_message(
        _post("/api/flex/event/message",
              {"event": evt.event_id, "description": "We're reading Dune."}),
        aa, db.get_community(cid), app, aa_mem)
    assert db.get_flexible_event("a1", evt.event_id).description == "We're reading Dune."


def test_send_poll_features_the_message(ddb_table, monkeypatch) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    evt.description = "We're reading Dune this summer."
    db.put_flexible_event(evt, expected_version=evt.version)
    sent = []
    monkeypatch.setattr("community_organizer.providers.email.get_email_provider",
        lambda: type("S", (), {"send": lambda self, **kw: sent.append(kw)})())
    web._api_flex_event_send_poll(
        _post("/api/flex/event/send-poll", {"event": evt.event_id}),
        aa, db.get_community(cid), app, aa_mem)
    assert sent and "We're reading Dune this summer." in sent[0]["body_text"]


def test_close_includes_confirm_message(ddb_table, monkeypatch) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    _mk_token(cid, evt, bob.user_id, token="tb")
    web._api_flex_token_vote(_post("/api/e/vote", {
        "token": "tb", f"vote_{opt.option_id}": "yes", "party_size": "1"}))
    sent = []
    monkeypatch.setattr("community_organizer.providers.email.get_email_provider",
        lambda: type("S", (), {"send": lambda self, **kw: sent.append(kw)})())
    web._api_flex_event_close(
        _post("/api/flex/event/close",
              {"event": evt.event_id, "winning_option": opt.option_id,
               "confirm_message": "Bring your copy of Dune!",
               "sorry_message": "Sorry you can't make it."}),
        aa, db.get_community(cid), app, aa_mem)
    confirms = [kw for kw in sent if kw["kind"] == "event_confirmed"]
    assert confirms and any("Bring your copy of Dune!" in kw["body_text"]
                            for kw in confirms)


def test_close_screen_single_date_confirms_not_picks(ddb_table) -> None:
    """The decision gate stays for a single-date event, but with no pointless
    one-option 'pick the winning date' radio — the date is shown as a fact and
    confirmed directly. The winning_option is still submitted so the close
    pipeline is unchanged."""
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid, date="2026-08-15")          # single date
    ev = {"requestContext": {"http": {"method": "GET"}},
          "queryStringParameters": {"event": evt.event_id}}
    body = web._flex_event_results_page(
        ev, aa, db.get_community(cid), app, aa_mem)["body"]
    assert "Pick the winning date" not in body            # no 1-of-1 radio
    assert "Confirm this event" in body
    assert "Confirm &amp; send invites for" in body
    assert "type='radio' name='winning_option'" not in body
    # The date is still submitted, so close-review/close work unchanged.
    assert f"name='winning_option' value='{opt.option_id}'" in body


def test_close_screen_multi_date_still_picks(ddb_table) -> None:
    """Two+ dates keep the 'pick the winning date' radio."""
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    _add_option(cid, evt, "2026-08-22")
    ev = {"requestContext": {"http": {"method": "GET"}},
          "queryStringParameters": {"event": evt.event_id}}
    body = web._flex_event_results_page(
        ev, aa, db.get_community(cid), app, aa_mem)["body"]
    assert "Pick the winning date" in body
    assert "type='radio' name='winning_option'" in body


def test_declined_user_can_rejoin_after_close(ddb_table, monkeypatch) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    _mk_token(cid, evt, bob.user_id, token="tb")
    web._api_flex_token_vote(_post("/api/e/vote", {
        "token": "tb", f"vote_{opt.option_id}": "no"}))        # Bob declines (all-No)
    evt = db.get_flexible_event("a1", evt.event_id)
    evt.state, evt.winning_date, evt.winning_start_time = "scheduled", opt.iso_date, "18:30"
    db.put_flexible_event(evt, expected_version=evt.version)
    # The scheduled page offers a way back in.
    page = web._flex_token_page(_get("/e/tb"))
    assert "Changed your mind?" in page["body"] and "/api/e/join" in page["body"]
    # Joining flips them to attending and sends the calendar invite.
    sent = []
    monkeypatch.setattr("community_organizer.providers.email.get_email_provider",
        lambda: type("S", (), {"send": lambda self, **kw: sent.append(kw)})())
    web._api_flex_token_join(_post("/api/e/join", {"token": "tb"}))
    assert db.get_flexible_rsvp("a1", evt.event_id, bob.user_id).confirmed_response == "yes"
    assert sent and sent[0]["kind"] == "event_confirmed" and sent[0]["ics_content"]


def test_courtesy_note_includes_rejoin_link(ddb_table, monkeypatch) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    _mk_token(cid, evt, bob.user_id, token="tb")
    web._api_flex_token_vote(_post("/api/e/vote", {
        "token": "tb", f"vote_{opt.option_id}": "no"}))        # Bob declines
    sent = []
    monkeypatch.setattr("community_organizer.providers.email.get_email_provider",
        lambda: type("S", (), {"send": lambda self, **kw: sent.append(kw)})())
    web._api_flex_event_close(
        _post("/api/flex/event/close",
              {"event": evt.event_id, "winning_option": opt.option_id,
               "sorry_message": "So sorry!"}),
        aa, db.get_community(cid), app, aa_mem)
    missed = [kw for kw in sent if kw["kind"] == "event_missed"]
    assert missed and "/e/tb" in missed[0]["body_text"]        # rejoin link present
    assert "Yes, I can come" in missed[0]["body_text"]


def test_notify_toggle_saves(ddb_table) -> None:
    cid, app, aa, aa_mem, *_ = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    assert db.get_flexible_event("a1", evt.event_id).notify_on_response is False
    web._api_flex_event_save_notify(
        _post("/api/flex/event/notify",
              {"event": evt.event_id, "notify_on_response": "1"}),
        aa, db.get_community(cid), app, aa_mem)
    assert db.get_flexible_event("a1", evt.event_id).notify_on_response is True
    web._api_flex_event_save_notify(            # unchecked box -> field absent
        _post("/api/flex/event/notify", {"event": evt.event_id}),
        aa, db.get_community(cid), app, aa_mem)
    assert db.get_flexible_event("a1", evt.event_id).notify_on_response is False


def test_response_notifies_aa_only_when_enabled(ddb_table, monkeypatch) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    _mk_token(cid, evt, bob.user_id, token="tb")
    sent = []
    monkeypatch.setattr("community_organizer.providers.email.get_email_provider",
        lambda: type("S", (), {"send": lambda self, **kw: sent.append(kw)})())
    # Off by default: a response produces no AA notice.
    web._api_flex_token_vote(_post("/api/e/vote", {
        "token": "tb", f"vote_{opt.option_id}": "yes", "bringing": "salad"}))
    assert not [kw for kw in sent if kw["kind"] == "event_response_notice"]
    # Enable, then respond again: the AA is emailed with the details.
    evt = db.get_flexible_event("a1", evt.event_id)
    evt.notify_on_response = True
    db.put_flexible_event(evt, expected_version=evt.version)
    web._api_flex_token_vote(_post("/api/e/vote", {
        "token": "tb", f"vote_{opt.option_id}": "maybe", "bringing": "wine"}))
    notices = [kw for kw in sent if kw["kind"] == "event_response_notice"]
    assert notices and notices[0]["to_addr"] == aa.email
    assert "Bob" in notices[0]["subject"] and "wine" in notices[0]["body_text"]
    # The "see all responses" line carries a clickable results link for this event.
    assert f"/flex/event/results?event={evt.event_id}" in notices[0]["body_text"]


def test_rejoin_always_notifies_aa(ddb_table, monkeypatch) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    _mk_token(cid, evt, bob.user_id, token="tb")
    web._api_flex_token_vote(_post("/api/e/vote", {
        "token": "tb", f"vote_{opt.option_id}": "no"}))        # Bob declines
    evt = db.get_flexible_event("a1", evt.event_id)
    evt.state, evt.winning_date, evt.winning_start_time = "scheduled", opt.iso_date, "18:30"
    db.put_flexible_event(evt, expected_version=evt.version)   # notify still off
    sent = []
    monkeypatch.setattr("community_organizer.providers.email.get_email_provider",
        lambda: type("S", (), {"send": lambda self, **kw: sent.append(kw)})())
    web._api_flex_token_join(_post("/api/e/join", {"token": "tb"}))
    notices = [kw for kw in sent if kw["kind"] == "event_response_notice"]
    assert notices and notices[0]["to_addr"] == aa.email      # AA told despite toggle off
    assert "Bob" in notices[0]["subject"]


def test_results_page_has_message_editor(ddb_table) -> None:
    cid, app, aa, aa_mem, *_ = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    ev = {"requestContext": {"http": {"method": "GET"}},
          "queryStringParameters": {"event": evt.event_id}}
    body = web._flex_event_results_page(
        ev, aa, db.get_community(cid), app, aa_mem)["body"]
    assert "Message to the group" in body
    assert "/api/flex/event/message" in body


def test_cancel_scheduled_event(ddb_table) -> None:
    cid, app, aa, aa_mem, *_ = _seed(ddb_table)
    evt, opt = _mk_event(cid, state="scheduled")
    web._api_flex_event_cancel(
        _post("/api/flex/event/cancel", {"event": evt.event_id}),
        aa, db.get_community(cid), app, aa_mem)
    assert db.get_flexible_event("a1", evt.event_id).state == "cancelled"


def test_send_poll_mints_tokens_and_emails(ddb_table, monkeypatch) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, _ = _mk_event(cid)
    sent = []
    monkeypatch.setattr(
        "community_organizer.providers.email.get_email_provider",
        lambda: type("S", (), {"send": lambda self, **kw: sent.append(kw)})())
    web._api_flex_event_send_poll(
        _post("/api/flex/event/send-poll", {"event": evt.event_id}),
        aa, db.get_community(cid), app, aa_mem)
    kinds = {kw["kind"] for kw in sent}
    assert kinds == {"event_poll_invite"}
    to = {kw["to_addr"] for kw in sent}
    assert {"bob@example.com", "sue@example.com", "joe@example.com"} <= to
    # A token now exists per member and the body carries the magic link.
    for uid in (bob.user_id, sue.user_id, joe.user_id):
        assert db.get_event_token("a1", evt.event_id, uid) is not None
    assert any("/e/" in kw["body_text"] for kw in sent)
    # Re-send reuses tokens (no duplicates).
    sent.clear()
    web._api_flex_event_send_poll(
        _post("/api/flex/event/send-poll", {"event": evt.event_id}),
        aa, db.get_community(cid), app, aa_mem)
    assert len(list(db.list_event_tokens("a1", evt.event_id))) == 4   # aa + 3


def test_send_poll_skips_opted_out(ddb_table, monkeypatch) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, _ = _mk_event(cid)
    db.set_membership_opt_out("a1", bob.user_id, True)
    sent = []
    monkeypatch.setattr(
        "community_organizer.providers.email.get_email_provider",
        lambda: type("S", (), {"send": lambda self, **kw: sent.append(kw)})())
    web._api_flex_event_send_poll(
        _post("/api/flex/event/send-poll", {"event": evt.event_id}),
        aa, db.get_community(cid), app, aa_mem)
    assert "bob@example.com" not in {kw["to_addr"] for kw in sent}


def _capture(monkeypatch):
    sent = []
    monkeypatch.setattr(
        "community_organizer.providers.email.get_email_provider",
        lambda: type("S", (), {"send": lambda self, **kw: sent.append(kw)})())
    return sent


def test_send_poll_response_aware(ddb_table, monkeypatch) -> None:
    """Re-sending must be response-aware: a personal responder gets a recap of
    their answer, a household-covered member gets a 'your household already
    responded' note, and only the uncovered get a fresh invite."""
    from community_organizer.core.models import FlexibleRSVP
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    opt2 = _add_option(cid, evt, "2026-08-22")           # >1 date == poll copy
    # Bob answers for the whole 2-person household (bob + sue share h1), voting
    # on every date so he counts as a complete personal response (not "a new
    # date was added since you answered").
    db.put_flexible_rsvp(FlexibleRSVP(
        community_id=cid, app_id="a1", event_id=evt.event_id,
        user_id=bob.user_id, votes={opt.option_id: "yes", opt2.option_id: "no"},
        party_size=2, bringing="salad"))
    sent = _capture(monkeypatch)
    web._api_flex_event_send_poll(
        _post("/api/flex/event/send-poll", {"event": evt.event_id}),
        aa, db.get_community(cid), app, aa_mem)
    by_to = {kw["to_addr"]: kw for kw in sent}
    # Bob (personal responder) — recap of his answer, NOT a fresh invite.
    assert "we have your response" in by_to["bob@example.com"]["subject"].lower()
    assert "you previously responded" in by_to["bob@example.com"]["body_text"].lower()
    assert "salad" in by_to["bob@example.com"]["body_text"]
    assert "you're invited" not in by_to["bob@example.com"]["body_text"].lower()
    # Sue (household-covered) — points at Bob's response, no fresh invite.
    assert "household" in by_to["sue@example.com"]["subject"].lower()
    assert "Bob previously responded" in by_to["sue@example.com"]["body_text"]
    assert "you're invited" not in by_to["sue@example.com"]["body_text"].lower()
    # Joe (uncovered) — the normal invitation.
    assert "vote on dates" in by_to["joe@example.com"]["subject"].lower()
    assert "you're invited" in by_to["joe@example.com"]["body_text"].lower()


def test_send_poll_single_date_is_rsvp_invite(ddb_table, monkeypatch) -> None:
    """A single-date event emails a fixed-date RSVP invite ('you're invited on
    <date>, can you come?'), not a 'vote on dates' poll."""
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid, date="2026-08-15")         # single date
    sent = _capture(monkeypatch)
    web._api_flex_event_send_poll(
        _post("/api/flex/event/send-poll", {"event": evt.event_id}),
        aa, db.get_community(cid), app, aa_mem)
    inv = {kw["to_addr"]: kw for kw in sent}["joe@example.com"]
    assert "vote on dates" not in inv["subject"].lower()
    assert "you're invited" in inv["subject"].lower()
    assert "can you make it" in inv["body_text"].lower()
    assert "August 15" in inv["body_text"]               # the fixed date, in body
    assert "Proposed dates" not in inv["body_text"]      # no poll date list


def test_send_poll_declined_member_not_reinvited(ddb_table, monkeypatch) -> None:
    """A member who declined isn't asked to vote again on a re-send."""
    from community_organizer.core.models import FlexibleRSVP
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    db.put_flexible_rsvp(FlexibleRSVP(
        community_id=cid, app_id="a1", event_id=evt.event_id,
        user_id=joe.user_id, votes={opt.option_id: "no"},
        confirmed_response="no"))
    sent = _capture(monkeypatch)
    web._api_flex_event_send_poll(
        _post("/api/flex/event/send-poll", {"event": evt.event_id}),
        aa, db.get_community(cid), app, aa_mem)
    joe_body = next(kw["body_text"] for kw in sent
                    if kw["to_addr"] == "joe@example.com").lower()
    assert "won't work for you" in joe_body
    assert "changed your mind" in joe_body and "click here to respond" in joe_body
    assert "you're invited" not in joe_body


def test_send_poll_audience_not_declined_skips_decliners(ddb_table, monkeypatch) -> None:
    """audience=not_declined skips members who personally declined (individual
    only — no household logic), but reaches everyone else."""
    from community_organizer.core.models import FlexibleRSVP
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    db.put_flexible_rsvp(FlexibleRSVP(
        community_id=cid, app_id="a1", event_id=evt.event_id,
        user_id=joe.user_id, votes={opt.option_id: "no"},
        confirmed_response="no"))
    sent = _capture(monkeypatch)
    web._api_flex_event_send_poll(
        _post("/api/flex/event/send-poll",
              {"event": evt.event_id, "audience": "not_declined"}),
        aa, db.get_community(cid), app, aa_mem)
    to = {kw["to_addr"] for kw in sent}
    assert "joe@example.com" not in to
    assert {"bob@example.com", "sue@example.com"} <= to


def test_send_poll_audience_unanswered_only_uncovered(ddb_table, monkeypatch) -> None:
    """audience=unanswered reaches only members not already covered (personal
    answer or a household-mate's matching-headcount answer)."""
    from community_organizer.core.models import FlexibleRSVP
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    db.put_flexible_rsvp(FlexibleRSVP(
        community_id=cid, app_id="a1", event_id=evt.event_id,
        user_id=bob.user_id, votes={opt.option_id: "yes"}, party_size=2))
    sent = _capture(monkeypatch)
    web._api_flex_event_send_poll(
        _post("/api/flex/event/send-poll",
              {"event": evt.event_id, "audience": "unanswered"}),
        aa, db.get_community(cid), app, aa_mem)
    to = {kw["to_addr"] for kw in sent}
    assert "bob@example.com" not in to and "sue@example.com" not in to
    assert "joe@example.com" in to


def test_results_page_shows_audience_picker_after_send(ddb_table, monkeypatch) -> None:
    """Once links have gone out, the results page offers the 3-way audience
    picker rather than a single re-send button."""
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    _capture(monkeypatch)
    web._api_flex_event_send_poll(
        _post("/api/flex/event/send-poll", {"event": evt.event_id}),
        aa, db.get_community(cid), app, aa_mem)
    ev = {"requestContext": {"http": {"method": "GET"}},
          "queryStringParameters": {"event": evt.event_id}}
    body = web._flex_event_results_page(
        ev, aa, db.get_community(cid), app, aa_mem)["body"]
    assert "Send an update about this poll to" in body
    assert "Everyone except those who've declined" in body
    # aa (solo) + joe (solo) + bob&sue (h1) = 3 households, 4 individuals
    assert "haven't answered yet (3 households / 4 individuals)" in body
    # the at-a-glance names line, grouped by household
    assert "Haven't answered yet" in body
    assert "Bob &amp; Sue" in body        # household-mates shown together
    assert "Joe" in body and "Organizer" in body
    assert "Add a date" in body           # manage-dates control on an open poll


def test_add_and_remove_poll_date(ddb_table) -> None:
    """AA can add a candidate date to an open poll (and remove one), without
    disturbing existing options; duplicates are rejected."""
    cid, app, aa, aa_mem, *_ = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    assert len(list(db.list_flexible_poll_options("a1", evt.event_id))) == 1
    web._api_flex_event_add_date(
        _post("/api/flex/event/add-date",
              {"event": evt.event_id, "date": "2026-09-01", "time": "18:00"}),
        aa, db.get_community(cid), app, aa_mem)
    opts = list(db.list_flexible_poll_options("a1", evt.event_id))
    assert len(opts) == 2 and any(o.iso_date == "2026-09-01" for o in opts)
    # duplicate (same date+time) is a no-op
    web._api_flex_event_add_date(
        _post("/api/flex/event/add-date",
              {"event": evt.event_id, "date": "2026-09-01", "time": "18:00"}),
        aa, db.get_community(cid), app, aa_mem)
    assert len(list(db.list_flexible_poll_options("a1", evt.event_id))) == 2
    # remove the original date
    web._api_flex_event_remove_date(
        _post("/api/flex/event/remove-date",
              {"event": evt.event_id, "option": opt.option_id}),
        aa, db.get_community(cid), app, aa_mem)
    remaining = list(db.list_flexible_poll_options("a1", evt.event_id))
    assert [o.iso_date for o in remaining] == ["2026-09-01"]


def test_resend_nudges_responder_about_added_date(ddb_table, monkeypatch) -> None:
    """After a date is added, a re-send tells prior responders about the new
    date (not 'you're all set')."""
    from community_organizer.core.models import FlexibleRSVP, FlexiblePollOption
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    db.put_flexible_rsvp(FlexibleRSVP(
        community_id=cid, app_id="a1", event_id=evt.event_id,
        user_id=bob.user_id, votes={opt.option_id: "yes"}, party_size=1))
    # A new date is added after Bob answered.
    db.put_flexible_poll_option(FlexiblePollOption(
        community_id=cid, app_id="a1", event_id=evt.event_id,
        iso_date="2026-09-09", sort_key=1))
    sent = _capture(monkeypatch)
    web._api_flex_event_send_poll(
        _post("/api/flex/event/send-poll", {"event": evt.event_id}),
        aa, db.get_community(cid), app, aa_mem)
    bob_kw = next(kw for kw in sent if kw["to_addr"] == "bob@example.com")
    assert "new date" in bob_kw["subject"].lower()
    assert "weigh in on the new date" in bob_kw["body_text"].lower()
    assert "no need to respond again" not in bob_kw["body_text"].lower()


def test_resend_nudges_household_about_added_date(ddb_table, monkeypatch) -> None:
    """After a date is added, a household-covered spouse is also asked to weigh
    in on the new date (not just 'your household already responded')."""
    from community_organizer.core.models import FlexibleRSVP, FlexiblePollOption
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    # Bob answers for the whole 2-person household (covers sue).
    db.put_flexible_rsvp(FlexibleRSVP(
        community_id=cid, app_id="a1", event_id=evt.event_id,
        user_id=bob.user_id, votes={opt.option_id: "yes"}, party_size=2))
    db.put_flexible_poll_option(FlexiblePollOption(
        community_id=cid, app_id="a1", event_id=evt.event_id,
        iso_date="2026-09-09", sort_key=1))
    sent = _capture(monkeypatch)
    web._api_flex_event_send_poll(
        _post("/api/flex/event/send-poll", {"event": evt.event_id}),
        aa, db.get_community(cid), app, aa_mem)
    sue_kw = next(kw for kw in sent if kw["to_addr"] == "sue@example.com")
    assert "new date" in sue_kw["subject"].lower()
    assert "weigh in on the new date" in sue_kw["body_text"].lower()
    assert "no need to respond again" not in sue_kw["body_text"].lower()


# ---- public token flow ----------------------------------------------------

def test_token_page_renders_poll_form(ddb_table) -> None:
    # Two dates == a real poll: "which dates work? answer each."
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    _add_option(cid, evt, "2026-08-22")
    tok = _mk_token(cid, evt, bob.user_id)
    page = web._flex_token_page(_get(f"/e/{tok}"))
    assert page["statusCode"] == 200
    assert "Which dates work?" in page["body"]
    assert "What will you bring?" in page["body"]       # series bring_prompt
    assert "Answer for your family or for yourself" in page["body"]
    assert "Responding as <b>Bob</b>" in page["body"]   # whose link this is
    assert "for <b>each</b> date" in page["body"]        # answer-all instruction


def test_token_page_single_date_is_fixed_invite(ddb_table) -> None:
    # One date == a fixed-date invitation: "can you come?", no "dates" plural.
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)                            # single date
    tok = _mk_token(cid, evt, bob.user_id)
    page = web._flex_token_page(_get(f"/e/{tok}"))
    assert page["statusCode"] == 200
    assert "Can you come?" in page["body"]
    assert "Which dates work?" not in page["body"]
    assert "for <b>each</b> date" not in page["body"]
    assert "Let us know whether you can make it" in page["body"]
    # The RSVP controls and bring/headcount fields are still there.
    assert "What will you bring?" in page["body"]
    assert f"vote_{opt.option_id}" in page["body"]


def test_token_vote_records_rsvp(ddb_table) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    tok = _mk_token(cid, evt, bob.user_id)
    web._api_flex_token_vote(_post("/api/e/vote", {
        "token": tok, f"vote_{opt.option_id}": "yes",
        "party_size": "3", "bringing": "garden salad"}))
    r = db.get_flexible_rsvp("a1", evt.event_id, bob.user_id)
    assert r.votes[opt.option_id] == "yes"
    assert r.party_size == 3 and r.bringing == "garden salad"
    assert r.confirmed_response == "yes"


def test_token_vote_all_no_is_decline(ddb_table) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    tok = _mk_token(cid, evt, bob.user_id)
    web._api_flex_token_vote(_post("/api/e/vote", {
        "token": tok, f"vote_{opt.option_id}": "no"}))
    r = db.get_flexible_rsvp("a1", evt.event_id, bob.user_id)
    assert r.confirmed_response == "no"


def test_household_already_replied_banner(ddb_table) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    # Bob (household h1) replies first.
    bob_tok = _mk_token(cid, evt, bob.user_id, token="tok-bob")
    web._api_flex_token_vote(_post("/api/e/vote", {
        "token": bob_tok, f"vote_{opt.option_id}": "yes",
        "party_size": "2", "bringing": "wine"}))
    # Sue (same household) opens her link → sees Bob's per-date reply.
    sue_tok = _mk_token(cid, evt, sue.user_id, token="tok-sue")
    page = web._flex_token_page(_get(f"/e/{sue_tok}"))
    body = page["body"]
    assert "Already answered in your household" in body
    assert "Bob" in body
    assert "<b>Yes</b>" in body                  # Bob's per-date vote shown
    assert "bringing wine" in body
    assert "2 attending" in body
    # Joe (no household) doesn't get the section.
    joe_tok = _mk_token(cid, evt, joe.user_id, token="tok-joe")
    assert "Already answered in your household" not in web._flex_token_page(
        _get(f"/e/{joe_tok}"))["body"]


def test_token_optout_sets_membership_revokes_and_notifies_aa(
        ddb_table, monkeypatch) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    tok = _mk_token(cid, evt, bob.user_id)
    sent = []
    monkeypatch.setattr(
        "community_organizer.providers.email.get_email_provider",
        lambda: type("S", (), {"send": lambda self, **kw: sent.append(kw)})())
    web._api_flex_token_optout(_post("/api/e/optout", {"token": tok}))
    assert db.get_membership("a1", bob.user_id).opted_out is True
    assert db.get_event_token_by_value(tok).revoked is True
    notices = [kw for kw in sent if kw["kind"] == "event_optout_notice"]
    assert notices and notices[0]["to_addr"] == "aa@example.com"
    assert "Bob" in notices[0]["body_text"]


def test_invalid_token_terminal_page(ddb_table) -> None:
    _seed(ddb_table)
    page = web._flex_token_page(_get("/e/nonexistent"))
    assert page["statusCode"] == 200
    assert "no longer valid" in page["body"]


def test_revoked_token_rejected(ddb_table) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    tok = _mk_token(cid, evt, bob.user_id)
    db.revoke_event_token("a1", evt.event_id, bob.user_id)
    assert "no longer valid" in web._flex_token_page(_get(f"/e/{tok}"))["body"]


# ---- close: two cohorts + invites + sorry note ----------------------------

def test_close_invites_yes_maybe_and_sorry_to_no(ddb_table, monkeypatch) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    # Bob YES, Joe NO, Sue doesn't respond (pending → invited).
    db.put_event_token(EventToken(community_id=cid, app_id="a1",
                                  event_id=evt.event_id, user_id=bob.user_id,
                                  token="tb",
                                  expires_at="2099-01-01T00:00:00+00:00"))
    web._api_flex_token_vote(_post("/api/e/vote", {
        "token": "tb", f"vote_{opt.option_id}": "yes", "bringing": "bread"}))
    db.put_event_token(EventToken(community_id=cid, app_id="a1",
                                  event_id=evt.event_id, user_id=joe.user_id,
                                  token="tj",
                                  expires_at="2099-01-01T00:00:00+00:00"))
    web._api_flex_token_vote(_post("/api/e/vote", {
        "token": "tj", f"vote_{opt.option_id}": "no"}))

    sent = []
    monkeypatch.setattr(
        "community_organizer.providers.email.get_email_provider",
        lambda: type("S", (), {"send": lambda self, **kw: sent.append(kw)})())
    resp = web._api_flex_event_close(
        _post("/api/flex/event/close",
              {"event": evt.event_id, "winning_option": opt.option_id,
               "sorry_message": "Sorry you can't make it!"}),
        aa, db.get_community(cid), app, aa_mem)
    assert resp["statusCode"] == 302

    # Event is now scheduled; poll options are KEPT so the results page can
    # still show the vote history with the winner marked.
    e2 = db.get_flexible_event("a1", evt.event_id)
    assert e2.state == "scheduled" and e2.winning_date == "2026-08-15"
    assert len(list(db.list_flexible_poll_options("a1", evt.event_id))) == 1

    confirmed = {kw["to_addr"] for kw in sent if kw["kind"] == "event_confirmed"}
    missed = {kw["to_addr"] for kw in sent if kw["kind"] == "event_missed"}
    # Bob (yes) + Sue (pending) + organizer get the invite; Joe (no) gets sorry.
    assert "bob@example.com" in confirmed and "sue@example.com" in confirmed
    assert missed == {"joe@example.com"}
    # The invite carries an .ics with Bob's bringing.
    bob_invite = next(kw for kw in sent if kw["to_addr"] == "bob@example.com")
    assert "BEGIN:VEVENT" in (bob_invite.get("ics_content") or "")
    assert "You're bringing: bread" in (bob_invite.get("ics_content") or "")


def test_flexible_home_dashboard(ddb_table) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    _mk_event(cid)   # an open poll ("August Book Club")
    db.put_flexible_event(FlexibleEvent(
        community_id=cid, app_id="a1", title="July Dinner", state="scheduled",
        winning_date="2026-07-04", winning_start_time="18:00", location="Hall"))
    resp = web._flexible_home(event={}, user=aa, community=db.get_community(cid),
                              app=app, membership=aa_mem, org_name="Book Club")
    body = resp["body"]
    assert "Open polls" in body and "August Book Club" in body
    assert "Scheduled" in body and "July Dinner" in body
    assert "+ New event" in body                       # admin affordance
    assert "/flex/event/results?event=" in body        # manage links
    # a plain member sees the read view without the create button
    mem = db.get_membership("a1", bob.user_id)
    mbody = web._flexible_home(event={}, user=bob, community=db.get_community(cid),
                               app=app, membership=mem, org_name="Book Club")["body"]
    assert "+ New event" not in mbody and "August Book Club" in mbody


# ---- merged polls (an AA ran two polls for the same gathering) -------------

def _mk_merged_pair(cid):
    """A survivor poll and a tombstone merged into it, each with its own date
    and its own already-mailed token for Bob."""
    survivor, s_opt = _mk_event(cid, date="2026-08-15")
    dupe, d_opt = _mk_event(cid, date="2026-09-06")
    dupe.merged_into = survivor.event_id
    db.put_flexible_event(dupe)
    return survivor, s_opt, dupe, d_opt


def test_link_to_merged_poll_renders_the_survivor(ddb_table) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    survivor, s_opt, dupe, d_opt = _mk_merged_pair(cid)
    # Bob's link was mailed against the poll that later got merged away.
    _mk_token(cid, dupe, bob.user_id, token="tok-dupe")
    body = web._flex_token_page(_get("/e/tok-dupe"))["body"]
    assert "no longer valid" not in body
    # He's offered the survivor's dates, not the tombstone's frozen one.
    assert f"vote_{s_opt.option_id}" in body
    assert f"vote_{d_opt.option_id}" not in body


def test_vote_via_merged_poll_link_lands_on_survivor(ddb_table) -> None:
    """The one that would silently lose data: a vote cast through an old link
    must be written against the surviving event, not the tombstone."""
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    survivor, s_opt, dupe, d_opt = _mk_merged_pair(cid)
    _mk_token(cid, dupe, bob.user_id, token="tok-dupe")
    web._api_flex_token_vote(_post("/api/e/vote", {
        "token": "tok-dupe", f"vote_{s_opt.option_id}": "yes",
        "party_size": "2"}))
    assert db.get_flexible_rsvp("a1", dupe.event_id, bob.user_id) is None
    rsvp = db.get_flexible_rsvp("a1", survivor.event_id, bob.user_id)
    assert rsvp is not None
    assert rsvp.votes.get(s_opt.option_id) == "yes"


def test_merged_tombstone_hidden_from_event_list(ddb_table) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    survivor, s_opt, dupe, d_opt = _mk_merged_pair(cid)
    listed = [e.event_id for e in db.list_flexible_events("a1")]
    assert listed == [survivor.event_id]
    everything = {e.event_id for e in db.list_flexible_events(
        "a1", include_merged=True)}
    assert everything == {survivor.event_id, dupe.event_id}


def test_aa_results_link_to_merged_poll_redirects(ddb_table) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    survivor, s_opt, dupe, d_opt = _mk_merged_pair(cid)
    ev = {"rawPath": "/flex/event/results",
          "requestContext": {"http": {"method": "GET"}},
          "queryStringParameters": {"event": dupe.event_id}}
    resp = web._flex_event_results_page(
        ev, user=aa, community=db.get_community(cid), app=app,
        membership=aa_mem)
    assert resp["statusCode"] in (302, 303)
    assert resp["headers"]["Location"] == \
        f"/flex/event/results?event={survivor.event_id}"


def test_resolve_merged_event_survives_a_cycle(ddb_table) -> None:
    """A malformed chain must not hang the public link route."""
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    a, _ = _mk_event(cid)
    b, _ = _mk_event(cid)
    a.merged_into, b.merged_into = b.event_id, a.event_id
    db.put_flexible_event(a)
    db.put_flexible_event(b)
    assert db.resolve_merged_event("a1", a.event_id) in (a.event_id, b.event_id)


def test_optout_revokes_every_link_the_member_holds(ddb_table) -> None:
    """Opt-out is group-level: clicking it on one event's link must not leave
    another live event's link working."""
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    e1, _ = _mk_event(cid)
    e2, _ = _mk_event(cid, date="2026-09-06")
    _mk_token(cid, e1, bob.user_id, token="tok-one")
    _mk_token(cid, e2, bob.user_id, token="tok-two")
    web._api_flex_token_optout(_post("/api/e/optout", {"token": "tok-one"}))
    assert db.get_event_token("a1", e1.event_id, bob.user_id).revoked
    assert db.get_event_token("a1", e2.event_id, bob.user_id).revoked
    assert web._resolve_token("tok-two") is None


# ---- "only people I pick" audience ----------------------------------------

def _mailbox(monkeypatch):
    sent: list[dict] = []
    monkeypatch.setattr("community_organizer.providers.email.get_email_provider",
        lambda: type("S", (), {"send": lambda self, **kw: sent.append(kw)})())
    return sent


def test_pick_audience_mails_only_the_ticked_members(ddb_table, monkeypatch) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    sent = _mailbox(monkeypatch)
    web._api_flex_event_send_poll(
        _post("/api/flex/event/send-poll", {
            "event": evt.event_id, "audience": "selected",
            "user_id": bob.user_id, "subject": "Book Club: one more date",
            "note": "We added Sep 6 - could you weigh in?"}),
        aa, db.get_community(cid), app, aa_mem)
    assert [kw["to_addr"] for kw in sent] == ["bob@example.com"]
    body = sent[0]["body_text"]
    assert "We added Sep 6 - could you weigh in?" in body
    assert sent[0]["subject"] == "Book Club: one more date"
    # Her note travels with HIS link, not a shared one.
    tok = db.get_event_token("a1", evt.event_id, bob.user_id)
    assert f"/e/{tok.token}" in body
    # One email each -- no grouped send exposing addresses.
    assert all(kw.get("to_addrs") is None for kw in sent)


def test_pick_audience_skips_an_opted_out_member_even_if_ticked(
        ddb_table, monkeypatch) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    db.set_membership_opt_out("a1", bob.user_id, True)
    sent = _mailbox(monkeypatch)
    web._api_flex_event_send_poll(
        _post("/api/flex/event/send-poll", {
            "event": evt.event_id, "audience": "selected",
            "user_id": [bob.user_id, joe.user_id], "note": "hello"}),
        aa, db.get_community(cid), app, aa_mem)
    assert [kw["to_addr"] for kw in sent] == ["joe@example.com"]


def test_pick_audience_requires_a_selection_and_a_note(ddb_table, monkeypatch) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    sent = _mailbox(monkeypatch)
    nobody = web._api_flex_event_send_poll(
        _post("/api/flex/event/send-poll", {
            "event": evt.event_id, "audience": "selected", "note": "hi"}),
        aa, db.get_community(cid), app, aa_mem)
    assert "Pick%20at%20least%20one" in nobody["headers"]["Location"]
    silent = web._api_flex_event_send_poll(
        _post("/api/flex/event/send-poll", {
            "event": evt.event_id, "audience": "selected",
            "user_id": bob.user_id, "note": "   "}),
        aa, db.get_community(cid), app, aa_mem)
    assert "Write%20a%20message" in silent["headers"]["Location"]
    assert sent == []


def test_pick_audience_ignores_a_forged_non_member_id(ddb_table, monkeypatch) -> None:
    """user_id comes off the wire; it must be checked against the roster."""
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    outsider = User(community_id=cid, email="nope@example.com", name="Outsider")
    db.put_user(outsider)
    sent = _mailbox(monkeypatch)
    web._api_flex_event_send_poll(
        _post("/api/flex/event/send-poll", {
            "event": evt.event_id, "audience": "selected",
            "user_id": [bob.user_id, outsider.user_id], "note": "hello"}),
        aa, db.get_community(cid), app, aa_mem)
    assert [kw["to_addr"] for kw in sent] == ["bob@example.com"]


def test_results_page_offers_the_picker_with_nobody_ticked(ddb_table) -> None:
    cid, app, aa, aa_mem, bob, sue, joe = _seed(ddb_table)
    evt, opt = _mk_event(cid)
    _mk_token(cid, evt, bob.user_id)          # a re-send: options appear
    ev = {"rawPath": "/flex/event/results",
          "requestContext": {"http": {"method": "GET"}},
          "queryStringParameters": {"event": evt.event_id}}
    body = web._flex_event_results_page(
        ev, user=aa, community=db.get_community(cid), app=app,
        membership=aa_mem)["body"]
    assert "Only people I pick" in body
    assert f"name='user_id' value='{bob.user_id}'" in body
    # Nobody pre-armed.
    assert "checked>" not in body.split("pick-panel")[1]
