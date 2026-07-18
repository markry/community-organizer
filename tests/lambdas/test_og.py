"""Open Graph image route + unfurl meta tags."""
from __future__ import annotations

from community_organizer.lambdas import web


def test_og_image_response_is_png_binary():
    r = web._og_image_response()
    assert r["statusCode"] == 200
    assert r["headers"]["Content-Type"] == "image/png"
    assert r["headers"]["Cache-Control"].startswith("public")
    assert r["isBase64Encoded"] is True
    assert len(r["body"]) > 5000           # the real packaged PNG is ~210KB


def test_page_includes_og_tags():
    out = web._page("hello", title="Example Ushers")
    assert "property='og:image'" in out
    assert "/og-image.png" in out
    assert "name='twitter:card' content='summary_large_image'" in out
    # og:title rides the page title -> already per-app-ish
    assert "property='og:title' content='Example Ushers'" in out
    assert "property='og:image:width' content='1200'" in out


def test_og_title_is_escaped():
    out = web._page("x", title="A & B <ushers>")
    assert "A &amp; B &lt;ushers&gt;" in out
    assert "<ushers>" not in out           # no raw injection into the meta
