"""The seat planner (core/capacity.py): pure rules, no table."""
import datetime as dt

from community_organizer.core import capacity as seats
from community_organizer.core.models import FlexibleRSVP

OPT = "opt1"
NOW = dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.timezone.utc)
FAR = NOW + dt.timedelta(days=10)


def _t(minutes: int) -> str:
    return (NOW - dt.timedelta(minutes=100 - minutes)).isoformat(
        timespec="seconds")


def r(uid, vote, *, size=None, seat=None, at=0, **kw):
    x = FlexibleRSVP(community_id="c", app_id="a", event_id="e", user_id=uid,
                     votes={OPT: vote} if vote else {}, party_size=size,
                     seat=seat, queued_at=_t(at) if seat else None,
                     updated_at=_t(at), **kw)
    return x


HH = {}


def key(uid):
    return HH.get(uid, f"solo:{uid}")


def run(rsvps, cap=4, reached=False, starts=FAR, answered=None, now=NOW):
    rs = {x.user_id: x for x in rsvps}
    p = seats.plan(capacity=cap, rsvps=rs, hh_key=key, option_id=OPT,
                   state="scheduled", reached=reached, now=now,
                   starts_at=starts, answered_at=answered)
    for uid, d in p.updates.items():
        for k, v in d.items():
            setattr(rs[uid], k, v)
    return p, rs


def seat_of(rs):
    return {u: x.seat for u, x in rs.items()}


def setup_function():
    HH.clear()


def test_first_come_first_served_then_yes_tier():
    p, rs = run([r("a", "yes", size=2, at=1), r("b", "yes", size=2, at=2),
                 r("c", "yes", at=3)])
    assert seat_of(rs) == {"a": "seated", "b": "seated", "c": "wait_yes"}
    assert p.reached and [g.key for g in p.waitlisted] == ["solo:c"]


def test_blank_party_counts_one():
    _, rs = run([r(u, "yes", at=i) for i, u in enumerate("abcde")])
    assert list(seat_of(rs).values()).count("seated") == 4


def test_maybe_never_seated_and_not_offered_before_full():
    p, rs = run([r("a", "yes", at=1), r("m", "maybe", at=2)])
    assert seat_of(rs) == {"a": "seated", "m": "wait_maybe"}
    assert not p.offered and not p.reached


def test_cancellation_promotes_first_yes_whose_party_fits():
    _, rs = run([r("a", "yes", size=2, at=1), r("b", "yes", size=2, at=2),
                 r("c", "yes", size=3, at=3), r("d", "yes", at=4)])
    assert rs["c"].seat == "wait_yes" and rs["d"].seat == "wait_yes"
    rs["b"].votes = {OPT: "no"}
    p, rs = run(list(rs.values()), reached=True)
    # c's party of 3 doesn't fit the 2 freed seats; d's does.
    assert rs["d"].seat == "seated" and rs["c"].seat == "wait_yes"
    assert [g.key for g in p.promoted] == ["solo:d"]
    assert rs["b"].seat is None


def test_maybe_tier_only_when_no_yes_waiting_then_offer():
    _, rs = run([r("a", "yes", size=3, at=1), r("b", "yes", at=2),
                 r("m", "maybe", at=3)])
    rs["b"].votes = {OPT: "no"}
    p, rs = run(list(rs.values()), reached=True)
    assert rs["m"].seat == "offered" and rs["m"].offer_expires_at
    assert [g.key for g in p.offered] == ["solo:m"]


def test_offer_lapses_and_passes_on():
    _, rs = run([r("a", "yes", size=3, at=1), r("m", "maybe", at=2),
                 r("n", "maybe", at=3)], reached=True)
    assert rs["m"].seat == "offered"
    later = NOW + dt.timedelta(hours=25)
    p, rs = run(list(rs.values()), reached=True, now=later,
                starts=later + dt.timedelta(days=5))
    assert rs["m"].seat is None and rs["m"].offer_passed
    assert rs["n"].seat == "offered"
    assert [g.key for g in p.lapsed] == ["solo:m"]


def test_accepting_offer_seats():
    _, rs = run([r("a", "yes", size=3, at=1), r("m", "maybe", at=2)],
                reached=True)
    rs["m"].votes = {OPT: "yes"}
    p, rs = run(list(rs.values()), reached=True)
    assert rs["m"].seat == "seated" and not p.promoted


def test_unfilled_event_never_contacts_maybes():
    _, rs = run([r("a", "yes", at=1), r("m", "maybe", at=2)])
    rs["a"].votes = {OPT: "no"}
    p, rs = run(list(rs.values()))
    assert rs["m"].seat == "wait_maybe" and not p.offered


def test_cutoff_holds_promotion_and_names_next():
    _, rs = run([r("a", "yes", size=4, at=1), r("b", "yes", at=2)])
    rs["a"].votes = {OPT: "no"}
    p, rs = run(list(rs.values()), reached=True,
                starts=NOW + dt.timedelta(hours=5))
    assert rs["b"].seat == "wait_yes" and not p.promoted
    assert [g.key for g in p.cutoff_next] == ["solo:b"]


def test_new_yes_does_not_jump_waiter_held_by_cutoff():
    _, rs = run([r("a", "yes", size=4, at=1), r("b", "yes", at=2)])
    rs["a"].votes = {OPT: "no"}
    rs["z"] = r("z", "yes", at=50)
    p, rs = run(list(rs.values()), reached=True,
                starts=NOW + dt.timedelta(hours=5))
    assert rs["z"].seat == "wait_yes"


def test_household_is_one_unit_at_largest_party():
    HH.update({"h1": "hh", "h2": "hh"})
    _, rs = run([r("h1", "yes", size=3, at=1), r("h2", "yes", size=2, at=5),
                 r("x", "yes", at=6), r("y", "yes", at=7)])
    assert rs["h1"].seat == rs["h2"].seat == "seated"
    assert rs["x"].seat == "seated" and rs["y"].seat == "wait_yes"


def test_switch_tier_goes_to_back():
    _, rs = run([r("a", "yes", size=4, at=1), r("b", "yes", at=2),
                 r("m", "maybe", at=3)])
    rs["b"].votes = {OPT: "maybe"}
    _, rs = run(list(rs.values()), reached=True)
    assert rs["b"].seat == "wait_maybe"
    assert rs["b"].queued_at > rs["m"].queued_at


def test_seated_never_unseated_when_capacity_lowered():
    _, rs = run([r("a", "yes", size=2, at=1), r("b", "yes", size=2, at=2)])
    p, rs = run(list(rs.values()), cap=2)
    assert seat_of(rs) == {"a": "seated", "b": "seated"}
    assert p.free == 0


def test_raised_capacity_promotes():
    _, rs = run([r("a", "yes", size=4, at=1), r("b", "yes", at=2)])
    p, rs = run(list(rs.values()), cap=5, reached=True)
    assert rs["b"].seat == "seated" and p.promoted


def test_no_changes_is_stable():
    _, rs = run([r("a", "yes", at=1), r("b", "maybe", at=2)])
    p, _ = run(list(rs.values()))
    assert p.updates == {}
