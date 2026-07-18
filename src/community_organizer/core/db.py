"""DynamoDB single-table accessor for Community Organizer.

This file is the only place that touches DynamoDB directly. Every other
module uses these typed helpers and never builds its own keys.

Key schema (all entities live in one table, distinguished by PK/SK
prefixes — the classic single-table design)::

    Entity        PK                    SK                  GSI1PK            GSI1SK
    ------------  --------------------  ------------------  ----------------  --------------------
    Community     COMM#<cid>            META                —                 —
    User          COMM#<cid>            USER#<uid>          SUB#<sub>         COMM#<cid>
    Application   COMM#<cid>            APP#<aid>           —                 —
    Membership    APP#<aid>             MEMBER#<uid>        UMEM#<uid>        APP#<aid>
    SlotTemplate  APP#<aid>             TPL#<tid>           —                 —
    Schedule      APP#<aid>             SCH#<ym>            STATE#<state>     APP#<aid>#<ym>
    Slot          APP#<aid>#<ym>        SLOT#<date>#<sid>   —                 —
    Assignment    APP#<aid>#<ym>        ASGN#<sid>#<uid>    UASGN#<uid>       <date>
    Notification  APP#<aid>             NTF#<send_at>#<id>  PEND#<state>      <send_at>
    EmailLog      COMM#<cid>            EMAIL#<ts>#<eid>    DIR#<dir>         <ts>

Access patterns the schema enables:

    "List X by PK + SK prefix" (no GSI needed):
        list_users(community_id)         PK=COMM#cid, SK begins_with USER#
        list_applications(community_id)  PK=COMM#cid, SK begins_with APP#
        list_templates(app_id)           PK=APP#aid,  SK begins_with TPL#
        list_schedules(app_id)           PK=APP#aid,  SK begins_with SCH#
        list_slots(app_id, ym)           PK=APP#aid#ym, SK begins_with SLOT#
        list_assignments_for_month(...)  PK=APP#aid#ym, SK begins_with ASGN#

    "Lookup by GSI1":
        get_user_by_cognito_sub(sub)            GSI1PK=SUB#<sub>
        list_memberships_for_user(user_id)      GSI1PK=UMEM#<uid>
        list_assignments_for_user(uid)          GSI1PK=UASGN#<uid>
        list_pending_notifications(up_to)       GSI1PK=STATE#pending, GSI1SK <= up_to

GSI1 prefix discipline — distinct prefixes per entity (``SUB#`` /
``UMEM#`` / ``UASGN#`` / ``PEND#`` / ``DIR#`` / ``STATE#``) — eliminates
key collisions even though every entity shares the same GSI.

Optimistic concurrency
----------------------

``put_user`` and ``put_application`` accept an optional ``expected_version``
keyword. When provided, the write uses a ConditionExpression that fails
if the stored ``version`` no longer matches what the caller read. On
success the version is incremented. The web Lambda renders a hidden
``version`` input on every edit form and passes it back — so two admins
editing the same member at the same time, only the first save wins; the
second gets a ``ConcurrencyConflict`` and is shown the current values.

``transition_schedule_state`` uses the same pattern keyed on
``state`` — that's what makes publish idempotent (see
``publishing.publish_schedule``).

Tested by:
    tests/core/test_db.py            (key construction, CRUD, optimistic locking)
    tests/core/test_publishing_flow.py (exercises db indirectly via publishing)
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import os
from decimal import Decimal
from typing import Any, Iterator

import boto3
from boto3.dynamodb.conditions import Attr, Key

from .models import (
    Application,
    Assignment,
    BlockedDate,
    Cohort,
    CohortMembership,
    Community,
    EmailLog,
    EventToken,
    FlexibleEvent,
    FlexiblePollOption,
    FlexibleRSVP,
    FlexibleSeries,
    Membership,
    Notification,
    Schedule,
    Slot,
    SlotTemplate,
    StandingOccurrence,
    StandingRSVP,
    StandingSeries,
    SwapRequest,
    User,
)

TABLE_NAME = os.environ.get("TABLE_NAME", "community-organizer")

# ---- lightweight DynamoDB call metrics (perf instrumentation) --------------
# Counts DDB API calls + total time per request so the web handler can log a
# breakdown (a page doing 20 sequential reads vs. a cold start looks very
# different). Only active inside Lambda: in tests we keep the original
# per-call resource creation so moto's per-test mocking is unaffected.
import time as _time  # noqa: E402
import threading as _threading  # noqa: E402

_IN_LAMBDA = bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))
_ddb_resource = None
_ddb_metrics = {"count": 0, "time_ms": 0.0, "_starts": []}
# Lock keeps the counters accurate when handlers fan DDB reads out across a
# thread pool (perf work). time_ms is approximate under concurrency (calls
# overlap in wall-clock); count stays exact and total wall-time is what the
# PERF log's total_ms captures anyway.
_ddb_lock = _threading.Lock()


def _ddb_before(**_kw):
    with _ddb_lock:
        _ddb_metrics["_starts"].append(_time.monotonic())


def _ddb_after(**_kw):
    with _ddb_lock:
        _ddb_metrics["count"] += 1
        if _ddb_metrics["_starts"]:
            _ddb_metrics["time_ms"] += (
                _time.monotonic() - _ddb_metrics["_starts"].pop()) * 1000.0


def reset_ddb_metrics() -> None:
    _ddb_metrics["count"] = 0
    _ddb_metrics["time_ms"] = 0.0
    _ddb_metrics["_starts"].clear()


def get_ddb_metrics() -> tuple[int, float]:
    return _ddb_metrics["count"], round(_ddb_metrics["time_ms"], 1)


def _table():
    global _ddb_resource
    if not _IN_LAMBDA:
        # Unchanged behavior for tests: fresh resource each call so moto's
        # mock context (entered/exited per test) is always the live one.
        return boto3.resource("dynamodb").Table(TABLE_NAME)
    if _ddb_resource is None:
        # Cache the resource across warm invocations (connection reuse) and
        # register the timing hooks once. max_pool_connections is raised well
        # above botocore's default of 10 so handlers that fan reads out across
        # a thread pool don't queue on a starved connection pool (that queuing
        # made the first /schedules parallelization *slower*, not faster).
        from botocore.config import Config as _BotoConfig
        _ddb_resource = boto3.resource(
            "dynamodb",
            config=_BotoConfig(max_pool_connections=32))
        events = _ddb_resource.meta.client.meta.events
        events.register("before-call.dynamodb", _ddb_before)
        events.register("after-call.dynamodb", _ddb_after)
    return _ddb_resource.Table(TABLE_NAME)


# ---- PK / SK constructors -------------------------------------------------
#
# Every DDB write builds keys through these helpers — never inline. That way
# the canonical schema lives in one place and refactors (e.g. adding a
# prefix) touch only this section. Each helper is a one-liner; the
# entity-to-key mapping is summarized in the module docstring above.


def _comm_pk(community_id: str) -> str:
    """``COMM#<cid>`` — partition key for everything community-scoped
    (Community META, Users, Applications, EmailLogs)."""
    return f"COMM#{community_id}"


def _app_pk(app_id: str) -> str:
    """``APP#<aid>`` — partition for app-scoped, non-month items
    (Memberships, Templates, Schedules, Cohorts, Notifications)."""
    return f"APP#{app_id}"


def _app_month_pk(app_id: str, yyyy_mm: str) -> str:
    """``APP#<aid>#<ym>`` — partition for month-scoped items (Slots,
    Assignments). The month is in the PK so a single query fetches the
    full month without filtering."""
    return f"APP#{app_id}#{yyyy_mm}"


def _user_sk(user_id: str) -> str:
    return f"USER#{user_id}"


def _app_sk(app_id: str) -> str:
    return f"APP#{app_id}"


def _member_sk(user_id: str) -> str:
    return f"MEMBER#{user_id}"


def _tpl_sk(template_id: str) -> str:
    return f"TPL#{template_id}"


def _sch_sk(yyyy_mm: str) -> str:
    return f"SCH#{yyyy_mm}"


def _slot_sk(local_date: str, slot_id: str) -> str:
    return f"SLOT#{local_date}#{slot_id}"


def _asgn_sk(slot_id: str, user_id: str) -> str:
    return f"ASGN#{slot_id}#{user_id}"


def _email_sk(ts: str, email_id: str) -> str:
    return f"EMAIL#{ts}#{email_id}"


# ---- Helpers ---------------------------------------------------------------

def _coerce(v: Any) -> Any:
    """Recursively convert DDB types to plain Python.

    DDB returns numeric values as ``Decimal``. We coerce to ``int``
    when the value is whole, else ``float``. NaN / Infinity Decimals
    are rejected up front because ``v % 1`` would raise
    ``decimal.InvalidOperation`` mid-Lambda and crash the request
    (security fix D11). A malicious or corrupted item with a
    non-finite Decimal could otherwise denial-of-service any handler
    that reads it.
    """
    if isinstance(v, Decimal):
        # Guard before the modulo: Decimal('NaN') and ±Infinity are
        # not finite. Treat as float — JSON-safe via str(float('nan')),
        # and downstream code can detect with math.isnan / isinf.
        if not v.is_finite():
            return float(v)
        return int(v) if v % 1 == 0 else float(v)
    if isinstance(v, list):
        return [_coerce(x) for x in v]
    if isinstance(v, dict):
        return {k: _coerce(x) for k, x in v.items()}
    return v


def _strip_keys(item: dict[str, Any]) -> dict[str, Any]:
    return {k: _coerce(v) for k, v in item.items()
            if k not in {"PK", "SK", "GSI1PK", "GSI1SK"}}


def _hydrate(cls, item: dict[str, Any]):
    """Build a dataclass instance from a stripped DDB item, dropping
    keys that aren't fields on ``cls``.

    Without this filter, deploying a release that REMOVES a field from
    a dataclass would crash every read until every existing item in
    DDB is migrated — ``Cls(**raw)`` raises ``TypeError: unexpected
    keyword argument`` on the first stale row. The filter lets a
    rollback survive contact with already-migrated rows too (extra
    fields are just ignored).

    Tests: ``tests/core/test_db.py::test_hydrate_drops_unknown_fields``
    (security fix D21).
    """
    known = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in item.items() if k in known})


def _paginate_query(**query_kwargs: Any) -> Iterator[dict[str, Any]]:
    """Yield items across every page of a DDB ``query``.

    DDB returns at most 1 MB per page; anything beyond that requires
    re-querying with ``ExclusiveStartKey=LastEvaluatedKey``. Single-
    page callers silently drop the rest, which becomes a correctness
    failure as data grows (missing users, undelivered reminders,
    leaked stale notifications) — see security fix D7.

    Use as a drop-in replacement for the single-page idiom::

        # Before (truncates at 1 MB):
        resp = _table().query(...)
        for item in resp.get("Items", []):
            ...

        # After (paginates):
        for item in _paginate_query(...):
            ...

    The kwargs are forwarded verbatim to ``table.query`` and
    ``ExclusiveStartKey`` is injected on subsequent calls; do not pass
    ``ExclusiveStartKey`` yourself.
    """
    table = _table()
    last_key: dict[str, Any] | None = None
    while True:
        if last_key is not None:
            query_kwargs["ExclusiveStartKey"] = last_key
        resp = table.query(**query_kwargs)
        yield from resp.get("Items", [])
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            return


# ---- Community -------------------------------------------------------------

def put_community(comm: Community) -> None:
    item = dataclasses.asdict(comm) | {
        "PK": _comm_pk(comm.community_id),
        "SK": "META",
    }
    _table().put_item(Item=item)


def get_community(community_id: str) -> Community | None:
    resp = _table().get_item(Key={"PK": _comm_pk(community_id), "SK": "META"})
    item = resp.get("Item")
    return _hydrate(Community, _strip_keys(item)) if item else None


def list_communities() -> Iterator[Community]:
    """Yield every Community row in the table.

    Used by inbound + cross-community lookups when one Lambda
    serves multiple stacks via a shared queue. Implemented as a
    Scan with a SK=META filter — fine while the community count
    is small (we have two today). Move to a dedicated index if
    that ever changes.
    """
    last_key = None
    while True:
        kwargs: dict[str, Any] = {
            "FilterExpression": (
                Attr("SK").eq("META") & Attr("PK").begins_with("COMM#")),
        }
        if last_key:
            kwargs["ExclusiveStartKey"] = last_key
        resp = _table().scan(**kwargs)
        for item in resp.get("Items", []):
            yield _hydrate(Community, _strip_keys(item))
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            return


# ---- User ------------------------------------------------------------------

def put_user(user: User, *, expected_version: int | None = None) -> None:
    """Persist a User. Supports optimistic concurrency.

    Two modes:

        - **Unconditional** (``expected_version=None``): plain ``put_item``.
          Used for new-user creation and admin-side writes where there's
          no editing form (e.g. login-tracking, password reset).
        - **Conditional** (``expected_version=<int>``): the write
          succeeds only if the stored ``version`` attribute equals
          ``expected_version`` (or the attribute doesn't exist yet —
          backward-compat for pre-version-field records). On success,
          ``user.version`` is bumped to ``expected_version + 1``.

    Used by the web Lambda's "edit member" flow: the form renders a
    hidden ``version`` input matching the loaded user; on submit, this
    helper conditionally writes. If another admin saved in the meantime,
    we raise ``ConcurrencyConflict`` and the handler redirects back to
    the edit page with a red banner.

    Side note: GSI1 is populated only if ``cognito_sub`` is set —
    pre-Cognito user records (CSV import) don't yet have a sub and
    therefore aren't reachable by ``get_user_by_cognito_sub`` until
    they sign in.

    Raises:
        ConcurrencyConflict: conditional write failed because someone
            else's update landed between the caller's read and this
            write.
    """
    from botocore.exceptions import ClientError
    if expected_version is not None:
        user.version = (expected_version or 0) + 1
    item = dataclasses.asdict(user) | {
        "PK": _comm_pk(user.community_id),
        "SK": _user_sk(user.user_id),
    }
    if user.cognito_sub:
        item["GSI1PK"] = f"SUB#{user.cognito_sub}"
        item["GSI1SK"] = _comm_pk(user.community_id)
    if isinstance(user.quiet_hours, tuple):
        item["quiet_hours"] = list(user.quiet_hours)
    kwargs = {"Item": item}
    if expected_version is not None:
        kwargs["ConditionExpression"] = (
            "attribute_not_exists(version) OR version = :expected")
        kwargs["ExpressionAttributeValues"] = {":expected": expected_version}
    try:
        _table().put_item(**kwargs)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise ConcurrencyConflict(
                f"user {user.user_id} was modified by someone else") from e
        raise


def _user_from_item(item: dict[str, Any]) -> User:
    raw = _strip_keys(item)
    qh = raw.get("quiet_hours")
    if isinstance(qh, list) and len(qh) == 2:
        raw["quiet_hours"] = (qh[0], qh[1])
    # Route through _hydrate so unknown fields don't crash reads
    # after a schema-removal release (security fix D21).
    return _hydrate(User, raw)


def get_user(community_id: str, user_id: str) -> User | None:
    resp = _table().get_item(Key={
        "PK": _comm_pk(community_id), "SK": _user_sk(user_id),
    })
    item = resp.get("Item")
    return _user_from_item(item) if item else None


def list_users(community_id: str) -> Iterator[User]:
    for item in _paginate_query(
        KeyConditionExpression=Key("PK").eq(_comm_pk(community_id))
        & Key("SK").begins_with("USER#"),
    ):
        yield _user_from_item(item)


def delete_user(community_id: str, user_id: str) -> None:
    _table().delete_item(Key={
        "PK": _comm_pk(community_id), "SK": _user_sk(user_id),
    })


def get_user_by_email(community_id: str, email: str) -> User | None:
    target = email.strip().lower()
    for user in list_users(community_id):
        if user.email.strip().lower() == target:
            return user
    return None


def find_users_by_email_anywhere(email: str) -> list[tuple[str, User]]:
    """Return every (community_id, User) pair whose email matches.

    Used by inbound to route a reply when one Lambda serves
    multiple communities. Typical results:
      - ``[]``                — unknown sender; reject
      - ``[(cid, user)]``     — unambiguous; route to that community
      - ``[(c1, u1), ...]``   — same email exists in multiple
        communities (e.g. the same address in both prod and beta). Caller picks
        a tie-breaker — usually the env-default community.
    """
    target = email.strip().lower()
    hits: list[tuple[str, User]] = []
    for community in list_communities():
        for user in list_users(community.community_id):
            if user.email.strip().lower() == target:
                hits.append((community.community_id, user))
                break
    return hits


def get_user_by_cognito_sub(cognito_sub: str,
                             community_id: str | None = None
                             ) -> User | None:
    """GSI1 lookup: find the User by their Cognito ``sub`` claim.

    Called on every authenticated request (the web Lambda parses the
    ID token, extracts ``sub``, and resolves it back to our User
    record). GSI1 makes this O(1) instead of scanning the user list.

    When ``community_id`` is provided, the lookup is scoped to that
    community via the GSI1SK key. **Required** when the underlying
    table is shared across multiple communities — e.g. the prod /
    beta split where both stacks point at the same app table
    table but each Lambda's COMMUNITY_ID env var selects a different
    Community. Without the scope, two communities each having a
    User row for the same Cognito identity would resolve
    arbitrarily, causing cross-community confusion. When omitted,
    behaves as before (Limit=1, first match wins) — safe when only
    one community exists in the table.

    Returns None if the sub isn't bound to any user yet — that's the
    case during the brief window between Cognito user creation and our
    own user-record link (first sign-in writes the link).
    """
    kcx = Key("GSI1PK").eq(f"SUB#{cognito_sub}")
    if community_id:
        kcx = kcx & Key("GSI1SK").eq(_comm_pk(community_id))
    resp = _table().query(
        IndexName="GSI1",
        KeyConditionExpression=kcx,
        Limit=1,
    )
    items = resp.get("Items", [])
    return _user_from_item(items[0]) if items else None


# ---- Application -----------------------------------------------------------

def put_application(app: Application, *,
                    expected_version: int | None = None) -> None:
    """Persist an Application; same optimistic-concurrency contract as
    ``put_user`` — see that docstring for the full pattern.

    Used by the Settings page edit form so two admins can't clobber
    each other's terminology / arrival_label / etc. changes.
    """
    from botocore.exceptions import ClientError
    if expected_version is not None:
        app.version = (expected_version or 0) + 1
    item = dataclasses.asdict(app) | {
        "PK": _comm_pk(app.community_id),
        "SK": _app_sk(app.app_id),
    }
    kwargs = {"Item": item}
    if expected_version is not None:
        kwargs["ConditionExpression"] = (
            "attribute_not_exists(version) OR version = :expected")
        kwargs["ExpressionAttributeValues"] = {":expected": expected_version}
    try:
        _table().put_item(**kwargs)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise ConcurrencyConflict(
                f"application {app.app_id} was modified by someone else") from e
        raise


def get_application(community_id: str, app_id: str) -> Application | None:
    resp = _table().get_item(Key={
        "PK": _comm_pk(community_id), "SK": _app_sk(app_id),
    })
    item = resp.get("Item")
    return _hydrate(Application, _strip_keys(item)) if item else None


def list_applications(community_id: str) -> Iterator[Application]:
    for item in _paginate_query(
        KeyConditionExpression=Key("PK").eq(_comm_pk(community_id))
        & Key("SK").begins_with("APP#"),
    ):
        yield _hydrate(Application, _strip_keys(item))


def get_application_by_slug(community_id: str, slug: str) -> Application | None:
    """Resolve an app by its public_slug within a community. Linear over the
    community's apps (a community has a handful) — no separate index needed."""
    if not slug:
        return None
    for app in list_applications(community_id):
        if app.public_slug and app.public_slug == slug:
            return app
    return None


def delete_application(community_id: str, app_id: str) -> dict[str, int]:
    """Delete the Application AND every row scoped under it.

    Wipes, in order:
      1. CohortMemberships under each Cohort (their PK is COHORT#cid,
         a separate partition from APP#aid so we can't sweep them in
         step 3).
      2. Every item in every APP#<aid>#<yyyy_mm> partition — Slots,
         Assignments, SwapRequests. yyyy_mm values come from the
         schedule list; per-month partitions without a Schedule row
         are not visited (that state shouldn't happen, and chasing it
         would require a table scan).
      3. Every item in the APP#<aid> partition — Memberships,
         Templates, Schedules, Cohorts, Notifications. One sweep
         catches them all because they share the partition.
      4. The Application meta row itself.

    Returns a counts dict for the caller to log:
        {"cohort_members": N, "month_items": N, "app_items": N,
         "app_row": 1}

    Future guard (not yet implemented): refuse to cascade unless the
    caller has already deleted every Schedule, so production use
    requires an explicit two-step. During testing, callers accept
    the destruction directly.
    """
    counts = {"cohort_members": 0, "month_items": 0,
              "app_items": 0, "app_row": 0}

    with _table().batch_writer() as bw:
        for cohort in list(list_cohorts(app_id)):
            for cm in list(list_cohort_members(cohort.cohort_id)):
                bw.delete_item(Key={
                    "PK": _cohort_pk(cohort.cohort_id),
                    "SK": _cmem_sk(cm.user_id),
                })
                counts["cohort_members"] += 1

        yyyy_mms = {s.yyyy_mm for s in list_schedules(app_id)}
        for ym in yyyy_mms:
            for item in _paginate_query(
                KeyConditionExpression=Key("PK").eq(_app_month_pk(app_id, ym))
            ):
                bw.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
                counts["month_items"] += 1

        for item in _paginate_query(
            KeyConditionExpression=Key("PK").eq(_app_pk(app_id))
        ):
            bw.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
            counts["app_items"] += 1

        bw.delete_item(Key={
            "PK": _comm_pk(community_id), "SK": _app_sk(app_id),
        })
        counts["app_row"] = 1
    return counts


# ---- Membership ------------------------------------------------------------

def put_membership(mem: Membership) -> None:
    item = dataclasses.asdict(mem) | {
        "PK": _app_pk(mem.app_id),
        "SK": _member_sk(mem.user_id),
        "GSI1PK": f"UMEM#{mem.user_id}",
        "GSI1SK": _app_pk(mem.app_id),
    }
    _table().put_item(Item=item)


def get_membership(app_id: str, user_id: str) -> Membership | None:
    resp = _table().get_item(Key={
        "PK": _app_pk(app_id), "SK": _member_sk(user_id),
    })
    item = resp.get("Item")
    return _hydrate(Membership, _strip_keys(item)) if item else None


def delete_membership(app_id: str, user_id: str) -> None:
    _table().delete_item(Key={"PK": _app_pk(app_id), "SK": _member_sk(user_id)})


def list_memberships_for_app(app_id: str) -> Iterator[Membership]:
    for item in _paginate_query(
        KeyConditionExpression=Key("PK").eq(_app_pk(app_id))
        & Key("SK").begins_with("MEMBER#"),
    ):
        yield _hydrate(Membership, _strip_keys(item))


def set_membership_opt_out(app_id: str, user_id: str,
                           opted_out: bool) -> None:
    """Toggle a member's group-level email opt-out for THIS app.

    A single atomic ``update_item`` (not read-modify-put) so it never
    clobbers a concurrent membership edit. Guarded by ``attribute_exists``
    so a missing membership is a no-op rather than writing a phantom row
    that would later break ``_hydrate(Membership, ...)``.
    """
    from botocore.exceptions import ClientError
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    try:
        _table().update_item(
            Key={"PK": _app_pk(app_id), "SK": _member_sk(user_id)},
            UpdateExpression="SET opted_out = :o, opted_out_at = :t",
            ExpressionAttributeValues={
                ":o": opted_out,
                ":t": now if opted_out else None,
            },
            ConditionExpression="attribute_exists(SK)",
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == \
                "ConditionalCheckFailedException":
            return
        raise


def list_memberships_for_user(user_id: str) -> Iterator[Membership]:
    """GSI1 lookup: every Application this user is a member of.

    Used to render the user's home page (which apps do they see?) and
    to decide which apps' admin tools to expose. The complementary
    function ``list_memberships_for_app`` walks the *base* table in the
    other direction.
    """
    for item in _paginate_query(
        IndexName="GSI1",
        KeyConditionExpression=Key("GSI1PK").eq(f"UMEM#{user_id}"),
    ):
        yield _hydrate(Membership, _strip_keys(item))


# ---- BlockedDate -----------------------------------------------------------
#
# Member-declared "I can't do this day" rows. The admin's cohort
# pick-list reads them to fade out (and disable) unavailable members
# for any slot on a blocked date. App-scoped (a member of multiple
# apps can have different availability per app).
#
# Base table layout (one row per (app, user, date) block):
#   PK: APP#<app_id>
#   SK: BLOCK#<local_date>#<user_id>
#
# Query patterns:
#   * list_blocked_users_on_date(app_id, date): SK begins_with
#     "BLOCK#<date>#" — used once per slot-date when rendering the
#     cohort picker (admin schedule edit).
#
#   * list_blocked_dates_for_user(app_id, user_id): GSI1 lookup
#     where GSI1PK = "UBLOCK#<user_id>#<app_id>" and GSI1SK =
#     <local_date>. Powers the member's My Availability page.
#
# The compound GSI1PK lets a single round-trip return only this
# user's blocks for the active app, with the GSI1SK range filter
# trimming past dates at the index instead of in Python.

def _block_sk(local_date: str, user_id: str) -> str:
    return f"BLOCK#{local_date}#{user_id}"


def _user_app_block_gsi1pk(user_id: str, app_id: str) -> str:
    return f"UBLOCK#{user_id}#{app_id}"


def put_blocked_date(block: BlockedDate) -> None:
    item = dataclasses.asdict(block) | {
        "PK": _app_pk(block.app_id),
        "SK": _block_sk(block.local_date, block.user_id),
        "GSI1PK": _user_app_block_gsi1pk(block.user_id, block.app_id),
        "GSI1SK": block.local_date,
    }
    _table().put_item(Item=item)


def get_blocked_date(app_id: str, user_id: str, local_date: str) -> BlockedDate | None:
    resp = _table().get_item(Key={
        "PK": _app_pk(app_id),
        "SK": _block_sk(local_date, user_id),
    })
    item = resp.get("Item")
    return _hydrate(BlockedDate, _strip_keys(item)) if item else None


def delete_blocked_date(app_id: str, user_id: str, local_date: str) -> None:
    _table().delete_item(Key={
        "PK": _app_pk(app_id),
        "SK": _block_sk(local_date, user_id),
    })


def list_blocked_users_on_date(app_id: str, local_date: str) -> set[str]:
    """Return the set of user_ids who have blocked ``local_date`` for app.

    Cohort-picker filter. Cheap: one query per distinct slot date in
    the rendered month (we collect dates first and call this once per
    date rather than per-slot).
    """
    out: set[str] = set()
    for item in _paginate_query(
        KeyConditionExpression=Key("PK").eq(_app_pk(app_id))
        & Key("SK").begins_with(f"BLOCK#{local_date}#"),
    ):
        uid = item.get("user_id")
        if uid:
            out.add(uid)
    return out


def list_blocked_dates_for_user(
    app_id: str,
    user_id: str,
    *,
    since_date: str | None = None,
) -> Iterator[BlockedDate]:
    """All BlockedDate rows for one (user, app) pair.

    ``since_date`` (inclusive) trims past blocks at the index. Pass
    today's ISO date from the My-Availability page so the list shows
    forward-looking blocks only.
    """
    key_cond = Key("GSI1PK").eq(_user_app_block_gsi1pk(user_id, app_id))
    if since_date:
        key_cond = key_cond & Key("GSI1SK").gte(since_date)
    for item in _paginate_query(
        IndexName="GSI1", KeyConditionExpression=key_cond,
    ):
        yield _hydrate(BlockedDate, _strip_keys(item))


def is_user_assigned_on_date(app_id: str, user_id: str, local_date: str) -> bool:
    """True if ``user_id`` has any Assignment on ``local_date`` for ``app_id``.

    Used to refuse a BlockedDate insert when the member is already
    booked that day — they have to release the assignment first.
    Walks the user's assignment GSI rather than the month table since
    the My-Availability handler doesn't know which yyyy_mm contains
    the date.
    """
    for a in list_assignments_for_user(user_id, since_date=local_date):
        if a.app_id == app_id and a.local_date == local_date:
            return True
        # GSI is sorted by date asc; bail as soon as we pass the date.
        if a.local_date > local_date:
            return False
    return False


# ---- Cohort ----------------------------------------------------------------

def _cohort_sk(cohort_id: str) -> str:
    return f"COHORT#{cohort_id}"


def _cohort_pk(cohort_id: str) -> str:
    return f"COHORT#{cohort_id}"


def _cmem_sk(user_id: str) -> str:
    return f"CMEM#{user_id}"


def put_cohort(cohort: Cohort) -> None:
    item = dataclasses.asdict(cohort) | {
        "PK": _app_pk(cohort.app_id),
        "SK": _cohort_sk(cohort.cohort_id),
    }
    _table().put_item(Item=item)


def get_cohort(app_id: str, cohort_id: str) -> Cohort | None:
    resp = _table().get_item(Key={
        "PK": _app_pk(app_id), "SK": _cohort_sk(cohort_id),
    })
    item = resp.get("Item")
    return _hydrate(Cohort, _strip_keys(item)) if item else None


def list_cohorts(app_id: str) -> Iterator[Cohort]:
    for item in _paginate_query(
        KeyConditionExpression=Key("PK").eq(_app_pk(app_id))
        & Key("SK").begins_with("COHORT#"),
    ):
        yield _hydrate(Cohort, _strip_keys(item))


def delete_cohort(app_id: str, cohort_id: str) -> None:
    _table().delete_item(Key={"PK": _app_pk(app_id), "SK": _cohort_sk(cohort_id)})


def get_cohort_by_template(app_id: str, template_id: str) -> Cohort | None:
    for c in list_cohorts(app_id):
        if c.linked_template_id == template_id:
            return c
    return None


def put_cohort_membership(cm: CohortMembership) -> None:
    item = dataclasses.asdict(cm) | {
        "PK": _cohort_pk(cm.cohort_id),
        "SK": _cmem_sk(cm.user_id),
        "GSI1PK": f"UCOH#{cm.user_id}",
        "GSI1SK": f"COHORT#{cm.cohort_id}",
    }
    _table().put_item(Item=item)


def delete_cohort_membership(cohort_id: str, user_id: str) -> None:
    _table().delete_item(Key={
        "PK": _cohort_pk(cohort_id), "SK": _cmem_sk(user_id),
    })


def list_cohort_members(cohort_id: str) -> Iterator[CohortMembership]:
    for item in _paginate_query(
        KeyConditionExpression=Key("PK").eq(_cohort_pk(cohort_id))
        & Key("SK").begins_with("CMEM#"),
    ):
        yield _hydrate(CohortMembership, _strip_keys(item))


def list_cohorts_for_user(user_id: str) -> Iterator[CohortMembership]:
    for item in _paginate_query(
        IndexName="GSI1",
        KeyConditionExpression=Key("GSI1PK").eq(f"UCOH#{user_id}"),
    ):
        yield _hydrate(CohortMembership, _strip_keys(item))


# ---- SlotTemplate ----------------------------------------------------------

def put_template(tpl: SlotTemplate) -> None:
    item = dataclasses.asdict(tpl) | {
        "PK": _app_pk(tpl.app_id),
        "SK": _tpl_sk(tpl.template_id),
    }
    _table().put_item(Item=item)


def get_template(app_id: str, template_id: str) -> SlotTemplate | None:
    resp = _table().get_item(Key={
        "PK": _app_pk(app_id), "SK": _tpl_sk(template_id),
    })
    item = resp.get("Item")
    return _hydrate(SlotTemplate, _strip_keys(item)) if item else None


def list_templates(app_id: str) -> Iterator[SlotTemplate]:
    for item in _paginate_query(
        KeyConditionExpression=Key("PK").eq(_app_pk(app_id))
        & Key("SK").begins_with("TPL#"),
    ):
        yield _hydrate(SlotTemplate, _strip_keys(item))


def delete_template(app_id: str, template_id: str) -> None:
    _table().delete_item(Key={"PK": _app_pk(app_id), "SK": _tpl_sk(template_id)})


# ---- Schedule --------------------------------------------------------------

def put_schedule(sch: Schedule, *,
                 expected_version: int | None = None) -> None:
    """Persist a Schedule with optional optimistic concurrency.

    The state-machine transitions (draft → publishing → published)
    use their own ``transition_schedule_state`` CAS and don't need
    expected_version. This guard exists for non-state edits (notes,
    archived_at, future per-schedule metadata) where two AAs could
    race.

    Raises ConcurrencyConflict on a stale version.
    """
    from botocore.exceptions import ClientError
    if expected_version is not None:
        sch.version = (expected_version or 0) + 1
    item = dataclasses.asdict(sch) | {
        "PK": _app_pk(sch.app_id),
        "SK": _sch_sk(sch.yyyy_mm),
        "GSI1PK": f"STATE#{sch.state}",
        "GSI1SK": f"APP#{sch.app_id}#{sch.yyyy_mm}",
    }
    kwargs = {"Item": item}
    if expected_version is not None:
        kwargs["ConditionExpression"] = (
            "attribute_not_exists(version) OR version = :expected")
        kwargs["ExpressionAttributeValues"] = {":expected": expected_version}
    try:
        _table().put_item(**kwargs)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise ConcurrencyConflict(
                f"schedule {sch.app_id}/{sch.yyyy_mm} was modified "
                f"by someone else") from e
        raise


class ConcurrencyConflict(Exception):
    """Raised when a conditional write fails due to concurrent modification."""


def transition_schedule_state(app_id: str, yyyy_mm: str,
                              from_state: str, to_state: str,
                              published_at: str | None = None,
                              clear_published_at: bool = False,
                              archived_at: str | None = None,
                              clear_archived_at: bool = False) -> None:
    """Atomically transition a schedule's state from ``from_state`` to
    ``to_state``, or raise.

    This is the linchpin of publish idempotency. ``publish_schedule``
    calls this *before* sending any emails — if two admins click
    publish at the same time, only one wins the conditional update and
    the other gets ``ConcurrencyConflict`` (which the caller maps to
    "already published by another admin"). No broadcast goes out from
    the loser.

    Also updates the GSI1PK so the schedule moves between
    ``STATE#draft`` and ``STATE#published`` partitions on the index.

    Args:
        app_id, yyyy_mm: schedule identifier.
        from_state: current state we require (``"draft"`` for publish,
            ``"published"`` for unpublish).
        to_state: state to set.
        published_at: optional ISO timestamp; if given, written to the
            ``published_at`` attribute in the same atomic update.
        clear_published_at: if True, REMOVE the ``published_at``
            attribute in the same atomic update. Used by unpublish to
            null out the timestamp safely. Mutually exclusive with
            ``published_at``.

    Raises:
        ConcurrencyConflict: the schedule is no longer in ``from_state``
            (either someone else moved it, or it was already in the
            target state to begin with).
    """
    from botocore.exceptions import ClientError
    set_parts = ["#s = :to", "GSI1PK = :gsi"]
    expr_values = {
        ":from": from_state,
        ":to": to_state,
        ":gsi": f"STATE#{to_state}",
    }
    if published_at is not None:
        set_parts.append("published_at = :pa")
        expr_values[":pa"] = published_at
    if archived_at is not None:
        set_parts.append("archived_at = :aa")
        expr_values[":aa"] = archived_at
    update_expr = "SET " + ", ".join(set_parts)
    remove_parts = []
    if clear_published_at:
        remove_parts.append("published_at")
    if clear_archived_at:
        remove_parts.append("archived_at")
    if remove_parts:
        update_expr += " REMOVE " + ", ".join(remove_parts)
    try:
        _table().update_item(
            Key={"PK": _app_pk(app_id), "SK": _sch_sk(yyyy_mm)},
            UpdateExpression=update_expr,
            ConditionExpression="#s = :from",
            ExpressionAttributeNames={"#s": "state"},
            ExpressionAttributeValues=expr_values,
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise ConcurrencyConflict(
                f"schedule {yyyy_mm} not in state '{from_state}'") from e
        raise


def get_schedule(app_id: str, yyyy_mm: str) -> Schedule | None:
    resp = _table().get_item(Key={
        "PK": _app_pk(app_id), "SK": _sch_sk(yyyy_mm),
    })
    item = resp.get("Item")
    return _hydrate(Schedule, _strip_keys(item)) if item else None


def list_schedules(app_id: str) -> Iterator[Schedule]:
    for item in _paginate_query(
        KeyConditionExpression=Key("PK").eq(_app_pk(app_id))
        & Key("SK").begins_with("SCH#"),
    ):
        yield _hydrate(Schedule, _strip_keys(item))


def delete_schedule(app_id: str, yyyy_mm: str) -> None:
    _table().delete_item(Key={"PK": _app_pk(app_id), "SK": _sch_sk(yyyy_mm)})


# ---- Slot ------------------------------------------------------------------

def put_slot(slot: Slot, *,
             expected_version: int | None = None) -> None:
    """Persist a Slot with optional optimistic concurrency.

    Used by AA-driven edits (cancel slot, change name, adjust
    required volunteers). The atomic-signup path (D12) does NOT use
    this — it has its own TransactWriteItems guard that bumps
    ``assignment_count`` separately from ``version``.

    Raises ConcurrencyConflict on a stale version.
    """
    from botocore.exceptions import ClientError
    if expected_version is not None:
        slot.version = (expected_version or 0) + 1
    item = dataclasses.asdict(slot) | {
        "PK": _app_month_pk(slot.app_id, slot.yyyy_mm),
        "SK": _slot_sk(slot.local_date, slot.slot_id),
    }
    kwargs = {"Item": item}
    if expected_version is not None:
        kwargs["ConditionExpression"] = (
            "attribute_not_exists(version) OR version = :expected")
        kwargs["ExpressionAttributeValues"] = {":expected": expected_version}
    try:
        _table().put_item(**kwargs)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise ConcurrencyConflict(
                f"slot {slot.slot_id} was modified by someone else") from e
        raise


def put_slots(slots: list[Slot]) -> None:
    with _table().batch_writer() as batch:
        for slot in slots:
            item = dataclasses.asdict(slot) | {
                "PK": _app_month_pk(slot.app_id, slot.yyyy_mm),
                "SK": _slot_sk(slot.local_date, slot.slot_id),
            }
            batch.put_item(Item=item)


def list_slots(app_id: str, yyyy_mm: str) -> Iterator[Slot]:
    for item in _paginate_query(
        KeyConditionExpression=Key("PK").eq(_app_month_pk(app_id, yyyy_mm))
        & Key("SK").begins_with("SLOT#"),
    ):
        yield _hydrate(Slot, _strip_keys(item))


def delete_slot(app_id: str, yyyy_mm: str, local_date: str,
                slot_id: str) -> None:
    """Delete a single Slot. Used by the recurring-app template
    cascade. Coverage apps that want to nuke a whole period still
    use ``delete_slots``."""
    _table().delete_item(Key={
        "PK": _app_month_pk(app_id, yyyy_mm),
        "SK": _slot_sk(local_date, slot_id),
    })


def delete_slots(app_id: str, yyyy_mm: str) -> int:
    pk = _app_month_pk(app_id, yyyy_mm)
    table = _table()
    n = 0
    with table.batch_writer() as batch:
        for item in _paginate_query(
            KeyConditionExpression=Key("PK").eq(pk) & Key("SK").begins_with("SLOT#"),
            ProjectionExpression="PK, SK",
        ):
            batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
            n += 1
    return n


def delete_assignments_for_month(app_id: str, yyyy_mm: str) -> int:
    pk = _app_month_pk(app_id, yyyy_mm)
    table = _table()
    n = 0
    with table.batch_writer() as batch:
        for item in _paginate_query(
            KeyConditionExpression=Key("PK").eq(pk) & Key("SK").begins_with("ASGN#"),
            ProjectionExpression="PK, SK",
        ):
            batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
            n += 1
    return n


def get_slot(app_id: str, yyyy_mm: str, local_date: str, slot_id: str) -> Slot | None:
    resp = _table().get_item(Key={
        "PK": _app_month_pk(app_id, yyyy_mm),
        "SK": _slot_sk(local_date, slot_id),
    })
    item = resp.get("Item")
    return _hydrate(Slot, _strip_keys(item)) if item else None


def find_slot_in_month(app_id: str, yyyy_mm: str, slot_id: str) -> Slot | None:
    for s in list_slots(app_id, yyyy_mm):
        if s.slot_id == slot_id:
            return s
    return None


# ---- Assignment ------------------------------------------------------------

def put_assignment(asg: Assignment, *,
                   expected_version: int | None = None) -> None:
    """Unconditional write of an Assignment, optionally CAS-guarded.

    Used on admin-driven paths (admin assigns / bulk-assigns / swap
    accept) where we trust the caller's prior capacity check.
    Member-driven self-signup MUST use
    ``atomic_signup_assignment`` instead — concurrent self-signups
    can race and silently exceed the slot's ``max_volunteers``
    otherwise (security fix D12).

    Assignments are mostly immutable after creation today; the
    expected_version guard exists for future per-assignment
    mutations (swap-state, cohort tag, etc.) without a migration.
    Raises ConcurrencyConflict on stale version.
    """
    from botocore.exceptions import ClientError
    if expected_version is not None:
        asg.version = (expected_version or 0) + 1
    item = dataclasses.asdict(asg) | {
        "PK": _app_month_pk(asg.app_id, asg.yyyy_mm),
        "SK": _asgn_sk(asg.slot_id, asg.user_id),
        "GSI1PK": f"UASGN#{asg.user_id}",
        "GSI1SK": asg.local_date,
    }
    kwargs = {"Item": item}
    if expected_version is not None:
        kwargs["ConditionExpression"] = (
            "attribute_not_exists(version) OR version = :expected")
        kwargs["ExpressionAttributeValues"] = {":expected": expected_version}
    try:
        _table().put_item(**kwargs)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise ConcurrencyConflict(
                f"assignment {asg.assignment_id} was modified "
                f"by someone else") from e
        raise


def confirm_assignment(app_id: str, yyyy_mm: str, slot_id: str,
                       user_id: str, *, via: str) -> bool:
    """Set a single Assignment row as confirmed.

    Targets the existing row via UpdateExpression so we don't have to
    read-modify-write — this both avoids a race against the
    delete-then-rewrite swap path AND skips the version increment
    that put_assignment would do (confirmation is a stamp, not an
    edit). Returns True on a successful confirm, False if no row
    exists (the assignment was already removed, e.g. via a swap).

    Idempotent: re-confirming an already-confirmed row updates the
    timestamp + via field but doesn't double-stamp.
    """
    pk = _app_month_pk(app_id, yyyy_mm)
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    try:
        _table().update_item(
            Key={"PK": pk, "SK": _asgn_sk(slot_id, user_id)},
            UpdateExpression="SET confirmed_at = :ts, confirmed_via = :via",
            ConditionExpression="attribute_exists(SK)",
            ExpressionAttributeValues={":ts": now, ":via": via},
        )
        return True
    except Exception:  # noqa: BLE001
        # ConditionalCheckFailedException = row not there. Anything
        # else (rare) is best-effort silent — the caller can re-render
        # and try again. Confirming a non-existent assignment is a
        # no-op, not an error.
        return False


def delete_assignment(app_id: str, yyyy_mm: str, slot_id: str, user_id: str) -> None:
    """Unconditional delete + best-effort counter decrement.

    The decrement uses ``ADD assignment_count :neg_one`` on the
    slot — DDB allows the counter to go negative, but we cap at 0
    here for hygiene. Best-effort because we don't fail the user's
    withdraw if the counter update errors (e.g. the slot itself was
    already deleted).
    """
    pk = _app_month_pk(app_id, yyyy_mm)
    table = _table()
    table.delete_item(Key={"PK": pk, "SK": _asgn_sk(slot_id, user_id)})
    # Decrement the slot's counter so future signup capacity checks
    # remain accurate. We have to know the slot's SK (which includes
    # the local_date) to find it — look it up first.
    slot = _find_slot_sk(app_id, yyyy_mm, slot_id)
    if slot is None:
        return
    try:
        table.update_item(
            Key={"PK": pk, "SK": slot},
            UpdateExpression="ADD assignment_count :neg",
            ConditionExpression="assignment_count > :zero",
            ExpressionAttributeValues={":neg": -1, ":zero": 0},
        )
    except Exception:  # noqa: BLE001
        # Conditional failure (count was 0 or attr missing) is the
        # expected case during the lazy-init window — just swallow.
        pass


def _find_slot_sk(app_id: str, yyyy_mm: str, slot_id: str) -> str | None:
    """Look up just the SK of a Slot in (app_id, yyyy_mm) by slot_id.

    The Slot's SK is ``SLOT#<local_date>#<slot_id>`` — we don't know
    local_date from the assignment query string, so we have to scan
    the month's SLOT# prefix. Returns the SK or None.
    """
    pk = _app_month_pk(app_id, yyyy_mm)
    for item in _paginate_query(
        KeyConditionExpression=Key("PK").eq(pk)
        & Key("SK").begins_with("SLOT#"),
        FilterExpression=Key("slot_id").eq(slot_id),
        ProjectionExpression="SK",
    ):
        return item["SK"]
    return None


class CapacityExceeded(Exception):
    """Raised by ``atomic_signup_assignment`` when the slot is full."""


def atomic_signup_assignment(
    slot: Slot,
    *,
    user_id: str,
    community_id: str,
    created_by: str | None = None,
) -> Assignment:
    """Atomic self-signup: increment the slot's counter AND write the
    Assignment row in a single DDB TransactWriteItems call.

    The conditional check on the slot is
    ``attribute_not_exists(assignment_count) OR assignment_count < :max``,
    so the first signup against a legacy slot (one that pre-dates
    this counter field) lazy-initializes the counter at 1. The
    transaction is all-or-nothing: if the slot is full the
    assignment row is NOT written, and if the assignment row already
    exists (idempotent retry) the counter is NOT incremented.

    Args:
        slot: the Slot the member is signing up for.
        user_id: signing-up member.
        community_id: stamped onto the new Assignment.
        created_by: optional user_id of the actor (defaults to user_id
            for self-signup).

    Raises:
        CapacityExceeded: the slot is at ``max_volunteers``.

    See security fix D12.
    """
    from botocore.exceptions import ClientError
    # max_volunteers=None means "no cap" (e.g. adoration apps where
    # any number of people can sign up). 10_000 is well above any
    # realistic parish-scale slot count and keeps the DDB
    # ConditionExpression machinery uniform without branching here.
    # Existing rows where max happens to be 0 fall back to required
    # (legacy behavior).
    if slot.max_volunteers is None:
        max_cap = 10_000
    elif slot.max_volunteers == 0:
        max_cap = slot.required_volunteers
    else:
        max_cap = slot.max_volunteers
    pk = _app_month_pk(slot.app_id, slot.yyyy_mm)
    # Self-signup is implicitly confirmed — the member explicitly
    # asked for the slot. See #217 (assignment confirmation status).
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    asg = Assignment(
        community_id=community_id, app_id=slot.app_id,
        yyyy_mm=slot.yyyy_mm, slot_id=slot.slot_id,
        user_id=user_id, local_date=slot.local_date,
        created_by=created_by or user_id,
        confirmed_at=now,
        confirmed_via="self_signup",
    )
    asg_item = dataclasses.asdict(asg) | {
        "PK": pk,
        "SK": _asgn_sk(slot.slot_id, user_id),
        "GSI1PK": f"UASGN#{user_id}",
        "GSI1SK": slot.local_date,
    }
    slot_sk = _slot_sk(slot.local_date, slot.slot_id)
    table_name = TABLE_NAME
    client = boto3.client("dynamodb")
    # Build via the resource layer's serializer so types match what
    # ``_table().put_item`` would have produced.
    from boto3.dynamodb.types import TypeSerializer
    ts = TypeSerializer()
    asg_low = {k: ts.serialize(v) for k, v in asg_item.items()}
    try:
        client.transact_write_items(TransactItems=[
            {
                "Update": {
                    "TableName": table_name,
                    "Key": {"PK": {"S": pk}, "SK": {"S": slot_sk}},
                    "UpdateExpression": "ADD assignment_count :one",
                    "ConditionExpression": (
                        "attribute_not_exists(assignment_count) "
                        "OR assignment_count < :max"
                    ),
                    "ExpressionAttributeValues": {
                        ":one": {"N": "1"},
                        ":max": {"N": str(int(max_cap))},
                    },
                },
            },
            {
                "Put": {
                    "TableName": table_name,
                    "Item": asg_low,
                    "ConditionExpression": "attribute_not_exists(SK)",
                },
            },
        ])
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code == "TransactionCanceledException":
            # Reasons array tells us which leg failed — first item is
            # the slot-counter update (capacity), second is the
            # assignment put (duplicate). Either failure means the
            # signup couldn't proceed.
            reasons = e.response.get("CancellationReasons", []) or []
            counter_failed = (reasons[0].get("Code")
                              == "ConditionalCheckFailed") if reasons else False
            if counter_failed:
                raise CapacityExceeded(
                    f"slot {slot.slot_id} is at capacity "
                    f"({max_cap})") from e
            # Otherwise it's a duplicate Assignment — caller can
            # treat as success or surface as appropriate.
            raise
        raise
    return asg


def list_assignments_for_slot(app_id: str, yyyy_mm: str, slot_id: str) -> Iterator[Assignment]:
    for item in _paginate_query(
        KeyConditionExpression=Key("PK").eq(_app_month_pk(app_id, yyyy_mm))
        & Key("SK").begins_with(f"ASGN#{slot_id}#"),
    ):
        yield _hydrate(Assignment, _strip_keys(item))


def list_assignments_for_month(app_id: str, yyyy_mm: str) -> Iterator[Assignment]:
    for item in _paginate_query(
        KeyConditionExpression=Key("PK").eq(_app_month_pk(app_id, yyyy_mm))
        & Key("SK").begins_with("ASGN#"),
    ):
        yield _hydrate(Assignment, _strip_keys(item))


def list_assignments_for_user(user_id: str, *, since_date: str | None = None) -> Iterator[Assignment]:
    """GSI1 lookup: all assignments for one user across every month.

    Powers the member's "Your upcoming events" home-page section. The
    optional ``since_date`` filter (compared as an ISO string against
    ``GSI1SK = <local_date>``) excludes past assignments cheaply — DDB
    handles the range condition on the index instead of returning
    everything and filtering in Python.
    """
    key_cond = Key("GSI1PK").eq(f"UASGN#{user_id}")
    if since_date:
        key_cond = key_cond & Key("GSI1SK").gte(since_date)
    for item in _paginate_query(
        IndexName="GSI1", KeyConditionExpression=key_cond,
    ):
        yield _hydrate(Assignment, _strip_keys(item))


# ---- SwapRequest -----------------------------------------------------------

def _swap_sk(swap_id: str) -> str:
    return f"SWAP#{swap_id}"


def put_swap(swap: SwapRequest) -> None:
    item = dataclasses.asdict(swap) | {
        "PK": _app_month_pk(swap.app_id, swap.yyyy_mm),
        "SK": _swap_sk(swap.swap_id),
        "GSI1PK": f"SWAPUSER#{swap.requester_user_id}",
        "GSI1SK": swap.created_at,
    }
    _table().put_item(Item=item)


def get_swap(app_id: str, yyyy_mm: str, swap_id: str) -> SwapRequest | None:
    resp = _table().get_item(Key={
        "PK": _app_month_pk(app_id, yyyy_mm),
        "SK": _swap_sk(swap_id),
    })
    item = resp.get("Item")
    return _hydrate(SwapRequest, _strip_keys(item)) if item else None


def list_swaps_for_month(app_id: str, yyyy_mm: str,
                         *, state: str | None = None) -> Iterator[SwapRequest]:
    for item in _paginate_query(
        KeyConditionExpression=Key("PK").eq(_app_month_pk(app_id, yyyy_mm))
        & Key("SK").begins_with("SWAP#"),
    ):
        sr = _hydrate(SwapRequest, _strip_keys(item))
        if state and sr.state != state:
            continue
        yield sr


def list_swaps_for_user(user_id: str) -> Iterator[SwapRequest]:
    for item in _paginate_query(
        IndexName="GSI1",
        KeyConditionExpression=Key("GSI1PK").eq(f"SWAPUSER#{user_id}"),
    ):
        yield _hydrate(SwapRequest, _strip_keys(item))


# ---- Notification ----------------------------------------------------------

def _ntf_sk(send_at: str, notification_id: str) -> str:
    return f"NTF#{send_at}#{notification_id}"


def put_notification(ntf: Notification) -> None:
    item = dataclasses.asdict(ntf) | {
        "PK": _app_pk(ntf.app_id),
        "SK": _ntf_sk(ntf.send_at, ntf.notification_id),
        "GSI1PK": f"STATE#{ntf.state}",
        "GSI1SK": ntf.send_at,
    }
    _table().put_item(Item=item)


def put_notifications(ntfs: list[Notification]) -> None:
    with _table().batch_writer() as batch:
        for ntf in ntfs:
            item = dataclasses.asdict(ntf) | {
                "PK": _app_pk(ntf.app_id),
                "SK": _ntf_sk(ntf.send_at, ntf.notification_id),
                "GSI1PK": f"STATE#{ntf.state}",
                "GSI1SK": ntf.send_at,
            }
            batch.put_item(Item=item)


def claim_notification(notification_id: str, app_id: str, send_at: str) -> bool:
    """Atomically transition a Notification from ``pending`` -> ``in_flight``.

    The notifier Lambda calls this BEFORE sending a reminder email.
    If the conditional fails (state isn't ``pending`` anymore — already
    claimed by a concurrent invocation, or already sent / cancelled),
    return False and the caller skips this notification.

    Why: without the claim, a Lambda timeout in the middle of a batch
    causes the next minute's invocation to re-send already-delivered
    reminders. The claim is the idempotency token — at most one
    invocation can successfully transition pending -> in_flight.

    Also updates GSI1PK so the row moves out of the ``STATE#pending``
    partition, hiding it from subsequent ``list_pending_notifications``
    queries.

    Returns:
        True if we won the claim (state was pending, now in_flight).
        False if the conditional failed — someone else got it, or the
        row was already past pending.

    See security fix D13. Limitation: a Lambda that crashes between
    claim and send leaves the row stuck in ``in_flight``. Manual
    recovery (admin tool) is the current escape; a future watchdog
    that re-claims old in_flight rows would automate this.
    """
    from botocore.exceptions import ClientError
    try:
        _table().update_item(
            Key={"PK": _app_pk(app_id),
                 "SK": _ntf_sk(send_at, notification_id)},
            UpdateExpression="SET #s = :inflight, GSI1PK = :gsi",
            ConditionExpression="#s = :expected",
            ExpressionAttributeNames={"#s": "state"},
            ExpressionAttributeValues={
                ":inflight": "in_flight",
                ":expected": "pending",
                ":gsi": "STATE#in_flight",
            },
        )
        return True
    except ClientError as e:
        if (e.response.get("Error", {}).get("Code")
                == "ConditionalCheckFailedException"):
            return False
        raise


def list_pending_notifications(*, up_to: str) -> Iterator[Notification]:
    """GSI1 lookup: pending notifications with send_at <= up_to.

    Called by the notifier Lambda on its EventBridge schedule. ``up_to``
    is "now" in ISO UTC — anything before that is due to fire. Because
    GSI1PK partitions by state and GSI1SK is the send_at timestamp,
    DDB returns just the slice of pending-and-due notifications without
    scanning the whole table.

    Cost note: ``STATE#pending`` is a hot partition — every queued
    reminder lives there. That's fine at our scale (hundreds of
    notifications), but at very high volume this would warrant
    sharding the partition key by hour.
    """
    for item in _paginate_query(
        IndexName="GSI1",
        KeyConditionExpression=(
            Key("GSI1PK").eq("STATE#pending")
            & Key("GSI1SK").lte(up_to)
        ),
    ):
        yield _hydrate(Notification, _strip_keys(item))


def delete_notifications_for_app(app_id: str) -> int:
    table = _table()
    n = 0
    with table.batch_writer() as batch:
        for item in _paginate_query(
            KeyConditionExpression=Key("PK").eq(_app_pk(app_id))
            & Key("SK").begins_with("NTF#"),
            ProjectionExpression="PK, SK",
        ):
            batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
            n += 1
    return n


def delete_notifications_for_schedule(app_id: str, yyyy_mm: str) -> int:
    """Delete every Notification queued for a specific (app_id, yyyy_mm).

    DDB-side ``FilterExpression`` on ``yyyy_mm`` means the Lambda
    only receives the items it's going to delete (not every NTF# row
    for the app). DDB still reads everything under the ``NTF#``
    prefix — capacity unchanged — but bandwidth and Python work scale
    with matched-rows, not with total notifications.

    The deeper fix is including ``yyyy_mm`` in the SK so DDB never
    reads non-matching rows; that's tracked as D8 in
    ``docs/SECURITY-AUDIT.md`` and needs a schema migration.
    """
    table = _table()
    n = 0
    with table.batch_writer() as batch:
        for item in _paginate_query(
            KeyConditionExpression=Key("PK").eq(_app_pk(app_id))
            & Key("SK").begins_with("NTF#"),
            FilterExpression=Attr("yyyy_mm").eq(yyyy_mm),
            ProjectionExpression="PK, SK",
        ):
            batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
            n += 1
    return n


# ---- EmailLog --------------------------------------------------------------

def put_email_log(log: EmailLog) -> None:
    item = dataclasses.asdict(log) | {
        "PK": _comm_pk(log.community_id),
        "SK": _email_sk(log.ts, log.email_id),
        "GSI1PK": f"DIR#{log.direction}",
        "GSI1SK": log.ts,
    }
    _table().put_item(Item=item)


def find_email_log_by_provider_message_id(
        provider_message_id: str) -> EmailLog | None:
    """Look up an outbound email_log row by the provider's Message-ID.

    Used by inbound to thread a reply back to the original outbound
    via the In-Reply-To / References chain — the `related_app_id`
    field on the row tells us which app the conversation was about,
    so the forward can be scoped to that app's AAs instead of fanning
    to AAs of every app the sender belongs to.

    Strategy: query the DIR#outbound GSI partition with a server-
    side filter on `provider_message_id`. The filter scans every
    outbound row in the partition before returning, so this scales
    O(N) in total outbound emails — acceptable while volume is low
    (inbound is a few replies per day). Promote to a dedicated GSI
    (GSI2PK=`MSGID#<id>`) when this becomes a hot path.

    Returns the matching EmailLog or None if no row matches.
    """
    target = (provider_message_id or "").strip()
    if not target:
        return None
    # Strip any RFC 5322 angle brackets — In-Reply-To values arrive
    # wrapped like `<abc@host>` but provider_message_id is stored bare.
    target = target.strip("<>").strip()
    if not target:
        return None
    # Paginate until we find a match or exhaust the partition. With
    # a FilterExpression DDB applies Limit BEFORE the filter, so a
    # naive Limit=1 would silently miss matches that live past the
    # first scanned page. Paginating is required for correctness.
    last_key = None
    while True:
        kwargs: dict[str, Any] = {
            "IndexName": "GSI1",
            "KeyConditionExpression": Key("GSI1PK").eq("DIR#outbound"),
            "FilterExpression": Attr("provider_message_id").eq(target),
        }
        if last_key:
            kwargs["ExclusiveStartKey"] = last_key
        resp = _table().query(**kwargs)
        items = resp.get("Items", [])
        if items:
            return _hydrate(EmailLog, _strip_keys(items[0]))
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            return None


def list_email_logs(community_id: str, *, limit: int = 50,
                    newest_first: bool = True,
                    before_sk: str | None = None) -> Iterator[EmailLog]:
    kwargs = dict(
        KeyConditionExpression=Key("PK").eq(_comm_pk(community_id))
        & Key("SK").begins_with("EMAIL#"),
        ScanIndexForward=not newest_first,
        Limit=limit,
    )
    if before_sk:
        kwargs["ExclusiveStartKey"] = {
            "PK": _comm_pk(community_id),
            "SK": before_sk,
        }
    resp = _table().query(**kwargs)
    for item in resp.get("Items", []):
        yield _hydrate(EmailLog, _strip_keys(item))


# ============================================================================
# Date-centric event apps — slice 1 helpers
# ----------------------------------------------------------------------------
# Two app types (standing_event + flexible_event) share the wall-
# calendar UI but differ in flow. Keys are app-scoped; helpers
# mirror the User/Application pattern with optional CAS via
# expected_version. See design doc:
# design notes for the date-poll feature
# ============================================================================


# ---- Key helpers (new) ----------------------------------------------------

def _series_sk(series_id: str) -> str:
    """``SERIES#<sid>`` — both StandingSeries and FlexibleSeries
    live under the parent app's partition with this SK prefix."""
    return f"SERIES#{series_id}"


def _occ_sk(iso_date: str, occurrence_id: str) -> str:
    """``OCC#<iso_date>#<occ_id>`` — standing occurrences sort by date
    so listing a month is a SK begins_with."""
    return f"OCC#{iso_date}#{occurrence_id}"


def _occ_rsvp_sk(occurrence_id: str, user_id: str) -> str:
    """``OCC#<occ_id>#RSVP#<user_id>`` — one RSVP per (occurrence, user)
    so re-voting is an upsert."""
    return f"OCC#{occurrence_id}#RSVP#{user_id}"


def _flex_event_sk(event_id: str) -> str:
    return f"EVT#{event_id}"


def _flex_option_sk(event_id: str, sort_key: int, option_id: str) -> str:
    """``EVT#<eid>#OPT#<sort>#<oid>`` — options sort naturally by
    sort_key so the AA's "the third date proposed" stays the third
    row even after edits."""
    return f"EVT#{event_id}#OPT#{sort_key:04d}#{option_id}"


def _flex_rsvp_sk(event_id: str, user_id: str) -> str:
    """``EVT#<eid>#RSVP#<uid>`` — one row per (event, user) so re-
    voting in poll phase or updating bringing in scheduled phase is
    an upsert."""
    return f"EVT#{event_id}#RSVP#{user_id}"


# ---- StandingSeries ------------------------------------------------------

def put_standing_series(series: StandingSeries, *,
                        expected_version: int | None = None) -> None:
    """Upsert a StandingSeries. CAS-guarded via expected_version
    when set; raises ConcurrencyConflict on stale version."""
    from botocore.exceptions import ClientError
    if expected_version is not None:
        series.version = (expected_version or 0) + 1
    item = dataclasses.asdict(series) | {
        "PK": _app_pk(series.app_id),
        "SK": _series_sk(series.series_id),
    }
    kwargs = {"Item": item}
    if expected_version is not None:
        kwargs["ConditionExpression"] = (
            "attribute_not_exists(version) OR version = :expected")
        kwargs["ExpressionAttributeValues"] = {":expected": expected_version}
    try:
        _table().put_item(**kwargs)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise ConcurrencyConflict(
                f"standing_series {series.series_id} was modified "
                f"by someone else") from e
        raise


def get_standing_series_for_app(app_id: str) -> StandingSeries | None:
    """Return the single StandingSeries for this app, or None if
    not yet created. One series per app for standing_event apps."""
    for item in _paginate_query(
        KeyConditionExpression=Key("PK").eq(_app_pk(app_id))
        & Key("SK").begins_with("SERIES#"),
    ):
        return _hydrate(StandingSeries, _strip_keys(item))
    return None


# ---- StandingOccurrence --------------------------------------------------

def put_standing_occurrence(occ: StandingOccurrence, *,
                            expected_version: int | None = None) -> None:
    from botocore.exceptions import ClientError
    if expected_version is not None:
        occ.version = (expected_version or 0) + 1
    item = dataclasses.asdict(occ) | {
        "PK": _app_pk(occ.app_id),
        "SK": _occ_sk(occ.iso_date, occ.occurrence_id),
    }
    kwargs = {"Item": item}
    if expected_version is not None:
        kwargs["ConditionExpression"] = (
            "attribute_not_exists(version) OR version = :expected")
        kwargs["ExpressionAttributeValues"] = {":expected": expected_version}
    try:
        _table().put_item(**kwargs)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise ConcurrencyConflict(
                f"standing_occurrence {occ.occurrence_id} was modified "
                f"by someone else") from e
        raise


def list_standing_occurrences(app_id: str, *, from_date: str = "",
                              to_date: str = "9999-99-99"
                              ) -> Iterator[StandingOccurrence]:
    """Yield occurrences in a date range. SK is ``OCC#<iso_date>#...``
    so a range scan on SK between ``OCC#<from>`` and ``OCC#<to>``
    returns them in date order."""
    kcx = (Key("PK").eq(_app_pk(app_id))
           & Key("SK").between(f"OCC#{from_date}",
                               f"OCC#{to_date}#~"))
    for item in _paginate_query(KeyConditionExpression=kcx):
        # _paginate_query also returns RSVP items (SK starts with
        # OCC#<occ_id>#RSVP#...). Filter to plain occurrence rows —
        # those have exactly one "#" separator after OCC# (iso_date).
        sk = item.get("SK", "")
        if sk.startswith("OCC#") and "RSVP#" not in sk:
            yield _hydrate(StandingOccurrence, _strip_keys(item))


def get_standing_occurrence(app_id: str, iso_date: str,
                            occurrence_id: str
                            ) -> StandingOccurrence | None:
    resp = _table().get_item(Key={
        "PK": _app_pk(app_id),
        "SK": _occ_sk(iso_date, occurrence_id),
    })
    item = resp.get("Item")
    return _hydrate(StandingOccurrence, _strip_keys(item)) if item else None


# ---- StandingRSVP --------------------------------------------------------

def put_standing_rsvp(rsvp: StandingRSVP, *,
                      expected_version: int | None = None) -> None:
    from botocore.exceptions import ClientError
    if expected_version is not None:
        rsvp.version = (expected_version or 0) + 1
    item = dataclasses.asdict(rsvp) | {
        "PK": _app_pk(rsvp.app_id),
        "SK": _occ_rsvp_sk(rsvp.occurrence_id, rsvp.user_id),
        "GSI1PK": f"URSVP#{rsvp.user_id}",
        "GSI1SK": f"APP#{rsvp.app_id}#{rsvp.occurrence_id}",
    }
    kwargs = {"Item": item}
    if expected_version is not None:
        kwargs["ConditionExpression"] = (
            "attribute_not_exists(version) OR version = :expected")
        kwargs["ExpressionAttributeValues"] = {":expected": expected_version}
    try:
        _table().put_item(**kwargs)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise ConcurrencyConflict(
                f"standing_rsvp {rsvp.rsvp_id} was modified "
                f"by someone else") from e
        raise


def list_standing_rsvps_for_occurrence(
        app_id: str, occurrence_id: str) -> Iterator[StandingRSVP]:
    for item in _paginate_query(
        KeyConditionExpression=Key("PK").eq(_app_pk(app_id))
        & Key("SK").begins_with(f"OCC#{occurrence_id}#RSVP#"),
    ):
        yield _hydrate(StandingRSVP, _strip_keys(item))


def get_standing_rsvp(app_id: str, occurrence_id: str,
                      user_id: str) -> StandingRSVP | None:
    """One member's attendance response for an occurrence, or None if
    they haven't responded yet."""
    resp = _table().get_item(Key={
        "PK": _app_pk(app_id),
        "SK": _occ_rsvp_sk(occurrence_id, user_id),
    })
    item = resp.get("Item")
    return _hydrate(StandingRSVP, _strip_keys(item)) if item else None


# ---- FlexibleSeries ------------------------------------------------------

def put_flexible_series(series: FlexibleSeries, *,
                        expected_version: int | None = None) -> None:
    from botocore.exceptions import ClientError
    if expected_version is not None:
        series.version = (expected_version or 0) + 1
    item = dataclasses.asdict(series) | {
        "PK": _app_pk(series.app_id),
        "SK": _series_sk(series.series_id),
    }
    kwargs = {"Item": item}
    if expected_version is not None:
        kwargs["ConditionExpression"] = (
            "attribute_not_exists(version) OR version = :expected")
        kwargs["ExpressionAttributeValues"] = {":expected": expected_version}
    try:
        _table().put_item(**kwargs)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise ConcurrencyConflict(
                f"flexible_series {series.series_id} was modified "
                f"by someone else") from e
        raise


def get_flexible_series_for_app(app_id: str) -> FlexibleSeries | None:
    for item in _paginate_query(
        KeyConditionExpression=Key("PK").eq(_app_pk(app_id))
        & Key("SK").begins_with("SERIES#"),
    ):
        return _hydrate(FlexibleSeries, _strip_keys(item))
    return None


# ---- FlexibleEvent -------------------------------------------------------

def put_flexible_event(evt: FlexibleEvent, *,
                       expected_version: int | None = None) -> None:
    from botocore.exceptions import ClientError
    if expected_version is not None:
        evt.version = (expected_version or 0) + 1
    item = dataclasses.asdict(evt) | {
        "PK": _app_pk(evt.app_id),
        "SK": _flex_event_sk(evt.event_id),
        "GSI1PK": f"STATE#{evt.state}",
        "GSI1SK": f"APP#{evt.app_id}#{evt.event_id}",
    }
    kwargs = {"Item": item}
    if expected_version is not None:
        kwargs["ConditionExpression"] = (
            "attribute_not_exists(version) OR version = :expected")
        kwargs["ExpressionAttributeValues"] = {":expected": expected_version}
    try:
        _table().put_item(**kwargs)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise ConcurrencyConflict(
                f"flexible_event {evt.event_id} was modified "
                f"by someone else") from e
        raise


def get_flexible_event(app_id: str, event_id: str) -> FlexibleEvent | None:
    resp = _table().get_item(Key={
        "PK": _app_pk(app_id), "SK": _flex_event_sk(event_id),
    })
    item = resp.get("Item")
    return _hydrate(FlexibleEvent, _strip_keys(item)) if item else None


def list_flexible_events(app_id: str,
                         include_merged: bool = False) -> Iterator[FlexibleEvent]:
    """All FlexibleEvent rows for an app, regardless of state.

    Events merged into another are omitted unless ``include_merged``: a
    tombstone is not a thing an AA should see in a list or a state scan —
    it exists only so its already-mailed links still resolve.
    """
    for item in _paginate_query(
        KeyConditionExpression=Key("PK").eq(_app_pk(app_id))
        & Key("SK").begins_with("EVT#"),
    ):
        # Same partition holds OPT and RSVP rows; filter to event
        # meta rows (those have no further "#" segments after EVT#<id>).
        sk = item.get("SK", "")
        parts = sk.split("#")
        if len(parts) == 2 and parts[0] == "EVT":
            evt = _hydrate(FlexibleEvent, _strip_keys(item))
            if evt.merged_into and not include_merged:
                continue
            yield evt


# Depth cap on merged_into chains. A merge collapses chains (it repoints any
# existing tombstones at the new survivor), so a healthy chain is 1 hop; this
# only bounds the damage from a bad row.
_MAX_MERGE_HOPS = 8


def resolve_merged_event(app_id: str, event_id: str) -> str:
    """Follow ``merged_into`` to the surviving event id.

    Returns ``event_id`` unchanged when it isn't a tombstone (the common
    case, one extra get). Cycle- and depth-guarded: a malformed chain
    returns the last id reached rather than looping forever, because this
    sits on the public magic-link path — a member with a good link must not
    eat an exception over a bad row elsewhere.
    """
    seen: set[str] = set()
    eid = event_id
    for _ in range(_MAX_MERGE_HOPS):
        if eid in seen:
            return eid
        seen.add(eid)
        evt = get_flexible_event(app_id, eid)
        if evt is None or not evt.merged_into:
            return eid
        eid = evt.merged_into
    return eid


def delete_flexible_event(app_id: str, event_id: str) -> int:
    """Delete a flexible event AND every row under it (poll options, magic-link
    tokens, RSVPs) — they all share ``PK=APP#<aid>, SK begins_with EVT#<eid>``.
    Used when an AA removes a poll that was never sent. Returns rows deleted."""
    rows = list(_paginate_query(
        KeyConditionExpression=Key("PK").eq(_app_pk(app_id))
        & Key("SK").begins_with(f"EVT#{event_id}"),
    ))
    with _table().batch_writer() as batch:
        for it in rows:
            batch.delete_item(Key={"PK": it["PK"], "SK": it["SK"]})
    return len(rows)


# ---- FlexiblePollOption --------------------------------------------------

def put_flexible_poll_option(opt: FlexiblePollOption) -> None:
    """Unconditional put — options are created in bulk on poll open
    and removed wholesale on poll close. No concurrent edit risk."""
    item = dataclasses.asdict(opt) | {
        "PK": _app_pk(opt.app_id),
        "SK": _flex_option_sk(opt.event_id, opt.sort_key, opt.option_id),
    }
    _table().put_item(Item=item)


def list_flexible_poll_options(
        app_id: str, event_id: str) -> Iterator[FlexiblePollOption]:
    """Options for one event, sorted by sort_key via the SK prefix."""
    for item in _paginate_query(
        KeyConditionExpression=Key("PK").eq(_app_pk(app_id))
        & Key("SK").begins_with(f"EVT#{event_id}#OPT#"),
    ):
        yield _hydrate(FlexiblePollOption, _strip_keys(item))


def delete_flexible_poll_options(app_id: str, event_id: str) -> int:
    """Wipe all options for an event (called on poll close — only
    the winning date survives, stored on FlexibleEvent.winning_date)."""
    n = 0
    with _table().batch_writer() as bw:
        for item in _paginate_query(
            KeyConditionExpression=Key("PK").eq(_app_pk(app_id))
            & Key("SK").begins_with(f"EVT#{event_id}#OPT#"),
        ):
            bw.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
            n += 1
    return n


def delete_flexible_poll_option(app_id: str, event_id: str,
                                option_id: str) -> bool:
    """Remove ONE candidate date from an open poll. Votes keyed on the other
    options are untouched; a vote on this option simply becomes inert."""
    for opt in list_flexible_poll_options(app_id, event_id):
        if opt.option_id == option_id:
            _table().delete_item(Key={
                "PK": _app_pk(app_id),
                "SK": _flex_option_sk(event_id, opt.sort_key, opt.option_id)})
            return True
    return False


# ---- FlexibleRSVP --------------------------------------------------------

def put_flexible_rsvp(rsvp: FlexibleRSVP, *,
                      expected_version: int | None = None) -> None:
    from botocore.exceptions import ClientError
    if expected_version is not None:
        rsvp.version = (expected_version or 0) + 1
    item = dataclasses.asdict(rsvp) | {
        "PK": _app_pk(rsvp.app_id),
        "SK": _flex_rsvp_sk(rsvp.event_id, rsvp.user_id),
        "GSI1PK": f"URSVP#{rsvp.user_id}",
        "GSI1SK": f"APP#{rsvp.app_id}#{rsvp.event_id}",
    }
    kwargs = {"Item": item}
    if expected_version is not None:
        kwargs["ConditionExpression"] = (
            "attribute_not_exists(version) OR version = :expected")
        kwargs["ExpressionAttributeValues"] = {":expected": expected_version}
    try:
        _table().put_item(**kwargs)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise ConcurrencyConflict(
                f"flexible_rsvp {rsvp.rsvp_id} was modified "
                f"by someone else") from e
        raise


def get_flexible_rsvp(app_id: str, event_id: str,
                      user_id: str) -> FlexibleRSVP | None:
    resp = _table().get_item(Key={
        "PK": _app_pk(app_id),
        "SK": _flex_rsvp_sk(event_id, user_id),
    })
    item = resp.get("Item")
    return _hydrate(FlexibleRSVP, _strip_keys(item)) if item else None


def list_flexible_rsvps(app_id: str,
                        event_id: str) -> Iterator[FlexibleRSVP]:
    for item in _paginate_query(
        KeyConditionExpression=Key("PK").eq(_app_pk(app_id))
        & Key("SK").begins_with(f"EVT#{event_id}#RSVP#"),
    ):
        yield _hydrate(FlexibleRSVP, _strip_keys(item))


# ---- EventToken (passwordless poll magic-links) --------------------------

def _event_token_sk(event_id: str, user_id: str) -> str:
    """``EVT#<eid>#TOK#<uid>`` — one token per (event, user) so re-sending
    the poll email upserts the same row rather than minting duplicates."""
    return f"EVT#{event_id}#TOK#{user_id}"


def put_event_token(tok: EventToken) -> None:
    """Upsert a magic-link token. GSI1 keys it by the RAW token
    (``TOK#<token>``) for an O(1) lookup from the public no-auth route.
    Also writes a numeric ``ttl`` (epoch of expires_at) so DynamoDB TTL can
    reap it once enabled; until then validity is enforced in code."""
    item = dataclasses.asdict(tok) | {
        "PK": _app_pk(tok.app_id),
        "SK": _event_token_sk(tok.event_id, tok.user_id),
        "GSI1PK": f"TOK#{tok.token}",
        "GSI1SK": f"APP#{tok.app_id}#{tok.event_id}",
    }
    try:
        item["ttl"] = int(dt.datetime.fromisoformat(tok.expires_at).timestamp())
    except (ValueError, TypeError):
        pass
    _table().put_item(Item=item)


def get_event_token_by_value(token: str) -> EventToken | None:
    """Resolve a raw magic-link token to its EventToken via GSI1 (O(1),
    no scan) — the hot path for every public poll request."""
    resp = _table().query(
        IndexName="GSI1",
        KeyConditionExpression=Key("GSI1PK").eq(f"TOK#{token}"),
        Limit=1,
    )
    items = resp.get("Items", [])
    return _hydrate(EventToken, _strip_keys(items[0])) if items else None


def get_event_token(app_id: str, event_id: str,
                    user_id: str) -> EventToken | None:
    """Existing token for (event, user), if any — lets a poll re-send
    reuse the same link instead of minting a second one."""
    resp = _table().get_item(Key={
        "PK": _app_pk(app_id),
        "SK": _event_token_sk(event_id, user_id),
    })
    item = resp.get("Item")
    return _hydrate(EventToken, _strip_keys(item)) if item else None


def list_event_tokens(app_id: str, event_id: str) -> Iterator[EventToken]:
    """All minted tokens for an event (who already has a link)."""
    for item in _paginate_query(
        KeyConditionExpression=Key("PK").eq(_app_pk(app_id))
        & Key("SK").begins_with(f"EVT#{event_id}#TOK#"),
    ):
        yield _hydrate(EventToken, _strip_keys(item))


def revoke_event_tokens_for_user(app_id: str, user_id: str) -> int:
    """Revoke EVERY event token this user holds in the app. Returns the count.

    Opt-out is group-level, so revoking only the link they happened to click
    would leave any other live event's link working — which reads to the
    member as "I asked to be left alone and it ignored me". A member can
    legitimately hold links to several concurrent events.
    """
    n = 0
    for item in _paginate_query(
        KeyConditionExpression=Key("PK").eq(_app_pk(app_id))
        & Key("SK").begins_with("EVT#"),
    ):
        sk = item.get("SK", "")
        parts = sk.split("#")
        # EVT#<eid>#TOK#<uid>
        if len(parts) == 4 and parts[2] == "TOK" and parts[3] == user_id:
            _table().update_item(
                Key={"PK": item["PK"], "SK": sk},
                UpdateExpression="SET revoked = :r",
                ExpressionAttributeValues={":r": True},
            )
            n += 1
    return n


def revoke_event_token(app_id: str, event_id: str, user_id: str) -> None:
    """Set a token as revoked (on group-level opt-out). The public route
    rejects revoked tokens. No-op if no token exists."""
    from botocore.exceptions import ClientError
    try:
        _table().update_item(
            Key={"PK": _app_pk(app_id),
                 "SK": _event_token_sk(event_id, user_id)},
            UpdateExpression="SET revoked = :r",
            ExpressionAttributeValues={":r": True},
            ConditionExpression="attribute_exists(SK)",
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == \
                "ConditionalCheckFailedException":
            return
        raise
