"""Send-Email rework: audience x schedule-copy composition.

Covers the precedence ladder (full > union-of-slices > own-slice), per-cohort
fan-out, archived-month selectability, all-active concatenation, no double
sends, and the Draft/Active/History terminology helpers.
"""
from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from community_organizer.core import db
from community_organizer.core.models import (
    Application, Assignment, Cohort, CohortMembership, Community, EmailLog,
    Membership, Schedule, Slot, User,
)
from community_organizer.lambdas import web


@dataclass
class FakeProvider:
    name: str = "fake"
    sent: list[dict[str, Any]] = field(default_factory=list)

    def send(self, **kwargs: Any) -> EmailLog:
        self.sent.append(kwargs)
        return EmailLog(
            community_id=kwargs.get("community_id", ""), direction="outbound",
            from_addr=kwargs.get("from_addr", ""), to_addr=kwargs.get("to_addr", ""),
            subject=kwargs.get("subject", ""), provider=self.name,
            kind=kwargs.get("kind", "other"), outcome="accepted")


def _slot(cid, app, month, tid, name, date, uid) -> None:
    s = Slot(community_id=cid, app_id=app.app_id, yyyy_mm=month, template_id=tid,
             name=name, day_of_week=6, start_time="08:00",
             arrival_offset_minutes=10, duration_minutes=60,
             required_volunteers=1, min_volunteers=1,
             concrete_date=date, local_date=date)
    db.put_slot(s)
    if uid:
        db.put_assignment(Assignment(
            community_id=cid, app_id=app.app_id, yyyy_mm=month,
            slot_id=s.slot_id, user_id=uid, local_date=date))


def _seed(ddb_table):
    cid = "c1"
    db.put_community(Community(community_id=cid, name="Parish"))
    app = Application(community_id=cid, name="Ushers", app_type="coverage",
                      app_id="ush", default_timezone="America/New_York")
    db.put_application(app)
    # Users: an AA, two cohort members (Alice/Bob), one plain member (Dave).
    aa = User(community_id=cid, email="aa@example.com", name="Boss")
    alice = User(community_id=cid, email="alice@example.com", name="Alice")
    bob = User(community_id=cid, email="bob@example.com", name="Bob")
    dave = User(community_id=cid, email="dave@example.com", name="Dave")
    for u in (aa, alice, bob, dave):
        db.put_user(u)
    db.put_membership(Membership(community_id=cid, app_id="ush",
                                 user_id=aa.user_id, app_role="aa"))
    for u in (alice, bob, dave):
        db.put_membership(Membership(community_id=cid, app_id="ush", user_id=u.user_id))
    # Two cohorts, each linked to a distinct template.
    ca = Cohort(community_id=cid, app_id="ush", name="A-team", linked_template_id="t1")
    cb = Cohort(community_id=cid, app_id="ush", name="B-team", linked_template_id="t2")
    db.put_cohort(ca)
    db.put_cohort(cb)
    db.put_cohort_membership(CohortMembership(cohort_id=ca.cohort_id, user_id=alice.user_id))
    db.put_cohort_membership(CohortMembership(cohort_id=cb.cohort_id, user_id=bob.user_id))
    # Active (published) schedule: t1 slot -> Alice, t2 slot -> Bob.
    db.put_schedule(Schedule(community_id=cid, app_id="ush", yyyy_mm="2030-06",
                             state="published"))
    _slot(cid, app, "2030-06", "t1", "Alpha-shift", "2030-06-07", alice.user_id)
    _slot(cid, app, "2030-06", "t2", "Bravo-shift", "2030-06-14", bob.user_id)
    return cid, app, aa, alice, bob, dave, ca, cb


def _post(fields: dict) -> dict:
    pairs = []
    for k, v in fields.items():
        for item in (v if isinstance(v, (list, tuple)) else [v]):
            pairs.append((k, item))
    return {"rawPath": "/api/admin/send-email",
            "requestContext": {"http": {"method": "POST"}},
            "body": urllib.parse.urlencode(pairs), "isBase64Encoded": False}


def _run(monkeypatch, event, aa, cid, app):
    fake = FakeProvider()
    monkeypatch.setattr("community_organizer.providers.email.get_email_provider", lambda: fake)
    web._api_send_email(event, aa, db.get_community(cid), app,
                        db.get_membership("ush", aa.user_id))
    return fake


def _emails_to(fake, addr):
    """Every send where addr is a recipient — matches personalized (to_addr)
    and group (to_addrs) sends alike."""
    return [k for k in fake.sent
            if addr == k.get("to_addr") or addr in (k.get("to_addrs") or [])]


def _to(fake, addr):
    got = _emails_to(fake, addr)
    return got[0] if got else None


# ---- per-cohort slice -----------------------------------------------------

def test_cohort_slice_is_one_group_email_with_only_that_cohorts_rows(
        ddb_table, monkeypatch):
    cid, app, aa, alice, bob, dave, ca, cb = _seed(ddb_table)
    fake = _run(monkeypatch, _post({
        "mode": "select", "subject": "Hi", "body": "your slots",
        "cohort": ca.cohort_id,
        f"cohort_sched_{ca.cohort_id}": "1",
        f"cohort_month_{ca.cohort_id}": "2030-06"}), aa, cid, app)
    # ONE group email (reply-all), not one-per-member.
    assert len(fake.sent) == 1
    m = fake.sent[0]
    assert "alice@example.com" in m["to_addrs"]           # cohort member
    assert "aa@example.com" in m["to_addrs"]              # sender rides along
    assert "bob@example.com" not in m["to_addrs"]         # other cohort not included
    assert "Alice" in m["body_html"] and "Bob" not in m["body_html"]


def test_per_cohort_fanout_group_email_each(ddb_table, monkeypatch):
    cid, app, aa, alice, bob, dave, ca, cb = _seed(ddb_table)
    fake = _run(monkeypatch, _post({
        "mode": "select", "subject": "Hi", "body": "b",
        "cohort": [ca.cohort_id, cb.cohort_id],
        f"cohort_sched_{ca.cohort_id}": "1", f"cohort_month_{ca.cohort_id}": "2030-06",
        f"cohort_sched_{cb.cohort_id}": "1", f"cohort_month_{cb.cohort_id}": "2030-06"}),
        aa, cid, app)
    assert len(fake.sent) == 2                       # one group per cohort
    a, b = _to(fake, "alice@example.com"), _to(fake, "bob@example.com")
    assert "Alice" in a["body_html"] and "Bob" not in a["body_html"]
    assert "Bob" in b["body_html"] and "Alice" not in b["body_html"]
    # Reply-all scope: Bob isn't on Alice's cohort email and vice versa.
    assert "bob@example.com" not in a["to_addrs"]
    assert "alice@example.com" not in b["to_addrs"]


def test_full_include_supersedes_slice(ddb_table, monkeypatch):
    cid, app, aa, alice, bob, dave, ca, cb = _seed(ddb_table)
    fake = _run(monkeypatch, _post({
        "mode": "select", "subject": "Hi", "body": "b",
        "cohort": ca.cohort_id,
        f"cohort_sched_{ca.cohort_id}": "1", f"cohort_month_{ca.cohort_id}": "2030-06",
        "sel_include_full": "1", "sel_full_month": "2030-06"}), aa, cid, app)
    a = _to(fake, "alice@example.com")
    assert "Alice" in a["body_html"] and "Bob" in a["body_html"]   # full month


def test_individual_and_raw_are_cced_on_each_cohort_email(ddb_table, monkeypatch):
    cid, app, aa, alice, bob, dave, ca, cb = _seed(ddb_table)
    fake = _run(monkeypatch, _post({
        "mode": "select", "subject": "Hi", "body": "b",
        "cohort": [ca.cohort_id, cb.cohort_id],
        f"cohort_sched_{ca.cohort_id}": "1", f"cohort_month_{ca.cohort_id}": "2030-06",
        f"cohort_sched_{cb.cohort_id}": "1", f"cohort_month_{cb.cohort_id}": "2030-06",
        "user_id": dave.user_id, "extra_emails": "guest@example.com"}), aa, cid, app)
    # Add-ins are CC'd on BOTH cohort emails, so across them they see the
    # union of slices (Alice from A, Bob from B).
    for addr in ("dave@example.com", "guest@example.com"):
        ems = _emails_to(fake, addr)
        assert len(ems) == 2, addr                       # cc'd on each cohort
        blob = " ".join(e["body_html"] for e in ems)
        assert "Alice" in blob and "Bob" in blob, addr
    # Cohort-only members still see only their own slice.
    assert "Bob" not in _to(fake, "alice@example.com")["body_html"]
    assert "Alice" not in _to(fake, "bob@example.com")["body_html"]


def test_recipient_deduped_within_a_cohort_email(ddb_table, monkeypatch):
    cid, app, aa, alice, bob, dave, ca, cb = _seed(ddb_table)
    # Alice is a member of cohort A AND individually added — she must appear
    # at most once in any single email's recipient list.
    fake = _run(monkeypatch, _post({
        "mode": "select", "subject": "Hi", "body": "b",
        "cohort": [ca.cohort_id, cb.cohort_id],
        f"cohort_sched_{ca.cohort_id}": "1", f"cohort_month_{ca.cohort_id}": "2030-06",
        f"cohort_sched_{cb.cohort_id}": "1", f"cohort_month_{cb.cohort_id}": "2030-06",
        "user_id": alice.user_id}), aa, cid, app)
    for m in fake.sent:
        addrs = m.get("to_addrs") or [m.get("to_addr")]
        assert addrs.count("alice@example.com") <= 1           # no duplicate on one email


# ---- full include to all members + archived + all-active ------------------

def test_all_members_with_archived_month(ddb_table, monkeypatch):
    cid, app, aa, alice, bob, dave, ca, cb = _seed(ddb_table)
    # Archive the month; it must still be selectable for a copy send.
    from community_organizer.core import publishing
    publishing.archive_schedule(app, "2030-06", archived_at="2030-06-30T00:00:00+00:00")
    fake = _run(monkeypatch, _post({
        "mode": "all", "subject": "Hi", "body": "b",
        "all_include_schedule": "1", "all_copy_month": "2030-06"}), aa, cid, app)
    a = _to(fake, "alice@example.com")
    assert a and "Alice" in a["body_html"] and "Bob" in a["body_html"]


def test_all_active_months_concatenates(ddb_table, monkeypatch):
    cid, app, aa, alice, bob, dave, ca, cb = _seed(ddb_table)
    # A second active month with its own slot.
    db.put_schedule(Schedule(community_id=cid, app_id="ush", yyyy_mm="2030-07",
                             state="published"))
    _slot(cid, app, "2030-07", "t1", "July-shift", "2030-07-05", dave.user_id)
    fake = _run(monkeypatch, _post({
        "mode": "all", "subject": "Hi", "body": "b",
        "all_include_schedule": "1", "all_copy_month": web._ALL_ACTIVE_MONTHS}),
        aa, cid, app)
    a = _to(fake, "alice@example.com")
    assert a and "JUNE 2030" in a["body_html"].upper()
    assert "JULY 2030" in a["body_html"].upper()


def test_plain_email_no_schedule_still_works(ddb_table, monkeypatch):
    cid, app, aa, alice, bob, dave, ca, cb = _seed(ddb_table)
    fake = _run(monkeypatch, _post({
        "mode": "all", "subject": "Notice", "body": "no schedule here"}),
        aa, cid, app)
    assert fake.sent and all("Alpha-shift" not in (k.get("body_html") or "")
                             for k in fake.sent)


# ---- terminology ----------------------------------------------------------

def test_state_labels_active_history(ddb_table):
    assert web._state_label("published") == "Active"
    assert web._state_label("archived") == "History"
    assert web._state_label("draft") == "Draft"


def test_send_email_page_renders_new_controls(ddb_table):
    cid, app, aa, alice, bob, dave, ca, cb = _seed(ddb_table)
    ev = {"requestContext": {"http": {"method": "GET"}}}
    r = web._send_email_page(ev, aa, db.get_community(cid), app,
                             db.get_membership("ush", aa.user_id))
    assert r["statusCode"] == 200
    body = r["body"]
    assert "Include a copy of the schedule" in body      # all-members modifier
    assert "Include the full schedule" in body           # select modifier
    assert "Send this cohort their schedule" in body      # per-cohort slice
    assert "All active months" in body                    # full-picker option
    assert "A-team" in body and "B-team" in body          # cohort rows
    assert "all_send_copy" not in body                    # old radio gone
    assert "haven't responded" not in body   # poll-only quick-pick, not coverage


def test_flexible_haven_responded_quickpick(ddb_table):
    """flexible_event app with an open poll shows a '+ haven't responded'
    quick-pick keyed on FlexibleRSVP (NOT sign-in), alongside 'never logged
    in'. A member who answered via the login-free link is excluded."""
    from community_organizer.core.models import FlexibleEvent, FlexibleRSVP
    cid = "cflex"
    db.put_community(Community(community_id=cid, name="Parish"))
    app = Application(community_id=cid, name="Summer Book Club",
                      app_type="flexible_event", app_id="bcx")
    db.put_application(app)
    aa = User(community_id=cid, email="aa@example.com", name="Organizer")
    m1 = User(community_id=cid, email="m1@example.com", name="Ann Responded")
    m2 = User(community_id=cid, email="m2@example.com", name="Zed Pending")
    for u in (aa, m1, m2):
        db.put_user(u)
        db.put_membership(Membership(
            community_id=cid, app_id="bcx", user_id=u.user_id,
            app_role="aa" if u is aa else "member"))
    evt = FlexibleEvent(community_id=cid, app_id="bcx", title="August Meet",
                        state="poll")
    db.put_flexible_event(evt)
    # m1 responded via magic link (has an RSVP); aa and m2 did not.
    db.put_flexible_rsvp(FlexibleRSVP(
        community_id=cid, app_id="bcx", event_id=evt.event_id,
        user_id=m1.user_id, votes={"opt": "yes"}))

    ev = {"requestContext": {"http": {"method": "GET"}}}
    r = web._send_email_page(ev, aa, db.get_community(cid), app,
                             db.get_membership("bcx", aa.user_id))
    assert r["statusCode"] == 200
    body = r["body"]
    # Both quick-picks present; "never logged in" is kept.
    assert "never logged in (" in body
    # Non-responders among the 3 members = aa + m2 = 2 (m1 responded → excluded).
    assert "haven't responded (2)" in body


def _hh_seed(ddb_table, *, responder_party_size, hh_members):
    """Seed a flexible poll: a solo AA, a solo member 'C', and a household
    'h1' of `hh_members` members where the first (A) responds with
    `responder_party_size`. Returns the rendered page body."""
    from community_organizer.core.models import FlexibleEvent, FlexibleRSVP
    cid = "chh"
    db.put_community(Community(community_id=cid, name="Parish"))
    app = Application(community_id=cid, name="BC", app_type="flexible_event",
                      app_id="hha")
    db.put_application(app)
    aa = User(community_id=cid, email="aa@example.com", name="Org")
    c = User(community_id=cid, email="c@example.com", name="C Solo")
    hh = [User(community_id=cid, email=f"h{i}@example.com", name=f"H{i}",
               household_id="h1") for i in range(hh_members)]
    for u in [aa, c, *hh]:
        db.put_user(u)
        db.put_membership(Membership(community_id=cid, app_id="hha",
                                     user_id=u.user_id,
                                     app_role="aa" if u is aa else "member"))
    evt = FlexibleEvent(community_id=cid, app_id="hha", title="M", state="poll")
    db.put_flexible_event(evt)
    db.put_flexible_rsvp(FlexibleRSVP(
        community_id=cid, app_id="hha", event_id=evt.event_id,
        user_id=hh[0].user_id, votes={"o": "yes"},
        party_size=responder_party_size))
    ev = {"requestContext": {"http": {"method": "GET"}}}
    r = web._send_email_page(ev, aa, db.get_community(cid), app,
                             db.get_membership("hha", aa.user_id))
    assert r["statusCode"] == 200
    return r["body"]


def test_haven_responded_household_covered(ddb_table):
    """Household of 2, responder says party_size=2 (== household size): the
    other spouse is treated as covered and left out."""
    body = _hh_seed(ddb_table, responder_party_size=2, hh_members=2)
    # members = aa, C, H0(responded), H1. H1 covered by H0's full headcount.
    # Non-responders = aa + C = 2.
    assert "haven't responded (2)" in body


def test_haven_responded_household_mismatch_reverts(ddb_table):
    """Household of 3, responder says party_size=2 (!= 3): can't tell who's
    covered, so the household's non-responders stay in the list."""
    body = _hh_seed(ddb_table, responder_party_size=2, hh_members=3)
    # members = aa, C, H0(responded), H1, H2. 2 != 3 → household not covered.
    # Non-responders = aa + C + H1 + H2 = 4.
    assert "haven't responded (4)" in body


def test_schedule_action_verbs(ddb_table):
    from community_organizer.core.models import Schedule
    draft = Schedule(community_id="c1", app_id="a1", yyyy_mm="2030-06", state="draft")
    active = Schedule(community_id="c1", app_id="a1", yyyy_mm="2030-06", state="published")
    assert "Make active" in web._schedule_action(draft)
    assert "Return to draft" in web._schedule_action(active)
    assert "Archive" in web._schedule_action(active)


def test_opted_out_member_is_never_a_recipient(ddb_table, monkeypatch):
    """Opt-out is group-level and app-wide. The poll sender has always honoured
    it; this page used to mail opted-out members anyway."""
    cid, app, aa, alice, bob, dave, ca, cb = _seed(ddb_table)
    db.set_membership_opt_out("ush", dave.user_id, True)

    # "Everyone" must skip him.
    fake = _run(monkeypatch, _post({"subject": "Hi", "body": "All", "mode": "all"}),
                aa, cid, app)
    assert _emails_to(fake, "dave@example.com") == []
    assert _emails_to(fake, "alice@example.com")

    # And so must an explicit pick -- an AA ticking him doesn't override his
    # opt-out.
    fake = _run(monkeypatch, _post({"subject": "Hi", "body": "Note",
                                    "mode": "select",
                                    "user_id": [dave.user_id, alice.user_id]}),
                aa, cid, app)
    assert _emails_to(fake, "dave@example.com") == []
    assert _emails_to(fake, "alice@example.com")
