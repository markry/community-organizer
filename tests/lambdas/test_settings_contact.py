"""Personal settings: phone + email/SMS channel controls (the SMS opt-in UI)."""
from __future__ import annotations

import urllib.parse

from community_organizer.core import db
from community_organizer.core.models import Application, Community, Membership, User
from community_organizer.lambdas import web


def _setup(channel="email", phone=None):
    cid = "example-community"
    db.put_community(Community(community_id=cid, name="Parish"))
    app = Application(community_id=cid, name="Book Club",
                      app_type="flexible_event", app_id="bookclub")
    db.put_application(app)
    u = User(community_id=cid, email="admin@example.com", name="Morgan",
             channel=channel, phone=phone)
    db.put_user(u)
    db.put_membership(Membership(community_id=cid, app_id="bookclub",
                                 user_id=u.user_id, app_role="member"))
    return cid, app, u


def _post(fields):
    return {
        "rawPath": "/api/settings/save",
        "requestContext": {"http": {"method": "POST"}},
        "body": urllib.parse.urlencode(fields),
    }


def test_save_sets_phone_and_channel(ddb_table):
    cid, app, u = _setup()
    ev = _post({"channel": "both", "phone": "555-555-0100", "alarm": "60"})
    resp = web._api_settings_save(ev, u, db.get_community(cid), app,
                                  db.get_membership("bookclub", u.user_id))
    assert resp["statusCode"] == 302
    fresh = db.get_user(cid, u.user_id)
    assert fresh.channel == "both"
    assert fresh.phone == "555-555-0100"


def test_blank_phone_clears(ddb_table):
    cid, app, u = _setup(channel="sms", phone="555-555-0100")
    ev = _post({"channel": "email", "phone": "", "alarm": ""})
    web._api_settings_save(ev, u, db.get_community(cid), app,
                           db.get_membership("bookclub", u.user_id))
    fresh = db.get_user(cid, u.user_id)
    assert fresh.phone is None
    assert fresh.channel == "email"


def test_invalid_channel_ignored(ddb_table):
    cid, app, u = _setup(channel="email")
    ev = _post({"channel": "carrier-pigeon", "phone": "5555550100"})
    web._api_settings_save(ev, u, db.get_community(cid), app,
                           db.get_membership("bookclub", u.user_id))
    fresh = db.get_user(cid, u.user_id)
    assert fresh.channel == "email"            # unchanged, not the bogus value
    assert fresh.phone == "5555550100"


def test_settings_page_renders_phone_and_channel(ddb_table):
    cid, app, u = _setup(channel="sms", phone="555-555-0100")
    resp = web._settings_page({}, u, db.get_community(cid), app,
                              db.get_membership("bookclub", u.user_id))
    body = resp["body"]
    assert "name='phone'" in body
    assert "555-555-0100" in body              # current value prefilled
    assert "name='channel'" in body
    assert "Text message (SMS)" in body
    # the 'sms' radio is the checked one
    assert "value='sms' checked" in body


def test_member_nudge_enabled(ddb_table):
    cid, app, u = _setup(channel="email", phone="555-555-0100")
    body = web._settings_page({}, u, db.get_community(cid), app,
                              db.get_membership("bookclub", u.user_id))["body"]
    assert "onsubmit='return _settingsCheck(this)'" in body
    assert "var _memberNudge=true" in body     # members get the optional nudge


def test_member_nudge_off_for_admin_but_guard_stays(ddb_table):
    cid, app, u = _setup(channel="email", phone="555-555-0100")
    db.put_membership(Membership(community_id=cid, app_id="bookclub",
                                 user_id=u.user_id, app_role="aa"))
    body = web._settings_page({}, u, db.get_community(cid), app,
                              db.get_membership("bookclub", u.user_id))["body"]
    assert "var _memberNudge=false" in body     # admin: no optional nudge
    assert "_settingsCheck" in body             # ...but the coherence guard stays


def test_sms_without_phone_downgrades_to_email(ddb_table):
    cid, app, u = _setup(channel="email", phone=None)
    ev = _post({"channel": "both", "phone": ""})      # text, but no number
    resp = web._api_settings_save(ev, u, db.get_community(cid), app,
                                  db.get_membership("bookclub", u.user_id))
    assert "nophone=1" in resp["headers"]["Location"]
    fresh = db.get_user(cid, u.user_id)
    assert fresh.channel == "email"             # not persisted as both
    assert fresh.phone is None


def test_sms_with_phone_persists(ddb_table):
    cid, app, u = _setup(channel="email", phone=None)
    ev = _post({"channel": "sms", "phone": "555-555-0100"})
    web._api_settings_save(ev, u, db.get_community(cid), app,
                           db.get_membership("bookclub", u.user_id))
    fresh = db.get_user(cid, u.user_id)
    assert fresh.channel == "sms"               # valid number -> honored


def test_settings_reachable_link_in_flexible_home_for_member(ddb_table):
    cid, app, u = _setup()
    resp = web._flexible_home(event={}, user=u, community=db.get_community(cid),
                              app=app, membership=db.get_membership("bookclub", u.user_id),
                              org_name="Book Club")
    assert "/settings" in resp["body"]         # members can find their settings
