"""Seats and the waitlist for a flexible event with a capacity.

The design (agreed with the product owner, see the project's
design-capacity-waitlist notes):

* Capacity counts people: each household once, at the largest party size any
  of its answering members gave; a blank party size counts as 1.
* While seats remain, a yes takes one, first come first served. Once full, a
  yes joins the **yes tier** of the waitlist. A maybe never holds a seat: it
  joins the **maybe tier**. Both tiers are in the order people joined them.
* Freed seats are handed on only once the event has actually been full (the
  ``reached`` latch). An event that never filled has no line to work through.
  Turning a yes away because their party didn't fit counts as full.
* Handing on: the first yes-tier household whose party fits is seated (the
  caller sends the calendar invite). Only when nobody is waiting on the yes
  tier is the maybe tier reached, and a maybe gets an *offer* that holds the
  seat for 24 hours; accepting (answering yes) seats them, letting it lapse
  or declining takes them out of the line until they answer again.
* Within 24 hours of the start nobody is handed a seat or an offer
  automatically; the caller tells the organizer who is next instead.
* Seats are never taken away from someone still coming: lowering the capacity
  or an organizer seating someone over it just means nobody else is seated
  until enough seats free up.

This module is pure: ``plan`` takes the replies as they are stored and returns
the seat fields that should change plus what happened, for the caller to
write (atomically, see ``db.commit_seat_plan``) and announce.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

OFFER_HOURS = 24
CUTOFF_HOURS = 24

# Seat states, best first. A household's state is the best one any of its
# answering members holds.
_RANK = {"seated": 0, "offered": 1, "wait_yes": 2, "wait_maybe": 3, None: 4}

SEAT_FIELDS = ("seat", "queued_at", "offer_expires_at", "offer_passed",
               "seat_forced")


def answer_for(rsvp, option_id: str | None, state: str) -> str | None:
    """A member's answer for the date that seats are held on: "yes",
    "maybe", "no" or None (no answer). The vote on that date is the live
    answer (every path that changes attendance after the poll keeps it in
    step); an event with no options (adopted) falls back to the overall
    response."""
    if rsvp is None:
        return None
    vote = rsvp.votes.get(option_id) if option_id else None
    if vote in ("yes", "maybe", "no"):
        return vote
    if rsvp.confirmed_response == "no":
        return "no"
    if state == "poll":
        return None
    # Voted in the poll, but not on this date: not an answer for it. (The
    # overall response says only that they could come on SOME date.)
    if option_id and any(v in ("yes", "maybe", "no")
                         for v in (rsvp.votes or {}).values()):
        return None
    return (rsvp.confirmed_response
            if rsvp.confirmed_response in ("yes", "maybe") else None)


def party_of(rsvp) -> int:
    return rsvp.party_size if rsvp.party_size and rsvp.party_size > 0 else 1


@dataclass
class Group:
    """A household (or one person on their own) as the seat-holding unit."""
    key: str
    user_ids: list[str]                 # members with a yes/maybe answer
    answer: str | None                  # "yes", "maybe" or None
    size: int
    seat: str | None
    queued_at: str | None
    offer_expires_at: str | None
    offer_passed: bool
    forced: bool


@dataclass
class Plan:
    # user_id -> {field: value} for every reply whose seat fields change.
    updates: dict[str, dict] = field(default_factory=dict)
    promoted: list[Group] = field(default_factory=list)    # waitlist -> seat
    offered: list[Group] = field(default_factory=list)     # maybe offered
    waitlisted: list[Group] = field(default_factory=list)  # newly in line
    lapsed: list[Group] = field(default_factory=list)      # offer expired
    reached: bool = False               # the latch, after this plan
    # Within the cutoff, with seats free and people waiting: who is next.
    cutoff_next: list[Group] = field(default_factory=list)
    free: int = 0
    seated: int = 0
    forced: list[str] = field(default_factory=list)  # seated over capacity
    # Households an organizer just seated by hand (see web._flex_plan_seats).
    seated_now: list[Group] = field(default_factory=list)
    # Filled in by the caller once committed (web._flex_reseat): households
    # that now hold seats they didn't, what to tell them (None: nothing, the
    # caller sends its own), and who in each household to tell.
    newly_seated: list[Group] = field(default_factory=list)
    news: dict[str, str | None] = field(default_factory=dict)
    recipients: dict[str, list[str]] = field(default_factory=dict)
    waiting_yes: int = 0
    waiting_maybe: int = 0


def _iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)


def when(ts: str | None) -> dt.datetime:
    """An ISO timestamp as an instant, for ordering: stored values mix
    second and microsecond precision, which don't sort correctly as text.
    Missing or unreadable values sort first."""
    if not ts:
        return _EPOCH
    try:
        t = dt.datetime.fromisoformat(ts)
    except ValueError:
        return _EPOCH
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def _later(a: str | None, b: dt.datetime) -> bool:
    """True when ISO time ``a`` is after ``b`` (a missing ``a`` is not)."""
    if not a:
        return False
    try:
        return dt.datetime.fromisoformat(a) > b
    except ValueError:
        return False


def build_groups(rsvps: dict, hh_key, option_id: str | None,
                 state: str) -> list[Group]:
    """Fold replies into households. ``hh_key(user_id)`` gives the household
    grouping key (a per-person key for someone not in one)."""
    by_key: dict[str, list] = {}
    for uid, r in rsvps.items():
        by_key.setdefault(hh_key(uid), []).append(r)
    groups = []
    for key, rs in by_key.items():
        answers = {r.user_id: answer_for(r, option_id, state) for r in rs}
        coming = [r for r in rs if answers[r.user_id] in ("yes", "maybe")]
        if any(answers[r.user_id] == "yes" for r in coming):
            ans = "yes"
        elif coming:
            ans = "maybe"
        else:
            ans = None
        held = [r for r in coming if r.seat] or coming
        seat = min((r.seat for r in held), key=lambda s: _RANK.get(s, 4),
                   default=None)
        # A reply that has never been placed (answered before the cap was in
        # force) stands in line from when it was last saved.
        queued = sorted((r.queued_at or r.updated_at for r in held
                         if r.queued_at or r.updated_at), key=when)
        expiries = sorted(r.offer_expires_at for r in held
                          if r.seat == "offered" and r.offer_expires_at)
        groups.append(Group(
            key=key, user_ids=sorted(r.user_id for r in coming), answer=ans,
            size=max((party_of(r) for r in coming), default=0), seat=seat,
            queued_at=queued[0] if queued else None,
            offer_expires_at=expiries[0] if expiries else None,
            offer_passed=any(r.offer_passed for r in coming),
            forced=any(r.seat_forced for r in coming)))
    return groups


def plan(*, capacity: int, rsvps: dict, hh_key, option_id: str | None,
         state: str, reached: bool, now: dt.datetime,
         starts_at: dt.datetime | None,
         answered_at: dict[str, str] | None = None) -> Plan:
    """Work out where everyone stands.

    ``rsvps``: user_id -> FlexibleRSVP for the event. ``answered_at``: for
    replies just saved, when that answer was given (it becomes their place
    in line if they are joining it); others keep their stored place.
    ``starts_at``: the event's start, for the 24-hour cutoff (None: no
    cutoff)."""
    answered_at = answered_at or {}
    now_iso = _iso(now)
    in_cutoff = (starts_at is not None
                 and starts_at - now <= dt.timedelta(hours=CUTOFF_HOURS))
    out = Plan()
    target: dict[str, dict] = {}        # group key -> seat fields

    def place(g: Group) -> str:
        """Their time in line: kept while they stay in the same tier,
        restarted when they join it (or switch tiers)."""
        mine = [answered_at[u] for u in g.user_ids if u in answered_at]
        if g.queued_at and not (mine and g.seat is None):
            return g.queued_at
        return min(mine, key=when) if mine else (g.queued_at or now_iso)

    groups = build_groups(rsvps, hh_key, option_id, state)
    used = 0
    yes_line: list[tuple[str, Group]] = []
    maybe_line: list[tuple[str, Group]] = []
    for g in groups:
        if g.answer is None:
            target[g.key] = {"seat": None, "queued_at": None,
                             "offer_expires_at": None, "offer_passed": False,
                             "seat_forced": False}
            continue
        if g.seat == "seated" and g.answer == "yes":
            target[g.key] = {"seat": "seated", "queued_at": g.queued_at,
                             "offer_expires_at": None, "offer_passed": False,
                             "seat_forced": g.forced}
            used += g.size
            continue
        if g.seat == "offered" and g.answer == "maybe":
            if g.offer_expires_at and not _later(g.offer_expires_at, now):
                out.lapsed.append(g)
                target[g.key] = {"seat": None, "queued_at": g.queued_at,
                                 "offer_expires_at": None,
                                 "offer_passed": True, "seat_forced": False}
                continue
            target[g.key] = {"seat": "offered", "queued_at": g.queued_at,
                             "offer_expires_at": g.offer_expires_at,
                             "offer_passed": False, "seat_forced": False}
            used += g.size
            continue
        if g.seat == "offered" and g.answer == "yes":
            # Took the offer: the seat was being held for them.
            target[g.key] = {"seat": "seated", "queued_at": g.queued_at,
                             "offer_expires_at": None, "offer_passed": False,
                             "seat_forced": False}
            used += g.size
            continue
        if g.answer == "yes":
            # Waiting on the yes tier, or arriving: a seated household that
            # switched away and back, a maybe switching to yes, a new answer.
            if g.seat != "wait_yes":
                g = _restart(g, place(g) if g.seat is None else now_iso)
            yes_line.append((g.queued_at, g))
        else:
            if g.offer_passed and g.seat is None:
                target[g.key] = {"seat": None, "queued_at": g.queued_at,
                                 "offer_expires_at": None,
                                 "offer_passed": True, "seat_forced": False}
                continue
            if g.seat != "wait_maybe":
                g = _restart(g, place(g) if g.seat is None else now_iso)
            maybe_line.append((g.queued_at, g))

    yes_line.sort(key=lambda p: (when(p[0]), p[1].key))
    maybe_line.sort(key=lambda p: (when(p[0]), p[1].key))
    free = capacity - used
    skipped_waiter = False
    for _, g in yes_line:
        was_waiting = g.seat == "wait_yes"
        # A waiter is handed a seat only once the event has been full, and
        # not inside the cutoff. A new answer takes a free seat only when
        # nobody who can use it is already waiting ahead of it.
        may_seat = (g.size <= free
                    and not (was_waiting and (in_cutoff or not reached))
                    and not (not was_waiting and skipped_waiter))
        if may_seat:
            target[g.key] = {"seat": "seated", "queued_at": g.queued_at,
                             "offer_expires_at": None, "offer_passed": False,
                             "seat_forced": False}
            free -= g.size
            used += g.size
            if was_waiting:
                out.promoted.append(g)
            continue
        if was_waiting and g.size <= free:
            skipped_waiter = True
            if in_cutoff and reached:
                out.cutoff_next.append(g)
        target[g.key] = {"seat": "wait_yes", "queued_at": g.queued_at,
                         "offer_expires_at": None, "offer_passed": False,
                         "seat_forced": False}
        if not was_waiting:
            out.waitlisted.append(g)
        out.waiting_yes += 1
    # Full means full for someone: turning a yes away (their party didn't
    # fit what was left) counts, so they get the next seats that fit.
    if used >= capacity or out.waiting_yes:
        reached = True
    for _, g in maybe_line:
        offer = (reached and not in_cutoff and out.waiting_yes == 0
                 and g.size <= free)
        if offer:
            target[g.key] = {"seat": "offered", "queued_at": g.queued_at,
                             "offer_expires_at": _iso(
                                 now + dt.timedelta(hours=OFFER_HOURS)),
                             "offer_passed": False, "seat_forced": False}
            free -= g.size
            used += g.size
            out.offered.append(g)
            continue
        if (reached and in_cutoff and out.waiting_yes == 0
                and g.size <= free and not out.cutoff_next):
            out.cutoff_next.append(g)
        target[g.key] = {"seat": "wait_maybe", "queued_at": g.queued_at,
                         "offer_expires_at": None, "offer_passed": False,
                         "seat_forced": False}
        if g.seat != "wait_maybe":
            out.waitlisted.append(g)
        out.waiting_maybe += 1
    if used >= capacity:
        reached = True

    # Spread each household's fields over its members' replies.
    for uid, r in rsvps.items():
        t = target.get(hh_key(uid))
        if t is None:
            continue
        coming = answer_for(r, option_id, state) in ("yes", "maybe")
        want = t if coming else {"seat": None, "queued_at": None,
                                 "offer_expires_at": None,
                                 "offer_passed": False, "seat_forced": False}
        diff = {k: v for k, v in want.items() if getattr(r, k) != v}
        if diff:
            out.updates[uid] = diff
    out.reached = reached
    out.free = max(0, capacity - used)
    out.seated = sum(g.size for g in groups
                     if target.get(g.key, {}).get("seat") in ("seated",))
    return out


def _restart(g: Group, at: str) -> Group:
    return Group(key=g.key, user_ids=g.user_ids, answer=g.answer, size=g.size,
                 seat=g.seat, queued_at=at,
                 offer_expires_at=g.offer_expires_at,
                 offer_passed=False, forced=g.forced)


def seats_held(rsvps: dict, hh_key, key: str, option_id: str | None,
               state: str) -> int:
    """How many seats the household ``key`` holds now (0 if none)."""
    for g in build_groups(rsvps, hh_key, option_id, state):
        if g.key == key:
            return g.size if g.seat in ("seated", "offered") else 0
    return 0


def seats_taken(rsvps: dict, hh_key, option_id: str | None,
                state: str) -> int:
    return sum(g.size for g in build_groups(rsvps, hh_key, option_id, state)
               if g.seat in ("seated", "offered"))
