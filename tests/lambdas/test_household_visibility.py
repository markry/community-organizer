"""AA-facing household visibility on the app Members page (read-only, scoped)."""
from __future__ import annotations

from community_organizer.core import db
from community_organizer.core.models import Application, Community, Membership, User
from community_organizer.lambdas import web


def _seed():
    cid = "test-community"
    db.put_community(Community(community_id=cid, name="Parish"))
    app = Application(community_id=cid, name="Book Club",
                      app_type="flexible_event", app_id="bc")
    db.put_application(app)
    aa = User(community_id=cid, email="aa@example.com", name="Organizer")
    db.put_user(aa)
    db.put_membership(Membership(community_id=cid, app_id="bc",
                                 user_id=aa.user_id, app_role="aa"))
    return cid, app, aa


def test_members_page_shows_household_among_app_members(ddb_table):
    cid, app, aa = _seed()
    alice = User(community_id=cid, email="a@example.com", name="Alice Smith",
                 household_id="hh-1")
    bob = User(community_id=cid, email="b@example.com", name="Bob Smith",
               household_id="hh-1")
    for u in (alice, bob):
        db.put_user(u)
        db.put_membership(Membership(community_id=cid, app_id="bc",
                                     user_id=u.user_id, app_role="member"))
    body = web._users_page({}, aa, db.get_community(cid), app,
                           db.get_membership("bc", aa.user_id))["body"]
    assert "Household: Bob Smith" in body      # Alice's row names Bob
    assert "Household: Alice Smith" in body     # Bob's row names Alice


def test_household_scoped_to_app_members_only(ddb_table):
    """A member whose household partner is NOT in this app shows no household
    tie (an AA mustn't see ties to people outside their app)."""
    cid, app, aa = _seed()
    carol = User(community_id=cid, email="c@example.com", name="Carol Jones",
                 household_id="hh-2")
    dave = User(community_id=cid, email="d@example.com", name="Dave Jones",
                household_id="hh-2")           # same household, different app
    for u in (carol, dave):
        db.put_user(u)
    db.put_membership(Membership(community_id=cid, app_id="bc",
                                 user_id=carol.user_id, app_role="member"))
    # dave is NOT a member of bc
    body = web._users_page({}, aa, db.get_community(cid), app,
                           db.get_membership("bc", aa.user_id))["body"]
    assert "Dave Jones" not in body            # outsider never surfaced
    assert "Household:" not in body            # Carol shows no in-app tie


def test_three_person_household_lists_all_others(ddb_table):
    cid, app, aa = _seed()
    parent1 = User(community_id=cid, email="p1@example.com", name="Pat Lee",
                   household_id="hh-3")
    parent2 = User(community_id=cid, email="p2@example.com", name="Jamie Lee",
                   household_id="hh-3")
    kid = User(community_id=cid, email="k@example.com", name="Sam Lee",
               household_id="hh-3")
    for u in (parent1, parent2, kid):
        db.put_user(u)
        db.put_membership(Membership(community_id=cid, app_id="bc",
                                     user_id=u.user_id, app_role="member"))
    body = web._users_page({}, aa, db.get_community(cid), app,
                           db.get_membership("bc", aa.user_id))["body"]
    assert "Household: Jamie Lee, Sam Lee" in body     # Pat sees both others
