"""Per-app public page: slug, /a/<slug> landing + deep-link, card art."""
from __future__ import annotations

import base64
import struct
import zlib

from community_organizer.core import db
from community_organizer.core.models import Application, Community, Membership, User
from community_organizer.lambdas import web


def _tiny_png() -> bytes:
    """A valid 1x1 PNG (magic bytes matter for the upload validator)."""
    def chunk(typ, data):
        c = typ + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c))
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00\xff\xff\xff")
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


def _seed(slug="summer-book-club", desc="A friendly summer read.", role="aa"):
    cid = "test-community"
    db.put_community(Community(community_id=cid, name="Parish"))
    app = Application(community_id=cid, name="Summer Book Club",
                      app_type="flexible_event", app_id="bookclub",
                      public_slug=slug, description=desc)
    db.put_application(app)
    u = User(community_id=cid, email="a@b.com", name="AA")
    db.put_user(u)
    db.put_membership(Membership(community_id=cid, app_id="bookclub",
                                 user_id=u.user_id, app_role=role))
    return cid, app, u


# ---- slug helpers ---------------------------------------------------------

def test_slugify():
    assert web._slugify("Summer Couples Book Club") == "summer-couples-book-club"
    assert web._slugify("  Lectors & Ushers!! ") == "lectors-ushers"
    assert web._slugify("") == "app"


def test_unique_slug_appends(ddb_table):
    db.put_community(Community(community_id="c1", name="C"))
    a = Application(community_id="c1", name="Ushers", app_type="coverage",
                    public_slug="ushers")
    db.put_application(a)
    assert web._unique_slug("c1", "Ushers") == "ushers-2"
    assert web._unique_slug("c1", "Ushers", exclude_app_id=a.app_id) == "ushers"


def test_app_create_assigns_slug(ddb_table):
    db.put_community(Community(community_id="test-community", name="C"))
    ca = User(community_id="test-community", email="ca@example.com", name="CA",
              community_role="ca")
    db.put_user(ca)
    event = {"requestContext": {"http": {"method": "POST"}},
             "queryStringParameters": {"name": "Men's Group",
                                       "app_type": "flexible_event"}}
    web._api_app_create(event, ca, db.get_community("test-community"))
    apps = [a for a in db.list_applications("test-community")
            if a.name == "Men's Group"]
    assert apps and apps[0].public_slug == "men-s-group"


# ---- /a/<slug> landing ----------------------------------------------------

def test_landing_unknown_slug_404(ddb_table, monkeypatch):
    monkeypatch.setattr(web, "COMMUNITY_ID", "test-community")
    db.put_community(Community(community_id="test-community", name="C"))
    r = web._app_landing({}, "nope")
    assert r["statusCode"] == 404


def test_landing_public_card_for_logged_out(ddb_table, monkeypatch):
    cid, app, _ = _seed()
    monkeypatch.setattr(web, "COMMUNITY_ID", cid)
    r = web._app_landing({}, app.public_slug)            # no auth cookie
    assert r["statusCode"] == 200
    b = r["body"]
    assert "Summer Book Club" in b
    assert "Sign in to Summer Book Club" in b
    assert f"/login?next=" in b and "app_id%3Dbookclub" in b  # deep-link through login
    # per-app OG tags
    assert f"https://{web.DOMAIN_NAME}/og/bookclub.png" in b
    assert "property='og:title' content='Summer Book Club'" in b
    assert "A friendly summer read." in b                 # og:description


def test_router_resolves_home_and_a_prefixes(ddb_table, monkeypatch):
    """Canonical /home/<slug> and the legacy /a/<slug> alias both route to
    the public landing for the app (so links shared before the rename
    still work)."""
    cid, app, _ = _seed()
    monkeypatch.setattr(web, "COMMUNITY_ID", cid)
    for prefix in ("/home/", "/a/"):
        r = web.lambda_handler({"rawPath": f"{prefix}{app.public_slug}"}, None)
        assert r["statusCode"] == 200, prefix
        assert "Summer Book Club" in r["body"], prefix


def test_landing_member_deep_links_in(ddb_table, monkeypatch):
    cid, app, u = _seed()
    monkeypatch.setattr(web, "COMMUNITY_ID", cid)
    monkeypatch.setattr(web, "_maybe_current_user", lambda e: u)
    r = web._app_landing({}, app.public_slug)
    assert r["statusCode"] == 302
    assert r["headers"]["Location"] == "/?app_id=bookclub"


def test_landing_signed_in_nonmember_no_leak(ddb_table, monkeypatch):
    cid, app, _ = _seed()
    other = User(community_id=cid, email="x@y", name="Outsider")
    db.put_user(other)
    monkeypatch.setattr(web, "COMMUNITY_ID", cid)
    monkeypatch.setattr(web, "_maybe_current_user", lambda e: other)
    r = web._app_landing({}, app.public_slug)
    assert r["statusCode"] == 200
    assert "not a member" in r["body"]                    # no deep-link, no data


# ---- og image route -------------------------------------------------------

def test_og_image_falls_back_to_generic(ddb_table, monkeypatch):
    cid, app, _ = _seed()
    monkeypatch.setattr(web, "COMMUNITY_ID", cid)
    r = web._app_og_image_response("bookclub")               # no custom art
    assert r["statusCode"] == 200
    assert r["headers"]["Content-Type"] == "image/png"


# ---- save + upload --------------------------------------------------------

def test_sharing_save_sets_slug_and_desc(ddb_table):
    cid, app, u = _seed(slug="old-slug")
    event = {"requestContext": {"http": {"method": "POST"}},
             "queryStringParameters": {
                 "slug": "Summer Reads", "description": " new blurb ",
                 "version": str(app.version)}}
    r = web._api_sharing_save(event, u, db.get_community(cid), app,
                              db.get_membership("bookclub", u.user_id))
    assert r["statusCode"] == 302
    fresh = db.get_application(cid, "bookclub")
    assert fresh.public_slug == "summer-reads"
    assert fresh.description == "new blurb"


def test_sharing_save_rejects_duplicate_slug(ddb_table):
    cid, app, u = _seed(slug="bookclub-slug")
    other = Application(community_id=cid, name="Other", app_type="coverage",
                        app_id="other", public_slug="taken")
    db.put_application(other)
    event = {"requestContext": {"http": {"method": "POST"}},
             "queryStringParameters": {"slug": "taken", "version": "0"}}
    r = web._api_sharing_save(event, u, db.get_community(cid), app,
                              db.get_membership("bookclub", u.user_id))
    assert "error=" in r["headers"]["Location"]
    assert db.get_application(cid, "bookclub").public_slug == "bookclub-slug"  # unchanged


# ---- editable public link on the App Settings tab -------------------------

def test_app_slug_save_updates_and_redirects_to_next(ddb_table):
    cid, app, u = _seed(slug="old-slug")
    event = {"requestContext": {"http": {"method": "POST"}},
             "queryStringParameters": {
                 "slug": "Tuesday Nights", "version": str(app.version),
                 "next": "/admin/settings"}}
    r = web._api_app_slug_save(event, u, db.get_community(cid), app,
                               db.get_membership("bookclub", u.user_id))
    assert r["statusCode"] == 302
    assert r["headers"]["Location"].startswith("/admin/settings?notice=")
    assert db.get_application(cid, "bookclub").public_slug == "tuesday-nights"


def test_app_slug_save_rejects_duplicate(ddb_table):
    cid, app, u = _seed(slug="bookclub-slug")
    db.put_application(Application(community_id=cid, name="Other",
                                  app_type="coverage", app_id="other",
                                  public_slug="taken"))
    event = {"requestContext": {"http": {"method": "POST"}},
             "queryStringParameters": {"slug": "taken", "version": "0",
                                       "next": "/admin/settings"}}
    r = web._api_app_slug_save(event, u, db.get_community(cid), app,
                               db.get_membership("bookclub", u.user_id))
    assert "error=" in r["headers"]["Location"]
    assert "/admin/settings" in r["headers"]["Location"]       # error lands on next
    assert db.get_application(cid, "bookclub").public_slug == "bookclub-slug"  # unchanged


def test_app_slug_save_allows_ca_who_is_not_app_admin(ddb_table):
    # A community admin who holds no AA membership in this app can still
    # edit the link (Share-page gate parity).
    cid, app, _ = _seed(slug="old", role="member")
    ca = User(community_id=cid, email="ca@example.com", name="CA", community_role="ca")
    db.put_user(ca)
    event = {"requestContext": {"http": {"method": "POST"}},
             "queryStringParameters": {"slug": "renamed", "version": str(app.version),
                                       "next": "/admin/settings"}}
    r = web._api_app_slug_save(event, ca, db.get_community(cid), app, None)
    assert r["statusCode"] == 302
    assert db.get_application(cid, "bookclub").public_slug == "renamed"


def test_settings_page_shows_public_page_block(ddb_table):
    cid, app, u = _seed(slug="summer-reads", desc="Summer read.")
    event = {"requestContext": {"http": {"method": "GET"}}}
    r = web._admin_settings_page(event, u, db.get_community(cid), app,
                                 db.get_membership("bookclub", u.user_id))
    assert r["statusCode"] == 200
    body = r["body"]
    # Consolidated Public page block: link, slug, description, card image.
    assert "Public page" in body
    assert "/api/app/slug" in body
    assert f"https://{web.DOMAIN_NAME}/home/summer-reads" in body
    assert "Public description" in body and "Summer read." in body
    assert "/api/sharing/upload-art" in body and "Card image" in body
    # No standalone Share tab in the nav anymore.
    assert "/admin/sharing" not in body


def test_sharing_route_redirects_to_settings(ddb_table):
    cid, app, u = _seed()
    r = web._app_sharing_page({"requestContext": {"http": {"method": "GET"}}},
                              u, db.get_community(cid), app,
                              db.get_membership("bookclub", u.user_id))
    assert r["statusCode"] == 302
    assert r["headers"]["Location"] == "/admin/settings"


def test_slug_save_also_persists_description(ddb_table):
    cid, app, u = _seed(slug="old", desc="old blurb")
    event = {"requestContext": {"http": {"method": "POST"}},
             "queryStringParameters": {
                 "slug": "New Name", "description": " fresh blurb ",
                 "version": str(app.version), "next": "/admin/settings"}}
    r = web._api_app_slug_save(event, u, db.get_community(cid), app,
                               db.get_membership("bookclub", u.user_id))
    assert r["statusCode"] == 302
    fresh = db.get_application(cid, "bookclub")
    assert fresh.public_slug == "new-name"
    assert fresh.description == "fresh blurb"


def test_settings_page_public_block_for_event_app(ddb_table):
    # Event apps (date-poll) used to reach the public page via the Share
    # tab; after consolidation they reach it on Settings, and the coverage
    # reminders form is NOT rendered for them.
    cid, app, u = _seed(slug="book-club")          # _seed app_type is flexible_event
    event = {"requestContext": {"http": {"method": "GET"}}}
    r = web._admin_settings_page(event, u, db.get_community(cid), app,
                                 db.get_membership("bookclub", u.user_id))
    assert r["statusCode"] == 200
    body = r["body"]
    assert "Public page" in body and "/api/app/slug" in body
    assert "/api/settings/defaults" not in body    # coverage reminders form suppressed


def test_settings_page_coverage_app_keeps_reminders(ddb_table):
    # A coverage app still shows the reminders/terminology form alongside
    # the new public-page block.
    cid = "cov-c"
    db.put_community(Community(community_id=cid, name="Parish"))
    app = Application(community_id=cid, name="Ushers", app_type="coverage",
                      app_id="ush", public_slug="ushers")
    db.put_application(app)
    u = User(community_id=cid, email="aa2@b.com", name="AA2")
    db.put_user(u)
    db.put_membership(Membership(community_id=cid, app_id="ush",
                                 user_id=u.user_id, app_role="aa"))
    event = {"requestContext": {"http": {"method": "GET"}}}
    r = web._admin_settings_page(event, u, db.get_community(cid), app,
                                 db.get_membership("ush", u.user_id))
    body = r["body"]
    assert "Public page" in body                   # consolidated block present
    assert "/api/settings/defaults" in body        # reminders form still here


def test_settings_page_materialises_slug_when_missing(ddb_table):
    cid, app, u = _seed(slug=None)
    assert db.get_application(cid, "bookclub").public_slug is None
    event = {"requestContext": {"http": {"method": "GET"}}}
    web._admin_settings_page(event, u, db.get_community(cid), app,
                             db.get_membership("bookclub", u.user_id))
    assert db.get_application(cid, "bookclub").public_slug          # now set


def test_upload_art_rejects_non_image(ddb_table, monkeypatch):
    cid, app, u = _seed()
    monkeypatch.setattr(web, "OG_ART_BUCKET", "b")
    body = (b'------x\r\nContent-Disposition: form-data; name="art"; '
            b'filename="a.png"\r\nContent-Type: image/png\r\n\r\n'
            b'not a real image\r\n------x--\r\n')
    event = {"requestContext": {"http": {"method": "POST"}},
             "headers": {"content-type": "multipart/form-data; boundary=----x"},
             "body": base64.b64encode(body).decode(), "isBase64Encoded": True}
    r = web._api_sharing_upload_art(event, u, db.get_community(cid), app,
                                    db.get_membership("bookclub", u.user_id))
    assert "error=" in r["headers"]["Location"]
    assert db.get_application(cid, "bookclub").og_art_content_type is None


def test_upload_art_accepts_png(ddb_table, monkeypatch):
    import boto3
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="ogart")
    cid, app, u = _seed()
    monkeypatch.setattr(web, "OG_ART_BUCKET", "ogart")
    monkeypatch.setattr(web, "_s3_client_cache", None)
    png = _tiny_png()
    body = (b'------x\r\nContent-Disposition: form-data; name="art"; '
            b'filename="card.png"\r\nContent-Type: image/png\r\n\r\n'
            + png + b'\r\n------x--\r\n')
    event = {"requestContext": {"http": {"method": "POST"}},
             "headers": {"content-type": "multipart/form-data; boundary=----x"},
             "body": base64.b64encode(body).decode(), "isBase64Encoded": True}
    r = web._api_sharing_upload_art(event, u, db.get_community(cid), app,
                                    db.get_membership("bookclub", u.user_id))
    assert r["statusCode"] == 302
    assert db.get_application(cid, "bookclub").og_art_content_type == "image/png"
