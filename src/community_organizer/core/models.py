"""Domain models for Community Organizer.

Plain dataclasses; DDB serialization lives in core/db.py.
Single-tenant: one Community per deployment. community_id is a data key,
not a routing mechanism.
"""
from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from typing import Literal

CommunityRole = Literal["ca", "ua", "member"]
AppRole = Literal["aa", "member"]
AppType = Literal["coverage", "recurring_commitments",
                  "standing_event", "flexible_event"]
# A "period" is the unit a Schedule covers — a calendar month for the
# original coverage flow ("Sun 8 AM Ushers, May 2026") OR an ISO week
# for recurring_commitments ("Wed→Thu adoration, week of 2026-05-25").
# Storage stays on the existing `yyyy_mm` attribute — for monthly apps
# that's "2026-05", for weekly apps it's "2026-W22" (ISO 8601 week).
# `ad_hoc` is the flexible_event marker — no fixed period; each event
# stands alone with its own winning_date. No yyyy_mm semantics apply.
PeriodType = Literal["monthly", "weekly", "ad_hoc"]
Channel = Literal["email", "sms", "both", "none"]
Recurrence = Literal[
    "weekly",
    "biweekly_even",
    "biweekly_odd",
    "monthly_first_sat",
    "monthly_last_sun",
    "rrule",
    # standing_event recurrences — ordinal weekday of month, 1=first
    # 5=last (last-of-month if month has only 4). "Nth weekday of
    # month" covers the K-of-C / parish-council / book-club pattern.
    "monthly_1st_mon", "monthly_1st_tue", "monthly_1st_wed",
    "monthly_1st_thu", "monthly_1st_fri", "monthly_1st_sat",
    "monthly_1st_sun",
    "monthly_2nd_mon", "monthly_2nd_tue", "monthly_2nd_wed",
    "monthly_2nd_thu", "monthly_2nd_fri", "monthly_2nd_sat",
    "monthly_2nd_sun",
    "monthly_3rd_mon", "monthly_3rd_tue", "monthly_3rd_wed",
    "monthly_3rd_thu", "monthly_3rd_fri", "monthly_3rd_sat",
    "monthly_3rd_sun",
    "monthly_4th_mon", "monthly_4th_tue", "monthly_4th_wed",
    "monthly_4th_thu", "monthly_4th_fri", "monthly_4th_sat",
    "monthly_4th_sun",
    "monthly_last_mon", "monthly_last_tue", "monthly_last_wed",
    "monthly_last_thu", "monthly_last_fri", "monthly_last_sat",
    "monthly_last_sun",
]
ScheduleState = Literal["draft", "publishing", "published", "archived",
                        "materialized"]
# Per-occurrence state for standing_event series.
OccurrenceState = Literal["scheduled", "cancelled", "moved"]
# Per-event state for flexible_event apps. `poll` is the Doodle
# phase; `scheduled` is post-finalization; `completed` is the
# auto-archive target N days after `winning_date`.
FlexibleEventState = Literal["poll", "scheduled", "cancelled", "completed"]
# Member response on an attendance-tracked occurrence or a
# scheduled flexible_event. Used for both RSVP and poll-vote
# values; non-responders show as "uncategorized" (no row at all).
AttendanceResponse = Literal["yes", "no", "maybe"]
EmailDirection = Literal["inbound", "outbound"]
EmailKind = Literal[
    "publish_broadcast",
    "reminder",
    "change_notification",
    "swap_request",
    "admin_command",
    "command_reply",
    "bounce",
    "auto_reply",
    "smoke_test",
    # flexible_event (date-poll / book club) lifecycle:
    "event_poll_invite",     # magic-link poll email to each member
    "event_confirmed",       # calendar invite once the date is picked
    "event_missed",          # "sorry you can't join this time" courtesy note
    "event_optout_notice",   # AA alert that a member opted out of the group
    "event_response_notice", # AA alert on each member response / post-close rejoin
    "event_cancelled",       # event called off (CANCEL .ics) — fast-follow
    "event_reply_nudge",     # auto-reply: "use your link, replies aren't recorded"
    "other",
]
EmailOutcome = Literal[
    "accepted",
    "delivered",
    "bounced",
    "rejected_dmarc",
    "rejected_allowlist",
    "error",
]


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _new_id() -> str:
    return uuid.uuid4().hex


@dataclass
class Community:
    community_id: str
    name: str
    default_timezone: str = "America/New_York"
    admin_email_allowlist: list[str] = field(default_factory=list)
    # Public hostname this community is served from — used by the
    # notifier to build email links that point at the right stack
    # when multiple communities share one DDB table + one Lambda.
    # `None` means "fall back to env DOMAIN_NAME" — the prior
    # single-community behavior, kept so existing rows that predate
    # this field keep working unchanged.
    public_url: str | None = None
    created_at: str = field(default_factory=_now)


@dataclass
class Application:
    community_id: str
    name: str
    # app_type is intentionally required (no default) — each app type
    # has its own UX and behaviors; the system shouldn't pick one for
    # you. Existing DDB rows already carry "coverage" written
    # explicitly (the prior default fired at creation time), so reads
    # of legacy data still work.
    app_type: AppType
    app_id: str = field(default_factory=_new_id)
    # Admin-supplied short description shown on the cross-app launcher
    # page so a multi-app member can choose which app to enter. Plain
    # text; rendered HTML-escaped. Blank means "no description set."
    # Doubles as the public og:description on the app's shareable page.
    description: str = ""
    # Public, human-readable URL slug for this app's shareable front door
    # at /a/<slug> (unfurls with the app's own card; deep-links members
    # straight into this app). Unique within the community; AA-editable.
    # None until generated (on app create, or first visit to the Share page).
    public_slug: str | None = None
    # Set when an AA uploads custom social-card art (stored in the OG-art S3
    # bucket under the app_id). Holds the image's MIME type ("image/png" /
    # "image/jpeg"); None = use the generic Community Organizer image.
    og_art_content_type: str | None = None
    terminology: str = "volunteer"
    terminology_plural: str = ""  # blank → derived by pluralizer
    event_noun: str = "event"
    event_noun_plural: str = ""  # blank → derived by pluralizer
    default_timezone: str | None = None
    arrival_label: str = "please arrive by"
    assignment_mode: str = "assigned"
    trade_default_release: bool = True
    default_lead_times: list[int] = field(default_factory=lambda: [1440, 120])
    group_email_mode: bool = False
    display_order: int = 0
    active: bool = True
    # How far ahead the recurring-commitments home page lets a user
    # navigate, expressed in months. Default 6 — beyond that we
    # block the "next 4 weeks" button to avoid infinite scrolling
    # past where the admin has scheduled. Ignored for coverage apps.
    visible_horizon_months: int = 6
    # The unit a Schedule covers for this app. Defaults to monthly
    # (the original Ushers flow). recurring_commitments apps should
    # set this to "weekly" — see scheduling.materialize() for how
    # the period_id format ("2026-05" vs "2026-W22") changes the
    # date enumeration.
    period_type: PeriodType = "monthly"
    # Per-app defaults for the "Add new event template" form. Each
    # is None when no app default has been set, in which case the
    # form falls back to the hardcoded constants. Once the admin
    # adds the first template, the successive-add prefill on the
    # templates page advances start_time by duration and copies the
    # other values forward — these app defaults seed that chain.
    # max_volunteers default doesn't have an "unlimited" encoding
    # here; admins who want unlimited (adoration apps) clear the
    # max field once on the first template and the prefill carries
    # the blank state forward.
    template_default_day_of_week: int | None = None
    template_default_start_time: str | None = None
    template_default_duration_minutes: int | None = None
    template_default_arrival_offset_minutes: int | None = None
    template_default_required_volunteers: int | None = None
    template_default_min_volunteers: int | None = None
    template_default_max_volunteers: int | None = None
    version: int = 0
    created_at: str = field(default_factory=_now)


@dataclass
class Membership:
    community_id: str
    app_id: str
    user_id: str
    app_role: AppRole = "member"
    created_at: str = field(default_factory=_now)
    # Group-level email opt-out, scoped to THIS app (a user in many
    # flexible_event groups opts out of each independently). Set when a
    # member takes the "stop emailing me about this group" action on a
    # poll form; the send-poll/close fan-outs skip opted-out members.
    opted_out: bool = False
    opted_out_at: str | None = None


@dataclass
class Cohort:
    community_id: str
    app_id: str
    name: str
    cohort_id: str = field(default_factory=_new_id)
    linked_template_id: str | None = None
    created_at: str = field(default_factory=_now)


@dataclass
class CohortMembership:
    cohort_id: str
    user_id: str
    created_at: str = field(default_factory=_now)


SwapState = Literal["pending", "completed", "cancelled"]


@dataclass
class SwapRequest:
    community_id: str
    app_id: str
    yyyy_mm: str
    requester_user_id: str
    release_slot_id: str
    preferred_slot_ids: list[str] = field(default_factory=list)
    released: bool = False
    state: SwapState = "pending"
    swap_id: str = field(default_factory=_new_id)
    accepter_user_id: str | None = None
    accepted_slot_id: str | None = None
    created_at: str = field(default_factory=_now)
    completed_at: str | None = None


@dataclass
class User:
    community_id: str
    email: str
    name: str
    community_role: CommunityRole = "member"
    user_id: str = field(default_factory=_new_id)
    cognito_sub: str | None = None
    phone: str | None = None
    preferred_tz: str | None = None
    channel: Channel = "email"
    lead_times_minutes: list[int] = field(default_factory=lambda: [1440, 120])
    calendar_alarm_minutes: int | None = 60
    quiet_hours: tuple[str, str] | None = ("22:00", "07:00")
    email_undeliverable: bool = False
    # Distinct SES complaints received for this user's email. The
    # bounce Lambda silences (email_undeliverable=True) only after
    # COMPLAINT_THRESHOLD complaints accumulate — one mis-click on
    # "this is spam" no longer kills future delivery (security fix
    # D17).
    complaint_count: int = 0
    notes: str | None = None
    last_login_at: str | None = None
    login_count: int = 0
    # Community-scoped family grouping. Members sharing a household_id
    # are one family (spouses + kids who are their own members). Used by
    # flexible_event polls to warn a responder that someone in their
    # household already replied. None = ungrouped (warning no-ops).
    household_id: str | None = None
    version: int = 0
    created_at: str = field(default_factory=_now)


@dataclass
class SlotTemplate:
    community_id: str
    app_id: str
    name: str
    day_of_week: int
    start_time: str
    duration_minutes: int
    template_id: str = field(default_factory=_new_id)
    recurrence: Recurrence = "weekly"
    rrule: str | None = None
    arrival_offset_minutes: int = 10
    required_volunteers: int = 2
    min_volunteers: int = 1
    # None means "no cap" (unlimited signups). Use this for apps like
    # Eucharistic Adoration where there's no point capping volunteers
    # — a slot can never have "too many" people praying.
    # For coverage apps that want a cap, set an int > required_volunteers.
    # Existing templates default to 5 to preserve current behavior;
    # the admin must explicitly clear the field to opt into unlimited.
    max_volunteers: int | None = 5
    tags: list[str] = field(default_factory=list)
    active_from: str | None = None
    active_until: str | None = None
    # Whether assignments for slots derived from this template should
    # get auto-materialized reminder notifications. Default True
    # preserves existing Ushers behavior (people don't memorize a
    # different monthly assignment, they want reminders).
    # Set False for weekly Recurring Commitments templates where the
    # member's calendar app already holds the recurrence — auto
    # reminders would be noise.
    auto_reminders: bool = True
    # For monthly apps: which occurrence of day_of_week in each month
    # this template represents. 1=first, 2=second, 3=third, 4=fourth,
    # -1=last. None means "weekly" (the default and the meaning for
    # period_type=weekly apps). materialize() uses this when
    # period_type is monthly to pick the right single date per month
    # rather than every matching weekday.
    ordinal: int | None = None
    created_at: str = field(default_factory=_now)


@dataclass
class Schedule:
    community_id: str
    app_id: str
    yyyy_mm: str
    state: ScheduleState = "draft"
    created_at: str = field(default_factory=_now)
    published_at: str | None = None
    archived_at: str | None = None
    # Optimistic concurrency counter. Edit forms render a hidden
    # `version` input matching the loaded row; put_schedule checks it
    # via expected_version. Defaults to 0 for pre-existing rows that
    # predate this field — _hydrate fills the default automatically.
    version: int = 0


@dataclass
class Slot:
    community_id: str
    app_id: str
    yyyy_mm: str
    template_id: str
    name: str
    day_of_week: int
    start_time: str
    arrival_offset_minutes: int
    duration_minutes: int
    required_volunteers: int
    min_volunteers: int
    concrete_date: str
    local_date: str
    slot_id: str = field(default_factory=_new_id)
    # None = no cap, see SlotTemplate.max_volunteers for the rationale.
    max_volunteers: int | None = 5
    tags: list[str] = field(default_factory=list)
    notes: str | None = None
    cancelled: bool = False
    # Materialized counter used as the source of truth for the
    # signup capacity check. Maintained atomically alongside the
    # Assignment row via DDB TransactWriteItems
    # (db.atomic_signup_assignment). See security fix D12.
    assignment_count: int = 0
    created_at: str = field(default_factory=_now)
    # Optimistic concurrency counter for AA-edited fields (cancelled,
    # required_volunteers, notes, name). `assignment_count` is updated
    # by the atomic signup path which bypasses version (it has its own
    # transactional guard) — see db.put_slot.
    version: int = 0


ConfirmedVia = Literal[
    "self_signup",      # member clicked "Sign me up"
    "member_login",     # member logged in and pressed Confirm on the schedule
    "ical_reply",       # PARTSTAT=ACCEPTED reply arrived via inbound SES
    "admin_override",   # admin pressed "confirm" on the schedule edit row
]


@dataclass
class Assignment:
    community_id: str
    app_id: str
    yyyy_mm: str
    slot_id: str
    user_id: str
    local_date: str
    assignment_id: str = field(default_factory=_new_id)
    created_at: str = field(default_factory=_now)
    created_by: str | None = None
    # Optimistic concurrency counter. Today's Assignments are mostly
    # immutable after creation (delete+insert pattern), but having the
    # field in place lets future per-assignment mutations (cohort tag,
    # swap-completed-at, etc.) opt into CAS without a migration.
    version: int = 0
    # Confirmation state. None until one of the four paths sets it:
    # member self-signup, member self-confirm, admin override, or an
    # inbound iCal PARTSTAT=ACCEPTED reply. The reasons are tracked
    # so the per-name UI tooltip can read "confirmed Jun 7 by admin"
    # vs "confirmed Jun 7 (signed up themselves)" without a separate
    # log lookup. See #217.
    confirmed_at: str | None = None
    confirmed_via: ConfirmedVia | None = None


@dataclass
class BlockedDate:
    """A member-declared "I can't do this day" for an app.

    Forward-looking only: the admin's cohort pick-list filters
    these out (rendered faded + tagged "(blocked)") so the member
    isn't auto-assigned on days they've marked off. Existing
    assignments on the same date are NOT auto-released — the member
    must release them first, and we refuse to add the block while an
    assignment is in place. App-scoped because a member might want
    different availability across apps (e.g. usher vs. K-of-C).
    """
    community_id: str
    app_id: str
    user_id: str
    # ISO date "YYYY-MM-DD" in the community's local timezone — same
    # shape as Slot.local_date / Assignment.local_date so set-membership
    # lookups against assignment dates are direct string compares.
    local_date: str
    created_at: str = field(default_factory=_now)


NotificationState = Literal["pending", "in_flight", "sent", "cancelled"]


@dataclass
class Notification:
    community_id: str
    app_id: str
    user_id: str
    # For coverage/recurring (source="slot") this is the Slot id. For
    # standing_event (source="occurrence") it is the StandingOccurrence id —
    # whose trailing 10 chars are the occurrence's ISO date (see standing.py).
    slot_id: str
    yyyy_mm: str
    send_at: str
    lead_minutes: int
    state: NotificationState = "pending"
    # Which subsystem queued this + how the notifier renders it. "slot" keeps
    # every existing row working without a migration.
    source: str = "slot"
    notification_id: str = field(default_factory=_new_id)
    sent_at: str | None = None
    email_id: str | None = None
    created_at: str = field(default_factory=_now)


@dataclass
class EmailLog:
    community_id: str
    direction: EmailDirection
    from_addr: str
    to_addr: str
    subject: str
    provider: str
    kind: EmailKind
    outcome: EmailOutcome
    ts: str = field(default_factory=_now)
    email_id: str = field(default_factory=_new_id)
    cc: list[str] = field(default_factory=list)
    provider_message_id: str | None = None
    related_user_id: str | None = None
    related_app_id: str | None = None
    related_slot_id: str | None = None
    related_yyyy_mm: str | None = None
    body_excerpt: str = ""
    error_detail: str | None = None
    # Forward-compat: stamp publish broadcasts with a stable key so a
    # future retry-storm guard can check "already sent" before
    # firing again. Not yet read; populating now keeps the field
    # available without a migration. See concurrency notes.
    idempotency_key: str | None = None


# ============================================================================
# Date-centric event apps (standing_event + flexible_event) — slice 1
# ----------------------------------------------------------------------------
# Two app types that share a wall-calendar UI but differ in flow:
#   * standing_event — recurring meeting (K of C monthly, prayer group
#     weekly). Mostly an announcement. Optional attendance tracking
#     and optional .ics calendar invites. Reminders on by default.
#   * flexible_event — ad-hoc events (potluck, book club). Each event
#     is created individually (direct OR via poll) with per-event
#     location and a prominent "what are you bringing" field.
#
# Both ignore Cohort/CohortMembership — those exist for coverage's
# subgrouping and aren't useful when "everyone in the app gets all
# notifications" is the model. The plumbing stays in the data model
# for future use.
#
# Design doc:
# design notes for the date-poll feature
# ============================================================================


@dataclass
class StandingSeries:
    """Series-level metadata for a standing_event app.

    One per app. Holds the recurrence rule + defaults inherited by
    each materialized StandingOccurrence (location, time, duration).
    """
    community_id: str
    app_id: str
    recurrence: Recurrence
    default_location: str | None = None
    default_start_time: str | None = None       # "HH:MM" 24h
    default_duration_minutes: int = 60
    # OFF by default — most standing events are pure announcements.
    # When on, members see yes/no/maybe buttons inline on each
    # occurrence; non-responders show as uncategorized (no row).
    attendance_tracking: bool = False
    # OFF by default — .ics attached to each occurrence's reminder
    # email so the user's calendar app shows the meeting. Distinct
    # from `reminder_lead_days` which controls the reminder *email*.
    send_calendar_invites: bool = False
    # Reminder lead in days; 0 disables. On by default (=1).
    reminder_lead_days: int = 1
    series_id: str = field(default_factory=_new_id)
    version: int = 0
    created_at: str = field(default_factory=_now)


@dataclass
class StandingOccurrence:
    """One concrete instance (a specific date) of a StandingSeries.

    Materialized lazily on AA edit or member view of an upcoming
    month. `state` captures cancellation/move exceptions — the AA's
    secondary actions per occurrence. `location` / `start_time` are
    None when the occurrence inherits from the series defaults; set
    when an AA overrides for that specific date.
    """
    community_id: str
    app_id: str
    series_id: str
    iso_date: str                                # "2026-07-14"
    state: OccurrenceState = "scheduled"
    location: str | None = None                  # None = inherit series
    start_time: str | None = None                # None = inherit series
    moved_to_date: str | None = None             # set when state="moved"
    notes: str | None = None
    occurrence_id: str = field(default_factory=_new_id)
    version: int = 0
    created_at: str = field(default_factory=_now)


@dataclass
class StandingRSVP:
    """Per-(occurrence, user) attendance response.

    Only written when the parent StandingSeries.attendance_tracking
    is True. Absence of a row means "uncategorized" (the member
    hasn't responded), NOT "implicit no" — design decision so
    non-responders aren't penalized.
    """
    community_id: str
    app_id: str
    occurrence_id: str
    user_id: str
    response: AttendanceResponse                 # yes | no | maybe
    rsvp_id: str = field(default_factory=_new_id)
    version: int = 0
    updated_at: str = field(default_factory=_now)


@dataclass
class FlexibleSeries:
    """Series-level defaults for a flexible_event app.

    No recurrence rule — each event is created ad hoc. Holds the
    per-app default location (each event can override) and the
    "what are you bringing" prompt wording.
    """
    community_id: str
    app_id: str
    default_location: str | None = None
    bring_prompt: str = "What are you bringing?"
    series_id: str = field(default_factory=_new_id)
    version: int = 0
    created_at: str = field(default_factory=_now)


@dataclass
class FlexibleEvent:
    """One event within a FlexibleSeries. Lives in one of four states:

      poll      — accepting yes/no/maybe votes on candidate dates
      scheduled — date finalized, accepting RSVPs + bringing
      cancelled — event called off
      completed — auto-archived N days after winning_date

    `winning_date` is set when state transitions poll -> scheduled
    OR on direct creation (Path A). Direct creation skips the poll
    phase entirely.

    `merged_into` makes the row a tombstone — see the field comment.
    """
    community_id: str
    app_id: str
    title: str
    state: FlexibleEventState = "poll"
    description: str | None = None
    location: str | None = None                  # None = series default
    winning_date: str | None = None              # ISO date
    winning_start_time: str | None = None        # "HH:MM" 24h
    winning_duration_minutes: int = 120          # for the calendar invite
    notify_on_response: bool = False             # email AA(s) on each response
    poll_closes_at: str | None = None            # optional ISO datetime
    # Event id this one was folded into, when an AA ran two polls for the
    # same gathering. The row stays as a tombstone rather than being deleted
    # because links were already mailed against it: token resolution follows
    # this to the surviving event, so those links keep working. Tombstones are
    # hidden from list_flexible_events unless include_merged=True.
    merged_into: str | None = None
    event_id: str = field(default_factory=_new_id)
    created_at: str = field(default_factory=_now)
    created_by: str | None = None
    version: int = 0


@dataclass
class FlexiblePollOption:
    """A candidate date in the poll phase. Removed when the parent
    FlexibleEvent transitions out of `poll` state (only the winning
    date survives, stored on FlexibleEvent.winning_date)."""
    community_id: str
    app_id: str
    event_id: str
    iso_date: str
    start_time: str | None = None                # forward-compat; rarely used today
    label: str | None = None                     # "after 7pm" etc.
    option_id: str = field(default_factory=_new_id)
    sort_key: int = 0


@dataclass
class FlexibleRSVP:
    """Per-(event, user) row that carries BOTH the poll votes and the
    post-confirmation response + bringing field. One row per pair so
    re-voting or updating "bringing" is an upsert.

    Phase=poll:        votes[option_id] -> "yes" | "no" | "maybe"
    Phase=scheduled:   confirmed_response set; votes dict may persist
                       for audit but is no longer used for routing
    """
    community_id: str
    app_id: str
    event_id: str
    user_id: str
    votes: dict[str, str] = field(default_factory=dict)
    confirmed_response: AttendanceResponse | None = None
    bringing: str | None = None
    # Best-guess household headcount (including the responder) — the
    # member answers for their whole family. Summed (household-grouped)
    # for the AA's total expected attendance.
    party_size: int | None = None
    rsvp_id: str = field(default_factory=_new_id)
    version: int = 0
    updated_at: str = field(default_factory=_now)


@dataclass
class EventToken:
    """Passwordless magic-link credential for a flexible_event poll.

    One per (event, user), minted when the AA sends the poll email. The
    raw ``token`` (secrets.token_urlsafe, 256-bit) is the lookup key:
    possession of it authenticates the member to act ONLY as that user on
    ONLY that event — never broader account access. Stays valid across the
    event's whole lifecycle (poll -> scheduled). ``revoked`` is set when
    the member takes the group-level opt-out. ``expires_at`` is checked in
    code (and is the DDB TTL attribute once that's enabled).
    """
    community_id: str
    app_id: str
    event_id: str
    user_id: str
    token: str
    expires_at: str
    revoked: bool = False
    created_at: str = field(default_factory=_now)
    # Set when the inbound Lambda auto-nudges this member after they
    # REPLY to the poll email instead of using their link. Bounds the
    # nudge to once per (event, user) so an out-of-office auto-responder
    # can't create a reply loop. Unknown-key default → no migration.
    reply_nudged_at: str | None = None
