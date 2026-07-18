"""Schedule publishing — broadcast emails + state machine + reminders.

This module owns the **publish** operation, which is the main user-visible
event in the app: an admin clicks "Publish this schedule" and every member
of the application receives a personalized broadcast notice; assigned
members receive one additional calendar invite per assigned slot; and a
row of reminder Notifications is scheduled in DynamoDB.

The flow has four observable side effects, in this order:

    1. **State transition (lock).** ``Schedule.state`` goes from
       ``draft`` to ``publishing`` (atomic conditional update). This is
       the lock — see "publishing lock" below.

    2. **Broadcast send.** One ``provider.send(...)`` per member who has
       deliverable email. Members with ``email_undeliverable``, ``channel
       == "none"``, or no email are skipped silently. The broadcast
       carries the full month-grid HTML table and per-recipient prose;
       it does NOT carry any .ics attachment. Deliberately uniform
       across clients — see "why split broadcast from invites" below.

    3. **Invite sends.** For each assigned (user, slot) pair, one
       additional ``provider.send(...)`` with a single-event ``.ics``
       (``METHOD:REQUEST``, one VEVENT). A user with three assignments
       receives one broadcast plus three invite emails.

    4. **Reminder materialization.** For each future-dated assignment the
       member's ``lead_times_minutes`` list is unrolled into one
       ``Notification`` per lead. The notifier Lambda picks these up off
       a DDB-stream-fed EventBridge schedule.

    5. **State commit.** ``Schedule.state`` goes from ``publishing`` to
       ``published`` (atomic conditional update). At this point the
       broadcast, invites, and reminders are durable and visible to
       members.

Why split broadcast from invites
--------------------------------

Email clients render multi-event ``.ics`` attachments inconsistently:
Apple Mail collapses them under a single banner, Outlook desktop
sometimes stacks them, Gmail imports them silently into Google
Calendar with no inline RSVP. The "one invite = one calendar entry"
mapping removes that variability — every member sees the broadcast
notice as a normal email, then a sequence of individual calendar
invites with the per-event accept/decline affordance their client
expects.

Publishing lock — atomicity guarantee
-------------------------------------

The transient ``publishing`` state is the lock that makes the whole
publish flow act as a unit, even though we can't make SES sends and
DDB writes part of a single transaction.

Invariants:

    - At most one publish for a given schedule can be in flight at any
      time. Concurrent publishes race on the ``draft -> publishing``
      conditional update; only one wins, the other gets
      ``ConcurrencyConflict``.
    - An unpublish during a publish has no effect on data. Unpublish
      requires ``from_state="published"``; while we're in
      ``"publishing"``, that condition fails. The user gets a
      friendly error and the publish completes normally.
    - A publish handler crash leaves the schedule stuck in
      ``"publishing"`` (no automatic timeout). The admin recovery path
      (``cli.py schedules force-reset`` or a future admin UI button)
      flips it back to ``draft``. This is by design: silently
      auto-recovering could collide with a recovering publish that
      actually IS still running on another invocation.

Why split into ``plan_publish`` / ``plan_invites`` + ``publish_schedule``?
The plan functions are pure (in the "no side effects" sense — they just
read DDB) and so are unit-testable + dry-runnable. ``publish_schedule``
wraps them with the state transition, the actual sends, and reminder
writes.

Republishing: ``unpublish_schedule`` flips state back to ``draft`` and
clears pending Notifications, but leaves assignments and the schedule
record itself intact. A subsequent ``publish_schedule`` re-runs the whole
broadcast + invite + reminder materialization. Calendar UIDs on the
single-event ``.ics`` are stable (slot_id + user_id based, see
``ical.make_event_ics``), so re-sent invites land as updates to the
existing calendar events rather than duplicates.

Tested by:
    tests/core/test_publishing_pure.py   (pure helpers + _build_email)
    tests/core/test_publishing_flow.py   (plan_publish, publish_schedule,
                                          idempotency, unpublish, reminders)
"""
from __future__ import annotations

import datetime as dt
import html
import os
from dataclasses import dataclass

from . import db
from .ical import make_event_ics
from .models import (
    Application, Assignment, Community, Membership, Notification,
    Schedule, Slot, User,
)

DOMAIN_NAME = os.environ.get("DOMAIN_NAME", "community.example.org")


def _community_host(community) -> str:
    """Hostname for URLs in publish/invite email bodies.

    Mirrors notifier._community_host. publish_schedule and friends
    run in the web Lambda where DOMAIN_NAME env already matches the
    stack, so for prod-only / beta-only deployments this returns the
    env value unchanged. But threading Community.public_url through
    keeps the multi-community pattern uniform across notifier, web,
    and inbound, so future code paths don't accidentally regress to
    the wrong host."""
    if community is not None and getattr(community, "public_url", None):
        return community.public_url
    return DOMAIN_NAME


def _h(s: str) -> str:
    """Shortcut for ``html.escape(s, quote=True)``.

    Used at every f-string interpolation point in the HTML body so
    admin/user-supplied text (slot.name, user.name, app.name,
    event_noun, arrival_label) cannot inject HTML/JS into recipients'
    HTML-rendering email clients.
    """
    return html.escape(s or "", quote=True)

# Two-letter weekday + month-name lookup tables. Kept module-level so they're
# allocated once at import time rather than per-call.
_DAY_LABEL = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri", 5: "Sat", 6: "Sun"}
_MONTH_LABEL = {1: "January", 2: "February", 3: "March", 4: "April",
                5: "May", 6: "June", 7: "July", 8: "August",
                9: "September", 10: "October", 11: "November", 12: "December"}


def _fmt_time(hhmm: str) -> str:
    """Convert 24-hour ``"HH:MM"`` to 12-hour ``"H:MM AM/PM"`` for emails.

    The schedule edit form stores times in 24-hour form because that's
    what the HTML5 ``<input type="time">`` returns. Emails read more
    naturally in 12-hour form, so this helper handles the conversion
    just before rendering.

    Examples::

        _fmt_time("08:00") == "8:00 AM"
        _fmt_time("14:30") == "2:30 PM"
        _fmt_time("00:15") == "12:15 AM"   # midnight bucket
        _fmt_time("12:00") == "12:00 PM"   # noon bucket
    """
    h, m = (int(x) for x in hhmm.split(":"))
    # Range guard (security fix D20). Without it, malformed input like
    # "25:00" rendered as "13:00 PM" instead of failing loud.
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"_fmt_time: out-of-range {hhmm!r}")
    suffix = "AM" if h < 12 else "PM"
    h12 = h if 1 <= h <= 12 else (12 if h == 0 else h - 12)
    return f"{h12}:{m:02d} {suffix}"


def _fmt_date(iso_date: str) -> str:
    """Convert ISO ``"YYYY-MM-DD"`` to ``"DayAbbr, Month D"`` for email lists.

    No year is rendered — the email subject already says the year, and
    repeating it on every bullet point clutters the body.

    Example: ``_fmt_date("2026-05-31") == "Sun, May 31"``.
    """
    y, mo, d = (int(x) for x in iso_date.split("-"))
    date = dt.date(y, mo, d)
    return f"{_DAY_LABEL[date.weekday()]}, {_MONTH_LABEL[mo]} {d}"


def _arrival_hhmm(slot: Slot) -> str:
    """Return the arrival time as ``"HH:MM"`` (24-hour).

    Arrival = ``slot.start_time`` minus ``slot.arrival_offset_minutes``
    (the "arrive early" field on the slot template). Used by both the
    broadcast email body and the .ics ``DESCRIPTION`` ("please arrive
    by 7:50 AM"). Computed via a throwaway ``datetime(2000,1,1,...)``
    so timedelta arithmetic handles minute rollover and underflow
    automatically — no manual modular arithmetic.
    """
    h, m = (int(x) for x in slot.start_time.split(":"))
    base = dt.datetime(2000, 1, 1, h, m)
    arrival = base - dt.timedelta(minutes=slot.arrival_offset_minutes)
    return f"{arrival.hour:02d}:{arrival.minute:02d}"


def _month_human(yyyy_mm: str) -> str:
    """Convert ``"YYYY-MM"`` to ``"MonthName YYYY"`` for prose.

    Example: ``_month_human("2026-05") == "May 2026"``. Used in the
    subject line and the opening sentence of the broadcast body.
    """
    y, m = yyyy_mm.split("-")
    return f"{_MONTH_LABEL[int(m)]} {y}"


@dataclass
class PublishPlan:
    """One planned broadcast email — the unit of work ``plan_publish`` returns.

    Holds everything ``publish_schedule`` needs to call ``provider.send``
    without touching the database again:

        - ``user``       : recipient (used for email + user_id audit trail)
        - ``slots``      : their assignments for the month (sorted)
        - ``subject``    : pre-rendered email subject
        - ``body_text``  : plain-text body (always present)
        - ``body_html``  : HTML body (present whenever body_text is)

    The broadcast carries no ``.ics`` attachment — calendar invites are
    sent separately as one email per assigned slot, see ``InvitePlan``.

    Splitting plan-from-send like this makes the broadcast unit-testable:
    ``plan_publish`` is pure-ish (only DDB reads) and deterministic given
    its inputs. The actual ``provider.send`` is then a thin shim.
    """
    user: User
    slots: list[Slot]
    subject: str
    body_text: str
    body_html: str | None = None


@dataclass
class InvitePlan:
    """One planned calendar-invite email — produced by ``plan_invites``.

    One ``InvitePlan`` per (assigned user, slot) pair. Each carries a
    single-event ``.ics`` (``METHOD:REQUEST``, one VEVENT) so every
    client renders it as a standalone accept/decline invitation.

        - ``user``        : assignee
        - ``slot``        : the slot they're being invited to
        - ``subject``     : pre-rendered email subject
        - ``body_text``   : plain-text body
        - ``body_html``   : HTML body
        - ``ics_content`` : single-event VCALENDAR string
    """
    user: User
    slot: Slot
    subject: str
    body_text: str
    body_html: str
    ics_content: str


def _lead_time_desc(minutes: list[int]) -> str:
    """Render a list of reminder lead times as a human phrase.

    The publish email tells each user when they'll get reminded; this
    helper turns the raw minute counts on ``User.lead_times_minutes``
    into natural language. Sorted largest-first because that's how a
    person would naturally describe a schedule ("a day and 2 hours
    before", not "120 minutes and a day").

    Examples::

        _lead_time_desc([1440, 120]) == "1 day and 2 hours"
        _lead_time_desc([60])        == "1 hour"
        _lead_time_desc([30])        == "30 minutes"
        _lead_time_desc([])          == "2 hours"   # safety default

    Units thresholds: >= 1440min => days, >= 60min => hours, else minutes.
    Pluralization is naive: "1 day" / "2 days" only — no irregulars.
    """
    parts = []
    for m in sorted(minutes, reverse=True):
        if m >= 1440:
            days = m // 1440
            parts.append(f"{days} day{'s' if days > 1 else ''}")
        elif m >= 60:
            hours = m // 60
            parts.append(f"{hours} hour{'s' if hours > 1 else ''}")
        else:
            parts.append(f"{m} minute{'s' if m > 1 else ''}")
    return " and ".join(parts) if parts else "2 hours"


def _build_email(*, user: User, community: Community, app: Application,
                 yyyy_mm: str, slots: list[Slot],
                 table_html: str = "") -> tuple[str, str, str]:
    """Render the (subject, body_text, body_html) for one user's broadcast.

    Two code paths inside, branching on whether the user has any slots:

        - **Has assignments**: opening paragraph names how many, a bullet
          list of (date, slot name, arrival/start times), reminder lead
          description, withdraw/trade pointer, signature.
        - **No assignments**: short note that the schedule went out + a
          pointer to take open slots.

    Both paths embed ``table_html`` (the full schedule table, supplied by
    the caller from ``schedule_email``) at the bottom of the HTML body
    so every recipient sees the full month, not just their own row.

    Returns a triple of (subject, body_text, body_html). The caller
    passes these straight to ``provider.send``.

    Why kwargs-only (``*,``): three of the params are dataclasses that
    look similar at call sites (``user, community, app``). Forcing
    keyword args makes calls self-documenting and prevents accidental
    swaps that the type checker can't catch (all three are objects).
    """
    month = _month_human(yyyy_mm)
    org_name = app.name
    site_url = f"https://{_community_host(community)}/your-schedule"
    lead_desc = _lead_time_desc(user.lead_times_minutes or [1440, 120])
    event_type = app.event_noun or "event"
    subject = f"{org_name} -- {month} schedule published"
    # arrival_label is on every Application now, but the hasattr guard
    # protects callers in test code that build minimal stub objects.
    arr_label = app.arrival_label if hasattr(app, 'arrival_label') and app.arrival_label else ""
    if slots:
        def _slot_line(s: Slot) -> str:
            base = f"  - {_fmt_date(s.local_date)} -- {s.name}"
            if arr_label and s.arrival_offset_minutes:
                return f"{base} ({arr_label} {_fmt_time(_arrival_hhmm(s))}, starts {_fmt_time(s.start_time)})"
            return f"{base} (starts {_fmt_time(s.start_time)})"
        rows = "\n".join(_slot_line(s) for s in slots)
        body_text = (
            f"Hi {user.name},\n\n"
            f"The {month} schedule has been published. "
            f"You are assigned to the following {len(slots)} "
            f"slot{'s' if len(slots) != 1 else ''}:\n\n"
            f"{rows}\n\n"
            f"You will receive reminder emails {lead_desc} before each "
            f"event. You can change your reminder settings at:\n"
            f"  {site_url}\n\n"
            f"If you need to withdraw from any of these, you can decline "
            f"the calendar event or visit the link above to release your "
            f"slot, take additional slots, or trade for a different one. "
            f"If you withdraw, others covering that {event_type} and "
            f"administrators will be notified, as well as others from "
            f"your cohort, encouraging someone to sign up.\n\n"
            f"-- {org_name}\n"
        )
        # Each slot line goes through _h — slot.name and arrival/start
        # times can flow in admin-controlled text.
        slot_rows_html = "".join(
            f"<li>{_h(_slot_line(s).strip().lstrip('-').strip())}</li>"
            for s in slots)
        body_html = (
            f'<div style="font-family:Arial,sans-serif;font-size:14px">'
            f'<p>Hi {_h(user.name)},</p>'
            f'<p>The {_h(month)} schedule has been published. '
            f'You are assigned to the following '
            f'{len(slots)} slot{"s" if len(slots) != 1 else ""}:</p>'
            f'<ul>{slot_rows_html}</ul>'
            f'<p>You will receive reminder emails {_h(lead_desc)} before each '
            f'event. You can change your reminder settings at '
            f'<a href="{_h(site_url)}">{_h(site_url)}</a>.</p>'
            f'<p>If you need to withdraw from any of these, you can decline '
            f'the calendar event or visit the link above to release your '
            f'slot, take additional slots, or trade for a different one.</p>'
            f'<p>Full schedule below:</p>'
            f'{table_html}'  # Already-rendered table (escapes within it).
            f'<p style="margin-top:16px">-- {_h(org_name)}</p>'
            f'</div>'
        )
    else:
        body_text = (
            f"Hi {user.name},\n\n"
            f"The {month} schedule has been published. "
            f"You don't have any assignments this month, but additional "
            f"slots for each {event_type} are available if "
            f"you'd like to ensure complete coverage:\n"
            f"  {site_url}\n\n"
            f"-- {org_name}\n"
        )
        body_html = (
            f'<div style="font-family:Arial,sans-serif;font-size:14px">'
            f'<p>Hi {_h(user.name)},</p>'
            f'<p>The {_h(month)} schedule has been published. '
            f'You don\'t have any assignments this month, but additional '
            f'slots for each {_h(event_type)} are available if you\'d like to '
            f'help with coverage: <a href="{_h(site_url)}">{_h(site_url)}</a>.</p>'
            f'<p>Full schedule below:</p>'
            f'{table_html}'
            f'<p style="margin-top:16px">-- {_h(org_name)}</p>'
            f'</div>'
        )
    return subject, body_text, body_html


def plan_publish(community: Community, app: Application,
                 yyyy_mm: str) -> list[PublishPlan]:
    """Build (don't send) the per-recipient broadcast PublishPlans for a month.

    Reads from DDB:
        - all slots for ``app.app_id`` + ``yyyy_mm``
        - all assignments for the same scope
        - all memberships for the app (defines who's eligible)
        - all users in the community (so we can join on user_id)

    For every community user who is a member of this app AND has
    deliverable email (``channel != "none"``, ``email_undeliverable``
    is False, ``email`` is set), build a PublishPlan with:

        - subject + body via ``_build_email``
        - the user's slots, sorted by (date, start_time)

    Calendar invites are NOT attached to the broadcast — they're sent
    separately via ``plan_invites`` so every client renders one
    accept/decline UI per event regardless of how it handles multi-
    event .ics files.

    Note: skip-filters are silent. A user with ``channel == "none"`` does
    not appear in the returned plans and won't cause an email send. This
    is the safety hatch for the 18 imported users who were configured
    with notifications disabled during the initial import.

    The schedule_email table HTML (full month, all members) is generated
    once here and embedded in every recipient's HTML body — that way the
    expensive grid render happens once per publish, not once per user.
    """
    from . import schedule_email
    table_html = schedule_email.generate_schedule_table_html(community, app, yyyy_mm)
    slots_by_id = {s.slot_id: s for s in db.list_slots(app.app_id, yyyy_mm)}
    asgns_by_user: dict[str, list[Assignment]] = {}
    for a in db.list_assignments_for_month(app.app_id, yyyy_mm):
        asgns_by_user.setdefault(a.user_id, []).append(a)

    member_user_ids = {m.user_id for m in db.list_memberships_for_app(app.app_id)}
    plans: list[PublishPlan] = []
    for user in db.list_users(community.community_id):
        if user.user_id not in member_user_ids:
            continue
        if user.email_undeliverable or not user.email or user.channel == "none":
            continue
        user_asgns = asgns_by_user.get(user.user_id, [])
        user_slots = [slots_by_id[a.slot_id] for a in user_asgns
                      if a.slot_id in slots_by_id]
        user_slots.sort(key=lambda s: (s.local_date, s.start_time))
        subject, body_text, body_html = _build_email(
            user=user, community=community, app=app,
            yyyy_mm=yyyy_mm, slots=user_slots,
            table_html=table_html,
        )
        plans.append(PublishPlan(user=user, slots=user_slots,
                                subject=subject, body_text=body_text,
                                body_html=body_html))
    return plans


def _build_invite_email(*, user: User, app: Application,
                        slot: Slot,
                        host: str = DOMAIN_NAME) -> tuple[str, str, str]:
    """Render (subject, body_text, body_html) for a single calendar invite.

    Short and focused: the recipient already got the full schedule via
    the broadcast — this email exists to deliver one calendar event.
    The body identifies the slot and points back at the self-service
    page in case they need to release it.

    `host` is the URL hostname to embed in the body — pass
    ``_community_host(community)`` so multi-community shared notifiers
    route each user back to the right stack.
    """
    org_name = app.name
    site_url = f"https://{host}/your-schedule"
    arr_label = app.arrival_label if hasattr(app, 'arrival_label') and app.arrival_label else ""
    when = f"{_fmt_date(slot.local_date)} at {_fmt_time(slot.start_time)}"
    arrival_line = ""
    if arr_label and slot.arrival_offset_minutes:
        arrival_line = (
            f"({arr_label} {_fmt_time(_arrival_hhmm(slot))})")
    subject = f"{org_name}: {slot.name} -- {_fmt_date(slot.local_date)}"
    body_text = (
        f"Hi {user.name},\n\n"
        f"Calendar invitation for your {org_name} assignment:\n\n"
        f"  {slot.name}\n"
        f"  {when}\n"
        + (f"  {arrival_line}\n" if arrival_line else "")
        + f"\n"
        f"The invitation is attached. If you need to release this "
        f"assignment, decline the calendar event or visit:\n"
        f"  {site_url}\n\n"
        f"-- {org_name}\n"
    )
    body_html = (
        f'<div style="font-family:Arial,sans-serif;font-size:14px">'
        f'<p>Hi {_h(user.name)},</p>'
        f'<p>Calendar invitation for your {_h(org_name)} assignment:</p>'
        f'<p style="margin-left:1em">'
        f'<strong>{_h(slot.name)}</strong><br>'
        f'{_h(when)}'
        + (f'<br>{_h(arrival_line)}' if arrival_line else "")
        + f'</p>'
        f'<p>The invitation is attached. If you need to release this '
        f'assignment, decline the calendar event or visit '
        f'<a href="{_h(site_url)}">{_h(site_url)}</a>.</p>'
        f'<p style="margin-top:16px">-- {_h(org_name)}</p>'
        f'</div>'
    )
    return subject, body_text, body_html


def plan_invites(community: Community, app: Application,
                 yyyy_mm: str) -> list[InvitePlan]:
    """Build (don't send) one InvitePlan per (assignee, slot) pair.

    Mirrors ``plan_publish``'s skip-filters (email_undeliverable,
    channel="none", missing email, non-member) so callers can rely on
    "every InvitePlan corresponds to a send that will actually go out."

    Each InvitePlan carries a single-event ``.ics`` via
    ``ical.make_event_ics``. The UID is per-(slot, user, domain) so
    republish updates the same calendar entry rather than duplicating.
    """
    slots_by_id = {s.slot_id: s for s in db.list_slots(app.app_id, yyyy_mm)}
    member_user_ids = {m.user_id for m in db.list_memberships_for_app(app.app_id)}
    users_by_id = {u.user_id: u
                   for u in db.list_users(community.community_id)}
    tz_name = (app.default_timezone or community.default_timezone
               or "America/New_York")
    arr_label = (app.arrival_label if hasattr(app, 'arrival_label')
                 and app.arrival_label else "")

    invites: list[InvitePlan] = []
    for a in db.list_assignments_for_month(app.app_id, yyyy_mm):
        user = users_by_id.get(a.user_id)
        slot = slots_by_id.get(a.slot_id)
        if user is None or slot is None:
            continue
        if user.user_id not in member_user_ids:
            continue
        if user.email_undeliverable or not user.email or user.channel == "none":
            continue
        if slot.cancelled:
            continue
        arrival_text = None
        if arr_label and slot.arrival_offset_minutes:
            arrival_text = f"{arr_label} {_fmt_time(_arrival_hhmm(slot))}"
        host = _community_host(community)
        ics = make_event_ics(
            slot, user.user_id, user.email,
            domain=host, community_name=app.name,
            timezone=tz_name, arrival_text=arrival_text,
            alarm_minutes=user.calendar_alarm_minutes,
        )
        subject, body_text, body_html = _build_invite_email(
            user=user, app=app, slot=slot, host=host)
        invites.append(InvitePlan(user=user, slot=slot, subject=subject,
                                  body_text=body_text, body_html=body_html,
                                  ics_content=ics))
    invites.sort(key=lambda p: (p.user.name, p.slot.local_date,
                                p.slot.start_time))
    return invites


def publish_schedule(community: Community, app: Application, yyyy_mm: str, *,
                     dry_run: bool = False) -> dict:
    """Transition a draft schedule to published. State-only — no
    broadcast emails, no per-slot invites. Members can immediately
    see the published schedule on /your-schedule and self-signup.

    History note (#215 — 2026-06-06): previously this function ALSO
    sent the broadcast email + per-slot calendar invites in one shot.
    The implementation was refactored to decouple the two concepts: publishing means
    "open for member edits"; broadcasting (with an embedded schedule
    table + per-slot .ics for assignees) is a separate admin action
    on the send-email page. See ``send_published_schedule_broadcast``.

    Steps, in order:

        1. Load the Schedule. Raise ValueError if missing.
        2. Refuse if already published or mid-publish.
        3. **Dry-run short-circuit**: return the planned summary
           without writing anything (lets the CLI ``--dry-run`` flag
           still preview what would happen — the broadcast/invite
           counts come from ``plan_publish``/``plan_invites`` so
           admins can sanity-check before the broadcast step).
        4. **Atomic state transition**: ``draft -> publishing`` via
           ``db.transition_schedule_state``. ConcurrencyConflict on
           a contested transition.
        5. Materialize Notifications for in-future reminder times.
        6. **Atomic state commit**: ``publishing -> published``.

    Returns a dict for the CLI/web response::

        {
            "yyyy_mm": "2026-05",
            "state": "published",
            "would_send": 42,              # potential broadcast count
            "would_send_invites": 84,      # potential invite count
            "reminders_created": 168,
            "dry_run": False,
        }

    Why the state transition is two-phase (publishing → published):
    leaves the door open for a future async-send mode where the
    broadcast (now a separate action) runs in the "publishing"
    window if desired. For now the window is brief — bounded by
    reminder materialization.
    """
    sch = db.get_schedule(app.app_id, yyyy_mm)
    if sch is None:
        raise ValueError(f"schedule {yyyy_mm} not found")
    if sch.state == "publishing" and not dry_run:
        raise ValueError(
            f"schedule {yyyy_mm} is already being published (started "
            f"{sch.published_at}); wait for it to finish or force-reset "
            f"via the admin recovery path")
    if sch.state == "published" and not dry_run:
        raise ValueError(f"schedule {yyyy_mm} is already published")

    plans = plan_publish(community, app, yyyy_mm)
    invite_plans = plan_invites(community, app, yyyy_mm)
    summary: dict = {
        "yyyy_mm": yyyy_mm,
        "state": sch.state,
        "would_send": len(plans),
        "would_send_invites": len(invite_plans),
        "dry_run": dry_run,
    }
    if dry_run:
        return summary

    published_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    try:
        db.transition_schedule_state(
            app.app_id, yyyy_mm,
            from_state="draft", to_state="publishing",
            published_at=published_at,
        )
    except db.ConcurrencyConflict as e:
        raise ValueError(
            f"schedule {yyyy_mm} is not in draft state (concurrent "
            f"publish/unpublish, or already published)") from e
    sch.state = "publishing"
    sch.published_at = published_at

    ntf_count = _materialize_reminders(community, app, yyyy_mm)

    db.transition_schedule_state(
        app.app_id, yyyy_mm,
        from_state="publishing", to_state="published",
    )
    sch.state = "published"

    summary["state"] = sch.state
    summary["reminders_created"] = ntf_count
    return summary


def send_published_schedule_broadcast(
    community: Community, app: Application, yyyy_mm: str, *,
    provider, from_addr: str,
    subject_override: str | None = None,
    body_prefix_text: str | None = None,
    body_prefix_html: str | None = None,
) -> dict:
    """Send a broadcast email (schedule table embedded) + per-slot
    .ics calendar invites for an ALREADY-published schedule.

    Decoupled from ``publish_schedule`` per #215 — publishing now
    only transitions state. This function is the admin's "tell
    everyone what their schedule looks like" action, callable any
    number of times against a published schedule (typically once,
    when the admin decides the schedule is close to filled).

    Idempotency / re-send safety: the per-slot ICS UID is stable
    per (slot_id, user_id, domain) tuple (see ``ical.py``), so a
    re-send updates a previously-delivered calendar event in
    place — no duplicate events on members' calendars. Calendar
    clients typically merge silently when nothing has changed.

    Returns::

        {
            "yyyy_mm": "...",
            "would_send": len(broadcast_plans),
            "would_send_invites": len(invite_plans),
            "sent": broadcasts_actually_sent,
            "invites_sent": invites_actually_sent,
            "by_outcome": {"accepted": N, ...},
        }
    """
    sch = db.get_schedule(app.app_id, yyyy_mm)
    if sch is None:
        raise ValueError(f"schedule {yyyy_mm} not found")
    if sch.state != "published":
        raise ValueError(
            f"schedule {yyyy_mm} is in state {sch.state!r}, not "
            f"'published' — only published schedules can be broadcast")

    plans = plan_publish(community, app, yyyy_mm)
    invite_plans = plan_invites(community, app, yyyy_mm)

    by_outcome: dict[str, int] = {}
    broadcasts_sent = 0
    for p in plans:
        subj = subject_override if subject_override else p.subject
        body_text = p.body_text
        if body_prefix_text:
            body_text = (
                f"{body_prefix_text.strip()}\n\n"
                f"------------------------------------------------------------\n\n"
                f"{p.body_text}"
            )
        body_html = p.body_html
        if body_html and body_prefix_html:
            body_html = body_prefix_html + "<hr>" + body_html
        try:
            log_row = provider.send(
                community_id=community.community_id,
                from_addr=from_addr,
                to_addr=p.user.email,
                subject=subj,
                body_text=body_text,
                body_html=body_html,
                kind="publish_broadcast",
                related_user_id=p.user.user_id,
                related_app_id=app.app_id,
                related_yyyy_mm=yyyy_mm,
                ics_content=None,
            )
            by_outcome[log_row.outcome] = by_outcome.get(log_row.outcome, 0) + 1
            broadcasts_sent += 1
        except Exception:
            import logging
            logging.getLogger(__name__).exception(
                "publish_broadcast send failed for user %s", p.user.user_id)
            by_outcome["error"] = by_outcome.get("error", 0) + 1

    invites_sent = 0
    for ip in invite_plans:
        try:
            log_row = provider.send(
                community_id=community.community_id,
                from_addr=from_addr,
                to_addr=ip.user.email,
                subject=ip.subject,
                body_text=ip.body_text,
                body_html=ip.body_html,
                kind="publish_broadcast",
                related_user_id=ip.user.user_id,
                related_app_id=app.app_id,
                related_yyyy_mm=yyyy_mm,
                related_slot_id=ip.slot.slot_id,
                ics_content=ip.ics_content,
            )
            by_outcome[log_row.outcome] = by_outcome.get(log_row.outcome, 0) + 1
            invites_sent += 1
        except Exception:
            import logging
            logging.getLogger(__name__).exception(
                "publish invite send failed for user %s slot %s",
                ip.user.user_id, ip.slot.slot_id)
            by_outcome["error"] = by_outcome.get("error", 0) + 1

    return {
        "yyyy_mm": yyyy_mm,
        "would_send": len(plans),
        "would_send_invites": len(invite_plans),
        "sent": broadcasts_sent,
        "invites_sent": invites_sent,
        "by_outcome": by_outcome,
    }


def _materialize_reminders(community: Community, app: Application,
                           yyyy_mm: str) -> int:
    """Wipe and recreate the pending Notifications for a published month.

    Called from ``publish_schedule`` after the broadcast goes out. For
    every assignment in the month, expands the assignee's
    ``lead_times_minutes`` list into one Notification per future-dated
    lead. Past-dated leads are skipped silently (otherwise republishing
    a mid-month schedule would queue a reminder to fire immediately).

    Why delete first: this function is also reused on republish, where
    the previous publish's reminders may already exist. Delete-then-
    insert is the simplest way to ensure the final state matches the
    current assignments without tracking diffs.

    Skip-filters: same as ``plan_publish`` — users with
    ``email_undeliverable``, ``channel == "none"``, or no email get no
    Notifications. Cancelled slots are also skipped.

    The notifier Lambda watches the DDB stream and converts these into
    actual sends at the right time, so this function is the source of
    truth for "what reminders are queued."

    Returns the count of Notifications actually inserted (for the
    publish summary).
    """
    from zoneinfo import ZoneInfo
    tz_name = app.default_timezone or community.default_timezone
    tz = ZoneInfo(tz_name)
    default_leads = [1440, 120]

    db.delete_notifications_for_schedule(app.app_id, yyyy_mm)

    slots_by_id = {s.slot_id: s for s in db.list_slots(app.app_id, yyyy_mm)}
    users_by_id = {u.user_id: u for u in db.list_users(community.community_id)}
    # Templates by id — used to honor per-template auto_reminders. For
    # Recurring Commitments apps, weekly templates opt out (the
    # member's calendar app holds the recurrence already; auto
    # reminders would be noise). Ushers templates default to True
    # because monthly assignments are easier to forget.
    templates_by_id = {t.template_id: t
                       for t in db.list_templates(app.app_id)}
    ntfs: list[Notification] = []
    for a in db.list_assignments_for_month(app.app_id, yyyy_mm):
        slot = slots_by_id.get(a.slot_id)
        if not slot or slot.cancelled:
            continue
        # Per-template opt-out — if the slot's template said
        # auto_reminders=False, skip notification materialization
        # for assignments derived from it.
        tpl = templates_by_id.get(slot.template_id)
        if tpl is not None and not tpl.auto_reminders:
            continue
        user = users_by_id.get(a.user_id)
        if not user or user.email_undeliverable or user.channel == "none":
            continue
        leads = user.lead_times_minutes or default_leads
        h, m = (int(x) for x in slot.start_time.split(":"))
        y, mo, d = (int(x) for x in slot.local_date.split("-"))
        arrival = dt.datetime(y, mo, d, h, m, tzinfo=tz) - dt.timedelta(
            minutes=slot.arrival_offset_minutes)
        for lead in leads:
            send_at = arrival - dt.timedelta(minutes=lead)
            send_at_utc = send_at.astimezone(dt.timezone.utc)
            if send_at_utc <= dt.datetime.now(dt.timezone.utc):
                continue
            ntfs.append(Notification(
                community_id=community.community_id,
                app_id=app.app_id,
                user_id=a.user_id,
                slot_id=a.slot_id,
                yyyy_mm=yyyy_mm,
                send_at=send_at_utc.isoformat(timespec="seconds"),
                lead_minutes=lead,
            ))
    if ntfs:
        db.put_notifications(ntfs)
    return len(ntfs)


def unpublish_schedule(app: Application, yyyy_mm: str) -> Schedule:
    """Revert a published schedule to draft and clear pending reminders.

    Side effects:
        - All pending Notifications for this schedule are deleted (so no
          stale reminders fire after a republish).
        - Schedule.state -> "draft" and Schedule.published_at -> None.
        - Assignments, slots, and the calendar invites already on members'
          calendars are NOT touched. That's deliberate — most members
          have already accepted invites, and cancelling them would be
          noisy. If the admin wants members notified, they send a
          manual note from the Send Email page.

    Calendar UID stability: when you re-publish, ``ical.make_event_ics``
    uses ``slot_id`` + ``user_id`` as the UID, so re-sent invites
    overwrite the earlier ones in most calendar apps rather than
    appearing as duplicates.

    Notification (the email to fellow App Admins about the unpublish) is
    NOT sent from here — that's a web-only concern, handled by
    ``lambdas/web.py:_notify_admins_of_unpublish``. This function stays
    pure-core so the CLI can call it without dragging in web deps.

    Returns the updated Schedule (state="draft").
    """
    sch = db.get_schedule(app.app_id, yyyy_mm)
    if sch is None:
        raise ValueError(f"schedule {yyyy_mm} not found")
    # Atomic state transition. The original code did a read-then-put
    # which could clobber a concurrent publish: admin A publishes,
    # admin B reads draft, A's publish lands, B's put writes "draft"
    # back and broadcast goes out without a published state. The
    # conditional update here only succeeds if the schedule is
    # currently "published" — raising ConcurrencyConflict otherwise
    # (security fix M4).
    try:
        db.transition_schedule_state(
            app.app_id, yyyy_mm,
            from_state="published", to_state="draft",
            clear_published_at=True,
        )
    except db.ConcurrencyConflict:
        raise ValueError(
            f"schedule {yyyy_mm} is not currently published")
    db.delete_notifications_for_schedule(app.app_id, yyyy_mm)
    sch.state = "draft"
    sch.published_at = None
    return sch


def archive_schedule(app: Application, yyyy_mm: str, *,
                     archived_at: str) -> Schedule:
    """Admin-declared age-out: mark a published schedule as history.

    This is deliberately NON-destructive and NOT a cancellation — the
    opposite of unpublish. ``published_at``, every pending reminder
    Notification, the slots, the assignments, and the calendar invites
    already on members' devices are ALL left untouched. Reminders for
    still-future shifts keep firing and saved ``.ics`` invites keep
    working. The only effect is *visibility*: an archived schedule drops
    out of the default admin/member screens and the Send-Email audience,
    surfacing only behind an explicit "show past" expander. A schedule can
    be archived even while its month is still current — aging out is the
    admin's call, not the calendar's. Reverse with ``reactivate_schedule``.

    Returns the updated Schedule (state="archived").
    """
    sch = db.get_schedule(app.app_id, yyyy_mm)
    if sch is None:
        raise ValueError(f"schedule {yyyy_mm} not found")
    try:
        db.transition_schedule_state(
            app.app_id, yyyy_mm,
            from_state="published", to_state="archived",
            archived_at=archived_at,
        )
    except db.ConcurrencyConflict:
        raise ValueError(f"schedule {yyyy_mm} is not currently published")
    sch.state = "archived"
    sch.archived_at = archived_at
    return sch


def reactivate_schedule(app: Application, yyyy_mm: str) -> Schedule:
    """Bring an archived schedule back to active (archived -> published).

    Inverse of ``archive_schedule``. Clears ``archived_at``; ``published_at``
    and all schedule contents are untouched (archiving never altered them).

    Returns the updated Schedule (state="published").
    """
    sch = db.get_schedule(app.app_id, yyyy_mm)
    if sch is None:
        raise ValueError(f"schedule {yyyy_mm} not found")
    try:
        db.transition_schedule_state(
            app.app_id, yyyy_mm,
            from_state="archived", to_state="published",
            clear_archived_at=True,
        )
    except db.ConcurrencyConflict:
        raise ValueError(f"schedule {yyyy_mm} is not currently archived")
    sch.state = "published"
    sch.archived_at = None
    return sch
