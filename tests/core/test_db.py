"""Tests for ``community_organizer.core.db`` — the single-table accessor.

This file pins three categories of behavior:

1. **CRUD round-trip**: data written via ``put_*`` comes back unchanged
   via ``get_*``. Validates key construction + Decimal/list/dict
   coercion + that no extra DDB attributes leak into the dataclass.

2. **Listing by prefix**: ``list_users``, ``list_applications``, etc.
   each use a different SK prefix on the same PK, so a community with
   users + apps + email logs gives clean separation per call.

3. **Optimistic concurrency**:
       - put_user / put_application with stale version raise
         ConcurrencyConflict
       - transition_schedule_state requires from_state to match
       - The condition uses ``attribute_not_exists OR version =`` so
         legacy records without a version field are still writable
         the first time.

Also covered (briefly): GSI1 lookups for cognito_sub, user memberships,
user assignments, and pending notifications.
"""
from __future__ import annotations

import datetime as dt
import math
from decimal import Decimal

import pytest

from community_organizer.core import db
from community_organizer.core.models import (
    Application, Assignment, Community, EventToken, Membership, Notification,
    Schedule, Slot, User,
)


# ---- _coerce: numeric edge cases -----------------------------------------

@pytest.mark.parametrize("decimal_value, expected", [
    (Decimal("0"), 0),
    (Decimal("42"), 42),
    (Decimal("-7"), -7),
    (Decimal("3.5"), 3.5),
    (Decimal("0.0001"), 0.0001),
])
def test_coerce_finite_decimals(decimal_value, expected) -> None:
    """Whole-valued Decimals → int; fractional → float (DDB stores as Decimal)."""
    result = db._coerce(decimal_value)
    assert result == expected
    assert type(result) is type(expected)


def test_coerce_nan_decimal_returns_nan_float_not_crash(ddb_table) -> None:
    """``Decimal('NaN') % 1`` raises ``decimal.InvalidOperation``. A
    malicious or corrupted DDB item with a NaN numeric field would
    otherwise crash whatever handler reads it (security fix D11).
    Guard converts to a NaN float instead — JSON-safe and detectable
    via ``math.isnan``."""
    result = db._coerce(Decimal("NaN"))
    assert isinstance(result, float)
    assert math.isnan(result)


def test_coerce_infinity_decimal_returns_inf_float(ddb_table) -> None:
    """Same guard for ±Infinity."""
    pos = db._coerce(Decimal("Infinity"))
    neg = db._coerce(Decimal("-Infinity"))
    assert math.isinf(pos) and pos > 0
    assert math.isinf(neg) and neg < 0


def test_coerce_recursive_into_lists_and_dicts() -> None:
    """The NaN guard kicks in at any nesting depth."""
    nested = {"a": [Decimal("3"), Decimal("NaN")], "b": {"c": Decimal("Infinity")}}
    out = db._coerce(nested)
    assert out["a"][0] == 3
    assert math.isnan(out["a"][1])
    assert math.isinf(out["b"]["c"])


# ---- D7: pagination across LastEvaluatedKey ------------------------------

def test_paginate_query_follows_last_evaluated_key(ddb_table, monkeypatch) -> None:
    """Pin the contract: ``_paginate_query`` keeps calling
    ``table.query`` with ``ExclusiveStartKey`` until DDB stops
    returning ``LastEvaluatedKey`` (security fix D7).

    Real DDB needs >1 MB of data to return multiple pages, which is
    expensive to seed. Instead we stub ``table.query`` to return a
    fake multi-page response and assert the helper visits every
    page.
    """
    pages = [
        {"Items": [{"PK": "A", "SK": "1"}, {"PK": "A", "SK": "2"}],
         "LastEvaluatedKey": {"PK": "A", "SK": "2"}},
        {"Items": [{"PK": "A", "SK": "3"}],
         "LastEvaluatedKey": {"PK": "A", "SK": "3"}},
        {"Items": [{"PK": "A", "SK": "4"}, {"PK": "A", "SK": "5"}]},
        # If the helper kept going after a page with no LEK we'd see
        # this page too — and the test would fail with extra items.
        {"Items": [{"PK": "A", "SK": "SHOULD_NOT_REACH"}]},
    ]
    calls = []

    class FakeTable:
        def query(self, **kwargs):
            calls.append(kwargs)
            return pages[len(calls) - 1]

    monkeypatch.setattr(db, "_table", lambda: FakeTable())
    items = list(db._paginate_query(KeyConditionExpression="dummy"))
    assert [it["SK"] for it in items] == ["1", "2", "3", "4", "5"]
    # Three calls, not four — stops on the page with no LEK.
    assert len(calls) == 3
    # Second and third calls have ExclusiveStartKey injected from the
    # previous LEK; first call does not.
    assert "ExclusiveStartKey" not in calls[0]
    assert calls[1]["ExclusiveStartKey"] == {"PK": "A", "SK": "2"}
    assert calls[2]["ExclusiveStartKey"] == {"PK": "A", "SK": "3"}


def test_list_users_visits_every_page(ddb_table, monkeypatch) -> None:
    """End-to-end: a list_* helper backed by ``_paginate_query`` must
    return every user across multiple DDB pages."""
    # Seed 5 users in the real moto table.
    for i in range(5):
        db.put_user(User(community_id="c1", email=f"u{i}@example.com", name=f"U{i}"))

    # Force pagination by patching the moto table's query to chunk
    # responses into 2 items per page.
    real_table = db._table()
    real_query = real_table.query

    def chunked_query(**kwargs):
        kwargs.pop("ExclusiveStartKey", None)
        full = real_query(**kwargs)
        items = full.get("Items", [])
        # The "ExclusiveStartKey" param decides where to resume.
        # Since moto returns all items at once, simulate paging by
        # slicing based on which sub-call we're on.
        return full

    # Easier: just seed enough users that moto naturally paginates.
    # moto-DDB respects "Limit" so use it to force pagination.
    # Use a counter to track how many calls happen.
    call_count = {"n": 0}

    def counting_query(**kwargs):
        call_count["n"] += 1
        kwargs["Limit"] = 2   # force ≤2 items per call
        return real_query(**kwargs)

    monkeypatch.setattr(real_table, "query", counting_query)
    monkeypatch.setattr(db, "_table", lambda: real_table)
    users = list(db.list_users("c1"))
    assert len(users) == 5             # nothing dropped
    assert call_count["n"] >= 3         # at least 3 pages × 2 items


# ---- D12: atomic slot signup capacity --------------------------------------

def _seed_slot(ddb_table, *, max_vol: int | None = 2,
               required: int = 2) -> Slot:
    """Insert one Slot and return it. Counter not initialized
    (lazy). Pass max_vol=None for an uncapped slot."""
    slot = Slot(
        community_id="c1", app_id="a1", yyyy_mm="2030-06",
        template_id="t1", name="Sun 8 AM", day_of_week=6,
        start_time="08:00", arrival_offset_minutes=0,
        duration_minutes=60, required_volunteers=required,
        min_volunteers=1, max_volunteers=max_vol,
        concrete_date="2030-06-02", local_date="2030-06-02",
    )
    db.put_slot(slot)
    return slot


def test_atomic_signup_under_capacity_succeeds(ddb_table) -> None:
    """First signup against a fresh slot works AND lazy-inits the
    counter to 1."""
    slot = _seed_slot(ddb_table, max_vol=2)
    asg = db.atomic_signup_assignment(
        slot, user_id="u1", community_id="c1")
    assert asg.user_id == "u1"
    # Verify the counter is now 1.
    fresh = next(db.list_slots(slot.app_id, slot.yyyy_mm))
    assert fresh.assignment_count == 1
    # Verify the assignment row exists.
    assigns = list(db.list_assignments_for_slot(
        slot.app_id, slot.yyyy_mm, slot.slot_id))
    assert len(assigns) == 1


def test_atomic_signup_at_capacity_raises(ddb_table) -> None:
    """Once the counter hits max_volunteers the next signup must
    raise CapacityExceeded, and neither the counter nor any
    assignment row should change (security fix D12)."""
    slot = _seed_slot(ddb_table, max_vol=2)
    db.atomic_signup_assignment(slot, user_id="u1", community_id="c1")
    db.atomic_signup_assignment(slot, user_id="u2", community_id="c1")

    with pytest.raises(db.CapacityExceeded):
        db.atomic_signup_assignment(slot, user_id="u3", community_id="c1")

    fresh = next(db.list_slots(slot.app_id, slot.yyyy_mm))
    assert fresh.assignment_count == 2     # NOT bumped past max
    assigns = list(db.list_assignments_for_slot(
        slot.app_id, slot.yyyy_mm, slot.slot_id))
    assert len(assigns) == 2               # u3 NOT written


def test_atomic_signup_with_uncapped_slot_never_raises(ddb_table) -> None:
    """max_volunteers=None means "no cap" — admins of adoration-style
    apps opt into this so any number of people can sign up. Pin that
    a long string of signups never trips CapacityExceeded."""
    slot = _seed_slot(ddb_table, max_vol=None, required=1)
    for i in range(25):
        db.atomic_signup_assignment(
            slot, user_id=f"u{i}", community_id="c1")
    fresh = next(db.list_slots(slot.app_id, slot.yyyy_mm))
    assert fresh.assignment_count == 25
    assigns = list(db.list_assignments_for_slot(
        slot.app_id, slot.yyyy_mm, slot.slot_id))
    assert len(assigns) == 25


def test_delete_assignment_decrements_counter(ddb_table) -> None:
    """Releasing a slot must decrement the counter so future
    signups see capacity again."""
    slot = _seed_slot(ddb_table, max_vol=2)
    db.atomic_signup_assignment(slot, user_id="u1", community_id="c1")
    db.atomic_signup_assignment(slot, user_id="u2", community_id="c1")

    db.delete_assignment(slot.app_id, slot.yyyy_mm, slot.slot_id, "u1")

    fresh = next(db.list_slots(slot.app_id, slot.yyyy_mm))
    assert fresh.assignment_count == 1
    # New signup should now succeed where it previously raised.
    db.atomic_signup_assignment(slot, user_id="u3", community_id="c1")


# ---- D13: notifier idempotency claim ---------------------------------------

def _seed_notification(ddb_table, *, state: str = "pending") -> Notification:
    ntf = Notification(
        community_id="c1", app_id="a1", user_id="u1",
        slot_id="s1", yyyy_mm="2030-06",
        send_at="2030-06-01T00:00:00+00:00",
        lead_minutes=60, state=state,
    )
    db.put_notification(ntf)
    return ntf


def test_hydrate_drops_unknown_fields(ddb_table) -> None:
    """A DDB item carrying a field the dataclass no longer has must
    NOT crash hydration. This lets a release that removes a field
    survive contact with un-migrated rows (security fix D21)."""
    db.put_user(User(community_id="c1", email="u@example.com", name="U"))
    raw = db._strip_keys({
        "community_id": "c1", "email": "u@example.com", "name": "U",
        "user_id": "u1",
        "phantom_legacy_field": "removed-in-newer-release",
    })
    # The pre-fix call (User(**raw)) would have raised TypeError here.
    got = db._hydrate(User, raw)
    assert got.email == "u@example.com"
    assert not hasattr(got, "phantom_legacy_field")


def test_claim_notification_succeeds_when_pending(ddb_table) -> None:
    """First claim against a pending row wins; state moves to
    in_flight; GSI partition moves with it."""
    ntf = _seed_notification(ddb_table, state="pending")
    won = db.claim_notification(ntf.notification_id, ntf.app_id, ntf.send_at)
    assert won is True
    # Row is no longer in the STATE#pending partition.
    pending_after = list(db.list_pending_notifications(
        up_to="2099-01-01T00:00:00+00:00"))
    assert ntf.notification_id not in {n.notification_id for n in pending_after}


def test_claim_notification_loses_when_already_claimed(ddb_table) -> None:
    """Second claim fails — at most one invocation wins, so the
    reminder is sent at most once (security fix D13)."""
    ntf = _seed_notification(ddb_table, state="pending")
    assert db.claim_notification(
        ntf.notification_id, ntf.app_id, ntf.send_at) is True
    assert db.claim_notification(
        ntf.notification_id, ntf.app_id, ntf.send_at) is False


def test_claim_notification_loses_when_already_sent(ddb_table) -> None:
    """If a notification has already been marked ``sent``, the claim
    fails — we never re-send."""
    ntf = _seed_notification(ddb_table, state="sent")
    assert db.claim_notification(
        ntf.notification_id, ntf.app_id, ntf.send_at) is False


def test_delete_notifications_for_schedule_targets_only_match(ddb_table) -> None:
    """Pre-fix this function listed ALL ``NTF#`` rows for the app
    and filtered ``yyyy_mm`` in Python. Now it uses a DDB
    ``FilterExpression``, so we verify the externally-observable
    contract: only the matching-month rows are deleted, the others
    stay intact (D8 mitigation)."""
    # Seed notifications across three months for the same app.
    for ym in ("2030-05", "2030-06", "2030-07"):
        for i in range(3):
            db.put_notification(Notification(
                community_id="c1", app_id="a1", user_id=f"u{i}",
                slot_id=f"s{i}", yyyy_mm=ym,
                send_at=f"{ym}-01T00:00:00+00:00",
                lead_minutes=60,
            ))

    deleted = db.delete_notifications_for_schedule("a1", "2030-06")
    assert deleted == 3

    # Other months untouched. We can confirm by hitting the pending
    # GSI: all six remaining rows are still STATE#pending.
    remaining = list(db.list_pending_notifications(
        up_to="2099-01-01T00:00:00+00:00"))
    yyyy_mms = sorted(n.yyyy_mm for n in remaining)
    assert yyyy_mms == ["2030-05", "2030-05", "2030-05",
                        "2030-07", "2030-07", "2030-07"]


def test_claim_notification_loses_when_cancelled(ddb_table) -> None:
    """Cancelled rows can't be claimed either — defense in depth
    against a cancelled-then-resurrected race."""
    ntf = _seed_notification(ddb_table, state="cancelled")
    assert db.claim_notification(
        ntf.notification_id, ntf.app_id, ntf.send_at) is False


def test_atomic_signup_idempotent_on_duplicate(ddb_table) -> None:
    """Signing up the same user twice (e.g. double-click) must NOT
    double-increment — the Put leg's
    attribute_not_exists(SK) condition catches the duplicate and
    cancels the whole transaction; counter stays put."""
    slot = _seed_slot(ddb_table, max_vol=5)
    db.atomic_signup_assignment(slot, user_id="u1", community_id="c1")

    with pytest.raises(Exception):
        db.atomic_signup_assignment(slot, user_id="u1", community_id="c1")

    fresh = next(db.list_slots(slot.app_id, slot.yyyy_mm))
    assert fresh.assignment_count == 1     # not 2


# ---- CRUD round-trip ------------------------------------------------------

def test_community_round_trip(ddb_table) -> None:
    db.put_community(Community(community_id="c1", name="Test Parish"))
    got = db.get_community("c1")
    assert got is not None
    assert got.name == "Test Parish"
    assert got.community_id == "c1"


def test_user_round_trip_with_quiet_hours_tuple(ddb_table) -> None:
    """quiet_hours is a tuple in the model but DDB stores it as a list.
    The accessor must coerce it back to a tuple on read."""
    u = User(community_id="c1", email="a@example.com", name="Alice",
             quiet_hours=("22:00", "07:00"))
    db.put_user(u)
    got = db.get_user("c1", u.user_id)
    assert got is not None
    assert got.quiet_hours == ("22:00", "07:00")
    assert isinstance(got.quiet_hours, tuple)


def test_application_round_trip(ddb_table) -> None:
    a = Application(community_id="c1", name="Test Ushers",
                    event_noun="Mass", app_type="coverage")
    db.put_application(a)
    got = db.get_application("c1", a.app_id)
    assert got is not None
    assert got.name == "Test Ushers"
    assert got.event_noun == "Mass"


# ---- List-by-prefix patterns ----------------------------------------------

def test_list_users_does_not_return_applications(ddb_table) -> None:
    """Both User and Application live under ``PK=COMM#c1`` but with
    different SK prefixes (``USER#`` vs ``APP#``). Each ``list_*`` query
    only sees its own."""
    db.put_user(User(community_id="c1", email="a@example.com", name="Alice"))
    db.put_application(Application(community_id="c1", name="App A", app_type="coverage"))

    users = list(db.list_users("c1"))
    apps = list(db.list_applications("c1"))
    assert len(users) == 1
    assert len(apps) == 1
    assert users[0].name == "Alice"
    assert apps[0].name == "App A"


def test_list_users_returns_multiple(ddb_table) -> None:
    for name in ["Alice", "Bob", "Carol"]:
        db.put_user(User(community_id="c1", email=f"{name}@example.com", name=name))
    names = sorted(u.name for u in db.list_users("c1"))
    assert names == ["Alice", "Bob", "Carol"]


# ---- GSI1 lookups ---------------------------------------------------------

def test_get_user_by_cognito_sub(ddb_table) -> None:
    u = User(community_id="c1", email="a@example.com", name="Alice",
             cognito_sub="abc-123")
    db.put_user(u)
    got = db.get_user_by_cognito_sub("abc-123")
    assert got is not None and got.email == "a@example.com"


def test_get_user_by_cognito_sub_returns_none_if_no_sub(ddb_table) -> None:
    """Pre-Cognito users (no sub yet) are not reachable via this lookup."""
    db.put_user(User(community_id="c1", email="a@example.com", name="Alice"))
    assert db.get_user_by_cognito_sub("never-bound") is None


def test_list_memberships_for_user_via_gsi(ddb_table) -> None:
    """One user, two app memberships — GSI1 returns both."""
    db.put_membership(Membership(community_id="c1", app_id="appA", user_id="u1"))
    db.put_membership(Membership(community_id="c1", app_id="appB", user_id="u1"))
    db.put_membership(Membership(community_id="c1", app_id="appA", user_id="u2"))

    u1_apps = sorted(m.app_id for m in db.list_memberships_for_user("u1"))
    assert u1_apps == ["appA", "appB"]


def test_list_assignments_for_user_filters_by_since_date(ddb_table) -> None:
    """``since_date`` uses the GSI1SK range, so only future-dated rows
    come back. Saves both DDB-side cost and Python filtering."""
    for date in ["2026-01-15", "2026-06-15", "2026-12-15"]:
        db.put_assignment(Assignment(
            community_id="c1", app_id="a1", yyyy_mm=date[:7],
            slot_id="s1", user_id="u1", local_date=date,
        ))

    future = list(db.list_assignments_for_user("u1", since_date="2026-06-01"))
    dates = sorted(a.local_date for a in future)
    assert dates == ["2026-06-15", "2026-12-15"]


def test_list_pending_notifications_by_send_at(ddb_table) -> None:
    """The notifier Lambda asks "what's due now?" — GSI1 partitions by
    state and orders by send_at, so the answer is a range scan."""
    early = "2026-01-01T00:00:00+00:00"
    mid = "2026-06-01T00:00:00+00:00"
    late = "2026-12-01T00:00:00+00:00"
    for send_at in [early, mid, late]:
        db.put_notification(Notification(
            community_id="c1", app_id="a1", user_id="u1",
            slot_id="s1", yyyy_mm=send_at[:7],
            send_at=send_at, lead_minutes=60,
        ))

    due = list(db.list_pending_notifications(up_to=mid))
    sent_ats = sorted(n.send_at for n in due)
    assert sent_ats == [early, mid]    # late excluded


# ---- Optimistic concurrency: put_user ------------------------------------

def test_put_user_unconditional_writes(ddb_table) -> None:
    """No expected_version → unconditional put. Always succeeds."""
    u = User(community_id="c1", email="a@example.com", name="Alice")
    db.put_user(u)
    db.put_user(u)  # second put: also fine
    assert db.get_user("c1", u.user_id) is not None


def test_put_user_with_matching_version_succeeds(ddb_table) -> None:
    """Read user, edit, write with expected_version=stored → success.
    Version is bumped to N+1 on the way through."""
    u = User(community_id="c1", email="a@example.com", name="Alice")
    db.put_user(u)
    got = db.get_user("c1", u.user_id)
    assert got.version == 0

    got.name = "Alice Edited"
    db.put_user(got, expected_version=0)

    after = db.get_user("c1", u.user_id)
    assert after.name == "Alice Edited"
    assert after.version == 1


def test_put_user_with_stale_version_raises(ddb_table) -> None:
    """Two admins both load v0. First saves (becomes v1). Second's save
    with expected_version=0 fails with ConcurrencyConflict — the web
    Lambda catches this and renders the red "your edit was not saved"
    banner."""
    u = User(community_id="c1", email="a@example.com", name="Alice")
    db.put_user(u)

    # Admin A wins.
    a_view = db.get_user("c1", u.user_id)
    a_view.name = "From A"
    db.put_user(a_view, expected_version=0)

    # Admin B (still holding v0) tries to save. Must fail.
    b_view = User(community_id="c1", email="a@example.com", name="From B",
                  user_id=u.user_id, version=0)
    with pytest.raises(db.ConcurrencyConflict):
        db.put_user(b_view, expected_version=0)

    # Stored data reflects A, not B.
    final = db.get_user("c1", u.user_id)
    assert final.name == "From A"


def test_put_user_first_time_with_expected_version_zero_succeeds(ddb_table) -> None:
    """The ``attribute_not_exists(version) OR ...`` clause makes the
    very first conditional write succeed even though there's no stored
    version yet. Backwards-compat for records pre-versioning."""
    u = User(community_id="c1", email="a@example.com", name="Alice")
    # NB: no prior put — going straight to conditional.
    db.put_user(u, expected_version=0)
    got = db.get_user("c1", u.user_id)
    assert got is not None
    assert got.version == 1


# ---- Optimistic concurrency: put_application ------------------------------

def test_put_application_with_stale_version_raises(ddb_table) -> None:
    """Same contract as put_user, applied to Application — protects the
    Settings page from concurrent edits."""
    a = Application(community_id="c1", name="App A", app_type="coverage")
    db.put_application(a)
    a_view = db.get_application("c1", a.app_id)

    # First admin saves.
    a_view.name = "App A (edited)"
    db.put_application(a_view, expected_version=0)

    # Second admin still holds version 0.
    a_view2 = Application(community_id="c1", name="App A (conflicting)",
                          app_id=a.app_id, version=0,
                          app_type="coverage")
    with pytest.raises(db.ConcurrencyConflict):
        db.put_application(a_view2, expected_version=0)


# ---- transition_schedule_state -------------------------------------------

def test_transition_schedule_state_happy(ddb_table) -> None:
    sch = Schedule(community_id="c1", app_id="a1", yyyy_mm="2026-05")
    db.put_schedule(sch)

    db.transition_schedule_state("a1", "2026-05", "draft", "published",
                                 published_at="2026-05-01T00:00:00+00:00")
    got = db.get_schedule("a1", "2026-05")
    assert got.state == "published"
    assert got.published_at == "2026-05-01T00:00:00+00:00"


def test_transition_schedule_state_wrong_from_raises(ddb_table) -> None:
    """Schedule is already published. A second 'draft -> published'
    must fail (this is what publish_schedule depends on for
    idempotency)."""
    sch = Schedule(community_id="c1", app_id="a1", yyyy_mm="2026-05",
                   state="published")
    db.put_schedule(sch)

    with pytest.raises(db.ConcurrencyConflict):
        db.transition_schedule_state("a1", "2026-05", "draft", "published")


def test_transition_schedule_state_round_trip(ddb_table) -> None:
    """Publish -> unpublish -> republish is the supported cycle."""
    db.put_schedule(Schedule(community_id="c1", app_id="a1",
                             yyyy_mm="2026-05"))
    db.transition_schedule_state("a1", "2026-05", "draft", "published")
    db.transition_schedule_state("a1", "2026-05", "published", "draft")
    db.transition_schedule_state("a1", "2026-05", "draft", "published")
    assert db.get_schedule("a1", "2026-05").state == "published"


def test_list_communities_yields_all_communities(ddb_table) -> None:
    db.put_community(Community(community_id="cA", name="A"))
    db.put_community(Community(community_id="cB", name="B"))
    ids = sorted(c.community_id for c in db.list_communities())
    assert ids == ["cA", "cB"]


def test_find_users_by_email_anywhere_zero_match(ddb_table) -> None:
    db.put_community(Community(community_id="cA", name="A"))
    db.put_user(User(community_id="cA", email="a@example.com", name="A"))
    assert db.find_users_by_email_anywhere("missing@example.com") == []


def test_find_users_by_email_anywhere_single_match(ddb_table) -> None:
    db.put_community(Community(community_id="cA", name="A"))
    db.put_community(Community(community_id="cB", name="B"))
    only_in_b = User(community_id="cB", email="beta@example.com", name="Beta")
    db.put_user(only_in_b)
    hits = db.find_users_by_email_anywhere("beta@example.com")
    assert len(hits) == 1
    cid, user = hits[0]
    assert cid == "cB"
    assert user.email == "beta@example.com"


def test_find_users_by_email_anywhere_multi_match(ddb_table) -> None:
    """Same email in two communities returns
    both pairs; caller picks a tie-breaker."""
    db.put_community(Community(community_id="cA", name="A"))
    db.put_community(Community(community_id="cB", name="B"))
    db.put_user(User(community_id="cA", email="dual@example.com", name="In A"))
    db.put_user(User(community_id="cB", email="dual@example.com", name="In B"))
    hits = db.find_users_by_email_anywhere("dual@example.com")
    cids = sorted(c for c, _ in hits)
    assert cids == ["cA", "cB"]


def test_find_users_by_email_anywhere_case_insensitive(ddb_table) -> None:
    db.put_community(Community(community_id="cA", name="A"))
    db.put_user(User(community_id="cA", email="Mixed@Example.com", name="X"))
    hits = db.find_users_by_email_anywhere("mixed@example.com")
    assert len(hits) == 1


# ---- Optimistic concurrency: put_slot ------------------------------------

def test_put_slot_with_stale_version_raises(ddb_table) -> None:
    """Two AAs edit the same slot. First wins, second is rejected."""
    s = Slot(community_id="c1", app_id="a1", yyyy_mm="2026-05",
             template_id="t1", name="8 AM", day_of_week=6,
             start_time="08:00", arrival_offset_minutes=10,
             duration_minutes=60, required_volunteers=2,
             min_volunteers=1, concrete_date="2026-05-03",
             local_date="2026-05-03")
    db.put_slot(s)
    a_view = next(iter(db.list_slots("a1", "2026-05")))
    a_view.name = "From A"
    db.put_slot(a_view, expected_version=0)

    b_view = Slot(community_id="c1", app_id="a1", yyyy_mm="2026-05",
                  template_id="t1", name="From B", day_of_week=6,
                  start_time="08:00", arrival_offset_minutes=10,
                  duration_minutes=60, required_volunteers=2,
                  min_volunteers=1, concrete_date="2026-05-03",
                  local_date="2026-05-03",
                  slot_id=a_view.slot_id, version=0)
    with pytest.raises(db.ConcurrencyConflict):
        db.put_slot(b_view, expected_version=0)

    final = next(iter(db.list_slots("a1", "2026-05")))
    assert final.name == "From A"
    assert final.version == 1


def test_put_slot_first_time_with_expected_version_zero_succeeds(
        ddb_table) -> None:
    s = Slot(community_id="c1", app_id="a1", yyyy_mm="2026-05",
             template_id="t1", name="8 AM", day_of_week=6,
             start_time="08:00", arrival_offset_minutes=10,
             duration_minutes=60, required_volunteers=2,
             min_volunteers=1, concrete_date="2026-05-03",
             local_date="2026-05-03")
    db.put_slot(s, expected_version=0)
    got = next(iter(db.list_slots("a1", "2026-05")))
    assert got.version == 1


def test_put_slot_unconditional_does_not_bump_version(ddb_table) -> None:
    """Bulk paths (template materialization, batch updates) write
    without expected_version — version should stay untouched."""
    s = Slot(community_id="c1", app_id="a1", yyyy_mm="2026-05",
             template_id="t1", name="8 AM", day_of_week=6,
             start_time="08:00", arrival_offset_minutes=10,
             duration_minutes=60, required_volunteers=2,
             min_volunteers=1, concrete_date="2026-05-03",
             local_date="2026-05-03", version=7)
    db.put_slot(s)  # no expected_version
    got = next(iter(db.list_slots("a1", "2026-05")))
    assert got.version == 7


# ---- Optimistic concurrency: put_schedule --------------------------------

def test_put_schedule_with_stale_version_raises(ddb_table) -> None:
    sch = Schedule(community_id="c1", app_id="a1", yyyy_mm="2026-05")
    db.put_schedule(sch)

    a_view = db.get_schedule("a1", "2026-05")
    a_view.archived_at = "2026-06-04T00:00:00+00:00"
    db.put_schedule(a_view, expected_version=0)

    b_view = Schedule(community_id="c1", app_id="a1", yyyy_mm="2026-05",
                      version=0)
    with pytest.raises(db.ConcurrencyConflict):
        db.put_schedule(b_view, expected_version=0)


# ---- Optimistic concurrency: put_assignment ------------------------------

def test_put_assignment_with_stale_version_raises(ddb_table) -> None:
    a = Assignment(community_id="c1", app_id="a1", yyyy_mm="2026-05",
                   slot_id="s1", user_id="u1", local_date="2026-05-03")
    db.put_assignment(a)

    a_view = next(iter(db.list_assignments_for_month("a1", "2026-05")))
    a_view.created_by = "admin-A"
    db.put_assignment(a_view, expected_version=0)

    b_view = Assignment(community_id="c1", app_id="a1", yyyy_mm="2026-05",
                        slot_id="s1", user_id="u1", local_date="2026-05-03",
                        assignment_id=a_view.assignment_id,
                        created_by="admin-B", version=0)
    with pytest.raises(db.ConcurrencyConflict):
        db.put_assignment(b_view, expected_version=0)


# ---- EventToken (passwordless poll magic-links) ---------------------------

def _seed_token(token: str = "tok-abc") -> EventToken:
    return EventToken(
        community_id="c1", app_id="a1", event_id="e1", user_id="u1",
        token=token,
        expires_at=dt.datetime(2099, 1, 1, tzinfo=dt.timezone.utc).isoformat(),
    )


def test_event_token_round_trip_and_o1_lookup(ddb_table) -> None:
    tok = _seed_token("tok-xyz-123")
    db.put_event_token(tok)
    # O(1) lookup by the raw token value (GSI1).
    by_val = db.get_event_token_by_value("tok-xyz-123")
    assert by_val is not None
    assert (by_val.app_id, by_val.event_id, by_val.user_id) == ("a1", "e1", "u1")
    assert by_val.revoked is False
    # Lookup by (event, user) for the re-send/idempotency path.
    by_key = db.get_event_token("a1", "e1", "u1")
    assert by_key is not None and by_key.token == "tok-xyz-123"
    # Unknown token resolves to None (no scan, just empty GSI1 result).
    assert db.get_event_token_by_value("nope") is None


def test_event_token_resend_is_upsert_not_dupe(ddb_table) -> None:
    db.put_event_token(_seed_token("tok-1"))
    db.put_event_token(_seed_token("tok-2"))   # same (event,user), new token
    rows = list(db.list_event_tokens("a1", "e1"))
    assert len(rows) == 1                       # deterministic SK → one row
    assert rows[0].token == "tok-2"


def test_revoke_event_token(ddb_table) -> None:
    db.put_event_token(_seed_token("tok-rev"))
    db.revoke_event_token("a1", "e1", "u1")
    assert db.get_event_token_by_value("tok-rev").revoked is True
    # No-op (no raise) when there's no token for that (event, user).
    db.revoke_event_token("a1", "e1", "nobody")


# ---- set_membership_opt_out ----------------------------------------------

def test_set_membership_opt_out_toggle(ddb_table) -> None:
    db.put_membership(Membership(community_id="c1", app_id="a1", user_id="u1"))
    db.set_membership_opt_out("a1", "u1", True)
    m = db.get_membership("a1", "u1")
    assert m.opted_out is True and m.opted_out_at is not None
    assert m.community_id == "c1" and m.app_role == "member"   # row intact
    db.set_membership_opt_out("a1", "u1", False)
    m2 = db.get_membership("a1", "u1")
    assert m2.opted_out is False and m2.opted_out_at is None


def test_set_membership_opt_out_missing_is_noop(ddb_table) -> None:
    # No membership row exists → must NOT create a phantom row that would
    # break _hydrate(Membership, ...) on later reads.
    db.set_membership_opt_out("a1", "ghost", True)
    assert db.get_membership("a1", "ghost") is None
