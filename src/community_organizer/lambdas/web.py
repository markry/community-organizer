"""Web Lambda — SPA + REST behind Cognito hosted UI.

Routes:
  GET /                       admin dashboard or volunteer "your upcoming Masses"
  GET /schedules/<yyyy-mm>    admin drill-down: slots + assignments
  GET /admin/emails           admin email activity log
  GET /admin/users            admin user management
  GET /auth/callback          OAuth code exchange
  GET /auth/logout            clear cookies + Cognito logout
  GET /healthz                liveness
"""
from __future__ import annotations

import base64
import datetime as dt
import hmac
import json
import secrets

import boto3
import html
import logging
import os
import re
import urllib.parse
from zoneinfo import ZoneInfo

from community_organizer import auth
from community_organizer.core import (
    db, ical, publishing, recurrence, schedule_email, scheduling, standing,
)
from community_organizer.providers.sms import to_e164
from community_organizer.core.models import (
    Application,
    Assignment,
    BlockedDate,
    Cohort,
    Community,
    EmailLog,
    EventToken,
    FlexibleEvent,
    FlexiblePollOption,
    FlexibleRSVP,
    FlexibleSeries,
    Membership,
    Schedule,
    Slot,
    SlotTemplate,
    StandingOccurrence,
    StandingRSVP,
    StandingSeries,
    User,
)

_DAY_LABEL = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri", 5: "Sat", 6: "Sun"}
_DAY_SHORT = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri", 5: "Sat", 6: "Sun"}
_ORDINAL_SHORT = {1: "First", 2: "Second", 3: "Third", 4: "Fourth", -1: "Last"}


_HELP_ICON = ("<span style='display:inline-block;width:14px;height:14px;"
              "border-radius:50%;border:1px solid #aaa;text-align:center;"
              "font-size:10px;line-height:14px;color:#888;cursor:pointer;"
              "margin-left:4px;vertical-align:middle'>?</span>")


def _build_user_corner(user: User, community: Community | None,
                       current_app: "Application | None" = None) -> str:
    """Floating top-right chip with the user's name, role, the apps
    they can pivot into, and Sign out.

    Layout: single column, always. The chip's vertical real estate
    matters on mobile, so the apps list is capped at
    ``_CORNER_MAX_APP_LINES`` items. Beyond that, the last slot
    becomes a "More apps →" link to ``/launcher`` (the cross-app
    picker) and the rest are dropped from the chip. The current app
    is sorted first when present so it never falls into the overflow;
    the rest sort alphabetically by name.

    Pre-2026-06-03 the corner spilled extra apps into a second column
    to the left — fine on desktop, awful on phones where it ran off
    the screen. The cap + overflow link replaces that path entirely.
    """
    # Header lines.
    header_lines = [f"<b>{html.escape(user.name)}</b>"]
    if user.community_role in ("ca", "ua"):
        role_label = _ROLE_LABEL.get(user.community_role, user.community_role)
        header_lines.append(
            f"<span style='color:#555'>{html.escape(role_label)}</span>")
        # The CA landing page sits ABOVE all apps. Surface it here so
        # a CA can always step back out to manage apps/users without
        # hunting for the URL.
        header_lines.append(
            "<a href='/admin/apps' style='color:#888'>"
            "Community admin &rarr;</a>"
        )
    # For CAs/UAs show every app in the community (so they can pivot
    # without needing a Membership row); for plain members show only
    # the apps they belong to. Either way, each app becomes a clickable
    # pivot link via ?app_id=.
    if community:
        all_apps = list(db.list_applications(community.community_id))
    else:
        all_apps = []
    memberships = list(db.list_memberships_for_user(user.user_id))
    member_app_ids = {m.app_id: m for m in memberships}
    if user.community_role in ("ca", "ua"):
        visible_apps = all_apps
    else:
        visible_apps = [a for a in all_apps if a.app_id in member_app_ids]

    # Sort: current app first (so it's never the one that gets dropped
    # into the overflow), then alphabetically by name. MRU ordering is
    # deliberately not implemented here; revisit if multi-app users need it.
    current_id = current_app.app_id if current_app else None

    def _corner_sort_key(app_obj):
        return (
            0 if app_obj.app_id == current_id else 1,
            (app_obj.name or "").lower(),
        )

    visible_apps.sort(key=_corner_sort_key)

    overflow = len(visible_apps) > _CORNER_MAX_APP_LINES
    n_shown = (_CORNER_MAX_APP_LINES - 1) if overflow else len(visible_apps)
    apps_to_render = visible_apps[:n_shown]
    n_hidden = len(visible_apps) - n_shown

    app_lines: list[str] = []
    for app_obj in apps_to_render:
        mem = member_app_ids.get(app_obj.app_id)
        if mem:
            effective_role = mem.app_role
        elif user.community_role in ("ca", "ua"):
            effective_role = "aa"
        else:
            effective_role = "member"
        role_label = _ROLE_LABEL.get(effective_role, effective_role)
        help_url = "/help/admin" if effective_role == "aa" else "/help/member"
        help_link = (
            f"<a href='{help_url}' target='_blank' "
            f"title='Help'>{_HELP_ICON}</a>"
        )
        is_current = current_id == app_obj.app_id
        weight = "font-weight:600" if is_current else ""
        app_link = (
            f"<a href='/?app_id={app_obj.app_id}' "
            f"style='color:#555;{weight}'>{html.escape(app_obj.name)}</a>"
        )
        app_lines.append(
            f"{app_link}&mdash;{html.escape(role_label)}{help_link}"
        )

    if overflow:
        # Overflow leads to /launcher, the cross-app picker users already
        # know from idle re-entry.
        app_lines.append(
            f"<a href='/launcher' style='color:#2a7;font-style:italic'>"
            f"More apps ({n_hidden}) &rarr;</a>"
        )

    sign_out = (
        "<a href='/auth/logout' style='color:#888'>Sign out</a>"
    )
    # Light-gray translucent background so the menu stands out from
    # whatever content is scrolling underneath without obscuring it
    # entirely. Padding and rounded corners keep the chip visually
    # distinct from the body.
    base_style = ("position:fixed;top:12px;right:16px;font-size:0.8em;"
                  "color:#444;text-align:right;line-height:1.6;"
                  "background:rgba(245,245,245,0.85);"
                  "backdrop-filter:blur(4px);"
                  "-webkit-backdrop-filter:blur(4px);"
                  "padding:8px 12px;border-radius:8px;"
                  "border:1px solid rgba(0,0,0,0.06);"
                  "box-shadow:0 1px 3px rgba(0,0,0,0.06);"
                  # Mobile guardrail: never let the chip eat more than
                  # ~60% of the viewport width. Long app names wrap
                  # rather than push the chip off-screen.
                  "max-width:60vw")
    sign_out_block = f"<div style='margin-top:14px'>{sign_out}</div>"

    # Flat vs. collapsible. Total "lines" = header rows + app rows + the
    # sign-out row. On a phone a CA with several apps stacks 8-9 rows here
    # and crowds the page. Once content exceeds
    # _CORNER_COLLAPSED_MAX we collapse to a short summary (name + current
    # app, with Sign out kept one tap away) and tuck the role label,
    # community-admin link, and the full app list behind a click-to-expand
    # caret. Small chips (e.g. a single-app member) stay flat — nothing to
    # gain by hiding 2-3 rows.
    total_lines = len(header_lines) + len(app_lines) + 1
    if total_lines <= _CORNER_COLLAPSED_MAX:
        body = "<br>".join(header_lines + app_lines) + sign_out_block
        return f"<div style='{base_style}'>{body}</div>"

    def _effective_role(app_obj) -> str:
        mem = member_app_ids.get(app_obj.app_id)
        if mem:
            return mem.app_role
        if user.community_role in ("ca", "ua"):
            return "aa"
        return "member"

    summary_rows = [
        f"<b>{html.escape(user.name)}</b> "
        "<span class='sc-caret' style='color:#888'>&#9662;</span>"
    ]
    if current_app is not None:
        cur_role = _effective_role(current_app)
        cur_label = _ROLE_LABEL.get(cur_role, cur_role)
        summary_rows.append(
            f"<span style='color:#555'>{html.escape(current_app.name)}"
            f" &mdash; {html.escape(cur_label)}</span>"
        )
    # Behind the expander: the role label + community-admin link
    # (header_lines[1:]) and the full app-pivot list.
    detail_rows = header_lines[1:] + app_lines
    # Tiny vanilla-JS toggle (guarded so multiple injections are safe).
    # Flips the hidden detail block and swaps the caret glyph.
    toggle_js = (
        "<script>if(!window.scCornerToggle){window.scCornerToggle="
        "function(el){var m=el.parentNode.querySelector('.sc-more');"
        "var c=el.querySelector('.sc-caret');if(!m){return;}"
        "var open=m.style.display!=='none';"
        "m.style.display=open?'none':'block';"
        "if(c){c.innerHTML=open?'&#9662;':'&#9652;';}};}</script>"
    )
    # Sign out sits directly under the short summary here, so it gets a
    # tighter top margin than the flat layout's 14px (which would read as
    # an extra blank line under the collapsed chip).
    sign_out_tight = f"<div style='margin-top:2px'>{sign_out}</div>"
    body = (
        f"<div onclick='scCornerToggle(this)' style='cursor:pointer'>"
        f"{'<br>'.join(summary_rows)}</div>"
        f"<div class='sc-more' style='display:none;margin-top:6px'>"
        f"{'<br>'.join(detail_rows)}</div>"
        f"{sign_out_tight}{toggle_js}"
    )
    return f"<div style='{base_style}'>{body}</div>"


# Floating-corner cap. ≤5 apps render inline; >5 truncates to 4 + a
# "More apps →" link to /launcher. See _build_user_corner.
_CORNER_MAX_APP_LINES = 5

# Floating-corner collapse threshold. When the chip would render more
# than this many text rows (header + apps + sign-out), collapse it to a
# tap-to-expand summary so it stops crowding the page on mobile.
_CORNER_COLLAPSED_MAX = 4


def _auto_event_name(terminology: str, day_of_week: int, start_time: str,
                     ordinal: int | None = None) -> str:
    role = terminology.capitalize()
    day = _DAY_SHORT[day_of_week]
    time = _fmt_time(start_time)
    if ordinal is None:
        return f"{role} for {day} {time}"
    return (f"{role} for {_ORDINAL_SHORT.get(ordinal, '')} {day} {time}"
            .replace("  ", " "))
_STATE_COLORS = {"draft": "#888", "publishing": "#c80",
                 "published": "#2a7", "archived": "#aaa"}
# User-facing labels for schedule states. Internal state values stay as-is
# ("published" etc.); this is display-only. Vocabulary: Draft -> Active ->
# History, matching the age-out model. "published" reads as "Active" because
# publish is now state-only (it makes the schedule visible, it doesn't send).
_STATE_LABEL = {"draft": "Draft", "publishing": "Activating…",
                "published": "Active", "archived": "History",
                "materialized": "Active"}


def _state_label(state: str) -> str:
    return _STATE_LABEL.get(state, state)
_ROLE_LABEL = {"ca": "Community Admin", "ua": "User Admin", "aa": "App Admin", "member": "Member"}

# Number of rows ABOVE the row being edited that the post-save scroll
# fragment should target. Pulling the anchor up by N rows pushes the
# edited row N rows further down the viewport, giving it room to
# breathe under the floating corner overlay. 1 is the current setting;
# bump to 2 if the edited row still sits too high in practice.
_NEXT_ROW_OFFSET = 1

_SCHEDULE_PATH = re.compile(r"^/schedules/(\d{4}-\d{2})/?$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

log = logging.getLogger()
log.setLevel(logging.INFO)


DOMAIN_NAME = os.environ.get("DOMAIN_NAME", "community.example.org")

# Open Graph / social-unfurl image, served at /og-image.png. Loaded + base64'd
# once at cold start (a ~210KB PNG packaged alongside this module). Generic for
# now (calendar mark + "Community Organizer"); per-app art is a future override.
try:
    with open(os.path.join(os.path.dirname(__file__), "og_image.png"), "rb") as _f:
        _OG_IMAGE_B64 = base64.b64encode(_f.read()).decode()
except Exception:  # pragma: no cover - image always packaged, but never 500
    _OG_IMAGE_B64 = ""

COMMUNITY_ID = os.environ.get("COMMUNITY_ID", "")
# S3 bucket holding per-app custom social-card art (one object per app_id).
OG_ART_BUCKET = os.environ.get("OG_ART_BUCKET", "")
_s3_client_cache = None


def _s3():
    global _s3_client_cache
    if _s3_client_cache is None:
        _s3_client_cache = boto3.client("s3")
    return _s3_client_cache


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(name: str) -> str:
    """Lowercase, hyphenated, URL-safe slug from a display name."""
    s = _SLUG_RE.sub("-", (name or "").lower()).strip("-")
    return s[:60] or "app"


def _unique_slug(community_id: str, name: str,
                 exclude_app_id: str | None = None) -> str:
    """A _slugify(name) that's unique within the community (append -2, -3...)."""
    base = _slugify(name)
    taken = {a.public_slug for a in db.list_applications(community_id)
             if a.public_slug and a.app_id != exclude_app_id}
    if base not in taken:
        return base
    i = 2
    while f"{base}-{i}" in taken:
        i += 1
    return f"{base}-{i}"


def _ensure_slug(app: Application) -> str:
    """Return the app's public_slug, generating + persisting one on first use
    (covers apps created before slugs existed)."""
    if app.public_slug:
        return app.public_slug
    app.public_slug = _unique_slug(app.community_id, app.name, app.app_id)
    db.put_application(app)
    return app.public_slug

# Tracks which Application the user most recently pivoted into via
# ?app_id=... so that internal navigation (e.g. /admin/cohorts from
# the app home) stays in the same app instead of falling back to
# "first app by created_at". Cleared on logout.
ACTIVE_APP_COOKIE = "scheduler_app"
# Short TTL on purpose. Each authenticated request refreshes the
# cookie, so a user actively working in an app keeps it. Four hours
# of idle and the cookie expires → next visit lands on /launcher
# (for multi-app users), which is a sensible re-entry point after
# a break. CA-configurable later if a community wants something
# different.
_APP_COOKIE_TTL = 60 * 60 * 4   # 4 hours


# Control chars (CR/LF/NUL/tab/etc.) that must never appear in an
# email header — a CR/LF would inject a new header and an attacker
# could leak the email via Bcc:.
_HEADER_CTRL_RE = __import__("re").compile(r"[\x00-\x1f\x7f]")


def _text_to_html_paragraphs(text: str) -> str:
    """Convert plain text (typically from a ``<textarea>``) into
    structural HTML paragraphs suitable for embedding in an email
    body. Blank-line-separated blocks become ``<p>``; single
    newlines within a block become ``<br>``. The text is HTML-
    escaped first.

    Why not just wrap raw text in ``<div style='white-space:pre-wrap'>``?
    Many email clients (especially older Outlook + corporate webmail
    pipelines) strip or ignore that CSS property, collapsing the
    rendered text into a single wall with no paragraph breaks. Structural HTML
    (``<p>`` / ``<br>``) renders consistently because it doesn't
    depend on CSS preservation.

    Returns an empty string if the input is whitespace-only.
    """
    text = (text or "").strip()
    if not text:
        return ""
    paragraphs = re.split(r"\n\s*\n", text)
    out: list[str] = []
    for para in paragraphs:
        escaped = html.escape(para.strip())
        # Single newlines within a paragraph become <br>.
        escaped = escaped.replace("\n", "<br>")
        out.append(f"<p style='margin:0 0 12px 0'>{escaped}</p>")
    return "".join(out)


def _safe_header(s: str | None, *, max_len: int = 200) -> str:
    """Strip control characters from a string before using it as an
    email header value (Subject, display name, etc.).

    Defense in depth: even though the email library and SES sanitize
    some headers, relying on downstream services is fragile. We
    centralize the strip here so every From/To/Subject path is
    consistently scrubbed.
    """
    if not s:
        return ""
    cleaned = _HEADER_CTRL_RE.sub(" ", s)
    return cleaned[:max_len]


def _redirect_next(event: dict, default: str) -> dict:
    """Redirect to ``?next=`` if the form supplied one (validated via
    _safe_next) or to ``default``. The ``next`` value may carry an
    HTML fragment like ``/admin/community-users#user-abc`` so the
    landing page scrolls to the row the user was just editing —
    fragments must be passed as a hidden input on the form (browsers
    strip them from form action URLs)."""
    raw = _get_param(event, "next")
    return _redirect(_safe_next(raw) if raw else default)


def _safe_next(url: str | None) -> str:
    """Constrain a ``?next=`` parameter to a safe local path.

    Rejects anything that could become an off-host redirect:

        - absolute URLs (``http://...``, ``https://...``, ``//evil.com``)
        - protocol-relative (``//foo`` → browser interprets as
          ``https://foo``)
        - backslash-prefixed (``/\\evil.com`` → some browsers parse as
          protocol-relative)
        - missing leading ``/``

    Returns ``"/"`` for anything that doesn't pass.
    """
    if not url or not isinstance(url, str):
        return "/"
    # Must start with exactly one '/' and not '//' or '/\'.
    if not url.startswith("/"):
        return "/"
    if url.startswith("//") or url.startswith("/\\"):
        return "/"
    if "\r" in url or "\n" in url:
        return "/"
    return url


def _from_addr(admin_name: str | None = None,
               community_name: str | None = None) -> str:
    addr = f"organizer@{DOMAIN_NAME}"
    if admin_name and community_name:
        # Strip CR/LF/quote from display-name components — a name like
        # `Foo\r\nBcc: attacker@evil.com` would inject a Bcc header and
        # exfiltrate outbound mail otherwise.
        safe_name = _safe_header(admin_name).replace('"', '')
        safe_comm = _safe_header(community_name).replace('"', '')
        return f'"{safe_name} of {safe_comm}" <{addr}>'
    return addr


USER_POOL_ID = os.environ.get("USER_POOL_ID", "")
_cognito_client = None


def _get_cognito():
    global _cognito_client
    if _cognito_client is None:
        _cognito_client = boto3.client("cognito-idp",
                                       region_name=os.environ.get("AWS_REGION", "us-east-1"))
    return _cognito_client


def _create_cognito_user(email: str, name: str) -> str | None:
    if not USER_POOL_ID:
        return None
    try:
        # Create the user with MessageAction=SUPPRESS so no welcome email
        # is sent. We then set a random permanent password to move the
        # account into CONFIRMED state, which lets ForgotPassword work for
        # the unified new-user / forgot-password flow.
        import secrets
        random_pw = secrets.token_urlsafe(24) + "A1!"
        resp = _get_cognito().admin_create_user(
            UserPoolId=USER_POOL_ID,
            Username=email,
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
                {"Name": "name", "Value": name},
            ],
            MessageAction="SUPPRESS",
        )
        sub = resp["User"]["Username"]
        try:
            _get_cognito().admin_set_user_password(
                UserPoolId=USER_POOL_ID,
                Username=email,
                Password=random_pw,
                Permanent=True,
            )
        except Exception as e:
            log.warning("admin_set_user_password failed for %s: %s", email, e)
        log.info("created Cognito user %s for %s (CONFIRMED, no welcome email)",
                 sub, email)
        return sub
    except _get_cognito().exceptions.UsernameExistsException:
        try:
            resp = _get_cognito().admin_get_user(
                UserPoolId=USER_POOL_ID, Username=email)
            sub = resp["Username"]
            log.info("Cognito user already exists for %s, linked sub=%s", email, sub)
            return sub
        except Exception as e2:
            log.warning("Cognito admin_get_user failed for %s: %s", email, e2)
            return None
    except Exception as e:
        log.warning("Cognito admin_create_user failed for %s: %s", email, e)
        return None


def _get_param(event: dict, name: str) -> str | None:
    qs = event.get("queryStringParameters") or {}
    if name in qs:
        return qs[name]
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8", errors="replace")
    if body:
        parsed = urllib.parse.parse_qs(body, keep_blank_values=True)
        vals = parsed.get(name)
        if vals is not None:
            return vals[0]
    return None


def _http_method(event: dict) -> str:
    """The request's HTTP method, across both the v1 (``httpMethod``)
    and v2 (``requestContext.http.method``) Lambda event shapes."""
    return (event.get("requestContext", {}).get("http", {}).get("method")
            or event.get("httpMethod") or "GET").upper()


def _login_email_trusted(claims: dict) -> bool:
    """Whether a login's email claim is trustworthy enough to bond the
    Cognito identity to a pre-provisioned user record (the auto-link).

    True when Cognito asserts ``email_verified`` OR the login is a Google
    federation. Cognito does NOT propagate Google's verified-email flag —
    federated Google sign-ins arrive with ``email_verified=false`` even
    for @gmail addresses — but Google genuinely verifies the address it
    asserts, so a Google-federated login is trustworthy. Scoped to Google
    only: an arbitrary IdP with an unverified email is still refused
    (security fix D2). The token is JWKS-verified before we reach here, so
    a ``Google_*`` username / ``providerName: Google`` identity claim is
    authentic and unspoofable.
    """
    if claims.get("email_verified"):
        return True
    username = claims.get("cognito:username") or claims.get("username") or ""
    if isinstance(username, str) and username.startswith("Google_"):
        return True
    identities = claims.get("identities")
    if isinstance(identities, str):
        try:
            identities = json.loads(identities)
        except Exception:
            identities = None
    if isinstance(identities, list):
        for ident in identities:
            if isinstance(ident, dict) and "google" in (
                    str(ident.get("providerName", ""))
                    + str(ident.get("providerType", ""))).lower():
                return True
    return False


def _pluralize(word: str) -> str:
    """Naive English plural that handles -s, -x, -z, -ch, -sh suffixes."""
    if not word:
        return word
    w = word.lower()
    if w.endswith(("s", "x", "z")) or w.endswith(("ch", "sh")):
        return word + "es"
    if w.endswith("y") and len(w) > 1 and w[-2] not in "aeiou":
        return word[:-1] + "ies"
    return word + "s"


def _is_admin(user: User, membership: Membership | None = None) -> bool:
    """True iff this user has *App Admin* powers in the current app.

    App Admin status is decided solely by the per-app Membership row's
    ``app_role`` — not by the community role. A CA / UA who is only a
    plain Member of this specific app sees the same screens any other
    member sees, and the same API guards reject their mutations. They
    can still administer the app: they pivot to ``/admin/community-users``
    (CA-route, gated on community_role directly), promote themselves to
    AA, and come back. Community-wide CA powers live exclusively on the
    CA-mode surfaces (``/admin/apps``, ``/admin/community-users``).

    Earlier behavior (pre-#181) made any CA/UA an auto-admin in every
    app, but that broke the model that per-app powers come from per-app
    membership — a CA who joined an app as a regular member would
    still see the AA's "32 users total" home and the AA-only schedule
    create button. Now what you see matches what you are.
    """
    if membership and membership.app_role == "aa":
        return True
    return False


_CA_NAV = [
    ("Applications", "apps", "/admin/apps"),
    ("Community users", "community-users", "/admin/community-users"),
    ("Households", "households", "/admin/households"),
]


def _ca_nav_bar(current: str) -> str:
    """Bottom-of-page CA-mode navigation row.

    CA mode lives above any one app — there are only two surfaces
    here today (Apps + Community users). The bar exists for
    consistency with the AA admin nav (#194 chrome treatment): same
    visual frame, same "current page is bold" convention.

    There's no "back to home" concept inside CA mode (the two pages
    are siblings, not parent/child), so the leading-arrow trick from
    `_admin_nav_bar` doesn't apply here.
    """
    items: list[str] = []
    for label, nav_id, href in _CA_NAV:
        if nav_id == current:
            items.append(
                f"<span style='font-weight:600;color:#333'>{label}</span>")
        else:
            items.append(f"<a href='{href}' style='color:#2a7;"
                         f"text-decoration:none'>{label}</a>")
    return (
        "<nav style='margin-top:32px;padding-top:18px;"
        "border-top:2px solid #ccc;text-align:center;"
        "font-size:0.9em;color:#666'>"
        + " &nbsp;|&nbsp; ".join(items)
        + "</nav>"
    )


_ADMIN_NAV = [
    # "App home" rather than just "Home" — multi-app users (and all
    # CAs/UAs) need the distinction from the cross-app /launcher.
    ("App home", "home", "/"),
    ("Schedules", "schedules", "/schedules"),
    ("My Schedule", "my-schedule", "/your-schedule"),
    ("My Availability", "my-availability", "/my-availability"),
    ("Members", "members", "/admin/users"),
    ("Cohorts", "cohorts", "/admin/cohorts"),
    ("Send Email", "send-email", "/admin/send-email"),
    ("Emails", "emails", "/admin/emails"),
    ("Templates", "templates", "/admin/templates"),
    ("Settings", "settings", "/admin/settings"),
    # The former standalone "Share" tab (slug + card art + shareable link)
    # was folded into Settings (2026-06-29); /admin/sharing now redirects
    # there. No separate nav item.
    # Personal (per-user) settings — distinct from the app-config "Settings"
    # above. Kept out of the event-app skip list so an admin of a date-poll /
    # standing-event app can still reach their own notification + contact
    # settings (the only place to set phone + SMS). See _admin_nav_bar.
    ("My settings", "my-settings", "/settings"),
]


# App types that use the date-centric event UX (wall calendar + per-event /
# per-occurrence RSVP) rather than the coverage UX (slots / schedules /
# cohorts / templates).
EVENT_APP_TYPES = ("standing_event", "flexible_event")

# Coverage-only surfaces. When an EVENT_APP_TYPES app lands on one of these
# (a stale link, a guessed URL, a bookmarked deep link), _route bounces it
# home — these render slot / schedule / cohort / template UI with no meaning
# for an event app.
_COVERAGE_ONLY_PATHS = frozenset({
    "/schedules", "/your-schedule", "/my-availability",
    "/admin/templates", "/admin/cohorts",
    # NOTE: /admin/settings is intentionally NOT here — event apps now use
    # it too (it hosts the consolidated public-page controls). The page
    # itself renders only the relevant sections per app type.
})
_COVERAGE_ONLY_PREFIXES = (
    "/swap/", "/api/templates/", "/api/slots/", "/api/schedules/",
    "/api/swap/", "/api/cohorts/", "/api/settings/", "/api/assignments/",
)


def _is_coverage_only_path(path: str) -> bool:
    return (path in _COVERAGE_ONLY_PATHS
            or any(path.startswith(p) for p in _COVERAGE_ONLY_PREFIXES))


def _admin_nav_bar(current: str,
                   app: "Application | None" = None) -> str:
    """Render the bottom-of-page admin navigation row.

    For recurring_commitments apps, the home page IS the schedule
    view (a rolling 4-week grid), and there's no per-period
    draft/publish state to manage — so the "Schedules" tab is
    noise. Hidden for that app type.

    The leftmost item is always "App home" — when not the current
    page, it's rendered with a leading ← so it reads as a back-link
    even when the eye is scanning a long page in a hurry. The border-top
    is also a touch heavier than the rest of the page chrome so the
    nav block reads as navigation, not as more body content.
    """
    skip_ids: set[str] = set()
    extra: list[tuple[str, str, str]] = []
    if app is not None and app.app_type == "recurring_commitments":
        skip_ids.add("schedules")
    if app is not None and app.app_type in EVENT_APP_TYPES:
        # Event apps drop the coverage surfaces entirely. They DO keep the
        # Settings tab — it now hosts the public-page controls (link / card /
        # description) that used to live on the standalone Share tab; for
        # event apps it renders only that section (no coverage reminders).
        skip_ids |= {"schedules", "my-schedule", "my-availability",
                     "cohorts", "templates"}
        if app.app_type == "standing_event":
            extra.append(("Meeting schedule", "standing-setup",
                          "/standing/setup"))
    items = []
    for label, nav_id, href in (*_ADMIN_NAV, *extra):
        if nav_id in skip_ids:
            continue
        if nav_id == current:
            items.append(
                f"<span style='font-weight:600;color:#333'>{label}</span>")
        else:
            # Leading-arrow on App home so the back-link reads as such.
            display_label = (f"&larr; {label}" if nav_id == "home"
                             else label)
            items.append(f"<a href='{href}' style='color:#2a7;"
                         f"text-decoration:none'>{display_label}</a>")
    return (
        "<nav style='margin-top:32px;padding-top:18px;"
        "border-top:2px solid #ccc;text-align:center;"
        "font-size:0.9em;color:#666'>"
        + " &nbsp;|&nbsp; ".join(items)
        + "</nav>"
    )


def _warm() -> dict:
    """Warmer target (invoked by the EventBridge schedule). Unlike /healthz,
    this exercises the paths that make a cold *first real request* slow — it
    creates the boto3 DynamoDB client + connection (a cheap read) and primes
    the Cognito JWKS cache used for token verification — so the container is
    FULLY warm, not just its Python init. Best-effort; never errors."""
    try:
        db.get_community(COMMUNITY_ID)          # warms boto3 + DDB connection
    except Exception:
        pass
    try:
        auth._fetch_jwks()                      # warms token-verify JWKS cache
    except Exception:
        pass
    return _text(200, "warm")


def _api_rum(event: dict) -> dict:
    """Sink for the client Real-User-Monitoring beacon (navigator.sendBeacon).
    Logs perceived page-load timings and returns 204. Public + best-effort:
    the body is size-capped and any parse error is swallowed so a malformed
    or hostile beacon can never error or do work."""
    try:
        raw = event.get("body") or ""
        if event.get("isBase64Encoded"):
            raw = base64.b64decode(raw).decode("utf-8", "replace")
        if 0 < len(raw) <= 400:
            d = json.loads(raw)
            log.info("RUM path=%s ttfb=%s dcl=%s load=%s nav=%s",
                     str(d.get("p"))[:120], d.get("ttfb"), d.get("dcl"),
                     d.get("load"), str(d.get("type"))[:16])
    except Exception:
        pass
    return {"statusCode": 204, "headers": {}, "body": ""}


_CONTAINER_COLD = True
# Paths not worth a PERF log line (health checks, static art, RUM sink).
_PERF_SKIP_PREFIXES = ("/healthz", "/warmer", "/og-image.png", "/og/",
                       "/api/rum", "/favicon.ico")


def lambda_handler(event: dict, context) -> dict:  # noqa: ARG001
    """Thin timing wrapper around _dispatch. Logs one PERF line per request
    (total time, DDB call count/time, cold-start flag) so we can see where
    latency actually goes without hunting for slow pages. Near-zero overhead."""
    global _CONTAINER_COLD
    import time as _t
    cold, _CONTAINER_COLD = _CONTAINER_COLD, False
    path = event.get("rawPath") or event.get("path") or "/"
    if any(path.startswith(p) for p in _PERF_SKIP_PREFIXES):
        return _dispatch(event, context)
    db.reset_ddb_metrics()
    t0 = _t.monotonic()
    status = 0
    try:
        resp = _dispatch(event, context)
        if isinstance(resp, dict):
            status = resp.get("statusCode", 0)
        return resp
    finally:
        total_ms = (_t.monotonic() - t0) * 1000.0
        ddb_calls, ddb_ms = db.get_ddb_metrics()
        log.info("PERF path=%s method=%s status=%s total_ms=%.1f "
                 "ddb_calls=%d ddb_ms=%.1f cold=%s",
                 path, _http_method(event), status, total_ms,
                 ddb_calls, ddb_ms, cold)


def _dispatch(event: dict, context) -> dict:  # noqa: ARG001
    path = event.get("rawPath") or event.get("path") or "/"

    if path == "/healthz":
        return _text(200, "ok")
    if path == "/warmer":
        return _warm()
    if path == "/api/rum":
        return _api_rum(event)
    if path == "/og-image.png":
        return _og_image_response()
    # Per-app social-card art (public; one object per app in S3, generic
    # fallback). Path-based so it's unambiguous and crawler-friendly.
    if path.startswith("/og/") and path.endswith(".png"):
        return _app_og_image_response(path[len("/og/"):-len(".png")])
    # Public per-app front door: unfurl card + sign-in that deep-links into
    # this specific app. Canonical prefix is /home/<slug>; /a/<slug> is kept
    # as a permanent alias so links shared before the rename still resolve.
    if path.startswith("/home/"):
        return _app_landing(event, path[len("/home/"):].strip("/"))
    if path.startswith("/a/"):
        return _app_landing(event, path[len("/a/"):].strip("/"))
    if path == "/help/admin":
        return _help_page("admin")
    if path == "/help/member":
        return _help_page("member")
    if path == "/login":
        return _login_page(event)
    if path == "/login/password":
        return _login_password(event)
    if path == "/login/new-password":
        return _login_new_password(event)
    if path == "/login/forgot":
        return _login_forgot(event)
    if path == "/login/reset-password":
        return _login_reset_password(event)
    if path == "/login/google":
        qs = event.get("queryStringParameters") or {}
        next_after = qs.get("next") or "/"
        # Mint a fresh OAuth state, send it both in the authorize URL
        # AND in a short-lived cookie; the callback enforces that they
        # match. Without this, an attacker who initiated their own
        # OAuth flow could trick the victim into completing it
        # (login-CSRF; security fix D1).
        state = auth.new_oauth_state()
        resp = _redirect(auth.google_signin_url(state))
        resp.setdefault("cookies", []).append(
            f"scheduler_next={urllib.parse.quote(next_after)}; Path=/; "
            "HttpOnly; Secure; SameSite=Lax; Max-Age=300")
        resp["cookies"].append(auth.state_cookie_value(state))
        return resp
    if path == "/login/apple":
        # Mirrors /login/google — same state-cookie CSRF guard, same
        # next-cookie carry-through. Only the upstream IdP differs.
        qs = event.get("queryStringParameters") or {}
        next_after = qs.get("next") or "/"
        state = auth.new_oauth_state()
        resp = _redirect(auth.apple_signin_url(state))
        resp.setdefault("cookies", []).append(
            f"scheduler_next={urllib.parse.quote(next_after)}; Path=/; "
            "HttpOnly; Secure; SameSite=Lax; Max-Age=300")
        resp["cookies"].append(auth.state_cookie_value(state))
        return resp
    if path == "/auth/callback":
        return _auth_callback(event)
    if path == "/auth/logout":
        return _logout()
    # Public, passwordless flexible_event poll flow (no Cognito) — the
    # magic-link token IS the auth, so these run BEFORE the _route gate.
    if path.startswith("/e/"):
        return _flex_token_page(event)
    if path == "/api/e/vote":
        return _api_flex_token_vote(event)
    if path == "/api/e/optout":
        return _api_flex_token_optout(event)
    if path == "/api/e/join":
        return _api_flex_token_join(event)
    if path == "/":
        return _route(event, _home)
    m = _SCHEDULE_PATH.match(path)
    if m:
        return _route(event, lambda e, u, c, a, mem: _schedule_drilldown(
            u, c, a, mem, m.group(1), event=e))
    if path == "/schedules":
        return _route(event, _schedules_all_page)
    if path == "/admin/send-email":
        return _route(event, _send_email_page)
    if path == "/api/admin/send-email":
        return _route(event, _api_send_email)
    if path == "/admin/emails":
        return _route(event, _emails_page)
    if path == "/admin/users":
        return _route(event, _users_page)
    if path == "/api/users/add":
        return _route(event, _api_user_add)
    if path == "/api/users/edit":
        return _route(event, _api_user_edit)
    if path == "/api/users/delete":
        return _route(event, _api_user_delete)
    if path == "/api/users/toggle-membership":
        return _route(event, _api_user_toggle_membership)
    if path == "/api/users/set-community-role":
        return _route(event, _api_user_set_community_role)
    if path == "/api/users/clear-bounce":
        return _route(event, _api_user_clear_bounce)
    if path == "/api/users/reset-access":
        return _route(event, _api_user_reset_access)
    if path == "/api/users/remove-from-app":
        return _route(event, _api_user_remove_from_app)
    if path == "/admin/cohorts":
        return _route(event, _cohorts_page)
    if path == "/api/cohorts/add-member":
        return _route(event, _api_cohort_add_member)
    if path == "/api/cohorts/remove-member":
        return _route(event, _api_cohort_remove_member)
    if path == "/api/cohorts/delete":
        return _route(event, _api_cohort_delete)
    if path == "/api/assignments/release":
        return _route(event, _release_assignment)
    if path == "/api/assignments/signup":
        return _route(event, _signup_assignment)
    if path == "/assignments/cover":
        return _route(event, _cover_released_page)
    if path == "/api/assignments/cover":
        return _route(event, _api_cover_released)
    if path == "/your-schedule":
        return _route(event, _open_slots_page)
    if path == "/my-availability":
        return _route(event, _my_availability_page)
    if path == "/api/blocked-dates/add":
        return _route(event, _api_blocked_date_add)
    if path == "/api/blocked-dates/delete":
        return _route(event, _api_blocked_date_delete)
    if path == "/api/schedules/create":
        return _route(event, _api_schedule_create)
    if path == "/api/schedules/publish":
        return _route(event, _api_schedule_publish)
    if path == "/api/schedules/unpublish":
        return _route(event, _api_schedule_unpublish)
    if path == "/api/schedules/archive":
        return _route(event, _api_schedule_archive)
    if path == "/api/schedules/reactivate":
        return _route(event, _api_schedule_reactivate)
    if path == "/api/admin/assign":
        return _route(event, _api_admin_assign)
    if path == "/api/admin/bulk-assign":
        return _route(event, _api_admin_bulk_assign)
    if path == "/api/admin/unassign":
        return _route(event, _api_admin_unassign)
    if path == "/api/assignments/confirm":
        return _route(event, _api_assignment_confirm)
    if path == "/api/admin/confirm-assignment":
        return _route(event, _api_admin_confirm_assignment)
    if path == "/api/schedules/delete":
        return _route(event, _api_schedule_delete)
    if path == "/api/schedules/copy-from":
        return _route(event, _api_schedule_copy_from)
    if path == "/admin/templates":
        return _route(event, _templates_page)
    if path == "/admin/settings":
        return _route(event, _admin_settings_page)
    if path == "/admin/sharing":
        return _route(event, _app_sharing_page)
    if path == "/api/sharing/save":
        return _route(event, _api_sharing_save)
    if path == "/api/app/slug":
        return _route(event, _api_app_slug_save)
    if path == "/api/sharing/upload-art":
        return _route(event, _api_sharing_upload_art)
    if path == "/api/sharing/delete-art":
        return _route(event, _api_sharing_delete_art)
    if path == "/api/templates/add":
        return _route(event, _api_template_add)
    if path == "/api/templates/generate-range":
        return _route(event, _api_template_generate_range)
    if path == "/api/templates/delete-all":
        return _route(event, _api_templates_delete_all)
    if path == "/api/templates/edit":
        return _route(event, _api_template_edit)
    if path == "/api/templates/delete":
        return _route(event, _api_template_delete)
    if path == "/api/slots/cancel":
        return _route(event, _api_slot_cancel)
    if path == "/api/slots/add":
        return _route(event, _api_slot_add)
    if path == "/api/slots/edit":
        return _route(event, _api_slot_edit)
    if path == "/api/schedules/email-me":
        return _route(event, _api_schedule_email_me)
    if path == "/swap/new":
        return _route(event, _swap_new_page)
    if path == "/api/swap/create":
        return _route(event, _api_swap_create)
    if path.startswith("/swap/") and path.endswith("/accept"):
        return _route(event, _swap_accept_page)
    if path == "/api/swap/accept":
        return _route(event, _api_swap_accept)
    if path == "/api/swap/cancel":
        return _route(event, _api_swap_cancel)
    if path == "/settings":
        return _route(event, _settings_page)
    if path == "/api/settings/save":
        return _route(event, _api_settings_save)
    if path == "/api/schedules/send-summary":
        return _route(event, _api_schedule_send_summary)
    if path == "/api/settings/defaults":
        return _route(event, _api_settings_defaults)
    if path == "/standing/setup":
        return _route(event, _standing_setup_page)
    if path == "/api/standing/setup":
        return _route(event, _api_standing_setup_save)
    if path == "/api/standing/rsvp":
        return _route(event, _api_standing_rsvp)
    if path == "/api/standing/occurrence":
        return _route(event, _api_standing_occurrence_action)
    # flexible_event (date-poll / book club) AA flows
    if path == "/flex/event/new":
        return _route(event, _flex_event_new_page)
    if path == "/api/flex/event/create":
        return _route(event, _api_flex_event_create)
    if path == "/flex/event/results":
        return _route(event, _flex_event_results_page)
    if path == "/api/flex/event/send-poll":
        return _route(event, _api_flex_event_send_poll)
    if path == "/api/flex/event/close-review":
        return _route(event, _api_flex_event_close_review)
    if path == "/api/flex/event/close":
        return _route(event, _api_flex_event_close)
    if path == "/api/flex/event/cancel":
        return _route(event, _api_flex_event_cancel)
    if path == "/api/flex/event/add-date":
        return _route(event, _api_flex_event_add_date)
    if path == "/api/flex/event/remove-date":
        return _route(event, _api_flex_event_remove_date)
    if path == "/api/flex/event/message":
        return _route(event, _api_flex_event_save_message)
    if path == "/api/flex/event/notify":
        return _route(event, _api_flex_event_save_notify)
    if path.startswith("/ics/"):
        return _route(event, _serve_ics_for_assignment)
    if path == "/launcher":
        return _auth_route(event, _launcher_page)
    if path == "/admin/apps":
        return _ca_route(event, _ca_landing_page)
    if path == "/api/apps/create":
        return _ca_route(event, _api_app_create)
    if path == "/api/apps/delete":
        return _ca_route(event, _api_app_delete)
    if path in ("/api/apps/update", "/api/apps/update-description"):
        return _ca_route(event, _api_app_update)
    if path == "/admin/community-users":
        return _ca_route(event, _ca_users_page)
    if path == "/api/community-users/add":
        return _ca_route(event, _api_ca_user_add)
    if path == "/api/community-users/add-membership":
        return _ca_route(event, _api_ca_membership_add)
    if path == "/api/community-users/remove-membership":
        return _ca_route(event, _api_ca_membership_remove)
    if path == "/api/community-users/toggle-membership":
        return _ca_route(event, _api_ca_membership_toggle)
    if path == "/admin/households":
        return _ca_route(event, _ca_households_page)
    if path == "/api/households/pair":
        return _ca_route(event, _api_ca_household_pair)
    if path == "/api/households/unpair":
        return _ca_route(event, _api_ca_household_unpair)
    return _text(404, "not found")


def _route(event: dict, handler):
    cookies = auth.parse_cookies(event)
    token = cookies.get(auth.ID_COOKIE)
    refresh = cookies.get(auth.REFRESH_COOKIE)
    renewed_cookies: list[str] = []
    path = event.get("rawPath") or event.get("path") or "/"
    qs = event.get("rawQueryString") or ""
    next_url = f"{path}?{qs}" if qs else path

    if not token and not refresh:
        return _redirect(f"/login?next={urllib.parse.quote(next_url)}")

    claims = None
    if token:
        try:
            claims = auth.verify_id_token(token)
        except Exception:
            pass

    if claims is None and refresh:
        tokens = auth.refresh_tokens(refresh)
        if tokens and "id_token" in tokens:
            try:
                claims = auth.verify_id_token(tokens["id_token"])
                renewed_cookies = [
                    auth.set_cookie(auth.ID_COOKIE, tokens["id_token"],
                                    max_age=tokens.get("expires_in", 3600)),
                ]
            except Exception as e:
                log.warning("refreshed id_token verify failed: %s", e)

    if claims is None:
        return _redirect(f"/login?next={urllib.parse.quote(next_url)}")

    sub = claims.get("sub", "")
    user = db.get_user_by_cognito_sub(
        sub, community_id=os.environ.get("COMMUNITY_ID") or None
    ) if sub else None
    if user is None:
        email = claims.get("email", "")
        # SECURITY: auto-link is destructive — it bonds the Cognito
        # identity to an existing user record, giving the signer-in
        # permanent access to that account on every future login.
        # Only do it when the email is trustworthy: Cognito asserts
        # ``email_verified``, OR the login is a Google federation (Cognito
        # doesn't propagate Google's verified-email flag, but Google does
        # verify the address — see _login_email_trusted). An arbitrary IdP
        # with an unverified email is still refused so an attacker can't
        # sign up with a victim's email and take over their pre-provisioned
        # account (security fix D2).
        trusted = _login_email_trusted(claims)
        if email and trusted:
            community_id = os.environ.get("COMMUNITY_ID", "")
            user = db.get_user_by_email(community_id, email) if community_id else None
            if user and sub:
                user.cognito_sub = sub
                db.put_user(user)
                log.info("auto-linked cognito_sub %s to user %s via email %s",
                         sub, user.user_id, email)
        elif email and not trusted:
            log.warning("refusing email-based auto-link: email not verified "
                        "and not a Google federation (sub=%s, email=%s)",
                        sub, email)
    if user is None:
        return _html(403, _unprovisioned(claims.get("email", "")))

    community = db.get_community(user.community_id)

    # App resolution. Precedence:
    #   1. ?app_id=... in the URL — explicit deep-link or corner pivot
    #   2. ACTIVE_APP_COOKIE — last app the user explicitly pivoted to,
    #      so that clicking around /admin/cohorts etc. stays in the
    #      same app instead of falling back to "first by created_at"
    #   3. First Application in the community — single-app default and
    #      bootstrap fallback before any pivot has happened
    # Whenever (1) succeeds we also refresh the cookie so the choice
    # persists across the rest of the session.
    requested_app_id = _get_param(event, "app_id")
    cookie_app_id = cookies.get(ACTIVE_APP_COOKIE)
    app: Application | None = None
    if requested_app_id:
        app = db.get_application(user.community_id, requested_app_id)
    if app is None and cookie_app_id:
        app = db.get_application(user.community_id, cookie_app_id)
    # Cross-app boundary check (#189). The URL/cookie hint may name an
    # app the user has NO Membership in — either because they guessed
    # the app_id, were forwarded a URL by an admin, or were removed
    # from an app whose id still sits in their active-app cookie.
    # Without this check, member-level pages render that app's home,
    # /your-schedule, etc. — see smoke-test report 2026-06-03.
    # CAs/UAs deliberately bypass: they need to pivot into any app to
    # do roster work across the community. Plain members must be
    # provably in the app they're trying to enter.
    if app is not None and user.community_role not in ("ca", "ua"):
        if db.get_membership(app.app_id, user.user_id) is None:
            log.info("dropping app hint for %s: no membership in %s",
                     user.user_id, app.app_id)
            app = None  # Fall through to launcher / single-app logic.
    # Launcher redirect (task #175): if neither the URL nor a cookie
    # specifies an app, AND the user belongs to (or can see, for CA/UA)
    # multiple apps, send them to /launcher to pick. Single-app users
    # and CAs with one app still flow straight in (existing behavior).
    if app is None:
        all_apps = list(db.list_applications(user.community_id))
        if not all_apps:
            visible_apps: list[Application] = []
        elif user.community_role in ("ca", "ua"):
            visible_apps = all_apps
        else:
            mem_ids = {m.app_id for m in
                       db.list_memberships_for_user(user.user_id)}
            visible_apps = [a for a in all_apps if a.app_id in mem_ids]
        # The launcher is a human navigation affordance — only redirect
        # PAGE loads there. An API mutation (e.g. the CA community-users
        # page POSTing /api/users/edit) must never be bounced to a picker:
        # the CA works in CA-mode with no active-app cookie, so without this
        # guard the POST redirects to /launcher and the action is silently
        # dropped ("save did nothing"). For API calls we fall through and
        # resolve a default app below; user-CRUD handlers do their own
        # CA-vs-AA scoping and don't depend on which app it is.
        is_api = path.startswith("/api/")
        if len(visible_apps) > 1 and not is_api:
            return _redirect("/launcher")
        # 0 or 1 visible apps: prefer the user's one visible app
        # (matters when the community has many apps but the user
        # only belongs to one — the fallback used to pick the first
        # community app, which would be the wrong app for the user).
        # Bootstrap fallback to any community app exists so CAs with
        # zero memberships can still reach the app screens.
        if visible_apps:
            app = visible_apps[0]
        else:
            app = next(iter(all_apps), None)
    if app is None:
        return _html(403, _no_application(user, community))

    # Refresh the active-app cookie on EVERY authenticated request so
    # the 4-hour idle timer resets while the user is actively
    # working. After 4 idle hours the cookie expires and multi-app
    # users re-enter via /launcher.
    renewed_cookies.append(auth.set_cookie(
        ACTIVE_APP_COOKIE, app.app_id, max_age=_APP_COOKIE_TTL))

    membership = db.get_membership(app.app_id, user.user_id)

    # Event apps (standing/flexible) have no coverage surfaces — bounce home
    # rather than render slot/schedule/cohort/template UI for them.
    if app.app_type in EVENT_APP_TYPES and _is_coverage_only_path(path):
        return _redirect("/")

    resp = handler(event, user, community, app, membership)
    if renewed_cookies:
        resp.setdefault("cookies", []).extend(renewed_cookies)
    if resp.get("statusCode") == 200 and "text/html" in resp.get("headers", {}).get("Content-Type", ""):
        corner = _build_user_corner(user, community, current_app=app)
        body = resp.get("body", "")
        resp["body"] = body.replace("</body>", corner + "</body>", 1)
    return resp


def _auth_route(event: dict, handler):
    """Authenticate the request and dispatch ``handler(event, user,
    community)``. No app context, no role gate — used for the
    cross-app launcher where every authenticated user can land.

    Auth prelude is intentionally duplicated from _route / _ca_route
    rather than refactored. Three surfaces, ~30 lines each; easier
    to audit than a mode-switching shared helper.
    """
    cookies = auth.parse_cookies(event)
    token = cookies.get(auth.ID_COOKIE)
    refresh = cookies.get(auth.REFRESH_COOKIE)
    renewed_cookies: list[str] = []
    path = event.get("rawPath") or event.get("path") or "/"
    qs = event.get("rawQueryString") or ""
    next_url = f"{path}?{qs}" if qs else path

    if not token and not refresh:
        return _redirect(f"/login?next={urllib.parse.quote(next_url)}")
    claims = None
    if token:
        try:
            claims = auth.verify_id_token(token)
        except Exception:
            pass
    if claims is None and refresh:
        tokens = auth.refresh_tokens(refresh)
        if tokens and "id_token" in tokens:
            try:
                claims = auth.verify_id_token(tokens["id_token"])
                renewed_cookies = [
                    auth.set_cookie(auth.ID_COOKIE, tokens["id_token"],
                                    max_age=tokens.get("expires_in", 3600)),
                ]
            except Exception as e:
                log.warning("refreshed id_token verify failed: %s", e)
    if claims is None:
        return _redirect(f"/login?next={urllib.parse.quote(next_url)}")
    sub = claims.get("sub", "")
    user = db.get_user_by_cognito_sub(
        sub, community_id=os.environ.get("COMMUNITY_ID") or None
    ) if sub else None
    if user is None:
        return _html(403, _unprovisioned(claims.get("email", "")))

    community = db.get_community(user.community_id)
    resp = handler(event, user, community)
    if renewed_cookies:
        resp.setdefault("cookies", []).extend(renewed_cookies)
    if (resp.get("statusCode") == 200
            and "text/html" in resp.get("headers", {}).get("Content-Type", "")):
        corner = _build_user_corner(user, community, current_app=None)
        body = resp.get("body", "")
        resp["body"] = body.replace("</body>", corner + "</body>", 1)
    return resp


def _ca_route(event: dict, handler):
    """Like _route() but for routes that live ABOVE any single
    Application — the CA landing page, the community-wide user
    manager, the apps create/delete API. Gates on
    community_role in ("ca", "ua"). Skips the "must have one app"
    requirement so /admin/apps still works in a brand-new community
    that has zero applications.

    Duplicates the auth prelude from _route() rather than refactoring
    for sharing — two surfaces, ~30 lines, easier to audit than a
    shared helper that has to handle both shapes.
    """
    cookies = auth.parse_cookies(event)
    token = cookies.get(auth.ID_COOKIE)
    refresh = cookies.get(auth.REFRESH_COOKIE)
    renewed_cookies: list[str] = []
    path = event.get("rawPath") or event.get("path") or "/"
    qs = event.get("rawQueryString") or ""
    next_url = f"{path}?{qs}" if qs else path

    if not token and not refresh:
        return _redirect(f"/login?next={urllib.parse.quote(next_url)}")

    claims = None
    if token:
        try:
            claims = auth.verify_id_token(token)
        except Exception:
            pass

    if claims is None and refresh:
        tokens = auth.refresh_tokens(refresh)
        if tokens and "id_token" in tokens:
            try:
                claims = auth.verify_id_token(tokens["id_token"])
                renewed_cookies = [
                    auth.set_cookie(auth.ID_COOKIE, tokens["id_token"],
                                    max_age=tokens.get("expires_in", 3600)),
                ]
            except Exception as e:
                log.warning("refreshed id_token verify failed: %s", e)

    if claims is None:
        return _redirect(f"/login?next={urllib.parse.quote(next_url)}")

    sub = claims.get("sub", "")
    user = db.get_user_by_cognito_sub(
        sub, community_id=os.environ.get("COMMUNITY_ID") or None
    ) if sub else None
    if user is None:
        return _html(403, _unprovisioned(claims.get("email", "")))

    if user.community_role not in ("ca", "ua"):
        return _html(403, _page(
            "<h2>Community Admin only</h2>"
            "<p>This page is for Community Admins. "
            "<a href='/'>Back to your app</a>.</p>"))

    community = db.get_community(user.community_id)
    resp = handler(event, user, community)
    if renewed_cookies:
        resp.setdefault("cookies", []).extend(renewed_cookies)
    if (resp.get("statusCode") == 200
            and "text/html" in resp.get("headers", {}).get("Content-Type", "")):
        # No current_app on CA pages — the corner shows all apps and
        # the CA can pick one to enter.
        corner = _build_user_corner(user, community, current_app=None)
        body = resp.get("body", "")
        resp["body"] = body.replace("</body>", corner + "</body>", 1)
    return resp


def _home(event: dict, user: User, community: Community | None,
          app: Application, membership: Membership | None) -> dict:
    org_name = app.name if app else (community.name if community else user.community_id)
    # Explicit per-app-type dispatch. Adding a new app_type should
    # fail loud here (the final raise), not silently fall into the
    # coverage path — see the Application.app_type design note.
    if app.app_type == "recurring_commitments":
        # Pagination: ?month_offset=N where each step is one calendar
        # month. The view shows the current month + the next, so
        # offset=0 is "this calendar month and next". Calendar-anchored
        # rather than rolling — see #165.
        try:
            month_offset = max(0, int(_get_param(event, "month_offset") or 0))
        except (ValueError, TypeError):
            month_offset = 0
        return _html(200, _recurring_home(user, community, app, membership,
                                          event=event,
                                          org_name=org_name,
                                          month_offset=month_offset))
    if app.app_type == "flexible_event":
        # Date-poll apps get a state dashboard (open polls / scheduled /
        # past), not the wall calendar — a calendar is the wrong frame for
        # ad-hoc events.
        return _flexible_home(event=event, user=user, community=community,
                              app=app, membership=membership, org_name=org_name)
    if app.app_type == "standing_event":
        try:
            month_offset = int(_get_param(event, "month_offset") or 0)
        except (ValueError, TypeError):
            month_offset = 0
        return _event_home(event=event, user=user, community=community,
                           app=app, membership=membership,
                           org_name=org_name, month_offset=month_offset)
    if app.app_type == "coverage":
        if _is_admin(user, membership):
            templates = sorted(db.list_templates(app.app_id),
                               key=lambda t: (t.day_of_week, t.start_time))
            schedules: list[tuple[Schedule, int, int, int, int]] = []
            for sch in sorted(db.list_schedules(app.app_id), key=lambda s: s.yyyy_mm):
                slots_list = list(db.list_slots(app.app_id, sch.yyyy_mm))
                event_count = len(slots_list)
                asgns_by_slot: dict[str, int] = {}
                for a in db.list_assignments_for_month(app.app_id, sch.yyyy_mm):
                    asgns_by_slot[a.slot_id] = asgns_by_slot.get(a.slot_id, 0) + 1
                valid_slot_ids = {s.slot_id for s in slots_list}
                covered = sum(1 for s in slots_list
                              if asgns_by_slot.get(s.slot_id, 0) > 0)
                total_slots = sum(s.required_volunteers for s in slots_list)
                filled_slots = sum(v for k, v in asgns_by_slot.items()
                                   if k in valid_slot_ids)
                schedules.append((sch, event_count, covered, total_slots, filled_slots))
            # #190: scope the home-page email widget to THIS app's
            # activity. Pre-fix the widget walked the community-wide
            # log and rendered the most recent 5 emails regardless of
            # which app they belonged to — an AA of Coverage App B saw
            # Volunteer App subjects on their home, leaking member names
            # and cross-cohort activity. Fetch a wider window (50 by
            # default) and filter; the widget shows the most recent 5
            # tagged with related_app_id == app.app_id. Untagged emails
            # (none of the app handlers ought to be producing these —
            # all current handlers set related_app_id) are dropped from
            # the widget; if they start mattering we can revisit.
            recent_emails = []
            for log_row in db.list_email_logs(user.community_id, limit=50):
                if log_row.related_app_id == app.app_id:
                    recent_emails.append(log_row)
                    if len(recent_emails) >= 5:
                        break
            return _html(200, _admin_home(user, community, app, membership,
                                          event=event,
                                          org_name=org_name,
                                          templates=templates, schedules=schedules,
                                          recent_emails=recent_emails))
        return _html(200, _volunteer_home(user, community, app,
                                          event=event, org_name=org_name))
    raise ValueError(f"unhandled app_type: {app.app_type!r}")


# ============================================================================
# Event-app home page — shared wall-calendar for standing_event +
# flexible_event app types (slice 2 of the event-apps build-out).
# Renders a month-grid of events with prev/next-month navigation.
# ----------------------------------------------------------------------------
# Slice 2 scope: read-only. Standing apps show every materialized
# StandingOccurrence in the rendered month range; flexible apps show
# scheduled FlexibleEvents only (poll-state events surface in a
# separate widget above the grid — see TODO at the call site).
#
# Later slices wire up the AA-setup screens, RSVP buttons, and the
# poll → confirm flow. This file already pulls in the db helpers for
# both shapes (slice 1) so the renderer just needs ranged reads.
# ============================================================================


# Slice 2 design choice: show two consecutive months stacked
# vertically. Single-month is too "next-week-only", three is too much
# scroll on phones, two reads as "the near horizon."
_EVENT_HOME_MONTH_SPAN = 2


def _event_home(*, event: dict, user: User, community: Community | None,
                app: Application, membership: Membership | None,
                org_name: str, month_offset: int) -> dict:
    """Wall-calendar home for standing_event + flexible_event apps.

    Read-only in slice 2; setup affordances + per-occurrence actions
    arrive in slices 3-7. AA viewers see a small "Set up" hint when
    the app has no series row yet.
    """
    tz_name = (app.default_timezone
               or (community.default_timezone if community else "America/New_York"))
    today = dt.datetime.now(ZoneInfo(tz_name)).date()
    base_first = today.replace(day=1)
    first_month_first = _add_months(base_first, month_offset)

    months: list[dt.date] = [
        _add_months(first_month_first, i)
        for i in range(_EVENT_HOME_MONTH_SPAN)
    ]
    range_start = months[0]
    last_month = months[-1]
    range_end = _add_months(last_month, 1) - dt.timedelta(days=1)

    items_by_date: dict[str, list[dict]] = {}
    open_polls: list[FlexibleEvent] = []
    setup_hint = ""
    drawer_html = ""

    if app.app_type == "standing_event":
        series = db.get_standing_series_for_app(app.app_id)
        if series is None:
            if _is_admin(user, membership):
                setup_hint = (
                    "<p style='background:#fffbe6;border:1px solid #f0d674;"
                    "padding:10px 14px;border-radius:6px;color:#7a5a00;"
                    "margin:8px 0 16px 0'>"
                    "No recurrence is set up yet. "
                    "<a href='/standing/setup'>Set up the meeting schedule</a> "
                    "and the calendar will fill in automatically."
                    "</p>"
                )
        else:
            # Lazily materialize the recurrence into occurrences for the
            # viewed range so the calendar fills in (idempotent — see
            # standing.materialize_occurrences).
            try:
                occs = standing.materialize_occurrences(
                    series, range_start, range_end)
            except NotImplementedError:
                # rrule / unsupported rule — fall back to whatever exists.
                occs = list(db.list_standing_occurrences(
                    app.app_id,
                    from_date=range_start.isoformat(),
                    to_date=range_end.isoformat()))
            for occ in occs:
                href = (f"?month_offset={month_offset}"
                        f"&occ={urllib.parse.quote(occ.occurrence_id)}"
                        f"&d={occ.iso_date}#daydrawer")
                items_by_date.setdefault(occ.iso_date, []).append({
                    "label": app.name or "Meeting",
                    "time": occ.start_time or series.default_start_time,
                    "href": href,
                    "cancelled": occ.state == "cancelled",
                })
            if _is_admin(user, membership):
                setup_hint = (
                    "<p style='font-size:0.9em;margin:0 0 12px 0'>"
                    "<a href='/standing/setup'>Edit meeting schedule "
                    "&amp; settings</a></p>"
                )
            # Tap-a-day drawer (slice 4): when ?occ=<id> is present,
            # render the occurrence's detail + RSVP / AA actions below
            # the grid. We already hold the materialized list, so resolve
            # the selection in-memory rather than re-reading.
            sel_occ_id = _get_param(event, "occ")
            if sel_occ_id:
                sel = next((o for o in occs
                            if o.occurrence_id == sel_occ_id), None)
                if sel is not None:
                    drawer_html = _standing_occurrence_drawer(
                        event=event, occ=sel, series=series, app=app,
                        user=user, community=community,
                        membership=membership,
                        is_admin=_is_admin(user, membership),
                        month_offset=month_offset)
    elif app.app_type == "flexible_event":
        # FlexibleEvents are not range-indexed by date directly (winning_date
        # is a field, not the SK), so do a small in-memory filter.
        # Scale: an app's all-time event count is small (tens to low
        # hundreds at most), so reading them all is cheap.
        flex_admin = _is_admin(user, membership)
        for evt in db.list_flexible_events(app.app_id):
            if evt.state == "scheduled" and evt.winning_date:
                d = evt.winning_date
                if range_start.isoformat() <= d <= range_end.isoformat():
                    item = {"label": evt.title, "time": evt.winning_start_time}
                    if flex_admin:   # admins can open the event's roster
                        item["href"] = f"/flex/event/results?event={evt.event_id}"
                    items_by_date.setdefault(d, []).append(item)
            elif evt.state == "poll":
                open_polls.append(evt)
        if flex_admin:
            setup_hint = (
                "<p style='font-size:0.95em;margin:0 0 12px'>"
                "<a href='/flex/event/new'>+ New event / date poll</a></p>")

    # Open polls widget (flexible_event only)
    polls_section = ""
    if open_polls:
        _poll_admin = _is_admin(user, membership)
        rows = "".join(
            "<li style='padding:6px 0'>"
            + (f"<a href='/flex/event/results?event={p.event_id}'>"
               f"<b>{html.escape(p.title)}</b></a>" if _poll_admin
               else f"<b>{html.escape(p.title)}</b>")
            + (f" &mdash; <span style='color:#888'>"
               f"closes {html.escape(p.poll_closes_at[:10])}</span>"
               if p.poll_closes_at else "")
            + "</li>"
            for p in sorted(open_polls,
                            key=lambda p: p.poll_closes_at or "9999")
        )
        polls_section = (
            "<section style='margin:16px 0;padding:12px 16px;"
            "background:#f6f9f6;border:1px solid #cfe5cf;border-radius:6px'>"
            "<h2 style='font-size:1em;color:#2a7;margin:0 0 6px 0'>"
            "Open date polls</h2>"
            f"<ul style='margin:0;padding-left:20px'>{rows}</ul>"
            "</section>"
        )

    prev_url = f"?month_offset={month_offset - 1}"
    next_url = f"?month_offset={month_offset + 1}"
    grids = "".join(
        _render_wall_calendar(month_first=m,
                              items_by_date=items_by_date,
                              today=today)
        for m in months
    )
    nav = (
        "<div style='display:flex;justify-content:space-between;"
        "align-items:center;margin:8px 0 16px 0'>"
        f"<a href='{prev_url}' style='color:#2a7'>&larr; Previous</a>"
        + ("<a href='?month_offset=0' "
           "style='font-size:0.9em;color:#888;text-decoration:none'>"
           "Today</a>" if month_offset != 0 else "<span></span>")
        + f"<a href='{next_url}' style='color:#2a7'>Next &rarr;</a>"
        "</div>"
    )

    body = (
        f"<h1>{html.escape(org_name)}</h1>"
        + _flash_banner_html(event)
        + setup_hint
        + polls_section
        + nav
        + grids
        + drawer_html
        + (_admin_nav_bar("home", app=app)
           if _is_admin(user, membership)
           else "<p style='margin-top:24px'>"
                "<a href='/launcher'>Back to launcher</a></p>")
    )
    return _html(200, _page(body, narrow=False, title=org_name))


# ---- standing_event AA setup (slice 3) -------------------------------------

_STANDING_ORDINALS = [
    ("1st", "First"), ("2nd", "Second"), ("3rd", "Third"),
    ("4th", "Fourth"), ("last", "Last"),
]
_STANDING_WEEKDAYS = [
    ("mon", "Monday"), ("tue", "Tuesday"), ("wed", "Wednesday"),
    ("thu", "Thursday"), ("fri", "Friday"), ("sat", "Saturday"),
    ("sun", "Sunday"),
]


def _standing_recurrence_parts(rule: str | None) -> tuple[str, str]:
    """Split ``monthly_<ord>_<wd>`` into ``(ordinal, weekday)`` tokens,
    defaulting to ``("2nd", "tue")`` for a new/unknown series."""
    if rule and rule.startswith("monthly_"):
        parts = rule[len("monthly_"):].split("_")
        if len(parts) == 2:
            return parts[0], parts[1]
    return "2nd", "tue"


def _standing_setup_page(event: dict, user: User, community: Community | None,
                         app: Application, membership: Membership | None) -> dict:
    if app is None or app.app_type != "standing_event":
        return _redirect("/")
    if not _is_admin(user, membership):
        return _html(403, _page(
            "<h1>Not authorized</h1><p>Only app admins can set up the "
            "meeting schedule.</p>", title=app.name))

    series = db.get_standing_series_for_app(app.app_id)
    ord_tok, wd_tok = _standing_recurrence_parts(
        series.recurrence if series else None)
    loc = (series.default_location if series else "") or ""
    start_time = (series.default_start_time if series else "") or ""
    duration = series.default_duration_minutes if series else 60
    attendance = series.attendance_tracking if series else False
    invites = series.send_calendar_invites if series else False
    lead = series.reminder_lead_days if series else 1

    ord_opts = "".join(
        f"<option value='{v}'{' selected' if v == ord_tok else ''}>{lbl}</option>"
        for v, lbl in _STANDING_ORDINALS)
    wd_opts = "".join(
        f"<option value='{v}'{' selected' if v == wd_tok else ''}>{lbl}</option>"
        for v, lbl in _STANDING_WEEKDAYS)

    body = (
        "<h1>Meeting schedule</h1>"
        f"<p style='color:#666;margin-top:-6px'>{html.escape(app.name)}</p>"
        + _flash_banner_html(event)
        + "<form method='post' action='/api/standing/setup' "
          "style='text-align:left;max-width:480px'>"
        + "<fieldset style='border:1px solid #ddd;border-radius:8px;"
          "padding:16px;margin-bottom:20px'>"
          "<legend style='font-weight:600;color:#444;padding:0 8px'>"
          "When it meets</legend>"
          "<p style='color:#888;font-size:0.85em;margin:0 0 10px'>"
          "Recurs on this weekday-of-month, every month "
          "(e.g. “Second Tuesday”).</p>"
          f"<select name='ordinal' style='padding:6px'>{ord_opts}</select> "
          f"<select name='weekday' style='padding:6px'>{wd_opts}</select> "
          "<span style='color:#888'>of the month</span>"
          "<label style='display:block;margin:14px 0 4px'>"
          "Default start time</label>"
          f"<input type='time' name='start_time' "
          f"value='{html.escape(start_time)}' style='padding:6px'>"
          "<label style='display:block;margin:14px 0 4px'>"
          "Default duration (minutes)</label>"
          f"<input type='number' name='duration' min='5' step='5' "
          f"value='{int(duration)}' style='padding:6px;width:100px'>"
          "<label style='display:block;margin:14px 0 4px'>"
          "Default location</label>"
          f"<input type='text' name='location' value='{html.escape(loc)}' "
          "placeholder='e.g. Parish hall' style='padding:6px;width:100%'>"
          "</fieldset>"
        + "<fieldset style='border:1px solid #ddd;border-radius:8px;"
          "padding:16px;margin-bottom:20px'>"
          "<legend style='font-weight:600;color:#444;padding:0 8px'>"
          "Notifications</legend>"
          "<label style='display:block;margin:8px 0'>"
          f"<input type='checkbox' name='attendance'"
          f"{' checked' if attendance else ''}> "
          "Track attendance (members see yes / no / maybe on each "
          "meeting)</label>"
          "<label style='display:block;margin:8px 0'>"
          f"<input type='checkbox' name='invites'"
          f"{' checked' if invites else ''}> "
          "Send calendar invites (.ics) so meetings land on members' "
          "calendars</label>"
          "<label style='display:block;margin:14px 0 4px'>"
          "Reminder email lead time "
          "(days before; 0 = no reminder)</label>"
          f"<input type='number' name='lead_days' min='0' max='30' "
          f"value='{int(lead)}' style='padding:6px;width:100px'>"
          "</fieldset>"
        + "<p><button type='submit' style='padding:8px 24px;cursor:pointer;"
          "font-size:1em'>Save schedule</button>"
          "<a href='/' style='margin-left:16px'>Cancel</a></p>"
          "</form>"
        + _admin_nav_bar("standing-setup", app=app)
    )
    return _html(200, _page(body, title=app.name))


def _api_standing_setup_save(event: dict, user: User,
                             community: Community | None,
                             app: Application,
                             membership: Membership | None) -> dict:
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method != "POST":
        return _text(405, "POST required")
    if app is None or app.app_type != "standing_event":
        return _text(400, "not a standing_event app")
    if not _is_admin(user, membership):
        return _text(403, "admin only")

    body_str = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body_str = base64.b64decode(body_str).decode("utf-8", errors="replace")
    parsed = urllib.parse.parse_qs(body_str)

    ordinal = (parsed.get("ordinal", ["2nd"])[0] or "2nd").strip()
    weekday = (parsed.get("weekday", ["tue"])[0] or "tue").strip()
    rule = f"monthly_{ordinal}_{weekday}"
    if not recurrence.supports(rule):
        return _error_redirect("/standing/setup",
                               "Please choose a valid weekday-of-month.")

    location = (parsed.get("location", [""])[0] or "").strip() or None
    start_time = (parsed.get("start_time", [""])[0] or "").strip() or None
    try:
        duration = max(5, int(parsed.get("duration", ["60"])[0] or 60))
    except ValueError:
        duration = 60
    try:
        lead = max(0, min(30, int(parsed.get("lead_days", ["1"])[0] or 0)))
    except ValueError:
        lead = 1
    attendance = bool(parsed.get("attendance"))
    invites = bool(parsed.get("invites"))

    existing = db.get_standing_series_for_app(app.app_id)
    if existing is not None:
        existing.recurrence = rule
        existing.default_location = location
        existing.default_start_time = start_time
        existing.default_duration_minutes = duration
        existing.attendance_tracking = attendance
        existing.send_calendar_invites = invites
        existing.reminder_lead_days = lead
        try:
            db.put_standing_series(existing, expected_version=existing.version)
        except db.ConcurrencyConflict:
            return _error_redirect(
                "/standing/setup",
                "Someone else just updated this — reopen and try again.")
        series = existing
    else:
        series = StandingSeries(
            community_id=app.community_id, app_id=app.app_id,
            recurrence=rule, default_location=location,
            default_start_time=start_time, default_duration_minutes=duration,
            attendance_tracking=attendance, send_calendar_invites=invites,
            reminder_lead_days=lead)
        db.put_standing_series(series)

    # Materialize a 12-month forward window so the calendar fills immediately.
    tz_name = (app.default_timezone
               or (community.default_timezone if community else "America/New_York"))
    first = dt.datetime.now(ZoneInfo(tz_name)).date().replace(day=1)
    try:
        standing.materialize_occurrences(series, first, _add_months(first, 12))
    except NotImplementedError:
        pass
    # (Re)queue reminder emails for the forward window. Best-effort — a
    # failure here must not block saving the schedule.
    try:
        standing.materialize_occurrence_reminders(community, app, series)
    except Exception:
        log.exception("occurrence reminder materialization failed for %s",
                      app.app_id)

    log.info("standing series saved app=%s rule=%s attendance=%s lead=%d",
             app.app_id, rule, attendance, lead)
    return _redirect("/?notice=" + urllib.parse.quote("Meeting schedule saved."))


# ---- standing_event per-occurrence drawer + actions (slice 4) ---------------

_WEEKDAY_FULL = ("Monday", "Tuesday", "Wednesday", "Thursday",
                 "Friday", "Saturday", "Sunday")
_RSVP_LABEL = {"yes": "coming", "maybe": "maybe", "no": "not coming"}


def _drawer_url(month_offset: int, occ_id: str, iso_date: str, *,
                notice: str | None = None, error: str | None = None) -> str:
    """Build a link back to the home calendar with the day-drawer open
    for ``occ_id``. Flash params go before the ``#daydrawer`` fragment so
    they survive the round-trip and the browser scrolls to the drawer."""
    url = (f"/?month_offset={month_offset}"
           f"&occ={urllib.parse.quote(occ_id)}&d={iso_date}")
    if notice:
        url += "&notice=" + urllib.parse.quote(notice)
    if error:
        url += "&error=" + urllib.parse.quote(error)
    return url + "#daydrawer"


def _safe_month_offset(event: dict) -> int:
    try:
        return int(_get_param(event, "month_offset") or 0)
    except (ValueError, TypeError):
        return 0


def _standing_roster_html(app: Application, occ: StandingOccurrence) -> str:
    """Attendance roster for one occurrence: members grouped by their
    response (coming / maybe / not coming / no response)."""
    rsvps = {r.user_id: r.response
             for r in db.list_standing_rsvps_for_occurrence(
                 app.app_id, occ.occurrence_id)}
    member_ids = [m.user_id for m in db.list_memberships_for_app(app.app_id)]
    users = {u.user_id: u for u in db.list_users(app.community_id)}
    groups: dict[str, list[str]] = {"yes": [], "maybe": [], "no": [], "none": []}
    for uid in member_ids:
        u = users.get(uid)
        if u is None:
            continue
        groups[rsvps.get(uid, "none")].append(u.name or u.email)

    def col(key: str, label: str, color: str) -> str:
        names = sorted(groups[key])
        items = ("".join(f"<li>{html.escape(n)}</li>" for n in names)
                 or "<li style='color:#bbb;list-style:none;margin-left:-18px'>"
                    "&mdash;</li>")
        return (f"<div style='flex:1;min-width:130px'>"
                f"<div style='font-weight:600;color:{color}'>{label} "
                f"({len(names)})</div>"
                f"<ul style='margin:4px 0;padding-left:18px;font-size:0.9em'>"
                f"{items}</ul></div>")

    return (
        "<div style='margin:4px 0 2px;font-weight:600'>Attendance</div>"
        "<div style='display:flex;flex-wrap:wrap;gap:18px'>"
        + col("yes", "Coming", "#2a7")
        + col("maybe", "Maybe", "#b8860b")
        + col("no", "Not coming", "#c33")
        + col("none", "No response", "#888")
        + "</div>")


def _standing_occurrence_drawer(*, event: dict, occ: StandingOccurrence,
                                series: StandingSeries, app: Application,
                                user: User, community: Community | None,
                                membership: Membership | None,
                                is_admin: bool, month_offset: int) -> str:
    """Detail drawer for one standing-event occurrence.

    Members see date / time / location / notes plus yes/no/maybe RSVP
    buttons when the series tracks attendance. App admins additionally
    get the attendance roster and per-occurrence actions (override
    time/location/notes, cancel / reinstate).
    """
    d = dt.date.fromisoformat(occ.iso_date)
    date_label = (f"{_WEEKDAY_FULL[d.weekday()]}, "
                  f"{_MONTH_LABEL[d.month]} {d.day}, {d.year}")
    eff_time = occ.start_time or series.default_start_time
    eff_loc = occ.location or series.default_location
    cancelled = occ.state == "cancelled"
    hidden = (
        f"<input type='hidden' name='occ' value='{html.escape(occ.occurrence_id)}'>"
        f"<input type='hidden' name='d' value='{occ.iso_date}'>"
        f"<input type='hidden' name='month_offset' value='{month_offset}'>")

    parts = [
        "<section id='daydrawer' style='margin:20px 0;padding:18px 20px;"
        "border:1px solid #cfe5cf;border-radius:8px;background:#fbfdfb;"
        "text-align:left'>",
        _flash_banner_html(event),
        f"<h2 style='margin:0 0 4px 0;font-size:1.15em'>"
        f"{html.escape(date_label)}</h2>",
    ]
    if cancelled:
        parts.append(
            "<p style='margin:6px 0;padding:6px 10px;background:#fbeaea;"
            "border:1px solid #e0b4b4;border-radius:5px;color:#922;"
            "display:inline-block'><b>Cancelled</b> &mdash; no meeting "
            "this date.</p>")
    detail_bits: list[str] = []
    if eff_time:
        try:
            detail_bits.append(html.escape(_fmt_time(eff_time)))
        except (ValueError, AttributeError):
            pass
    if eff_loc:
        detail_bits.append(html.escape(eff_loc))
    if detail_bits:
        parts.append("<p style='margin:6px 0;color:#444'>"
                     + " &middot; ".join(detail_bits) + "</p>")
    if occ.notes:
        parts.append(f"<p style='margin:6px 0;color:#555'><b>Note:</b> "
                     f"{html.escape(occ.notes)}</p>")

    # --- member RSVP ---
    if series.attendance_tracking and not cancelled:
        mine = db.get_standing_rsvp(app.app_id, occ.occurrence_id, user.user_id)
        current = mine.response if mine else None
        btns = ""
        for val, lbl, color in (("yes", "Yes", "#2a7"),
                                ("maybe", "Maybe", "#b8860b"),
                                ("no", "No", "#c33")):
            on = val == current
            style = ("padding:7px 18px;margin-right:8px;cursor:pointer;"
                     f"border-radius:5px;font-size:0.95em;border:1px solid {color};"
                     + (f"background:{color};color:#fff;font-weight:600"
                        if on else f"background:#fff;color:{color}"))
            btns += (f"<button type='submit' name='response' value='{val}' "
                     f"style='{style}'>{lbl}</button>")
        cur_txt = (f"You're marked <b>{_RSVP_LABEL[current]}</b>."
                   if current else "You haven't responded yet.")
        parts.append(
            "<form method='post' action='/api/standing/rsvp' "
            "style='margin:14px 0 4px'>" + hidden
            + "<p style='margin:0 0 8px;font-weight:600'>Will you attend?</p>"
            + btns
            + f"<p style='margin:8px 0 0;color:#666;font-size:0.9em'>"
            f"{cur_txt}</p></form>")

    # --- AA roster + per-occurrence actions ---
    if is_admin:
        parts.append("<hr style='border:none;border-top:1px solid #e3efe3;"
                     "margin:16px 0'>")
        if series.attendance_tracking:
            parts.append(_standing_roster_html(app, occ))
        parts.append(
            "<details style='margin-top:12px'><summary style='cursor:pointer;"
            "font-weight:600;color:#2a7'>Edit this meeting</summary>"
            "<form method='post' action='/api/standing/occurrence' "
            "style='margin:12px 0;max-width:420px'>" + hidden
            + "<input type='hidden' name='action' value='update'>"
            "<label style='display:block;margin:8px 0 4px'>Time "
            "<span style='color:#999;font-weight:400'>(blank = series "
            "default)</span></label>"
            f"<input type='time' name='start_time' "
            f"value='{html.escape(occ.start_time or '')}' style='padding:6px'>"
            "<label style='display:block;margin:12px 0 4px'>Location "
            "<span style='color:#999;font-weight:400'>(blank = series "
            "default)</span></label>"
            f"<input type='text' name='location' "
            f"value='{html.escape(occ.location or '')}' "
            f"placeholder='{html.escape(series.default_location or '')}' "
            "style='padding:6px;width:100%'>"
            "<label style='display:block;margin:12px 0 4px'>Notes</label>"
            f"<input type='text' name='notes' "
            f"value='{html.escape(occ.notes or '')}' "
            "placeholder='e.g. guest speaker: Fr. X' "
            "style='padding:6px;width:100%'>"
            "<p style='margin:12px 0 0'><button type='submit' "
            "style='padding:7px 20px;cursor:pointer'>Save changes</button>"
            "</p></form></details>")
        if cancelled:
            parts.append(
                "<form method='post' action='/api/standing/occurrence' "
                "style='margin:8px 0'>" + hidden
                + "<input type='hidden' name='action' value='reinstate'>"
                "<button type='submit' style='padding:7px 18px;cursor:pointer;"
                "border:1px solid #2a7;background:#fff;color:#2a7;"
                "border-radius:5px'>Reinstate this meeting</button></form>")
        else:
            parts.append(
                "<form method='post' action='/api/standing/occurrence' "
                "style='margin:8px 0' onsubmit=\"return confirm('Cancel this "
                "meeting? Members will see it as cancelled and its reminder "
                "stops.')\">" + hidden
                + "<input type='hidden' name='action' value='cancel'>"
                "<button type='submit' style='padding:7px 18px;cursor:pointer;"
                "border:1px solid #c33;background:#fff;color:#c33;"
                "border-radius:5px'>Cancel this meeting</button></form>")

    parts.append(f"<p style='margin-top:14px'>"
                 f"<a href='/?month_offset={month_offset}' "
                 "style='color:#888'>Close</a></p></section>")
    return "".join(parts)


def _api_standing_rsvp(event: dict, user: User, community: Community | None,
                       app: Application, membership: Membership | None) -> dict:
    if _http_method(event) != "POST":
        return _text(405, "POST required")
    if app is None or app.app_type != "standing_event":
        return _text(400, "not a standing_event app")
    occ_id = (_get_param(event, "occ") or "").strip()
    iso_date = (_get_param(event, "d") or "").strip()
    response = (_get_param(event, "response") or "").strip()
    month_offset = _safe_month_offset(event)
    if response not in ("yes", "no", "maybe"):
        return _redirect(_drawer_url(month_offset, occ_id, iso_date,
                                     error="Please choose Yes, No, or Maybe."))
    series = db.get_standing_series_for_app(app.app_id)
    if series is None or not series.attendance_tracking:
        return _redirect(_drawer_url(
            month_offset, occ_id, iso_date,
            error="Attendance isn't being tracked for this app."))
    occ = db.get_standing_occurrence(app.app_id, iso_date, occ_id)
    if occ is None or occ.state == "cancelled":
        return _redirect(_drawer_url(
            month_offset, occ_id, iso_date,
            error="That meeting is no longer available."))
    try:
        existing = db.get_standing_rsvp(app.app_id, occ_id, user.user_id)
        if existing is not None:
            existing.response = response
            existing.updated_at = dt.datetime.now(
                dt.timezone.utc).isoformat(timespec="seconds")
            db.put_standing_rsvp(existing, expected_version=existing.version)
        else:
            db.put_standing_rsvp(StandingRSVP(
                community_id=user.community_id, app_id=app.app_id,
                occurrence_id=occ_id, user_id=user.user_id, response=response))
    except db.ConcurrencyConflict:
        return _redirect(_drawer_url(
            month_offset, occ_id, iso_date,
            error="Your response collided with another update — try again."))
    return _redirect(_drawer_url(month_offset, occ_id, iso_date,
                                 notice="Response saved."))


def _api_standing_occurrence_action(event: dict, user: User,
                                    community: Community | None,
                                    app: Application,
                                    membership: Membership | None) -> dict:
    if _http_method(event) != "POST":
        return _text(405, "POST required")
    if app is None or app.app_type != "standing_event":
        return _text(400, "not a standing_event app")
    if not _is_admin(user, membership):
        return _text(403, "admin only")
    occ_id = (_get_param(event, "occ") or "").strip()
    iso_date = (_get_param(event, "d") or "").strip()
    action = (_get_param(event, "action") or "").strip()
    month_offset = _safe_month_offset(event)
    occ = db.get_standing_occurrence(app.app_id, iso_date, occ_id)
    if occ is None:
        return _redirect(_drawer_url(month_offset, occ_id, iso_date,
                                     error="That meeting no longer exists."))
    if action == "cancel":
        occ.state = "cancelled"
        notice = "Meeting cancelled."
    elif action == "reinstate":
        occ.state = "scheduled"
        notice = "Meeting reinstated."
    elif action == "update":
        occ.start_time = (_get_param(event, "start_time") or "").strip() or None
        occ.location = (_get_param(event, "location") or "").strip() or None
        occ.notes = (_get_param(event, "notes") or "").strip() or None
        notice = "Meeting updated."
    else:
        return _redirect(_drawer_url(month_offset, occ_id, iso_date,
                                     error="Unknown action."))
    try:
        db.put_standing_occurrence(occ, expected_version=occ.version)
    except db.ConcurrencyConflict:
        return _redirect(_drawer_url(
            month_offset, occ_id, iso_date,
            error="Someone else just changed this meeting — try again."))
    # Cancel / reinstate / time changes shift which reminders fire and
    # when, so re-materialize the app's reminder queue. Best-effort — a
    # failure here must not block the state change the AA just made.
    series = db.get_standing_series_for_app(app.app_id)
    if series is not None:
        try:
            standing.materialize_occurrence_reminders(community, app, series)
        except Exception:
            log.exception("reminder re-materialization failed for %s after %s",
                          app.app_id, action)
    log.info("standing occurrence %s action=%s app=%s by=%s",
             occ_id, action, app.app_id, user.user_id)
    return _redirect(_drawer_url(month_offset, occ_id, iso_date, notice=notice))


# ============================================================================
# flexible_event (date-poll / book club): AA flows + passwordless token flow
# ============================================================================

EVENT_TOKEN_TTL_DAYS = 180
_RESP_LABEL = {"yes": "Yes", "no": "No", "maybe": "Maybe"}


def _parse_form(event: dict) -> dict:
    """Parse a urlencoded POST body into a parse_qs dict (multi-value)."""
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8", errors="replace")
    return urllib.parse.parse_qs(body, keep_blank_values=True)


def _public_domain(community: Community | None) -> str:
    """Hostname for member-facing links — the community's own public_url
    when set (multi-stack correctness), else this stack's DOMAIN_NAME."""
    if community is not None and getattr(community, "public_url", None):
        return community.public_url
    return DOMAIN_NAME


def _flex_guard(user: User, membership: Membership | None,
                app: Application | None) -> dict | None:
    """AA-route guard: 400 if not a flexible_event app, 403 if not an
    admin. Returns the error response, or None when OK."""
    if app is None or app.app_type != "flexible_event":
        return _text(400, "not a flexible_event app")
    if not _is_admin(user, membership):
        return _text(403, "admin only")
    return None


def _fmt_iso_date(iso: str) -> str:
    try:
        d = dt.date.fromisoformat(iso)
        return f"{_WEEKDAY_FULL[d.weekday()]}, {_MONTH_LABEL[d.month]} {d.day}"
    except (ValueError, TypeError):
        return iso


# ---- AA/member home: state dashboard (not a calendar) ----------------------

def _flexible_home(*, event: dict, user: User, community: Community | None,
                   app: Application, membership: Membership | None,
                   org_name: str) -> dict:
    """Date-poll home: a dashboard of state — open polls, scheduled events,
    and past — rather than a wall calendar. AAs get manage links + a
    create button; members get the same read view (they act via email)."""
    is_admin = _is_admin(user, membership)
    memberships = list(db.list_memberships_for_app(app.app_id))
    users = {u.user_id: u for u in db.list_users(app.community_id)}
    total_households = len({_hh_key(users, m.user_id) for m in memberships})
    open_polls, scheduled, past = [], [], []
    for evt in db.list_flexible_events(app.app_id):
        if evt.state == "poll":
            open_polls.append(evt)
        elif evt.state == "scheduled":
            scheduled.append(evt)
        else:
            past.append(evt)
    open_polls.sort(key=lambda e: e.created_at, reverse=True)
    scheduled.sort(key=lambda e: e.winning_date or "9999")
    past.sort(key=lambda e: (e.winning_date or e.created_at), reverse=True)

    def responded_households(eid: str) -> int:
        rsvps = list(db.list_flexible_rsvps(app.app_id, eid))
        resp, _ = _flex_household_counts(memberships, users, rsvps)
        return resp

    def n_attending(eid: str) -> int:
        # Household-deduped expected headcount (same basis as the results page).
        return _flex_headcount(
            list(db.list_flexible_rsvps(app.app_id, eid)), users)

    def results_link(e) -> str:
        return (f" &middot; <a href='/flex/event/results?event={e.event_id}'>"
                + ("Review &amp; results" if e.state == "poll" else "Details")
                + "</a>") if is_admin else ""

    parts = [f"<h1>{html.escape(org_name)}</h1>", _flash_banner_html(event)]
    if is_admin:
        parts.append(
            "<p style='margin:6px 0 18px'><a href='/flex/event/new' "
            "style='display:inline-block;padding:8px 18px;background:#2a7;"
            "color:#fff;border-radius:5px;text-decoration:none'>"
            "+ New event / date poll</a></p>")

    if open_polls:
        rows = ""
        for e in open_polls:
            opts = sum(1 for _ in db.list_flexible_poll_options(app.app_id, e.event_id))
            sent = sum(1 for _ in db.list_event_tokens(app.app_id, e.event_id))
            status = ("poll sent" if sent
                      else "<i style='color:#a80'>not sent yet</i>")
            rows += (f"<li style='padding:6px 0'><b>{html.escape(e.title)}</b>"
                     f" &middot; {responded_households(e.event_id)}/"
                     f"{total_households} households responded"
                     f" &middot; {opts} proposed date(s)"
                     + (f" &middot; {status}" if is_admin else "")
                     + results_link(e) + "</li>")
        parts.append("<h2 style='font-size:1.1em;color:#2a7'>Open polls</h2>"
                     "<ul style='margin:0 0 18px;padding-left:0;"
                     f"list-style:none'>{rows}</ul>")

    if scheduled:
        rows = ""
        for e in scheduled:
            when = _fmt_iso_date(e.winning_date or "")
            if e.winning_start_time:
                when += f" {_fmt_time(e.winning_start_time)}"
            line = f"<b>{html.escape(e.title)}</b> &middot; {html.escape(when)}"
            if e.location:
                line += f" &middot; {html.escape(e.location)}"
            line += f" &middot; {n_attending(e.event_id)} attending"
            rows += f"<li style='padding:6px 0'>{line}{results_link(e)}</li>"
        parts.append("<h2 style='font-size:1.1em;color:#2a7'>Scheduled</h2>"
                     "<ul style='margin:0 0 18px;padding-left:0;"
                     f"list-style:none'>{rows}</ul>")

    if past:
        rows = "".join(
            f"<li style='padding:4px 0;color:#666'>{html.escape(e.title)} "
            f"&middot; {html.escape(_fmt_iso_date(e.winning_date or '') or e.created_at[:10])}"
            f" ({html.escape(e.state)}){results_link(e)}</li>"
            for e in past)
        parts.append(
            "<details style='margin-bottom:12px'><summary style='cursor:pointer;"
            f"color:#888'>Past events ({len(past)})</summary>"
            "<ul style='padding-left:0;list-style:none'>"
            f"{rows}</ul></details>")

    if not (open_polls or scheduled or past):
        parts.append("<p style='color:#888'>No events yet."
                     + (" Click <b>+ New event</b> to create your first date "
                        "poll." if is_admin else " Check your email for invites.")
                     + "</p>")

    parts.append(_admin_nav_bar("home", app=app) if is_admin
                 else "<p style='margin-top:24px'>"
                      "<a href='/settings'>Your settings</a> &nbsp;|&nbsp; "
                      "<a href='/launcher'>Back to launcher</a></p>")
    return _html(200, _page("".join(parts), narrow=False, title=org_name))


# ---- AA: create event + candidate dates -----------------------------------

def _flex_event_new_page(event: dict, user: User, community: Community | None,
                         app: Application, membership: Membership | None) -> dict:
    guard = _flex_guard(user, membership, app)
    if guard:
        return guard
    series = db.get_flexible_series_for_app(app.app_id)
    default_loc = (series.default_location if series else "") or ""
    date_rows = "".join(
        "<div style='margin:5px 0'>"
        f"<input type='date' name='date{i}' style='padding:6px'> "
        f"<input type='time' name='time{i}' style='padding:6px'> "
        f"<input type='text' name='label{i}' placeholder='note (optional)' "
        "style='padding:6px'></div>"
        for i in range(6)
    )
    body = (
        "<h1>New event</h1>"
        f"<p style='color:#666;margin-top:-6px'>{html.escape(app.name)}</p>"
        + _flash_banner_html(event)
        + "<form method='post' action='/api/flex/event/create' "
          "style='text-align:left;max-width:520px'>"
          "<label style='display:block;margin:8px 0 4px'>Title</label>"
          "<input type='text' name='title' required "
          "style='padding:6px;width:100%'>"
          "<label style='display:block;margin:12px 0 4px'>Message to the "
          "group (optional)</label>"
          "<p style='color:#888;font-size:0.85em;margin:0 0 4px'>Describe the "
          "gathering — the book you're reading, what to expect, anything you'd "
          "like to say. It goes in the invitation email, and you can edit it "
          "later before sending.</p>"
          "<textarea name='description' rows='4' "
          "style='padding:6px;width:100%'></textarea>"
          "<label style='display:block;margin:12px 0 4px'>Location</label>"
          f"<input type='text' name='location' value='{html.escape(default_loc)}' "
          "style='padding:6px;width:100%'>"
          "<label style='display:block;margin:12px 0 4px'>Duration "
          "(minutes)</label>"
          "<input type='number' name='duration' min='15' step='15' value='120' "
          "style='padding:6px;width:100px'>"
          "<fieldset style='border:1px solid #ddd;border-radius:8px;"
          "padding:12px;margin:16px 0'>"
          "<legend style='font-weight:600;color:#444;padding:0 8px'>"
          "Proposed dates</legend>"
          "<p style='color:#888;font-size:0.85em;margin:0 0 8px'>"
          "Add <b>one</b> date for a fixed-date invitation (members just RSVP), "
          "or <b>several</b> to let the group pick. Members answer Yes / No / "
          "Maybe on each. Leave the time blank to decide it later.</p>"
        + date_rows
        + "</fieldset>"
          "<p><button type='submit' style='padding:8px 24px;cursor:pointer;"
          "font-size:1em'>Create poll</button>"
          "<a href='/' style='margin-left:16px'>Cancel</a></p>"
          "</form>"
        # New-event form isn't a nav tab — pass no current so "App home"
        # stays a clickable back-link.
        + _admin_nav_bar("", app=app)
    )
    return _html(200, _page(body, title=app.name))


def _api_flex_event_create(event: dict, user: User, community: Community | None,
                           app: Application,
                           membership: Membership | None) -> dict:
    if _http_method(event) != "POST":
        return _text(405, "POST required")
    guard = _flex_guard(user, membership, app)
    if guard:
        return guard
    parsed = _parse_form(event)
    title = (parsed.get("title", [""])[0] or "").strip()
    if not title:
        return _error_redirect("/flex/event/new", "Please enter a title.")
    description = (parsed.get("description", [""])[0] or "").strip() or None
    location = (parsed.get("location", [""])[0] or "").strip() or None
    try:
        duration = max(15, int(parsed.get("duration", ["120"])[0] or 120))
    except ValueError:
        duration = 120
    evt = FlexibleEvent(
        community_id=app.community_id, app_id=app.app_id, title=title,
        state="poll", description=description, location=location,
        winning_duration_minutes=duration, created_by=user.user_id)
    db.put_flexible_event(evt)
    n = 0
    for i in range(20):
        d = (parsed.get(f"date{i}", [""])[0] or "").strip()
        if not d:
            continue
        t = (parsed.get(f"time{i}", [""])[0] or "").strip() or None
        lbl = (parsed.get(f"label{i}", [""])[0] or "").strip() or None
        db.put_flexible_poll_option(FlexiblePollOption(
            community_id=app.community_id, app_id=app.app_id,
            event_id=evt.event_id, iso_date=d, start_time=t, label=lbl,
            sort_key=n))
        n += 1
    log.info("flexible event created app=%s event=%s dates=%d by=%s",
             app.app_id, evt.event_id, n, user.user_id)
    return _redirect(
        f"/flex/event/results?event={evt.event_id}&notice="
        + urllib.parse.quote("Poll created. Review it, then send it to the group."))


def _api_flex_event_add_date(event: dict, user: User,
                             community: Community | None, app: Application,
                             membership: Membership | None) -> dict:
    """Add one candidate date to an OPEN poll. Existing votes are untouched;
    prior responders see the new date as un-voted next time they open their
    link, and a re-send nudges them to weigh in."""
    if _http_method(event) != "POST":
        return _text(405, "POST required")
    guard = _flex_guard(user, membership, app)
    if guard:
        return guard
    event_id = (_get_param(event, "event") or "").strip()
    evt = db.get_flexible_event(app.app_id, event_id)
    if evt is None or evt.state != "poll":
        return _error_redirect("/", "That poll is not open.")
    d = (_get_param(event, "date") or "").strip()
    if not d:
        return _error_redirect(f"/flex/event/results?event={event_id}",
                               "Please pick a date to add.")
    t = (_get_param(event, "time") or "").strip() or None
    lbl = (_get_param(event, "label") or "").strip() or None
    options = list(db.list_flexible_poll_options(app.app_id, event_id))
    if any(o.iso_date == d and (o.start_time or "") == (t or "")
           for o in options):
        return _error_redirect(f"/flex/event/results?event={event_id}",
                               "That date is already in the poll.")
    db.put_flexible_poll_option(FlexiblePollOption(
        community_id=app.community_id, app_id=app.app_id, event_id=event_id,
        iso_date=d, start_time=t, label=lbl,
        sort_key=max((o.sort_key for o in options), default=-1) + 1))
    log.info("flex add-date app=%s event=%s date=%s by=%s",
             app.app_id, event_id, d, user.user_id)
    return _redirect(
        f"/flex/event/results?event={event_id}&notice="
        + urllib.parse.quote("Date added — re-send the poll so members can "
                             "weigh in on it."))


def _api_flex_event_remove_date(event: dict, user: User,
                                community: Community | None, app: Application,
                                membership: Membership | None) -> dict:
    """Remove one candidate date from an OPEN poll."""
    if _http_method(event) != "POST":
        return _text(405, "POST required")
    guard = _flex_guard(user, membership, app)
    if guard:
        return guard
    event_id = (_get_param(event, "event") or "").strip()
    evt = db.get_flexible_event(app.app_id, event_id)
    if evt is None or evt.state != "poll":
        return _error_redirect("/", "That poll is not open.")
    opt_id = (_get_param(event, "option") or "").strip()
    db.delete_flexible_poll_option(app.app_id, event_id, opt_id)
    return _redirect(f"/flex/event/results?event={event_id}&notice="
                     + urllib.parse.quote("Date removed."))


# ---- AA: results / tallies / close form -----------------------------------

def _flex_event_results_page(event: dict, user: User,
                             community: Community | None, app: Application,
                             membership: Membership | None) -> dict:
    guard = _flex_guard(user, membership, app)
    if guard:
        return guard
    event_id = (_get_param(event, "event") or "").strip()
    evt = db.get_flexible_event(app.app_id, event_id)
    if evt is None:
        return _error_redirect("/", "Event not found.")
    if evt.merged_into:
        # An AA following an old link/bookmark to a poll that's been folded
        # into another. Send them to the surviving one rather than rendering
        # a tombstone's frozen votes as if they were the live picture.
        return _redirect("/flex/event/results?event="
                         + db.resolve_merged_event(app.app_id, event_id))
    options = list(db.list_flexible_poll_options(app.app_id, event_id))
    rsvps = list(db.list_flexible_rsvps(app.app_id, event_id))
    rsvp_by_user = {r.user_id: r for r in rsvps}
    members = [m for m in db.list_memberships_for_app(app.app_id)]
    users = {u.user_id: u for u in db.list_users(app.community_id)}
    opted_out_ids = {m.user_id for m in members if m.opted_out}
    # Households already accounted for (personal answer or a covered spouse) —
    # used for the "haven't answered" line below.
    _covered_ids = _flex_covered_or_responded_ids(members, users, rsvps)

    def name_of(uid: str) -> str:
        u = users.get(uid)
        return (u.name or u.email) if u else uid

    # Per-date results counted by HOUSEHOLD, with the total people implied —
    # "Yes (6 for 12)" = 6 households / 12 people expected. A household's stance
    # is taken from whoever answered for it (the response with the largest
    # headcount). Names are shown grouped by household (e.g. "Bob & Sue").
    hh_members: dict[str, list[str]] = {}
    for m in members:
        hh_members.setdefault(_hh_key(users, m.user_id), []).append(m.user_id)
    hh_rsvps: dict[str, list] = {}
    for r in rsvps:
        hh_rsvps.setdefault(_hh_key(users, r.user_id), []).append(r)

    def _hh_names(hkey: str) -> str:
        return " & ".join(sorted(name_of(uid) for uid in hh_members.get(hkey, [])))

    def _hh_rep(hkey: str):
        rs = hh_rsvps.get(hkey)
        return max(rs, key=lambda r: (r.party_size or 0)) if rs else None

    def _bucket(entries: list[tuple], label: str, color: str) -> str:
        if not entries:
            return ""
        names = html.escape(", ".join(sorted(n for n, _ in entries)))
        return (f"<div style='margin:3px 0'><b style='color:{color}'>"
                f"{label} ({len(entries)} for {sum(h for _, h in entries)}):"
                f"</b> {names}</div>")

    def _pending(names: list[str]) -> str:
        if not names:
            return ""
        joined = html.escape(", ".join(sorted(names)))
        n = len(names)
        return (f"<details style='margin:3px 0'><summary style='cursor:pointer;"
                f"color:#999'><b style='color:#999'>Pending ({n} household"
                + ("s" if n != 1 else "") + ")</b></summary>"
                f"<div style='color:#999;margin-top:2px;font-size:0.95em'>"
                f"{joined}</div></details>")

    # Households we haven't heard from at all — same for every date.
    pending_hh = sorted(
        _hh_names(hk) for hk, uids in hh_members.items()
        if _hh_rep(hk) is None and not all(u in opted_out_ids for u in uids))

    tally_rows = ""
    for opt in options:
        yes_e: list[tuple] = []
        maybe_e: list[tuple] = []
        no_e: list[tuple] = []
        for hk, uids in hh_members.items():
            rep = _hh_rep(hk)
            if rep is None:
                continue
            v = rep.votes.get(opt.option_id)
            if v not in ("yes", "maybe", "no"):
                continue
            entry = (_hh_names(hk), rep.party_size or len(uids))
            (yes_e if v == "yes" else maybe_e if v == "maybe" else no_e).append(entry)
        label = _fmt_iso_date(opt.iso_date)
        if opt.start_time:
            label += f" {_fmt_time(opt.start_time)}"
        if opt.label:
            label += f" ({opt.label})"
        won = (evt.state == "scheduled" and opt.iso_date == evt.winning_date)
        bg = "background:#f0fff5;" if won else ""
        who = ("".join([
            _bucket(yes_e, "Yes", "#2a7"),
            _bucket(maybe_e, "Maybe", "#b8860b"),
            _bucket(no_e, "No", "#c33"),
            _pending(pending_hh),
        ]) or "<span style='color:#888'>no responses yet</span>")
        tally_rows += (
            f"<tr style='{bg}border-top:1px solid #eee'>"
            "<td style='padding:8px 10px;vertical-align:top'>"
            f"<b>{html.escape(label)}</b>"
            + (" <span style='color:#2a7;white-space:nowrap'>&check; chosen</span>"
               if won else "")
            + (" <form method='post' action='/api/flex/event/remove-date' "
               "style='display:inline' onsubmit=\"return confirm('Remove this "
               "date from the poll?')\">"
               f"<input type='hidden' name='event' value='{event_id}'>"
               f"<input type='hidden' name='option' value='{opt.option_id}'>"
               "<button type='submit' title='Remove this date' style='border:none;"
               "background:none;color:#c33;cursor:pointer;font-size:1.1em;"
               "line-height:1'>&times;</button></form>"
               if evt.state == "poll" else "")
            + "</td>"
            "<td style='padding:8px 10px;vertical-align:top;font-size:0.95em'>"
            f"{who}</td></tr>")

    # Bringing roster + household-deduped headcount
    bringing_rows = ""
    hh_best: dict[str, int] = {}
    for r in rsvps:
        if r.bringing:
            bringing_rows += (f"<li>{html.escape(name_of(r.user_id))}: "
                              f"{html.escape(r.bringing)}</li>")
        if r.confirmed_response != "no" and r.party_size:
            u = users.get(r.user_id)
            hh = (u.household_id if u and u.household_id else f"solo:{r.user_id}")
            hh_best[hh] = max(hh_best.get(hh, 0), r.party_size)
    total_headcount = sum(hh_best.values())
    declined = [r.user_id for r in rsvps if r.confirmed_response == "no"]

    # Close form (only while still a poll). The AA's deliberate click here is
    # the go/no-go decision gate — she reviews turnout first and may instead
    # cancel or change the date. With a single candidate date there's nothing
    # to "pick", so we present the date as a fact and confirm it directly rather
    # than rendering a pointless one-option radio; the gate itself stays.
    close_html = ""
    if evt.state == "poll" and options:
        if len(options) == 1:
            o = options[0]
            when = (_fmt_iso_date(o.iso_date)
                    + (f" {_fmt_time(o.start_time)}" if o.start_time else ""))
            close_body = (
                f"<input type='hidden' name='winning_option' value='{o.option_id}'>"
                "<b>Confirm this event</b>"
                f"<p style='margin:6px 0'>Date: <b>{html.escape(when)}</b></p>"
                "<p style='color:#888;font-size:0.85em;margin:0 0 8px'>Sends the "
                "calendar invite to everyone who's in, and a short note to anyone "
                "who can't make it. Not enough interest? You can still change the "
                "date or cancel below.</p>"
                "<p><button type='submit' style='padding:7px 18px;cursor:pointer'>"
                f"Confirm &amp; send invites for {html.escape(when)} &rarr;"
                "</button></p>")
        else:
            opt_radios = "".join(
                "<label style='display:block;margin:4px 0'>"
                f"<input type='radio' name='winning_option' value='{o.option_id}'> "
                f"{html.escape(_fmt_iso_date(o.iso_date))}"
                + (f" {_fmt_time(o.start_time)}" if o.start_time else "")
                + "</label>"
                for o in options)
            close_body = (
                "<b>Pick the winning date</b>"
                f"{opt_radios}"
                "<p><button type='submit' style='padding:7px 18px;cursor:pointer'>"
                "Review &amp; send invites &rarr;</button></p>")
        close_html = (
            "<form method='post' action='/api/flex/event/close-review' "
            "style='margin:14px 0;padding:12px;border:1px solid #cfe5cf;"
            "border-radius:8px;background:#fbfdfb'>"
            f"<input type='hidden' name='event' value='{event_id}'>"
            f"{close_body}</form>")

    tokens = list(db.list_event_tokens(app.app_id, event_id))
    sent = len(tokens)

    def _deliverable(uid):
        u = users.get(uid)
        return bool(u and u.email and not u.email_undeliverable
                    and u.channel != "none")
    # Who'd actually receive the "haven't answered yet" send: current members,
    # deliverable, not opted out, not already covered. Iterate MEMBERS, not
    # tokens — a stale token for a removed member must not inflate the count.
    _unanswered_uids = [m.user_id for m in members
                        if not m.opted_out and _deliverable(m.user_id)
                        and m.user_id not in _covered_ids]
    # Distinct households among them (solo members count as their own).
    _nr_hh = {(users[uid].household_id
               if uid in users and users[uid].household_id else f"solo:{uid}")
              for uid in _unanswered_uids}
    non_responder_hh = len(_nr_hh)
    non_responder_ppl = len(_unanswered_uids)
    # Names of the not-yet-answered, grouped by household (couples together),
    # so an admin can see at a glance who's still outstanding.
    _un_by_hh: dict[str, list[str]] = {}
    for uid in _unanswered_uids:
        u = users.get(uid)
        _un_by_hh.setdefault(
            u.household_id if u and u.household_id else f"solo:{uid}",
            []).append(name_of(uid))
    _un_groups = sorted(" & ".join(sorted(g)) for g in _un_by_hh.values())
    unanswered_names_html = ""
    if sent and _unanswered_uids and evt.state == "poll":
        unanswered_names_html = (
            "<div style='margin:6px 0 10px;font-size:0.9em;color:#555;"
            "max-width:560px'><b>Haven't answered yet</b> "
            f"({non_responder_hh} household" + ("s" if non_responder_hh != 1 else "")
            + f" / {non_responder_ppl} individual"
            + ("s" if non_responder_ppl != 1 else "") + "): "
            + html.escape(", ".join(_un_groups)) + "</div>")
    declined_ct = sum(1 for r in rsvps if r.confirmed_response == "no")
    _green_btn = ("padding:7px 18px;cursor:pointer;background:#2a7;color:#fff;"
                  "border:none;border-radius:5px")
    if evt.state != "poll":
        send_html = (f"<span style='color:#888;font-size:0.9em'>{sent} "
                     "link(s) sent</span>") if sent else ""
    elif not sent:
        # First send — straight to everyone in the group.
        send_html = (
            "<form method='post' action='/api/flex/event/send-poll'>"
            f"<input type='hidden' name='event' value='{event_id}'>"
            "<input type='hidden' name='audience' value='all'>"
            f"<button type='submit' style='{_green_btn}'>"
            "Send poll email to the group</button></form>")
    else:
        # Re-send — pick the audience. Every recipient gets response-aware copy
        # (their own recap / their household's answer / a change-your-mind
        # note), so any of these is safe to send.
        def _aopt(val, label, checked=False):
            return ("<label style='display:block;margin:3px 0'>"
                    f"<input type='radio' name='audience' value='{val}'"
                    + (" checked" if checked else "") + f"> {label}</label>")

        # "Only people I pick" — nobody ticked by default. The other three
        # audiences are derived sets; this one is hers to choose, so it opens
        # blank rather than pre-armed with a send to everybody.
        _org = app.name or (community.name if community else "")
        # "Book Club: August Book Club" -- don't prefix when the title already
        # carries the group's name, which it usually does for a one-event app.
        _subj = (evt.title if (not _org or _org.lower() in evt.title.lower())
                 else f"{_org}: {evt.title}")
        _pick_rows = "".join(
            "<label style='display:block;margin:2px 0'>"
            f"<input type='checkbox' name='user_id' value='{m.user_id}'> "
            + html.escape(name_of(m.user_id))
            + ("<span style='color:#2a7;font-size:0.85em'> &#10003; answered"
               "</span>" if m.user_id in _covered_ids else "")
            + "</label>"
            for m in sorted(members,
                            key=lambda m: (name_of(m.user_id) or "").lower())
            if not m.opted_out and _deliverable(m.user_id))
        _pick_html = (
            "<div id='pick-panel' style='display:none;margin:4px 0 0 18px;"
            "border-left:2px solid #ddd;padding-left:10px'>"
            + _pick_rows
            + "<p style='margin:8px 0 2px'><label>Subject<br>"
            "<input name='subject' style='width:100%;max-width:420px' "
            f"value='{html.escape(_subj)}'></label></p>"
            "<p style='margin:4px 0 2px'><label>Message<br>"
            "<textarea name='note' rows='4' style='width:100%;max-width:420px' "
            "placeholder='e.g. We added a fifth date &mdash; could you weigh "
            "in?'></textarea></label></p>"
            "<p style='margin:2px 0;color:#888;font-size:0.85em'>Each person "
            "gets their own poll link below your message. One email each "
            "&mdash; nobody sees anyone else&#39;s address.</p></div>")
        send_html = (
            "<form method='post' action='/api/flex/event/send-poll' "
            "style='margin:6px 0;text-align:left;max-width:460px'>"
            f"<input type='hidden' name='event' value='{event_id}'>"
            "<p style='margin:0 0 4px;font-weight:600'>Send an update about "
            "this poll to:</p>"
            + _aopt("all", "Everyone in the group", checked=True)
            + _aopt("not_declined", "Everyone except those who've declined"
                    + (f" ({declined_ct} declined)" if declined_ct else ""))
            + _aopt("unanswered",
                    "Only those who haven't answered yet ("
                    + f"{non_responder_hh} household"
                    + ("s" if non_responder_hh != 1 else "")
                    + f" / {non_responder_ppl} individual"
                    + ("s" if non_responder_ppl != 1 else "") + ")")
            + _aopt("selected", "Only people I pick &mdash; with a note from you")
            + _pick_html
            + f"<button type='submit' style='margin-top:6px;{_green_btn}' "
            "onclick=\"return confirm('Send this poll update to the selected "
            "group?');\">Send</button>"
            f"  <span style='color:#888;font-size:0.9em'>{sent} link(s) sent"
            "</span>"
            "<script>document.querySelectorAll(\"input[name='audience']\")"
            ".forEach(function(r){r.addEventListener('change',function(){"
            "document.getElementById('pick-panel').style.display="
            "(r.value==='selected'&&r.checked)?'block':'none';});});</script>"
            "</form>")

    # Editable 'message to the group' (carried in the invite + reminder emails)
    # — shown while it's still a poll so the AA can tune it at send time.
    msg_html = ""
    if evt.state == "poll":
        msg_html = (
            "<form method='post' action='/api/flex/event/message' "
            "style='margin:14px 0;max-width:560px;text-align:left'>"
            f"<input type='hidden' name='event' value='{event_id}'>"
            "<label style='display:block;font-weight:600;margin-bottom:2px'>"
            "Message to the group</label>"
            "<p style='color:#888;font-size:0.85em;margin:0 0 4px'>Goes in the "
            "invitation and reminder emails — the book, what to expect, anything "
            "you'd like to say.</p>"
            "<textarea name='description' rows='4' style='padding:6px;"
            f"width:100%'>{html.escape(evt.description or '')}</textarea>"
            "<p style='margin:6px 0 0'><button type='submit' "
            "style='padding:6px 16px;cursor:pointer'>Save message</button></p>"
            "</form>")

    state_banner = ""
    if evt.state == "scheduled":
        state_banner = (
            "<p style='background:#f0fff5;border:1px solid #2a7;"
            "border-radius:6px;padding:8px 12px'>Scheduled for "
            f"<b>{html.escape(_fmt_iso_date(evt.winning_date or ''))}"
            + (f" {_fmt_time(evt.winning_start_time)}"
               if evt.winning_start_time else "") + "</b></p>")

    # AA escape hatch: a never-sent poll is deleted outright (fixes a typo);
    # a sent poll / scheduled event is cancelled. Apostrophe-free copy so the
    # confirmSubmit JS string stays valid.
    cancel_html = ""
    if evt.state in ("poll", "scheduled"):
        if evt.state == "poll" and sent == 0:
            clabel = "Delete this poll"
            cmsg = ("Delete this poll? It has not been sent, so it will be "
                    "removed completely.")
        else:
            clabel = "Cancel this event"
            cmsg = ("Cancel this event? Anyone who opens their link will then "
                    "see that it is cancelled.")
        cancel_html = (
            "<form method='post' action='/api/flex/event/cancel' "
            "style='margin-top:20px'"
            f" onsubmit=\"return confirmSubmit(this,'{cmsg}','{clabel}',"
            "'#c33','Keep it')\">"
            f"<input type='hidden' name='event' value='{event_id}'>"
            "<button type='submit' style='font-size:0.85em;color:#c33;"
            "background:none;border:none;text-decoration:underline;"
            f"cursor:pointer;padding:0'>{clabel}</button></form>")

    # Per-event toggle: email the AA(s) on each member response (and always
    # on a post-close rejoin). Off by default. Auto-submits on change.
    notify_html = ""
    if evt.state in ("poll", "scheduled"):
        checked = " checked" if evt.notify_on_response else ""
        notify_html = (
            "<form method='post' action='/api/flex/event/notify' "
            "style='margin:10px 0;text-align:left'>"
            f"<input type='hidden' name='event' value='{event_id}'>"
            "<label style='font-size:0.92em;color:#555;cursor:pointer'>"
            "<input type='checkbox' name='notify_on_response' value='1'"
            f"{checked} onchange='this.form.submit()'> "
            "Email me each time someone responds</label></form>")

    body = (
        f"<h1>{html.escape(evt.title)}</h1>"
        f"<p style='color:#666;margin-top:-6px'>{html.escape(app.name)}</p>"
        + _flash_banner_html(event)
        + state_banner
        + (msg_html if evt.state == "poll"
           else (f"<p>{html.escape(evt.description)}</p>" if evt.description else ""))
        + (send_html if evt.state == "poll" else "")
        + unanswered_names_html
        + notify_html
        + "<h2 style='font-size:1.05em;margin-top:18px'>Date votes</h2>"
          "<p style='color:#888;font-size:0.85em;margin:-4px 0 6px'>Counts read "
          "<b>households for people</b> — e.g. <b>6 for 12</b> means 6 households, "
          "12 people expected.</p>"
          "<table style='border-collapse:collapse;width:100%;max-width:760px;"
          "text-align:left;table-layout:fixed'>"
          "<thead><tr style='color:#888;border-bottom:1px solid #ddd'>"
          "<th style='padding:4px 10px;width:210px'>Date</th>"
          "<th style='padding:4px 10px'>Who voted</th></tr></thead><tbody>"
        + (tally_rows or "<tr><td colspan='2' style='padding:6px;color:#888'>"
           "no candidate dates</td></tr>")
        + "</tbody></table>"
        + (("<form method='post' action='/api/flex/event/add-date' "
            "style='margin:8px 0 4px;font-size:0.9em'>"
            f"<input type='hidden' name='event' value='{event_id}'>"
            "<b>Add a date:</b> "
            "<input type='date' name='date' required style='padding:4px'> "
            "<input type='time' name='time' style='padding:4px' "
            "title='optional time'> "
            "<button type='submit' style='padding:5px 12px;cursor:pointer'>"
            "+ Add date</button></form>")
           if evt.state == "poll" else "")
        + f"<p style='margin-top:14px'><b>Expected headcount:</b> "
          f"{total_headcount} "
          "<span style='color:#888;font-size:0.9em'>(household-deduped)</span>"
          + (f" &middot; <b>Declined:</b> {len(declined)}" if declined else "")
          + (f" &middot; <b>Opted out of the group:</b> {len(opted_out_ids)}"
             if opted_out_ids else "")
        + "</p>"
        + ("<h2 style='font-size:1.05em;margin-top:14px'>Bringing</h2>"
           f"<ul>{bringing_rows}</ul>" if bringing_rows else "")
        + close_html
        + cancel_html
        # The results page isn't a nav tab — pass no current so "App home"
        # renders as a clickable back-link, not a disabled span.
        + _admin_nav_bar("", app=app)
    )
    return _html(200, _page(body, narrow=False, title=evt.title))


# ---- AA: send the poll email (mint/reuse tokens + magic links) ------------

def _flex_covered_or_responded_ids(memberships, users, rsvps) -> set:
    """User_ids that need no reminder for an open poll: they personally
    answered (any answer, including a decline), OR a household-mate answered
    with a headcount that exactly matches the household's member count.

    NOTE: an individual's DECLINE never 'covers' their household — we support
    accepting on behalf of a household but not declining on its behalf — so a
    decline only removes the individual, not their household-mates.
    """
    covered = {r.user_id for r in rsvps}   # personal responders incl. declines
    hh_size: dict[str, int] = {}
    for m in memberships:
        u = users.get(m.user_id)
        if u and u.household_id:
            hh_size[u.household_id] = hh_size.get(u.household_id, 0) + 1
    covered_hh = set()
    for r in rsvps:
        u = users.get(r.user_id)
        hid = u.household_id if u else None
        if hid and r.party_size and r.party_size == hh_size.get(hid):
            covered_hh.add(hid)
    for m in memberships:
        u = users.get(m.user_id)
        if u and u.household_id in covered_hh:
            covered.add(m.user_id)
    return covered


def _hh_key(users, uid: str) -> str:
    """Grouping key for a user's household — the household_id, or a per-user
    'solo:' key when they aren't in one."""
    u = users.get(uid)
    return u.household_id if u and u.household_id else f"solo:{uid}"


def _flex_headcount(rsvps, users) -> int:
    """Household-deduped expected headcount: max party_size reported per
    household among members who didn't decline (solo members count once)."""
    best: dict[str, int] = {}
    for r in rsvps:
        if r.confirmed_response == "no" or not r.party_size:
            continue
        k = _hh_key(users, r.user_id)
        best[k] = max(best.get(k, 0), r.party_size)
    return sum(best.values())


def _flex_household_counts(memberships, users, rsvps) -> tuple:
    """(responded_households, total_households): a household counts once and is
    'responded' if any member answered OR it's covered (headcount match)."""
    covered = _flex_covered_or_responded_ids(memberships, users, rsvps)
    total = {_hh_key(users, m.user_id) for m in memberships}
    responded = {_hh_key(users, uid) for uid in covered} & total
    return len(responded), len(total)


def _flex_response_summary_text(rsvp, options) -> str:
    """Plain-text recap of what a member already answered — echoed back in a
    re-send so a responder sees we remembered, not a blank 'please vote'."""
    lines = []
    for o in options:
        v = rsvp.votes.get(o.option_id)
        if v in _RESP_LABEL:
            lines.append("  - " + _fmt_iso_date(o.iso_date)
                         + (f" {_fmt_time(o.start_time)}" if o.start_time else "")
                         + f": {_RESP_LABEL[v]}")
    parts = []
    if lines:
        parts.append("Your response so far:\n" + "\n".join(lines))
    if rsvp.party_size:
        parts.append(f"Party size: {rsvp.party_size}")
    if rsvp.bringing:
        parts.append(f"Bringing: {rsvp.bringing}")
    return "\n".join(parts)


def _api_flex_event_send_poll(event: dict, user: User,
                              community: Community | None, app: Application,
                              membership: Membership | None) -> dict:
    if _http_method(event) != "POST":
        return _text(405, "POST required")
    guard = _flex_guard(user, membership, app)
    if guard:
        return guard
    event_id = (_get_param(event, "event") or "").strip()
    evt = db.get_flexible_event(app.app_id, event_id)
    if evt is None or evt.state != "poll":
        return _error_redirect("/", "That poll is not open.")
    series = db.get_flexible_series_for_app(app.app_id)
    options = list(db.list_flexible_poll_options(app.app_id, event_id))
    users = {u.user_id: u for u in db.list_users(app.community_id)}
    memberships = list(db.list_memberships_for_app(app.app_id))
    rsvps = {r.user_id: r for r in db.list_flexible_rsvps(app.app_id, event_id)}
    # Household coverage: a member is already "covered" when a household-mate
    # answered with a headcount matching the household size (same rule as the
    # send-email "haven't responded" quick-pick). Covered members get a "your
    # household already responded" note on a re-send, not a fresh invite.
    _hh_size: dict[str, int] = {}
    for _m in memberships:
        _u = users.get(_m.user_id)
        if _u and _u.household_id:
            _hh_size[_u.household_id] = _hh_size.get(_u.household_id, 0) + 1
    hh_cover: dict[str, tuple] = {}   # household_id -> (responder_user, rsvp)
    for _r in rsvps.values():
        _ru = users.get(_r.user_id)
        _hid = _ru.household_id if _ru else None
        if (_hid and _r.party_size and _r.party_size == _hh_size.get(_hid)
                and _hid not in hh_cover):
            hh_cover[_hid] = (_ru, _r)
    # Audience for a re-send: "all" (default), "not_declined" (skip members who
    # personally declined — individual only, no household logic), "unanswered"
    # (skip anyone already covered), or "selected" (an explicit list of members
    # the AA ticked, carrying her own note instead of the templated copy).
    audience = (_get_param(event, "audience") or "all").strip()
    picked: set[str] = set()
    note = ""
    note_subject = ""
    if audience == "selected":
        # _get_param returns one value; the picker posts a user_id per tick.
        _form = _parse_form(event)
        _member_ids = {m.user_id for m in memberships}
        picked = {uid for uid in _form.get("user_id", []) if uid in _member_ids}
        note = (_form.get("note", [""])[0] or "").strip()
        note_subject = _safe_header(_form.get("subject", [""])[0])
        if not picked:
            return _error_redirect(f"/flex/event/results?event={event_id}",
                                   "Pick at least one person to email.")
        if not note:
            return _error_redirect(f"/flex/event/results?event={event_id}",
                                   "Write a message to send.")
    covered_ids = _flex_covered_or_responded_ids(
        memberships, users, list(rsvps.values()))
    domain = _public_domain(community)
    from community_organizer.providers.email import get_email_provider
    provider = get_email_provider()
    from_addr = _from_addr(user.name, app.name)
    org_name = app.name or (community.name if community else "")

    date_lines = "\n".join(
        "  - " + _fmt_iso_date(o.iso_date)
        + (f" {_fmt_time(o.start_time)}" if o.start_time else "")
        + (f" ({o.label})" if o.label else "")
        for o in options)
    # A single-date event is a fixed-date invitation, not a date poll — the
    # invite says "you're invited on <date>, can you come?" rather than asking
    # people to pick among dates.
    single = len(options) == 1
    when_line = ""
    if single:
        _o0 = options[0]
        when_line = (_fmt_iso_date(_o0.iso_date)
                     + (f" {_fmt_time(_o0.start_time)}" if _o0.start_time else "")
                     + (f" ({_o0.label})" if _o0.label else ""))

    now = dt.datetime.now(dt.timezone.utc)
    sent = 0
    for m in memberships:
        if m.opted_out:
            continue
        u = users.get(m.user_id)
        if not u or not u.email or u.email_undeliverable or u.channel == "none":
            continue
        if audience == "selected" and m.user_id not in picked:
            continue
        if audience == "unanswered" and m.user_id in covered_ids:
            continue
        _mr = rsvps.get(m.user_id)
        if (audience == "not_declined" and _mr is not None
                and _mr.confirmed_response == "no"):
            continue
        tok = db.get_event_token(app.app_id, event_id, m.user_id)
        if tok is None:
            tok = EventToken(
                community_id=app.community_id, app_id=app.app_id,
                event_id=event_id, user_id=m.user_id,
                token=secrets.token_urlsafe(32),
                expires_at=(now + dt.timedelta(days=EVENT_TOKEN_TTL_DAYS))
                .isoformat(timespec="seconds"))
            db.put_event_token(tok)
        link = f"https://{domain}/e/{tok.token}"
        rsvp = rsvps.get(m.user_id)
        cover = hh_cover.get(u.household_id) if u.household_id else None
        if audience == "selected":
            # Her words, their link. No response-aware template — she picked
            # these people deliberately and knows what she wants to say.
            subject = note_subject or f"{org_name}: {evt.title}"
            body = (
                f"Hi {u.name},\n\n"
                f"{note}\n\n"
                "Your personal link to answer:\n\n"
                f"  {link}\n\n"
                "Proposed dates:\n"
                f"{date_lines}\n\n"
                "Replies to this email are not recorded — your answer only "
                "counts through the link.\n\n"
                "(This link is just for you — no login needed.)\n\n"
                f"-- {org_name}\n")
        elif rsvp is not None and rsvp.confirmed_response == "no":
            # Previously declined — don't ask again.
            subject = f"{org_name}: your response to {evt.title}"
            body = (
                f"Hi {u.name},\n\n"
                f"You previously let us know {evt.title} won't work for you this "
                f"time.\n\n"
                "If you've changed your mind, click here to respond:\n\n"
                f"  {link}\n\n"
                "(This link is just for you — no login needed.)\n\n"
                f"-- {org_name}\n")
        elif rsvp is not None:
            summ = _flex_response_summary_text(rsvp, options)
            _voted_all = all(rsvp.votes.get(o.option_id) in ("yes", "no", "maybe")
                             for o in options)
            if not _voted_all:
                # A date was added after they answered — invite them to weigh in.
                subject = f"{org_name}: a new date for {evt.title}"
                body = (
                    f"Hi {u.name},\n\n"
                    f"We've added a new date to {evt.title}. Here's what "
                    f"you've told us so far:\n\n"
                    + (summ + "\n\n" if summ else "")
                    + "When you have a moment, please weigh in on the new date "
                    "(and change anything else if you'd like) with your "
                    "personal link:\n\n"
                    f"  {link}\n\n"
                    "(This link is just for you — no login needed.)\n\n"
                    f"-- {org_name}\n")
            else:
                # Answered on every date — echo it back, invite a change only.
                subject = f"{org_name}: we have your response for {evt.title}"
                body = (
                    f"Hi {u.name},\n\n"
                    f"You previously responded to {evt.title} with:\n\n"
                    + (summ + "\n\n" if summ else "")
                    + "No need to respond again unless you'd like to change "
                    "your response — here's your personal link:\n\n"
                    f"  {link}\n\n"
                    "(This link is just for you — no login needed.)\n\n"
                    f"-- {org_name}\n")
        elif cover is not None and cover[0].user_id != m.user_id:
            # A household-mate already answered for the whole household.
            responder_u, responder_rsvp = cover
            summ = _flex_response_summary_text(responder_rsvp, options)
            _hh_voted_all = all(
                responder_rsvp.votes.get(o.option_id) in ("yes", "no", "maybe")
                for o in options)
            if not _hh_voted_all:
                # A date was added after the household answered — ask them to
                # weigh in on it too.
                subject = f"{org_name}: a new date for {evt.title}"
                body = (
                    f"Hi {u.name},\n\n"
                    f"We've added a new date to {evt.title}. Your household "
                    f"member {responder_u.name} answered for the earlier "
                    f"dates with:\n\n"
                    + (summ + "\n\n" if summ else "")
                    + "Please weigh in on the new date for your household using "
                    "your personal link:\n\n"
                    f"  {link}\n\n"
                    "(This link is just for you — no login needed.)\n\n"
                    f"-- {org_name}\n")
            else:
                subject = f"{org_name}: your household's response to {evt.title}"
                body = (
                    f"Hi {u.name},\n\n"
                    f"Your household member {responder_u.name} previously "
                    f"responded for your household to {evt.title} with:\n\n"
                    + (summ + "\n\n" if summ else "")
                    + "No need to respond again unless you'd like to change "
                    "your response — here's your personal link:\n\n"
                    f"  {link}\n\n"
                    "(This link is just for you — no login needed.)\n\n"
                    f"-- {org_name}\n")
        elif single:
            # Fixed-date invitation — one date, RSVP not a poll.
            subject = f"{org_name}: you're invited to {evt.title}"
            body = (
                f"Hi {u.name},\n\n"
                f"You're invited to {evt.title} on {when_line}.\n\n"
                + (f"{evt.description}\n\n" if evt.description else "")
                + "Can you make it? Answer for your family or for yourself — "
                "click your personal link to RSVP and say what you'll bring:\n\n"
                f"  {link}\n\n"
                "Please respond by clicking your personal link above. Replies to "
                "this email are not recorded — your answer only counts through "
                "the link.\n\n"
                "(This link is just for you — no login needed.)\n\n"
                f"-- {org_name}\n")
        else:
            # Not yet covered — the normal invitation.
            subject = f"{org_name}: vote on dates for {evt.title}"
            body = (
                f"Hi {u.name},\n\n"
                f"You're invited to {evt.title}.\n\n"
                + (f"{evt.description}\n\n" if evt.description else "")
                + "Answer for your family or for yourself — click your personal "
                "link to pick which dates work and say what you'll bring:\n\n"
                f"  {link}\n\n"
                "Proposed dates:\n"
                f"{date_lines}\n\n"
                "Please respond by clicking your personal link above. Replies to "
                "this email are not recorded — your answer only counts through "
                "the link.\n\n"
                "(This link is just for you — no login needed.)\n\n"
                f"-- {org_name}\n")
        provider.send(
            community_id=app.community_id, from_addr=from_addr,
            to_addr=u.email, subject=subject,
            body_text=body, kind="event_poll_invite",
            related_user_id=m.user_id, related_app_id=app.app_id)
        sent += 1
    log.info("flexible poll sent app=%s event=%s audience=%s recipients=%d",
             app.app_id, event_id, audience, sent)
    _notice = {
        "unanswered": f"Sent to {sent} who hadn't answered yet.",
        "not_declined":
            f"Sent to {sent} member(s) (skipping those who declined).",
        "selected": f"Sent your note to {sent} member(s).",
    }.get(audience, f"Poll email sent to {sent} member(s).")
    return _redirect(
        f"/flex/event/results?event={event_id}&notice="
        + urllib.parse.quote(_notice))


def _api_flex_event_save_message(event: dict, user: User,
                                 community: Community | None, app: Application,
                                 membership: Membership | None) -> dict:
    """AA edits the event's 'message to the group' (carried in the invite +
    reminder emails). Lives on the results page so it can be tuned at send time."""
    if _http_method(event) != "POST":
        return _text(405, "POST required")
    guard = _flex_guard(user, membership, app)
    if guard:
        return guard
    event_id = (_get_param(event, "event") or "").strip()
    evt = db.get_flexible_event(app.app_id, event_id)
    if evt is None:
        return _error_redirect("/", "Event not found.")
    evt.description = (_get_param(event, "description") or "").strip()
    try:
        db.put_flexible_event(evt, expected_version=evt.version)
    except db.ConcurrencyConflict:
        return _error_redirect(f"/flex/event/results?event={event_id}",
                               "Someone else just changed this — refresh.")
    return _redirect(f"/flex/event/results?event={event_id}&notice="
                     + urllib.parse.quote("Message saved."))


def _api_flex_event_save_notify(event: dict, user: User,
                                community: Community | None, app: Application,
                                membership: Membership | None) -> dict:
    """AA toggles 'email me on each response' for this event."""
    if _http_method(event) != "POST":
        return _text(405, "POST required")
    guard = _flex_guard(user, membership, app)
    if guard:
        return guard
    event_id = (_get_param(event, "event") or "").strip()
    evt = db.get_flexible_event(app.app_id, event_id)
    if evt is None:
        return _error_redirect("/", "Event not found.")
    evt.notify_on_response = bool(_get_param(event, "notify_on_response"))
    try:
        db.put_flexible_event(evt, expected_version=evt.version)
    except db.ConcurrencyConflict:
        return _error_redirect(f"/flex/event/results?event={event_id}",
                               "Someone else just changed this — refresh.")
    msg = ("You'll get an email for each response."
           if evt.notify_on_response
           else "Per-response emails are off.")
    return _redirect(f"/flex/event/results?event={event_id}&notice="
                     + urllib.parse.quote(msg))


# ---- AA: close (two-step — review the cohorts, then send) ------------------

def _flex_cohorts(app: Application, community: Community | None,
                  event_id: str, winning_option_id: str):
    """Split this app's members for the winning date into two deliverable,
    non-opted-out cohorts: ``confirmed`` (invite + .ics — anyone NOT a No on
    the winning date, incl. non-responders) and ``couldnt`` (courtesy note —
    No on the winning date or a full all-No decline). Returns
    (confirmed_users, couldnt_users, {user_id: FlexibleRSVP})."""
    rsvps = {r.user_id: r for r in db.list_flexible_rsvps(app.app_id, event_id)}
    users = {u.user_id: u for u in db.list_users(app.community_id)}
    confirmed: list[User] = []
    couldnt: list[User] = []
    for m in db.list_memberships_for_app(app.app_id):
        if m.opted_out:
            continue
        u = users.get(m.user_id)
        if not u or not u.email or u.email_undeliverable or u.channel == "none":
            continue
        r = rsvps.get(m.user_id)
        vote = r.votes.get(winning_option_id) if r else None
        declined = bool(r and r.confirmed_response == "no")
        if vote == "no" or declined:
            couldnt.append(u)
        else:
            confirmed.append(u)
    return confirmed, couldnt, rsvps


def _api_flex_event_close_review(event: dict, user: User,
                                 community: Community | None, app: Application,
                                 membership: Membership | None) -> dict:
    if _http_method(event) != "POST":
        return _text(405, "POST required")
    guard = _flex_guard(user, membership, app)
    if guard:
        return guard
    event_id = (_get_param(event, "event") or "").strip()
    winning = (_get_param(event, "winning_option") or "").strip()
    evt = db.get_flexible_event(app.app_id, event_id)
    if evt is None or evt.state != "poll":
        return _error_redirect("/", "That poll is not open.")
    opt = next((o for o in db.list_flexible_poll_options(app.app_id, event_id)
                if o.option_id == winning), None)
    if opt is None:
        return _error_redirect(f"/flex/event/results?event={event_id}",
                               "Please pick a winning date.")
    confirmed, couldnt, _rsvps = _flex_cohorts(app, community, event_id, winning)
    # "Attending" = household-deduped expected headcount of the invited cohort
    # (consistent with the home + results pages); the name list below still
    # shows every individual who gets the invite.
    _cusers = {u.user_id: u for u in confirmed + couldnt}
    _conf_ids = {u.user_id for u in confirmed}
    attending_headcount = _flex_headcount(
        [r for r in _rsvps if r.user_id in _conf_ids], _cusers)
    date_label = (_fmt_iso_date(opt.iso_date)
                  + (f" {_fmt_time(opt.start_time)}" if opt.start_time else ""))
    draft = (f"Hi,\n\n{date_label} was chosen for {evt.title}. So sorry you "
             "can't join us this time — we hope to see you at the next one!\n\n"
             f"-- {app.name}")
    confirm_draft = "Looking forward to seeing you — see you there!"

    def names(us: list[User]) -> str:
        return ", ".join(html.escape(u.name or u.email) for u in us) or "(none)"

    body = (
        f"<h1>Send invites — {html.escape(evt.title)}</h1>"
        f"<p>Winning date: <b>{html.escape(date_label)}</b></p>"
        "<form method='post' action='/api/flex/event/close'>"
        f"<input type='hidden' name='event' value='{event_id}'>"
        f"<input type='hidden' name='winning_option' value='{winning}'>"
        f"<h2 style='font-size:1.05em;color:#2a7'>Calendar invite &rarr; "
        f"{attending_headcount} attending "
        f"<span style='color:#888;font-weight:normal;font-size:0.85em'>"
        f"({len(confirmed)} invited)</span></h2>"
        f"<p style='color:#555'>{names(confirmed)}</p>"
        "<label style='display:block;margin:8px 0 4px'>Message to those who are "
        "coming (added to the confirmation, edit freely):</label>"
        "<textarea name='confirm_message' rows='3' "
        f"style='width:100%;max-width:520px;padding:6px'>{html.escape(confirm_draft)}"
        "</textarea>"
        f"<h2 style='font-size:1.05em;color:#c33'>Courtesy note &rarr; "
        f"{len(couldnt)} can't make it</h2>"
        f"<p style='color:#555'>{names(couldnt)}</p>"
        "<label style='display:block;margin:8px 0 4px'>Message to those who "
        "can't make it (edit freely):</label>"
        "<textarea name='sorry_message' rows='5' "
        f"style='width:100%;max-width:520px;padding:6px'>{html.escape(draft)}"
        "</textarea>"
        "<p><button type='submit' style='padding:8px 22px;cursor:pointer;"
        "background:#2a7;color:#fff;border:none;border-radius:5px'>"
        "Confirm date &amp; send all</button>"
        f"<a href='/flex/event/results?event={event_id}' "
        "style='margin-left:14px'>Back</a></p></form>"
        # Not a nav destination — pass no current so "App home" stays a
        # clickable back-link rather than a disabled span.
        + _admin_nav_bar("", app=app))
    return _html(200, _page(body, title=evt.title))


def _api_flex_event_close(event: dict, user: User,
                          community: Community | None, app: Application,
                          membership: Membership | None) -> dict:
    if _http_method(event) != "POST":
        return _text(405, "POST required")
    guard = _flex_guard(user, membership, app)
    if guard:
        return guard
    event_id = (_get_param(event, "event") or "").strip()
    winning = (_get_param(event, "winning_option") or "").strip()
    sorry_message = (_get_param(event, "sorry_message") or "").strip()
    confirm_message = (_get_param(event, "confirm_message") or "").strip()
    evt = db.get_flexible_event(app.app_id, event_id)
    if evt is None or evt.state != "poll":
        return _error_redirect("/", "That poll is already closed.")
    options = list(db.list_flexible_poll_options(app.app_id, event_id))
    opt = next((o for o in options if o.option_id == winning), None)
    if opt is None:
        return _error_redirect(f"/flex/event/results?event={event_id}",
                               "Please pick a winning date.")
    confirmed, couldnt, rsvps = _flex_cohorts(app, community, event_id, winning)
    series = db.get_flexible_series_for_app(app.app_id)
    loc = evt.location or (series.default_location if series else None)
    # Finalize (optimistic guard makes a double-submit safe).
    evt.state = "scheduled"
    evt.winning_date = opt.iso_date
    evt.winning_start_time = opt.start_time or "18:00"
    try:
        db.put_flexible_event(evt, expected_version=evt.version)
    except db.ConcurrencyConflict:
        return _error_redirect(f"/flex/event/results?event={event_id}",
                               "Someone else just closed this — refresh.")
    # Keep the poll options after finalizing so the event's results page
    # still shows the full vote history (winner marked) — they're no longer
    # rendered to members (the scheduled view replaces the poll form).

    from community_organizer.providers.email import get_email_provider
    provider = get_email_provider()
    from_addr = _from_addr(user.name, app.name)
    org_name = app.name or (community.name if community else "")
    domain = _public_domain(community)
    tz_name = (app.default_timezone
               or (community.default_timezone if community else None)
               or "America/New_York")
    date_label = (_fmt_iso_date(opt.iso_date)
                  + (f" {_fmt_time(evt.winning_start_time)}"
                     if evt.winning_start_time else ""))
    for u in confirmed:
        r = rsvps.get(u.user_id)
        bringing = r.bringing if r else None
        ics = ical.make_flexible_event_ics(
            event_id=event_id, iso_date=opt.iso_date,
            start_time=evt.winning_start_time,
            duration_minutes=evt.winning_duration_minutes,
            summary=evt.title, user_id=u.user_id, user_email=u.email,
            domain=domain, community_name=org_name, location=loc,
            bringing=bringing, timezone=tz_name)
        body = (f"Hi {u.name},\n\nIt's set: {evt.title} on {date_label}"
                + (f" at {loc}" if loc else "") + ".\n\n"
                + (f"{confirm_message}\n\n" if confirm_message else "")
                + (f"You're bringing: {bringing}\n\n" if bringing else "")
                + "A calendar invite is attached.\n\n"
                f"-- {org_name}\n")
        provider.send(
            community_id=app.community_id, from_addr=from_addr, to_addr=u.email,
            subject=f"{org_name}: confirmed — {evt.title} on {date_label}",
            body_text=body, kind="event_confirmed",
            related_user_id=u.user_id, related_app_id=app.app_id,
            ics_content=ics)
    for u in couldnt:
        # Include their personal link with a "change of mind" note — plans
        # change, and a No-voter can still join (see _api_flex_token_join).
        rejoin = ""
        tk = db.get_event_token(app.app_id, event_id, u.user_id)
        if tk:
            rejoin = ("\n\nIf your plans change, you can still join — just open "
                      "your personal link and choose “Yes, I can come”:\n"
                      f"  https://{domain}/e/{tk.token}\n")
        provider.send(
            community_id=app.community_id, from_addr=from_addr, to_addr=u.email,
            subject=f"{org_name}: {evt.title} — {date_label}",
            body_text=(sorry_message or
                       f"{date_label} was chosen for {evt.title}.") + rejoin + "\n",
            kind="event_missed",
            related_user_id=u.user_id, related_app_id=app.app_id)
    log.info("flexible event closed app=%s event=%s invited=%d sorry=%d",
             app.app_id, event_id, len(confirmed), len(couldnt))
    return _redirect("/?notice=" + urllib.parse.quote(
        f"{evt.title} confirmed for {date_label}. "
        f"{len(confirmed)} invited, {len(couldnt)} notified."))


def _api_flex_event_cancel(event: dict, user: User,
                           community: Community | None, app: Application,
                           membership: Membership | None) -> dict:
    """AA: remove a poll. Never-sent polls are deleted outright (a typo just
    disappears); sent polls / scheduled events are marked cancelled, so a
    member who opens their link sees it's cancelled."""
    if _http_method(event) != "POST":
        return _text(405, "POST required")
    guard = _flex_guard(user, membership, app)
    if guard:
        return guard
    event_id = (_get_param(event, "event") or "").strip()
    evt = db.get_flexible_event(app.app_id, event_id)
    if evt is None:
        return _error_redirect("/", "Event not found.")
    if evt.state in ("cancelled", "completed"):
        return _redirect("/?notice=" + urllib.parse.quote(
            "That event is already closed."))
    sent = any(True for _ in db.list_event_tokens(app.app_id, event_id))
    if evt.state == "poll" and not sent:
        db.delete_flexible_event(app.app_id, event_id)
        log.info("flexible event DELETED (unsent) app=%s event=%s by=%s",
                 app.app_id, event_id, user.user_id)
        msg = f"Deleted “{evt.title}”."
    else:
        evt.state = "cancelled"
        try:
            db.put_flexible_event(evt, expected_version=evt.version)
        except db.ConcurrencyConflict:
            return _error_redirect(f"/flex/event/results?event={event_id}",
                                   "Someone else just changed this — refresh.")
        log.info("flexible event CANCELLED app=%s event=%s by=%s",
                 app.app_id, event_id, user.user_id)
        msg = (f"Cancelled “{evt.title}”. Anyone who opens their "
               "link will see it's cancelled.")
    return _redirect("/?notice=" + urllib.parse.quote(msg))


# ---- public, passwordless token flow (no Cognito) -------------------------

def _resolve_token(token: str) -> EventToken | None:
    """Resolve + validate a raw magic-link token: must exist, match
    (constant-time), not be revoked, and not be expired. None otherwise.

    The returned token's ``event_id`` is the SURVIVING event: if the token was
    minted for an event that has since been merged into another, this follows
    the tombstone so an already-mailed link lands on the merged event. Every
    caller therefore reads and writes against the right event without knowing
    a merge happened — which is the point, since a vote written against a
    tombstone would vanish from the results page.

    Consequence: ``tok.event_id`` no longer necessarily identifies the token's
    own DDB row (SK is ``EVT#<minted-event>#TOK#<uid>``). Do not use it to
    key a write back to the token row. Nothing does today — opt-out revokes
    by user across the app, precisely so it doesn't need the minted id.
    """
    if not token:
        return None
    tok = db.get_event_token_by_value(token)
    if tok is None or not hmac.compare_digest(tok.token, token) or tok.revoked:
        return None
    try:
        if dt.datetime.fromisoformat(tok.expires_at) < \
                dt.datetime.now(dt.timezone.utc):
            return None
    except (ValueError, TypeError):
        pass
    tok.event_id = db.resolve_merged_event(tok.app_id, tok.event_id)
    return tok


def _flex_terminal_page(msg: str) -> dict:
    return _html(200, _page(
        f"<h1>Date and attendance poll</h1><p>{html.escape(msg)}</p>",
        title="Date and attendance poll"))


def _flex_token_page(event: dict) -> dict:
    raw = (event.get("rawPath") or event.get("path") or "")
    token = urllib.parse.unquote(raw[len("/e/"):]).strip("/")
    tok = _resolve_token(token)
    if tok is None:
        return _flex_terminal_page(
            "This link is no longer valid. Ask the organizer to resend it.")
    evt = db.get_flexible_event(tok.app_id, tok.event_id)
    if evt is None:
        return _flex_terminal_page("This event is no longer available.")
    if evt.state in ("cancelled", "completed"):
        return _flex_terminal_page(f"{evt.title} is {evt.state}.")
    community = db.get_community(tok.community_id)
    app = next((a for a in db.list_applications(tok.community_id)
                if a.app_id == tok.app_id), None)
    series = db.get_flexible_series_for_app(tok.app_id)
    user = db.get_user(tok.community_id, tok.user_id)
    rsvp = db.get_flexible_rsvp(tok.app_id, tok.event_id, tok.user_id)
    bring_prompt = (series.bring_prompt if series and series.bring_prompt
                    else "What are you bringing?")
    # Show whose link this is — important when a couple shares an inbox, so the
    # person knows which response they're entering.
    who_line = (
        "<p style='color:#666;margin-top:-6px'>Responding as "
        f"<b>{html.escape(user.name or user.email)}</b></p>"
        if user and (user.name or user.email) else "")

    # Other household members who already responded. Showing the next person
    # what the rest of their family picked — per date — lets them make an
    # informed, possibly different choice (e.g. not say Yes to a date a spouse
    # declined). Within a household this visibility is appropriate; the link's
    # household binding is the consent.
    household_rsvps: list[tuple[User, FlexibleRSVP]] = []
    if user and user.household_id:
        for r in db.list_flexible_rsvps(tok.app_id, tok.event_id):
            if r.user_id == tok.user_id:
                continue
            ru = db.get_user(tok.community_id, r.user_id)
            if ru and ru.household_id == user.household_id:
                household_rsvps.append((ru, r))

    def _hh_box(inner: str) -> str:
        return ("<div style='background:#fffbe6;border:1px solid #f0d674;"
                "padding:10px 14px;border-radius:6px;text-align:left;"
                "max-width:480px;margin:0 auto 14px'>"
                "<div style='font-weight:600'>Already answered in your "
                "household</div><div style='color:#777;font-size:0.85em;"
                "margin-bottom:6px'>Shown as guidance — you can still enter "
                "your own response below.</div>" + inner + "</div>")

    # Summary banner (used on the already-scheduled view, where per-date votes
    # are moot): name + overall response + party size + bringing.
    banner = ""
    if household_rsvps:
        def _summ(r: FlexibleRSVP) -> str:
            bits = []
            if r.confirmed_response:
                bits.append(_RESP_LABEL.get(r.confirmed_response,
                                            r.confirmed_response))
            if r.party_size:
                bits.append(f"{r.party_size} attending")
            if r.bringing:
                bits.append(f"bringing {r.bringing}")
            return " · ".join(bits)
        banner = _hh_box("".join(
            f"<div><b>{html.escape(ru.name or ru.email)}</b>"
            + (f": {html.escape(_summ(r))}" if _summ(r) else " replied")
            + "</div>" for ru, r in household_rsvps))

    def _hh_poll_detail(options) -> str:
        """Per-date breakdown of each household member's votes (poll view)."""
        if not household_rsvps:
            return ""
        labels = {o.option_id: (_fmt_iso_date(o.iso_date)
                  + (f" {_fmt_time(o.start_time)}" if o.start_time else ""))
                  for o in options}
        cards = ""
        for ru, r in household_rsvps:
            parts = [f"{html.escape(labels[o.option_id])}: "
                     f"<b>{_RESP_LABEL[r.votes[o.option_id]]}</b>"
                     for o in options if r.votes.get(o.option_id) in _RESP_LABEL]
            detail = "<br>".join(parts) if parts else (
                "declined all dates" if r.confirmed_response == "no"
                else "no dates marked yet")
            extra = []
            if r.party_size:
                extra.append(f"{r.party_size} attending")
            if r.bringing:
                extra.append("bringing " + html.escape(r.bringing))
            meta = (f"<div style='color:#555;font-size:0.88em;margin-top:2px'>"
                    + " · ".join(extra) + "</div>") if extra else ""
            cards += (f"<div style='margin:8px 0'>"
                      f"<b>{html.escape(ru.name or ru.email)}</b>"
                      f"<div style='font-size:0.92em;margin-top:2px'>{detail}"
                      f"</div>{meta}</div>")
        return _hh_box(cards)

    optout = (
        "<form method='post' action='/api/e/optout' style='margin-top:18px' "
        "onsubmit=\"return confirm('Stop ALL future emails about this group?')\">"
        f"<input type='hidden' name='token' value='{html.escape(token)}'>"
        "<button type='submit' style='background:none;border:none;color:#888;"
        "text-decoration:underline;cursor:pointer;font-size:0.85em;padding:0'>"
        "Stop emailing me about this group</button></form>")

    if evt.state == "scheduled":
        cur_bring = (rsvp.bringing if rsvp else "") or ""
        when = (_fmt_iso_date(evt.winning_date or "")
                + (f" {_fmt_time(evt.winning_start_time)}"
                   if evt.winning_start_time else ""))
        head = (
            f"<h1>{html.escape(evt.title)}</h1>" + who_line + banner
            + f"<p>It's set for <b>{html.escape(when)}</b>"
            + (f" at {html.escape(evt.location)}" if evt.location else "")
            + ".</p>")
        if rsvp and rsvp.confirmed_response == "no":
            # They declined during the poll — let them rejoin if plans changed.
            body = (head
                + "<p>You let us know you couldn't make it this time.</p>"
                "<p style='color:#2a7'><b>Changed your mind?</b> You can still "
                "join — we'll send you the calendar invite.</p>"
                "<form method='post' action='/api/e/join'>"
                f"<input type='hidden' name='token' value='{html.escape(token)}'>"
                "<button type='submit' style='padding:8px 22px;cursor:pointer;"
                "background:#2a7;color:#fff;border:none;border-radius:5px'>"
                "Yes, I can come</button></form>" + optout)
        else:
            body = (head
                + "<form method='post' action='/api/e/vote'>"
                f"<input type='hidden' name='token' value='{html.escape(token)}'>"
                f"<label>{html.escape(bring_prompt)}</label><br>"
                f"<input type='text' name='bringing' value='{html.escape(cur_bring)}' "
                "style='padding:6px;width:100%;max-width:420px'>"
                "<p><button type='submit' style='padding:7px 18px;cursor:pointer'>"
                "Save</button></p></form>" + optout)
        return _html(200, _page(body, title=evt.title))

    # poll view
    options = list(db.list_flexible_poll_options(tok.app_id, tok.event_id))
    votes = rsvp.votes if rsvp else {}
    rows = ""
    for o in options:
        label = (_fmt_iso_date(o.iso_date)
                 + (f" {_fmt_time(o.start_time)}" if o.start_time else "")
                 + (f" ({html.escape(o.label)})" if o.label else ""))
        cur = votes.get(o.option_id, "")
        radios = "".join(
            f"<label style='margin-right:12px'><input type='radio' "
            f"name='vote_{o.option_id}' value='{val}'"
            f"{' checked' if cur == val else ''}> {lbl}</label>"
            for val, lbl in (("yes", "Yes"), ("maybe", "Maybe"), ("no", "No")))
        rows += (f"<div style='margin:8px 0'><b>{html.escape(label)}</b><br>"
                 f"{radios}</div>")
    cur_party = str(rsvp.party_size) if rsvp and rsvp.party_size else ""
    cur_bring = (rsvp.bringing if rsvp else "") or ""
    # A single-date event is a fixed-date invitation, not a poll: Yes/Maybe/No
    # on the one date reads as an RSVP. Adapt the framing (heading, sub-line,
    # and the client-side confirms) so it doesn't talk about "dates" plural.
    single = len(options) == 1
    poll_heading = "Can you come?" if single else "Which dates work?"
    poll_subline = (
        "Let us know whether you can make it."
        if single else
        "Please mark <b>Yes</b>, <b>Maybe</b>, or <b>No</b> for <b>each</b> "
        "date below — not just one.")
    # \\u2019 = a JS-string-escaped apostrophe (this text is emitted inside a
    # single-quoted confirm('...') literal).
    js_incomplete = (
        "You haven\\u2019t chosen an answer. Please mark Yes, Maybe, or No. "
        "Submit anyway?"
        if single else
        "You haven\\u2019t answered every date. Please mark Yes, Maybe, or No "
        "for each one. Submit anyway?")
    js_allno = (
        "You\\u2019ve marked No, so you can\\u2019t make it \\u2014 and you "
        "won\\u2019t get further emails about this event. If that\\u2019s not "
        "right, choose Yes or Maybe. Continue?"
        if single else
        "You said no to all dates. You won\\u2019t get future emails about this "
        "event. To stay informed, mark Maybe on at least one. Continue?")
    body = (
        f"<h1>{html.escape(evt.title)}</h1>" + who_line + _hh_poll_detail(options)
        + (f"<p>{html.escape(evt.description)}</p>" if evt.description else "")
        + "<p style='color:#555'>Answer for your family or for yourself.</p>"
        "<form method='post' action='/api/e/vote' id='pollform'>"
        f"<input type='hidden' name='token' value='{html.escape(token)}'>"
        f"<h2 style='font-size:1.05em;margin-bottom:2px'>{poll_heading}</h2>"
        f"<p style='color:#555;margin:0 0 10px'>{poll_subline}</p>"
        f"{rows}"
        "<label style='display:block;margin:12px 0 4px'>How many from your "
        "household plan to attend?</label>"
        f"<input type='number' name='party_size' min='0' value='{cur_party}' "
        "style='padding:6px;width:90px;text-align:center'>"
        f"<label style='display:block;margin:12px 0 4px'>"
        f"{html.escape(bring_prompt)}</label>"
        f"<input type='text' name='bringing' value='{html.escape(cur_bring)}' "
        "style='padding:6px;width:100%;max-width:420px'>"
        "<p><button type='submit' style='padding:8px 22px;cursor:pointer;"
        "background:#2a7;color:#fff;border:none;border-radius:5px'>Submit"
        "</button></p></form>" + optout
        + "<script>var f=document.getElementById('pollform');"
        "if(f)f.addEventListener('submit',function(e){"
        "var groups={};f.querySelectorAll('input[type=radio]').forEach("
        "function(r){groups[r.name]=1;});"
        "var total=Object.keys(groups).length;"
        "var rs=f.querySelectorAll('input[type=radio]:checked');"
        f"if(rs.length<total){{if(!confirm('{js_incomplete}')){{"
        "e.preventDefault();return;}}}"
        "var ok=false;rs.forEach(function(r){if(r.value!=='no')ok=true;});"
        f"if(rs.length>0&&!ok){{if(!confirm('{js_allno}')){{"
        "e.preventDefault();}}}});</script>")
    return _html(200, _page(body, title=evt.title))


def _notify_app_admins(*, community_id: str, app_id: str, org_name: str,
                       subject: str, body_text: str, kind: str,
                       related_user_id: str | None = None,
                       users: dict[str, User] | None = None) -> None:
    """Email every AA of an app. Best-effort: swallows errors so a notify
    failure never breaks the member-facing action that triggered it."""
    try:
        if users is None:
            users = {u.user_id: u for u in db.list_users(community_id)}
        from community_organizer.providers.email import get_email_provider
        provider = get_email_provider()
        for m in db.list_memberships_for_app(app_id):
            if m.app_role != "aa":
                continue
            aa = users.get(m.user_id)
            if not aa or not aa.email or aa.email_undeliverable:
                continue
            provider.send(
                community_id=community_id, from_addr=_from_addr(None, org_name),
                to_addr=aa.email, subject=subject,
                body_text=body_text.replace("{aa_name}", aa.name or "there"),
                kind=kind, related_user_id=related_user_id,
                related_app_id=app_id)
    except Exception:
        log.exception("AA-notify failed app=%s kind=%s", app_id, kind)


def _api_flex_token_vote(event: dict) -> dict:
    if _http_method(event) != "POST":
        return _text(405, "POST required")
    parsed = _parse_form(event)
    token = (parsed.get("token", [""])[0] or "").strip()
    tok = _resolve_token(token)
    if tok is None:
        return _flex_terminal_page("This link is no longer valid.")
    evt = db.get_flexible_event(tok.app_id, tok.event_id)
    if evt is None or evt.state in ("cancelled", "completed"):
        return _flex_terminal_page(
            "This event is no longer accepting responses.")
    rsvp = db.get_flexible_rsvp(tok.app_id, tok.event_id, tok.user_id)
    if rsvp is None:
        rsvp = FlexibleRSVP(community_id=tok.community_id, app_id=tok.app_id,
                            event_id=tok.event_id, user_id=tok.user_id)
    if evt.state == "poll":
        votes: dict[str, str] = {}
        for o in db.list_flexible_poll_options(tok.app_id, tok.event_id):
            v = (parsed.get(f"vote_{o.option_id}", [""])[0] or "").strip()
            if v in ("yes", "no", "maybe"):
                votes[o.option_id] = v
        rsvp.votes = votes
        if votes and all(v == "no" for v in votes.values()):
            rsvp.confirmed_response = "no"          # all-No == decline
        elif any(v in ("yes", "maybe") for v in votes.values()):
            rsvp.confirmed_response = "yes"
    ps = (parsed.get("party_size", [""])[0] or "").strip()
    if ps != "":
        try:
            rsvp.party_size = max(0, int(ps))
        except ValueError:
            pass
    bringing = (parsed.get("bringing", [""])[0] or "").strip()
    rsvp.bringing = bringing or None
    rsvp.updated_at = dt.datetime.now(dt.timezone.utc).isoformat(
        timespec="seconds")
    try:
        db.put_flexible_rsvp(rsvp, expected_version=rsvp.version)
    except db.ConcurrencyConflict:
        return _flex_terminal_page(
            "Your response collided with another update — open your link "
            "again and re-save.")
    if evt.notify_on_response:
        member = db.get_user(tok.community_id, tok.user_id)
        app = next((a for a in db.list_applications(tok.community_id)
                    if a.app_id == tok.app_id), None)
        org_name = app.name if app else tok.app_id
        who = (member.name or member.email) if member else tok.user_id
        results_url = (f"https://{_public_domain(db.get_community(tok.community_id))}"
                       f"/flex/event/results?event={tok.event_id}")
        lines = []
        if evt.state == "poll":
            labels = {o.option_id: (_fmt_iso_date(o.iso_date)
                                    + (f" {_fmt_time(o.start_time)}"
                                       if o.start_time else ""))
                      for o in db.list_flexible_poll_options(
                          tok.app_id, tok.event_id)}
            for oid, v in rsvp.votes.items():
                lines.append(f"  {labels.get(oid, oid)}: {v.capitalize()}")
        if rsvp.party_size is not None:
            lines.append(f"  Headcount: {rsvp.party_size}")
        if rsvp.bringing:
            lines.append(f"  Bringing: {rsvp.bringing}")
        if rsvp.confirmed_response == "no":
            lines.append("  (declined — no to all dates)")
        detail = "\n".join(lines) if lines else "  (no details)"
        _notify_app_admins(
            community_id=tok.community_id, app_id=tok.app_id, org_name=org_name,
            subject=f"{org_name}: {who} responded to {evt.title}",
            body_text=(f"Hi {{aa_name}},\n\n{who} just responded to "
                       f"{evt.title}:\n\n{detail}\n\nSee all responses on the "
                       f"results page:\n\n  {results_url}\n\n"
                       "-- Community Organizer\n"),
            kind="event_response_notice", related_user_id=tok.user_id)
    if rsvp.confirmed_response == "no":
        return _flex_terminal_page(
            "Got it — you won't get further emails about this event. Open "
            "your link again any time if you change your mind.")
    return _flex_terminal_page(
        "Thanks! Your response is saved. Open your link again any time to "
        "update it.")


def _api_flex_token_optout(event: dict) -> dict:
    if _http_method(event) != "POST":
        return _text(405, "POST required")
    parsed = _parse_form(event)
    token = (parsed.get("token", [""])[0] or "").strip()
    tok = _resolve_token(token)
    if tok is None:
        return _flex_terminal_page("This link is no longer valid.")
    db.set_membership_opt_out(tok.app_id, tok.user_id, True)
    # Every link they hold, not just the one they clicked — opt-out is
    # group-level, and a member can hold links to several live events.
    db.revoke_event_tokens_for_user(tok.app_id, tok.user_id)
    app = next((a for a in db.list_applications(tok.community_id)
                if a.app_id == tok.app_id), None)
    member = db.get_user(tok.community_id, tok.user_id)
    org_name = app.name if app else tok.app_id
    who = (member.name or member.email) if member else tok.user_id
    _notify_app_admins(
        community_id=tok.community_id, app_id=tok.app_id, org_name=org_name,
        subject=f"{org_name}: {who} opted out of group emails",
        body_text=(f"Hi {{aa_name}},\n\n{who} asked to stop getting "
                   f"emails about {org_name}. You may want to follow "
                   "up with them personally.\n\n-- Community Organizer\n"),
        kind="event_optout_notice", related_user_id=tok.user_id)
    return _flex_terminal_page(
        "Done — you won't get any more emails about this group. (Your "
        "organizer has been let know, in case they'd like to follow up.)")


def _api_flex_token_join(event: dict) -> dict:
    """A member who declined during the poll changes their mind after the event
    is scheduled: flip them to attending and send the calendar invite."""
    if _http_method(event) != "POST":
        return _text(405, "POST required")
    parsed = _parse_form(event)
    token = (parsed.get("token", [""])[0] or "").strip()
    tok = _resolve_token(token)
    if tok is None:
        return _flex_terminal_page("This link is no longer valid.")
    evt = db.get_flexible_event(tok.app_id, tok.event_id)
    if evt is None or evt.state != "scheduled":
        return _flex_terminal_page("This event is no longer accepting changes.")
    user = db.get_user(tok.community_id, tok.user_id)
    rsvp = db.get_flexible_rsvp(tok.app_id, tok.event_id, tok.user_id)
    if rsvp is None:
        rsvp = FlexibleRSVP(community_id=tok.community_id, app_id=tok.app_id,
                            event_id=tok.event_id, user_id=tok.user_id)
    rsvp.confirmed_response = "yes"
    rsvp.updated_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    try:
        db.put_flexible_rsvp(rsvp, expected_version=rsvp.version)
    except db.ConcurrencyConflict:
        return _flex_terminal_page("Please open your link again and retry.")
    community = db.get_community(tok.community_id)
    app = next((a for a in db.list_applications(tok.community_id)
                if a.app_id == tok.app_id), None)
    series = db.get_flexible_series_for_app(tok.app_id)
    org_name = app.name if app else (community.name if community else "")
    loc = evt.location or (series.default_location if series else None)
    tz_name = ((app.default_timezone if app else None)
               or (community.default_timezone if community else None)
               or "America/New_York")
    date_label = (_fmt_iso_date(evt.winning_date or "")
                  + (f" {_fmt_time(evt.winning_start_time)}"
                     if evt.winning_start_time else ""))
    if user and user.email and not user.email_undeliverable:
        try:
            ics = ical.make_flexible_event_ics(
                event_id=tok.event_id, iso_date=evt.winning_date,
                start_time=evt.winning_start_time,
                duration_minutes=evt.winning_duration_minutes,
                summary=evt.title, user_id=user.user_id, user_email=user.email,
                domain=_public_domain(community), community_name=org_name,
                location=loc, bringing=rsvp.bringing, timezone=tz_name)
            from community_organizer.providers.email import get_email_provider
            body = (f"Hi {user.name},\n\nGlad you can make it!\n\n"
                    f"It's set: {evt.title} on {date_label}"
                    + (f" at {loc}" if loc else "") + ".\n\n"
                    "A calendar invite is attached.\n\n"
                    f"-- {org_name}\n")
            get_email_provider().send(
                community_id=tok.community_id, from_addr=_from_addr(None, org_name),
                to_addr=user.email, subject=f"{org_name}: see you at {evt.title}",
                body_text=body, kind="event_confirmed",
                related_user_id=user.user_id, related_app_id=tok.app_id,
                ics_content=ics)
        except Exception:
            log.exception("join .ics send failed app=%s user=%s",
                          tok.app_id, tok.user_id)
    # Always tell the AA(s): the event is already scheduled, so they're
    # likely no longer watching the results page when a No-voter rejoins.
    who = (user.name or user.email) if user else tok.user_id
    _notify_app_admins(
        community_id=tok.community_id, app_id=tok.app_id, org_name=org_name,
        subject=f"{org_name}: {who} can now make {evt.title}",
        body_text=(f"Hi {{aa_name}},\n\n{who} had said they couldn't make "
                   f"{evt.title}, but has changed their mind and is now "
                   f"coming on {date_label}"
                   + (f" at {loc}" if loc else "") + ".\n\n"
                   "They've been sent the calendar invite. Your headcount has "
                   "gone up by one.\n\n-- Community Organizer\n"),
        kind="event_response_notice", related_user_id=tok.user_id)
    return _flex_terminal_page(
        "Wonderful — you're in! We've sent you the calendar invite. Open this "
        "link again any time to say what you're bringing.")


_WEEKDAY_HEADER = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")


def _render_wall_calendar(*, month_first: dt.date,
                          items_by_date: dict[str, list[dict]],
                          today: dt.date) -> str:
    """Render a Sun-Sat month grid with event chips per cell.

    ``items_by_date`` keys are ISO date strings ("YYYY-MM-DD"); each
    value is a list of dicts shaped ``{"label": str, "time": str|None}``.
    The renderer treats this as the read-side data interface — callers
    can pull from StandingOccurrence, FlexibleEvent, or any future
    source as long as they hand back this shape.

    Why a generic shape vs. one fn per type: the design doc calls out
    the wall calendar as "built once". Keeping the renderer free of
    the source-data types means new event sources just have to flatten
    into this dict.

    Out-of-month days at the start/end of the grid render as muted
    placeholders so the grid stays a perfect 7-column rectangle (less
    visual jitter when scrolling between months).
    """
    import calendar as _cal
    year = month_first.year
    month = month_first.month
    last_dom = _cal.monthrange(year, month)[1]
    # Python's weekday(): Mon=0..Sun=6. Calendar starts Sunday so the
    # first cell's day-of-week is ((Mon=0) + 1) % 7.
    first_weekday = (month_first.weekday() + 1) % 7

    cells: list[str] = []
    # Leading filler from prev month
    prev_month_last = month_first - dt.timedelta(days=1)
    for i in range(first_weekday):
        d = prev_month_last - dt.timedelta(days=first_weekday - 1 - i)
        cells.append(_render_calendar_cell(date=d, in_month=False,
                                           items=[], today=today))
    # Days of this month
    for day in range(1, last_dom + 1):
        d = dt.date(year, month, day)
        items = items_by_date.get(d.isoformat(), [])
        cells.append(_render_calendar_cell(date=d, in_month=True,
                                           items=items, today=today))
    # Trailing filler to complete the final week
    while len(cells) % 7 != 0:
        next_idx = len(cells) - (first_weekday + last_dom) + 1
        d = (dt.date(year, month, last_dom) + dt.timedelta(days=next_idx))
        cells.append(_render_calendar_cell(date=d, in_month=False,
                                           items=[], today=today))

    header_row = "".join(
        f"<th style='padding:6px 4px;font-size:0.85em;color:#888;"
        "font-weight:600;text-align:center;border-bottom:1px solid #eee'>"
        f"{wd}</th>"
        for wd in _WEEKDAY_HEADER
    )
    week_rows = ""
    for week_start in range(0, len(cells), 7):
        week_rows += "<tr>" + "".join(cells[week_start:week_start + 7]) + "</tr>"

    month_label = f"{_MONTH_LABEL[month]} {year}"
    return (
        "<section style='margin-bottom:24px'>"
        f"<h2 style='font-size:1.05em;color:#444;margin:0 0 6px 0'>"
        f"{html.escape(month_label)}</h2>"
        "<table style='border-collapse:collapse;width:100%;"
        "table-layout:fixed'>"
        f"<thead><tr>{header_row}</tr></thead>"
        f"<tbody>{week_rows}</tbody></table>"
        "</section>"
    )


def _render_calendar_cell(*, date: dt.date, in_month: bool,
                          items: list[dict], today: dt.date) -> str:
    """One cell in the wall-calendar grid.

    Today gets a subtle green outline; out-of-month dates fade.
    Event chips are small green pills with the time prefix when one
    is set. Multiple events stack inside the cell.
    """
    bg = "#f9fdf9" if date == today else "transparent"
    border = "2px solid #2a7" if date == today else "1px solid #f0f0f0"
    day_color = "#bbb" if not in_month else ("#2a7" if date == today else "#444")
    chips_html = ""
    for it in items:
        time_prefix = (f"<span style='color:#888;font-weight:400'>"
                       f"{html.escape(_fmt_time(it['time']))} </span>"
                       if it.get("time") else "")
        cancelled = it.get("cancelled")
        chip_bg = "#f0f0f0" if cancelled else "#e8f4ec"
        chip_fg = "#999" if cancelled else "#1d5a36"
        label = it["label"] + (" (cancelled)" if cancelled else "")
        decoration = "line-through" if cancelled else "none"
        inner = (
            f"<div style='background:{chip_bg};color:{chip_fg};"
            "border-radius:3px;padding:2px 5px;margin-top:3px;"
            "font-size:0.8em;line-height:1.2;overflow:hidden;"
            f"white-space:nowrap;text-overflow:ellipsis;"
            f"text-decoration:{decoration}' "
            f"title='{html.escape(label)}'>"
            f"{time_prefix}{html.escape(label)}</div>"
        )
        # When the caller supplies an ``href`` (standing occurrences),
        # the chip opens the day's detail drawer; otherwise it's static.
        href = it.get("href")
        if href:
            chips_html += (f"<a href='{html.escape(href)}' "
                           "style='text-decoration:none;display:block'>"
                           f"{inner}</a>")
        else:
            chips_html += inner
    return (
        f"<td style='vertical-align:top;padding:4px 6px;background:{bg};"
        f"border:{border};height:84px;width:14.28%'>"
        f"<div style='font-size:0.85em;color:{day_color};"
        "font-weight:600'>"
        f"{date.day}</div>"
        f"{chips_html}"
        "</td>"
    )


_RECURRING_PAGE_WEEKS = 4    # legacy; the home page now paginates by month
_RECURRING_PAGE_MONTHS = 2   # always show the current month + next month


def _add_months(d: dt.date, months: int) -> dt.date:
    """Return ``d`` shifted by ``months`` calendar months, clamping the
    day to the destination month's length.

    Used by the recurring home pagination so "next month" advances
    cleanly across uneven month lengths (Jan 31 + 1 month → Feb 28/29).
    Equivalent to ``relativedelta(months=months)`` without the
    extra dependency.
    """
    import calendar as _cal
    total = d.month - 1 + months
    new_year = d.year + total // 12
    new_month = total % 12 + 1
    last_day = _cal.monthrange(new_year, new_month)[1]
    return dt.date(new_year, new_month, min(d.day, last_day))


def _ensure_period_materialized(community: Community | None,
                                app: Application, period_id: str) -> bool:
    """Idempotently materialize Slots + presumed Assignments for one
    period in a recurring_commitments app.

    Returns True if it actually materialized; False if it was already
    materialized (Schedule row exists) or the app type isn't
    recurring_commitments or there are no templates.

    The presumed-Assignment rule: for each slot whose template has a
    linked cohort, create an Assignment for every CURRENT cohort
    member. New cohort joins later only affect FUTURE periods (the
    join handler also fills in already-materialized periods — see
    _api_cohort_add_member).

    No-op for coverage apps — they still use the explicit
    "Create August Schedule" admin flow.
    """
    if app.app_type != "recurring_commitments":
        return False
    if community is None:
        return False
    if db.get_schedule(app.app_id, period_id) is not None:
        return False
    templates = list(db.list_templates(app.app_id))
    if not templates:
        return False
    tz = app.default_timezone or community.default_timezone or "UTC"
    slots = scheduling.materialize(
        community.community_id, app.app_id, period_id, tz, templates,
        period_type=app.period_type,
    )
    db.put_slots(slots)
    cohorts_by_template = {
        c.linked_template_id: c
        for c in db.list_cohorts(app.app_id)
        if c.linked_template_id
    }
    for slot in slots:
        cohort = cohorts_by_template.get(slot.template_id)
        if cohort is None:
            continue
        for cm in db.list_cohort_members(cohort.cohort_id):
            db.put_assignment(Assignment(
                community_id=community.community_id, app_id=app.app_id,
                yyyy_mm=period_id, slot_id=slot.slot_id,
                user_id=cm.user_id, local_date=slot.local_date,
                created_by="materialize",
            ))
    db.put_schedule(Schedule(
        community_id=community.community_id, app_id=app.app_id,
        yyyy_mm=period_id, state="materialized",
    ))
    log.info("lazy-materialized app %s period %s: %d slots",
             app.app_id, period_id, len(slots))
    return True


def _period_ids_for_window(start: dt.date, end_exclusive: dt.date,
                           period_type: str) -> list[str]:
    """Enumerate the period_ids whose partitions overlap [start, end).

    Lets the home page issue exactly the right partition queries
    regardless of whether the app stores slots monthly ("2026-05")
    or weekly ("2026-W22"). Returned in chronological order.
    """
    seen: dict[str, None] = {}    # dict preserves insertion order
    days = (end_exclusive - start).days
    for offset in range(days):
        d = start + dt.timedelta(days=offset)
        if period_type == "monthly":
            seen.setdefault(d.strftime("%Y-%m"), None)
        elif period_type == "weekly":
            iy, iw, _ = d.isocalendar()
            seen.setdefault(f"{iy:04d}-W{iw:02d}", None)
        else:
            raise ValueError(f"unknown period_type: {period_type!r}")
    return list(seen.keys())


def _recurring_home(user: User, community: Community | None,
                    app: Application, membership: Membership | None,
                    *, event: dict, org_name: str,
                    month_offset: int = 0) -> str:
    """Schedule-as-home for Recurring Commitments apps.

    Shows two calendar months anchored to month boundaries: the
    current month + the next month at month_offset=0. Each pagination
    click advances by ONE month so the visible "first" month becomes
    the next month and a fresh second month appears.

    Calendar-month anchoring (vs the previous 4-week rolling window)
    matches how people plan — "May 2026" is a stable label, not a
    sliding interval like "today through 28 days from now." Showing
    two months at a time means there's never a thin end-of-month
    sliver when you load the page late in a month.

    Storage stays per-ISO-week (Schedule rows are still keyed
    "YYYY-Www") — only the *view* unit changed.

    For each slot the user is assigned to: inline Withdraw + Trade
    buttons. For each slot regardless of assignment: an
    "Add/remove me from this slot's cohort" affordance — that's how
    members express "I'm willing to take this slot" for the
    Recurring Commitments use case.

    Navigation: ``← Previous month`` / ``Next month →``. Bounded
    forward by ``app.visible_horizon_months`` (default 6) and
    backward at the current month (no point looking at the past).
    """
    is_admin = _is_admin(user, membership)
    role_label = "App Admin" if is_admin else "Member"

    tz_name = (user.preferred_tz
               or (community.default_timezone if community else None)
               or app.default_timezone
               or "America/New_York")
    tz = ZoneInfo(tz_name)
    today = dt.datetime.now(tz).date()

    # Anchor to the first of the current month; advance by month_offset
    # months. Show 2 months in the window.
    this_month_first = today.replace(day=1)
    page_start = _add_months(this_month_first, month_offset)
    page_end_exclusive = _add_months(page_start, _RECURRING_PAGE_MONTHS)

    # Forward navigation cap: visible_horizon_months from today,
    # counted as calendar-month additions to the current month.
    horizon_months = app.visible_horizon_months or 6
    max_start = _add_months(this_month_first, horizon_months)
    can_next = page_start < max_start
    can_prev = month_offset > 0    # never scroll into the past

    # Collect slots in the page window. Slots are partitioned by
    # period_id ("2026-05" for monthly apps, "2026-W22" for weekly)
    # so enumerate every partition the window touches.
    periods_to_query = _period_ids_for_window(
        page_start, page_end_exclusive, app.period_type)
    # Materialize on-demand for any period that hasn't been seen.
    # This is what makes "templates ARE the schedule" — the admin
    # never has to click "Create Schedule" for a recurring app;
    # rendering the home page is the trigger.
    for pid in periods_to_query:
        _ensure_period_materialized(community, app, pid)
    # When showing the current month, clip past dates so a load on
    # May 27 doesn't surface May 1-26 as "now". For future months
    # (month_offset > 0) we show the whole month — those dates ARE
    # future at the time the user is browsing them.
    earliest_visible = max(page_start, today)
    slots: list[Slot] = []
    for pid in periods_to_query:
        for s in db.list_slots(app.app_id, pid):
            if s.cancelled:
                continue
            ld = dt.date.fromisoformat(s.local_date)
            if earliest_visible <= ld < page_end_exclusive:
                slots.append(s)
    slots.sort(key=lambda s: (s.local_date, s.start_time))

    # Index assignments per slot for the same window.
    asgns_by_slot: dict[str, list[Assignment]] = {}
    for pid in periods_to_query:
        for a in db.list_assignments_for_month(app.app_id, pid):
            asgns_by_slot.setdefault(a.slot_id, []).append(a)
    users_by_id = {u.user_id: u for u in db.list_users(user.community_id)}

    # User's current cohort memberships (so we can render the
    # right join/leave state per slot).
    my_cohort_ids = {cm.cohort_id for cm in db.list_cohorts_for_user(user.user_id)}
    # Find the cohort for each template_id present in this page.
    cohorts = {c.linked_template_id: c for c in db.list_cohorts(app.app_id)
               if c.linked_template_id}
    # ^ list_cohorts returns Cohort objs; mapping by linked_template_id
    # gives "this slot's cohort." If a template has no linked cohort
    # (unusual), the affordance just isn't rendered.

    # Admins also get a per-slot "+ Assign other…" picker — needs the
    # app members list to populate. Plain members and AAs alike see
    # only the self affordances.
    app_member_users: list[User] = []
    if is_admin:
        member_ids = {m.user_id for m in
                      db.list_memberships_for_app(app.app_id)}
        app_member_users = [u for u in users_by_id.values()
                            if u.user_id in member_ids]
    blocked_by_date = (_collect_blocked_by_date(app.app_id, slots)
                       if is_admin else None)
    cohort_members_by_template = (
        _collect_cohort_members_by_template(list(cohorts.values()))
        if is_admin else None)
    rows_html = _render_recurring_grid(
        slots=slots, asgns_by_slot=asgns_by_slot,
        users_by_id=users_by_id, current_user=user,
        cohorts_by_template_id=cohorts, my_cohort_ids=my_cohort_ids,
        is_admin=is_admin, app_members=app_member_users,
        blocked_by_date=blocked_by_date,
        cohort_members_by_template=cohort_members_by_template,
    )

    # Pagination links — one calendar month per step.
    prev_link = (
        f"<a href='/?month_offset={month_offset - 1}' "
        "style='color:#2a7'>&larr; Previous month</a>"
        if can_prev else
        "<span style='color:#bbb'>&larr; Previous month</span>"
    )
    next_link = (
        f"<a href='/?month_offset={month_offset + 1}' "
        "style='color:#2a7'>Next month &rarr;</a>"
        if can_next else
        "<span style='color:#bbb'>Next month &rarr;</span>"
    )

    if not slots:
        rows_html = (
            "<p style='color:#888;text-align:center;margin:32px 0'>"
            "No slots scheduled in this window.</p>"
        )

    # Admin nav lives at the bottom of every other admin page; bring
    # it here too so the recurring home isn't an island. The inline
    # admin_links section above used to be a quick-jump replacement,
    # but the user reported the inconsistency — the standard
    # _admin_nav_bar is the right answer.
    admin_links = (_admin_nav_bar("home", app=app)
                   if is_admin else "")

    # Calendar-month range label: "May 2026" if single-month, or
    # "May - June 2026" / "Dec 2025 - Jan 2026" for the two-month
    # view. Year only appears once when both months share it.
    second_month_first = _add_months(page_start, 1)
    if page_start.year == second_month_first.year:
        range_label = (
            f"{page_start.strftime('%B')} - "
            f"{second_month_first.strftime('%B %Y')}"
        )
    else:
        range_label = (
            f"{page_start.strftime('%b %Y')} - "
            f"{second_month_first.strftime('%b %Y')}"
        )

    body = (
        f"<h1>{html.escape(org_name)}</h1>"
        + _flash_banner_html(event)
        + f"<p style='color:#888;font-size:0.9em'>"
        f"Hello, {html.escape(user.name)} &middot; {html.escape(role_label)}"
        f"</p>"
        "<div style='display:flex;justify-content:space-between;"
        "align-items:center;margin-top:24px'>"
        f"<span>{prev_link}</span>"
        f"<span style='color:#444;font-weight:600'>{html.escape(range_label)}</span>"
        f"<span>{next_link}</span>"
        "</div>"
        + rows_html +
        "<div style='display:flex;justify-content:space-between;"
        "margin-top:16px'>"
        f"<span>{prev_link}</span>"
        f"<span>{next_link}</span>"
        "</div>"
        + admin_links +
        "<p style='margin-top:24px;text-align:center'>"
        "<a href='/settings'>Your cohort(s) and notification settings</a></p>"
        "<p style='margin-top:8px;text-align:center;font-size:0.85em'>"
        "<a href='/auth/logout' style='color:#888'>Sign out</a></p>"
    )
    return _page(body, narrow=False, title=org_name)


def _render_recurring_grid(*, slots: list[Slot],
                           asgns_by_slot: dict[str, list[Assignment]],
                           users_by_id: dict[str, User],
                           current_user: User,
                           cohorts_by_template_id: dict[str, "Cohort"],
                           my_cohort_ids: set[str],
                           is_admin: bool = False,
                           app_members: list[User] | None = None,
                           blocked_by_date: dict[str, set[str]] | None = None,
                           cohort_members_by_template: dict[str, set[str]] | None = None) -> str:
    """Render the slot grid for a recurring app.

    Personalization (per task #165 part 2): the viewer's own slots
    plus slots immediately adjacent (end time == this start, OR
    start time == this end) are rendered inline. Everything else
    on the same date is collapsed under a per-day ``<details>``
    with summary "+ N more slots" — click to expand.

    Adjacency is computed in UTC via ``concrete_date`` so a Wed
    11 PM → Thu 12 AM neighborhood works correctly across the
    midnight day-line.

    When ``is_admin`` is True and ``app_members`` is non-empty, each
    slot also gets an "Assign…" picker that lets the admin assign
    any app member to that slot (excluding those already assigned).
    The admin gets the same personalization default — the per-day
    chevron expands to the full list when they need it.
    """
    if not slots:
        return ""

    # Compute my slot IDs and their chronological neighbors (UTC).
    my_slot_ids = {
        s.slot_id for s in slots
        if any(a.user_id == current_user.user_id
               for a in asgns_by_slot.get(s.slot_id, []))
    }

    def _utc_range(s: Slot) -> tuple[dt.datetime, dt.datetime]:
        try:
            start = dt.datetime.fromisoformat(
                s.concrete_date.replace("Z", "+00:00"))
        except ValueError:
            # Should not happen — concrete_date is always ISO. Fall
            # back to a value that can't match anything else.
            return dt.datetime.min, dt.datetime.min
        return start, start + dt.timedelta(minutes=s.duration_minutes)

    times = {s.slot_id: _utc_range(s) for s in slots}
    adjacent_slot_ids: set[str] = set()
    for mine in (s for s in slots if s.slot_id in my_slot_ids):
        my_start, my_end = times[mine.slot_id]
        for s in slots:
            if s.slot_id == mine.slot_id:
                continue
            other_start, other_end = times[s.slot_id]
            if other_end == my_start or other_start == my_end:
                adjacent_slot_ids.add(s.slot_id)
    visible_slot_ids = my_slot_ids | adjacent_slot_ids

    def _render_row(slot: Slot) -> str:
        asgns = asgns_by_slot.get(slot.slot_id, [])
        rendered_names: list[str] = []
        for a in asgns:
            vol = users_by_id.get(a.user_id, _stub_user(a.user_id))
            rendered_names.append(_confirm_name_html(vol.name, a))
        i_am_on_it = any(a.user_id == current_user.user_id for a in asgns)
        my_assignment = next(
            (a for a in asgns if a.user_id == current_user.user_id), None)
        assignees_cell = (", ".join(rendered_names)
                          if rendered_names else
                          "<span style='color:#c80'>(open)</span>")

        actions: list[str] = []
        if i_am_on_it:
            actions.append(
                f"<a href='/ics/{slot.yyyy_mm}/{slot.slot_id}' "
                "style='color:#2a7;text-decoration:none' "
                "title='Download a single-event .ics for this slot'>"
                "&#128197; Add to calendar</a>"
            )
            actions.append(
                f"<a href='/swap/new?slot_id={slot.slot_id}"
                f"&month={slot.yyyy_mm}' "
                "style='color:#2a7;text-decoration:none'>Trade</a>"
            )
            actions.append(
                f"<button onclick=\"showReleaseModal('/api/assignments/release"
                f"?slot_id={slot.slot_id}&month={slot.yyyy_mm}')\" "
                "style='cursor:pointer;color:#c33;background:none;"
                "border:none;padding:0;text-decoration:underline;"
                "font-size:inherit'>Withdraw</button>"
            )
            if my_assignment and not my_assignment.confirmed_at:
                actions.append(
                    f"<form method='post' "
                    f"action='/api/assignments/confirm"
                    f"?slot_id={slot.slot_id}&month={slot.yyyy_mm}"
                    f"&next=/' style='display:inline'>"
                    "<button type='submit' style='cursor:pointer;"
                    "color:#2a7;background:none;border:none;padding:0;"
                    "text-decoration:underline;font-size:inherit'>"
                    "Confirm</button></form>"
                )
        else:
            # User isn't on this slot. Offer a one-off sign-up so they
            # can pick up a specific occurrence without committing to
            # the cohort. (Cohort opt-in is a separate affordance
            # below.) Uses the existing self-signup endpoint which
            # honors capacity + emails them the .ics.
            actions.append(
                f"<form method='post' action='/api/assignments/signup?"
                f"slot_id={slot.slot_id}&month={slot.yyyy_mm}' "
                "style='display:inline'>"
                "<button type='submit' style='cursor:pointer;"
                "color:#2a7;background:none;border:none;padding:0;"
                "font-size:inherit;text-decoration:underline'"
                " title='Sign up just for this occurrence'>"
                "+ Sign up</button></form>"
            )

        # Cohort affordance: only render if a cohort exists for this
        # slot's template. The cohorts_by_template_id map is keyed by
        # the cohort's linked_template_id, set at cohort creation.
        cohort = cohorts_by_template_id.get(slot.template_id)
        if cohort:
            if cohort.cohort_id in my_cohort_ids:
                actions.append(
                    f"<form method='post' action='/api/cohorts/remove-member?"
                    f"cohort_id={cohort.cohort_id}"
                    f"&user_id={current_user.user_id}&next=/' "
                    "style='display:inline'>"
                    "<button type='submit' style='cursor:pointer;"
                    "color:#2a7;background:none;border:none;padding:0;"
                    "font-size:inherit'>"
                    "&#10003; In cohort (leave)</button></form>"
                )
            else:
                actions.append(
                    f"<form method='post' action='/api/cohorts/add-member?"
                    f"cohort_id={cohort.cohort_id}"
                    f"&user_id={current_user.user_id}&next=/' "
                    "style='display:inline'"
                    f" onsubmit=\"return confirmSubmit(this,"
                    "'Commit to this slot every week? "
                    "You\\'ll get a recurring calendar invite, and "
                    "you can release individual weeks or leave the "
                    "cohort entirely anytime.',"
                    "'Commit','#2a7')\">"
                    "<button type='submit' style='cursor:pointer;"
                    "color:#2a7;background:none;border:none;padding:0;"
                    "font-size:inherit;text-decoration:underline'>"
                    "+ Take this slot weekly</button></form>"
                )

        # Admin-only: per-slot "Assign…" picker. Lets an AA / CA put
        # any app member on the slot without going to /admin/schedules.
        # Filters out users already assigned. onchange submits so the
        # admin only needs one click.
        if is_admin and app_members:
            assigned_uids = {a.user_id for a in asgns}
            # Three-section layout matching _drilldown_table:
            #   1) active cohort members (slot's natural picklist)
            #   2) "— others —" divider when both groups have entries
            #   3) active non-cohort members
            #   4) blocked members faded + disabled at the bottom
            available = sorted(
                [u for u in app_members if u.user_id not in assigned_uids],
                key=lambda u: u.name.lower(),
            )
            if available:
                blocked = (blocked_by_date or {}).get(slot.local_date, set())
                cohort_for_slot = (cohort_members_by_template or {}).get(
                    slot.template_id) or set()
                cohort_active = [u for u in available
                                 if u.user_id in cohort_for_slot
                                 and u.user_id not in blocked]
                others_active = [u for u in available
                                 if u.user_id not in cohort_for_slot
                                 and u.user_id not in blocked]
                blocked_entries = [u for u in available
                                   if u.user_id in blocked]
                parts: list[str] = []
                for u in cohort_active:
                    parts.append(
                        f"<option value='{u.user_id}'>"
                        f"{html.escape(u.name)}</option>")
                if cohort_active and others_active:
                    parts.append(
                        "<option value='' disabled "
                        "style='color:#aaa;font-style:italic'>"
                        "&mdash; others &mdash;</option>")
                for u in others_active:
                    parts.append(
                        f"<option value='{u.user_id}'>"
                        f"{html.escape(u.name)}</option>")
                for u in blocked_entries:
                    parts.append(
                        f"<option value='{u.user_id}' disabled "
                        f"style='color:#aaa;font-style:italic'>"
                        f"{html.escape(u.name)} (blocked)</option>")
                opts = "".join(parts)
                actions.append(
                    "<form method='post' "
                    f"action='/api/admin/assign?slot_id={slot.slot_id}"
                    f"&month={slot.yyyy_mm}&next=/' "
                    "style='display:inline'>"
                    "<select name='user_id' "
                    "onchange=\"if(this.value){"
                    "document.getElementById('loading').style.display='flex';"
                    "this.form.submit()}\" "
                    "style='font-size:0.85em;padding:1px;color:#444'>"
                    "<option value=''>+ Assign other…</option>"
                    f"{opts}"
                    "</select></form>"
                )

        actions_cell = (
            " &middot; ".join(actions)
            if actions else
            "<span style='color:#ccc'>&mdash;</span>"
        )

        return (
            "<tr style='border-bottom:1px solid #f0f0f0'>"
            f"<td style='padding:6px 8px;width:90px;color:#666'>"
            f"{html.escape(_fmt_time(slot.start_time))}</td>"
            f"<td style='padding:6px 8px;font-weight:600'>"
            f"{html.escape(slot.name)}</td>"
            f"<td style='padding:6px 8px'>{assignees_cell}</td>"
            f"<td style='padding:6px 8px;font-size:0.9em'>{actions_cell}</td>"
            "</tr>"
        )

    # Group slots by date and render visible / hidden per day.
    pieces: list[str] = []
    by_date: dict[str, list[Slot]] = {}
    for s in slots:
        by_date.setdefault(s.local_date, []).append(s)
    for date in sorted(by_date.keys()):
        day_slots = by_date[date]
        visible = [s for s in day_slots if s.slot_id in visible_slot_ids]
        hidden = [s for s in day_slots if s.slot_id not in visible_slot_ids]
        day_label = _pretty_date(date)
        pieces.append(
            "<h2 style='font-size:1.05em;color:#444;margin-top:24px;"
            f"border-bottom:1px solid #eee;padding-bottom:4px'>"
            f"{html.escape(day_label)}</h2>"
        )
        if visible:
            pieces.append(
                "<table style='border-collapse:collapse;width:100%;"
                "font-size:0.95em'>"
            )
            for s in visible:
                pieces.append(_render_row(s))
            pieces.append("</table>")
        if hidden:
            n = len(hidden)
            # Closed by default. Expanded markup is the same as the
            # visible table so the affordances inside still work.
            pieces.append(
                "<details style='margin-top:6px'>"
                "<summary style='cursor:pointer;color:#888;"
                "font-size:0.9em;padding:4px 0'>"
                f"+ {n} more slot{'s' if n != 1 else ''}</summary>"
                "<table style='border-collapse:collapse;width:100%;"
                "font-size:0.95em;margin-top:4px'>"
            )
            for s in hidden:
                pieces.append(_render_row(s))
            pieces.append("</table></details>")
    return "".join(pieces)


def _stub_user(user_id: str) -> User:
    """Fallback when an assignment references a user_id we can't find."""
    return User(community_id="?", email="?", name="(unknown)")


def _schedules_all_page(event: dict, user: User, community: Community | None,
                        app: Application, membership: Membership | None) -> dict:
    org_name = app.name if app else (community.name if community else user.community_id)
    if not _is_admin(user, membership):
        return _html(403, _page(
            f"<h1>{html.escape(org_name)}</h1><p>Admins only.</p>"
            "<p><a href='/'>Back</a></p>", title=org_name))
    schedules = list(db.list_schedules(app.app_id))
    # Active = published (archived is a distinct state, so it's already
    # excluded here). Archived months are admin-declared history: kept fully
    # intact, surfaced only behind the "Past schedules" expander below.
    active = sorted([s for s in schedules if s.state == "published"],
                    key=lambda s: s.yyyy_mm)
    archived = sorted([s for s in schedules if s.state == "archived"],
                      key=lambda s: s.yyyy_mm, reverse=True)
    if not active and not archived:
        body = (
            f"<h1>{html.escape(org_name)}</h1>"
            "<h2 style='font-size:1.1em;color:#444'>Active schedules</h2>"
            + _flash_banner_html(event)
            + "<p style='color:#888'>No active schedules yet.</p>"
            + _admin_nav_bar("schedules", app=app)
        )
        return _html(200, _page(body, narrow=False, title=org_name))

    users_by_id = {u.user_id: u for u in db.list_users(user.community_id)}
    members = {m.user_id for m in db.list_memberships_for_app(app.app_id)}
    all_cohorts = list(db.list_cohorts(app.app_id))
    cohort_members_by_template = _collect_cohort_members_by_template(all_cohorts)
    body = (
        f"<h1>{html.escape(org_name)}</h1>"
        "<h2 style='font-size:1.1em;color:#444'>Active schedules</h2>"
        + _flash_banner_html(event)
    )
    if not active:
        body += ("<p style='color:#888'>No active schedules. "
                 "See <b>Past schedules</b> below.</p>")
    # NOTE: thread-parallelizing these per-schedule reads was tried and made
    # this page SLOWER (2026-07-01): the queries return large result sets and
    # botocore's per-item deserialization is CPU-bound, so it serializes on
    # the GIL regardless of pool size. The real win is fewer/lighter round
    # trips (or lazy-loading each month's detail), not concurrency.
    for sch in active:
        slots = sorted(db.list_slots(app.app_id, sch.yyyy_mm),
                       key=lambda s: (s.local_date, s.start_time))
        asgns_by_slot: dict[str, list[Assignment]] = {}
        for a in db.list_assignments_for_month(app.app_id, sch.yyyy_mm):
            asgns_by_slot.setdefault(a.slot_id, []).append(a)
        blocked_by_date = _collect_blocked_by_date(app.app_id, slots)
        body += (
            f"<h3 style='margin-top:24px;color:#555'>"
            f"<a href='/schedules/{sch.yyyy_mm}' style='color:#2a7'>"
            f"{_month_label(sch.yyyy_mm)}</a></h3>"
        )
        body += _drilldown_table(slots, asgns_by_slot, users_by_id,
                                 yyyy_mm=sch.yyyy_mm, member_ids=members,
                                 cohorts=all_cohorts,
                                 blocked_by_date=blocked_by_date,
                                 cohort_members_by_template=cohort_members_by_template)
    if archived:
        links = "".join(
            f"<li style='padding:3px 0'>"
            f"<a href='/schedules/{s.yyyy_mm}' style='color:#2a7'>"
            f"{_month_label(s.yyyy_mm)}</a></li>"
            for s in archived)
        body += (
            "<details style='margin-top:28px'>"
            "<summary style='cursor:pointer;color:#888'>"
            f"Past schedules ({len(archived)})</summary>"
            "<p style='color:#999;font-size:0.85em;margin:6px 0'>Moved to "
            "history by an admin. Still viewable; reminders and calendar "
            "invites are unaffected. Open one to reactivate it.</p>"
            f"<ul style='padding-left:18px'>{links}</ul></details>")
    body += _admin_nav_bar("schedules", app=app)
    return _html(200, _page(body, narrow=False, title=org_name))


def _schedule_drilldown(user: User, community: Community | None,
                        app: Application, membership: Membership | None,
                        yyyy_mm: str, *, event: dict | None = None) -> dict:
    org_name = app.name if app else (community.name if community else user.community_id)
    if not _is_admin(user, membership):
        return _html(403, _page(
            f"<h1>{html.escape(org_name)}</h1><p>Admins only.</p>"
            "<p><a href='/'>Back</a></p>",
            title=org_name,
        ))
    sch = db.get_schedule(app.app_id, yyyy_mm)
    if sch is None:
        return _html(404, _page(
            f"<h1>{html.escape(org_name)}</h1>"
            f"<p>No schedule for {html.escape(yyyy_mm)}.</p>"
            "<p><a href='/'>Back</a></p>",
            title=org_name,
        ))
    slots = sorted(db.list_slots(app.app_id, yyyy_mm),
                   key=lambda s: (s.local_date, s.start_time))
    users_by_id = {u.user_id: u for u in db.list_users(user.community_id)}
    members = {m.user_id for m in db.list_memberships_for_app(app.app_id)}
    all_cohorts = list(db.list_cohorts(app.app_id))
    cohort_members_by_template = _collect_cohort_members_by_template(all_cohorts)
    asgns_by_slot: dict[str, list[Assignment]] = {}
    for a in db.list_assignments_for_month(app.app_id, yyyy_mm):
        asgns_by_slot.setdefault(a.slot_id, []).append(a)

    body = (
        f"<h1>{html.escape(org_name)}</h1>"
        + (_flash_banner_html(event) if event is not None else "")
        + f"<p style='color:#888'>"
        f"{html.escape(yyyy_mm)} "
        f"<span style='color:{_STATE_COLORS.get(sch.state, '#888')}'>"
        f"({html.escape(_state_label(sch.state))})</span></p>"
    )
    blocked_by_date = _collect_blocked_by_date(app.app_id, slots)
    body += _drilldown_table(slots, asgns_by_slot, users_by_id,
                             yyyy_mm=yyyy_mm, member_ids=members,
                             cohorts=all_cohorts,
                             blocked_by_date=blocked_by_date,
                             cohort_members_by_template=cohort_members_by_template,
                             is_draft=(sch.state == "draft"))
    event_noun = app.event_noun or "event"
    body += (
        "<details style='margin-top:16px'>"
        "<summary style='cursor:pointer;color:#666;font-size:0.9em'>"
        f"Add one-off {html.escape(event_noun)} to this schedule...</summary>"
        f"<form method='post' action='/api/slots/add?month={yyyy_mm}' "
        "style='margin:8px 0;display:flex;gap:8px;align-items:end;flex-wrap:wrap'>"
        "<label style='display:flex;flex-direction:column;font-size:0.85em;color:#666'>"
        f"Name (optional)<input name='name' placeholder='e.g. Christmas Day Mass' "
        "style='padding:4px;width:180px'></label>"
        "<label style='display:flex;flex-direction:column;font-size:0.85em;color:#666'>"
        "Date<input type='date' name='date' required value='2026-12-25' "
        "style='padding:4px'></label>"
        "<label style='display:flex;flex-direction:column;font-size:0.85em;color:#666'>"
        "Start<input type='time' name='start' required value='22:00' "
        "style='padding:4px'></label>"
        "<label style='display:flex;flex-direction:column;font-size:0.85em;color:#666'>"
        "Duration (min)<input type='number' name='duration' value='60' min='1' "
        "style='padding:4px;width:60px'></label>"
        "<label style='display:flex;flex-direction:column;font-size:0.85em;color:#666'>"
        "Arrive early (min)<input type='number' name='arrival' value='10' min='0' "
        "style='padding:4px;width:60px'></label>"
        "<label style='display:flex;flex-direction:column;font-size:0.85em;color:#666'>"
        "Required<input type='number' name='required' value='2' min='1' "
        "style='padding:4px;width:50px'></label>"
        "<label style='display:flex;flex-direction:column;font-size:0.85em;color:#666'>"
        "Minimum<input type='number' name='min_vol' value='1' min='1' "
        "style='padding:4px;width:50px'></label>"
        "<button type='submit' style='padding:4px 12px;cursor:pointer'>"
        "Add</button></form></details>"
    )
    has_assignments = any(asgns_by_slot.values())
    if sch.state == "draft":
        prev_months = sorted(
            [s.yyyy_mm for s in db.list_schedules(app.app_id)
             if s.yyyy_mm < yyyy_mm],
            reverse=True,
        )
        if prev_months:
            src = prev_months[0]
            confirm_js = ""
            if has_assignments:
                confirm_js = (" onsubmit=\"return confirmSubmit(this,"
                              "'This will replace all current assignments "
                              "with copied ones. Continue?','Copy','#c33')\"")
            body += (
                "<div style='margin-top:16px'>"
                f"<form method='post' action='/api/schedules/copy-from"
                f"?month={yyyy_mm}&source={src}' style='display:inline'"
                f"{confirm_js}>"
                "<button type='submit' style='cursor:pointer;font-size:0.95em;"
                "color:#2a7;background:none;border:none;"
                f"text-decoration:underline;padding:0'>"
                f"Copy assignments from {_month_label(src)}"
                "</button></form></div>"
            )
        body += (
            "<div style='margin-top:24px'>"
            f"<form method='post' action='/api/schedules/publish?month={yyyy_mm}'"
            " style='display:inline'>"
            "<button type='submit' style='padding:8px 24px;cursor:pointer;"
            "font-size:1em;color:white;background:#2a7;border:none;"
            "border-radius:4px'>Make active</button></form>"
            "</div>"
        )
    elif sch.state == "published":
        body += (
            "<div style='margin-top:24px;display:flex;gap:10px;flex-wrap:wrap'>"
            # Archive (move to history): admin-declared age-out. Non-destructive
            # — nothing is cancelled, reminders + .ics keep working; it just
            # leaves the default screens. Distinct from Unpublish.
            f"<form method='post' action='/api/schedules/archive?month={yyyy_mm}'"
            " style='display:inline'"
            " onsubmit=\"return confirmSubmit(this,"
            f"'Move the {_month_label(yyyy_mm)} schedule to history? It stays "
            "viewable under Past schedules and any calendar invites keep "
            "working \\u2014 it just won\\u2019t appear in the default schedule "
            "and email screens until you reactivate it.',"
            "'Move to history','#2a7')\">"
            "<button type='submit' style='padding:4px 12px;cursor:pointer;"
            "font-size:0.9em;color:#2a7;background:none;border:1px solid #2a7;"
            "border-radius:4px'>Archive (move to history)</button></form>"
            f"<form method='post' action='/api/schedules/unpublish?month={yyyy_mm}'"
            " style='display:inline'"
            " onsubmit=\"return confirmSubmit(this,"
            "'Return this schedule to draft? Members will no longer see it and any reminder emails not yet sent will be cancelled. Assignments are preserved.',"
            "'Return to draft','#a80')\">"
            "<button type='submit' style='padding:4px 12px;cursor:pointer;"
            "font-size:0.9em;color:#a80;background:none;border:1px solid #a80;"
            "border-radius:4px'>Return to draft</button></form>"
            "</div>"
        )
    elif sch.state == "archived":
        body += (
            "<div style='margin-top:24px'>"
            "<p style='color:#888;font-size:0.9em;margin:0 0 8px'>This schedule "
            "is in <b>history</b> &mdash; hidden from the default screens and "
            "Send-Email audience, but still live (reminders and calendar "
            "invites are unaffected).</p>"
            f"<form method='post' action='/api/schedules/reactivate?month={yyyy_mm}'"
            " style='display:inline'>"
            "<button type='submit' style='padding:4px 12px;cursor:pointer;"
            "font-size:0.9em;color:#2a7;background:none;border:1px solid #2a7;"
            "border-radius:4px'>Reactivate</button></form>"
            "</div>"
        )
    elif sch.state == "publishing":
        # In-flight publish lock. Show a status banner; do NOT show
        # Publish or Unpublish buttons — both would race with the
        # publish that's currently running on another invocation.
        # If this schedule sits in publishing for a long time, the
        # publish handler likely crashed; the admin recovery path
        # (CLI ``schedules force-reset``) flips it back to draft.
        started = html.escape(sch.published_at or "?")
        body += (
            "<div style='margin-top:24px;padding:12px;"
            "background:#fff8e7;border:1px solid #c80;border-radius:4px'>"
            f"<b style='color:#c80'>Activation in progress</b><br>"
            f"<span style='color:#666;font-size:0.9em'>Started at {started}. "
            "Members will see the schedule once it finishes activating. "
            "If this banner persists, the activation may have crashed "
            "— ask a Community Admin to run "
            "<code>community-organizer schedules force-reset</code> "
            f"for {html.escape(yyyy_mm)}.</span>"
            "</div>"
        )
    body += (
        "<div style='margin-top:24px;border-top:1px solid #eee;padding-top:16px'>"
        f"<form method='post' action='/api/schedules/send-summary?month={yyyy_mm}' "
        "style='display:flex;gap:8px;align-items:center;flex-wrap:wrap'>"
        "<span style='font-size:0.9em;color:#666'>"
        "Email schedule table to (semicolon-separate for multiple):</span>"
        f"<input type='text' name='to' value='{html.escape(user.email)}' "
        "style='padding:4px;width:360px;font-size:0.9em'>"
        "<button type='submit' style='padding:4px 16px;cursor:pointer;"
        "font-size:0.9em'>Send</button>"
        "</form></div>"
    )
    body += _admin_nav_bar("schedules", app=app)
    return _html(200, _page(body, narrow=False, title=org_name,
                            ))


_MONTH_LABEL = {1: "January", 2: "February", 3: "March", 4: "April",
                5: "May", 6: "June", 7: "July", 8: "August",
                9: "September", 10: "October", 11: "November", 12: "December"}


def _pretty_date(iso_date: str) -> str:
    y, mo, d = (int(x) for x in iso_date.split("-"))
    date = dt.date(y, mo, d)
    return f"{_DAY_LABEL[date.weekday()]}, {_MONTH_LABEL[mo]} {d}"


def _collect_blocked_by_date(app_id: str,
                             slots: list[Slot]) -> dict[str, set[str]]:
    """Look up blocked users for every distinct slot date in one place.

    One round-trip per distinct date — usually a handful per month, so
    cheaper than per-(slot, member) checks at render time. Returns an
    empty dict when slots is empty.
    """
    out: dict[str, set[str]] = {}
    for date in {s.local_date for s in slots}:
        out[date] = db.list_blocked_users_on_date(app_id, date)
    return out


def _collect_cohort_members_by_template(
        cohorts: list,
) -> dict[str, set[str]]:
    """Map template_id -> set of cohort-member user_ids for the picker.

    A slot's per-slot Assign dropdown is restricted to the cohort
    linked to the slot's template. If a template has no linked
    cohort the picker falls back to all app members (caller
    decides by checking for absence of the template_id).
    """
    out: dict[str, set[str]] = {}
    for c in (cohorts or []):
        tpl = getattr(c, "linked_template_id", None)
        if not tpl:
            continue
        out[tpl] = {cm.user_id for cm in db.list_cohort_members(c.cohort_id)}
    return out


_CONFIRMED_VIA_LABEL = {
    "self_signup": "signed up themselves",
    "member_login": "confirmed in app",
    "ical_reply": "accepted calendar invite",
    "admin_override": "marked by admin",
}


def _confirm_name_html(name: str, asg: Assignment | None) -> str:
    """Render an assignee's name with the confirmation indicator.

    Confirmed: small green ✓ prefix, normal-weight black name.
    Unconfirmed: name in muted #555, no prefix.
    The ✓ has a title= tooltip showing date + via for hover info.

    The confirmation display design: clear but not loud. Two reinforcing signals
    (presence/absence of ✓ + color firmness) give "mostly confirmed"
    vs "mostly soft" at a glance when scanning a column of names.
    """
    safe_name = html.escape(name)
    if asg is None or not asg.confirmed_at:
        return f"<span style='color:#555'>{safe_name}</span>"
    via_label = _CONFIRMED_VIA_LABEL.get(asg.confirmed_via or "", "confirmed")
    tooltip = (f"Confirmed {asg.confirmed_at[:10]} ({via_label})"
               if asg.confirmed_at else "Confirmed")
    return (f"<span title='{html.escape(tooltip)}' "
            f"style='color:#2a7;font-weight:600;margin-right:2px'>&check;</span>"
            f"{safe_name}")


def _drilldown_table(slots: list[Slot],
                     asgns_by_slot: dict[str, list[Assignment]],
                     users_by_id: dict[str, User], *,
                     yyyy_mm: str = "",
                     member_ids: set[str] | None = None,
                     cohorts: list | None = None,
                     blocked_by_date: dict[str, set[str]] | None = None,
                     cohort_members_by_template: dict[str, set[str]] | None = None,
                     is_draft: bool = False) -> str:
    if not slots:
        return "<p style='color:#888'>No slots in this month.</p>"
    editable = yyyy_mm and member_ids is not None
    rows: list[str] = []
    current_date: str | None = None
    for s in slots:
        if s.local_date != current_date:
            current_date = s.local_date
            cols = "6" if editable else "4"
            rows.append(
                "<tr style='background:#f5f5f5'>"
                f"<td colspan='{cols}' style='padding:8px 12px;font-weight:600;"
                "color:#444;border-top:1px solid #ddd'>"
                f"{html.escape(_pretty_date(s.local_date))}"
                "</td></tr>"
            )
        if s.cancelled:
            cancel_action = (
                f"<form method='post' action='/api/slots/cancel"
                f"?month={yyyy_mm}&slot_id={s.slot_id}&cancel=0'"
                " style='display:inline'>"
                "<button type='submit' style='font-size:0.8em;cursor:pointer;"
                "color:#2a7;background:none;border:none;text-decoration:underline;"
                "padding:0'>restore</button></form>"
            ) if editable else ""
            rows.append(
                "<tr style='opacity:0.5'>"
                f"<td style='padding:6px 12px;padding-left:32px;color:#999;"
                f"white-space:nowrap;text-decoration:line-through'>"
                f"{_fmt_time(s.start_time)}</td>"
                f"<td style='padding:6px 12px;color:#999;"
                f"text-decoration:line-through'>{html.escape(s.name)}</td>"
                "<td style='padding:6px 12px;color:#999'>cancelled</td>"
                f"<td style='padding:6px 12px'>{cancel_action}</td>"
                + (f"<td></td><td></td>" if editable else "")
                + "</tr>"
            )
            continue
        asgns = asgns_by_slot.get(s.slot_id, [])
        filled = len(asgns)
        full = _slot_full(s, filled)
        assigned_ids = {a.user_id for a in asgns}
        count_color = "#c33" if filled < s.min_volunteers else (
            "#2a7" if full else "#a80")
        name_parts = []
        for a in asgns:
            vol = users_by_id.get(a.user_id, _stub_user(a.user_id))
            if editable:
                confirm_action = ""
                if not a.confirmed_at:
                    # text link is clearer than a bare
                    # green check (which read as a non-clickable status
                    # indicator next to the confirmed-prefix glyph).
                    confirm_action = (
                        f" <form method='post' "
                        f"action='/api/admin/confirm-assignment"
                        f"?month={yyyy_mm}&slot_id={s.slot_id}"
                        f"&user_id={a.user_id}' style='display:inline'>"
                        "<button type='submit' title='Confirm this "
                        "assignment as confirmed (member told you "
                        "separately)' style='font-size:0.75em;"
                        "cursor:pointer;color:#2a7;background:none;"
                        "border:none;text-decoration:underline;"
                        "padding:0;margin-left:4px'>confirm</button>"
                        "</form>"
                    )
                name_parts.append(
                    f"{_confirm_name_html(vol.name, a)} "
                    f"<form method='post' action='/api/admin/unassign"
                    f"?month={yyyy_mm}&slot_id={s.slot_id}&user_id={a.user_id}'"
                    " style='display:inline'>"
                    "<button type='submit' style='font-size:0.75em;"
                    "cursor:pointer;color:#c33;background:none;border:none;"
                    "text-decoration:underline;padding:0;margin-left:4px'>"
                    "remove</button></form>"
                    + confirm_action
                )
            else:
                name_parts.append(_confirm_name_html(vol.name, a))
        names_html = ", ".join(name_parts) or "<span style='color:#aaa'>--</span>"
        assign_cell = ""
        if editable and not full:
            # All app members are eligible. Order matters:
            #   1) active cohort members  (slot's natural picklist)
            #   2) "— others —" divider (only if a non-empty cohort
            #      exists AND there are non-cohort members to show)
            #   3) active non-cohort members  (for the rare case where
            #      an admin needs to pick someone outside the cohort)
            #   4) blocked members faded + disabled (regardless of
            #      cohort affinity)
            # This three-section layout is cohort-first for the common case,
            # with other members still reachable for the off-pattern case.
            available_all = sorted(
                [(uid, users_by_id[uid].name) for uid in member_ids
                 if uid in users_by_id and uid not in assigned_ids],
                key=lambda x: x[1],
            )
            if available_all:
                blocked = (blocked_by_date or {}).get(s.local_date, set())
                cohort_for_slot = (cohort_members_by_template or {}).get(
                    s.template_id) or set()
                cohort_active = [(uid, n) for uid, n in available_all
                                 if uid in cohort_for_slot and uid not in blocked]
                others_active = [(uid, n) for uid, n in available_all
                                 if uid not in cohort_for_slot and uid not in blocked]
                blocked_entries = [(uid, n) for uid, n in available_all
                                   if uid in blocked]
                parts: list[str] = []
                for uid, name in cohort_active:
                    parts.append(
                        f"<option value='{uid}'>{html.escape(name)}</option>")
                if cohort_active and others_active:
                    parts.append(
                        "<option value='' disabled "
                        "style='color:#aaa;font-style:italic'>"
                        "&mdash; others &mdash;</option>")
                for uid, name in others_active:
                    parts.append(
                        f"<option value='{uid}'>{html.escape(name)}</option>")
                for uid, name in blocked_entries:
                    parts.append(
                        f"<option value='{uid}' disabled "
                        f"style='color:#aaa;font-style:italic'>"
                        f"{html.escape(name)} (blocked)</option>")
                opts = "".join(parts)
                available = available_all  # preserve original variable for the `if available:` guard below
                # bulk-add shortcuts make sense only
                # while drafting a schedule, and only for the slot's
                # OWN cohort. The "all" mode and other-cohort buttons
                # were tried, weren't useful, and added clutter. Once
                # a schedule is published the dropdown alone is the
                # remaining affordance.
                bulk_btns = ""
                if is_draft and cohorts:
                    matching_cohort = next(
                        (c for c in cohorts
                         if getattr(c, "linked_template_id", None)
                         == s.template_id),
                        None,
                    )
                    if matching_cohort is not None:
                        bulk_btns = (
                            f"<form method='post' "
                            f"action='/api/admin/bulk-assign"
                            f"?month={yyyy_mm}&slot_id={s.slot_id}"
                            f"&bulk_mode=cohort:{matching_cohort.cohort_id}'"
                            " style='display:inline'>"
                            "<button type='submit' style='font-size:0.75em;"
                            "cursor:pointer;color:#2a7;background:none;border:none;"
                            "text-decoration:underline;padding:0;margin-left:6px'>"
                            "Add entire cohort</button></form>"
                        )
                assign_cell = (
                    f"<form method='post' action='/api/admin/assign"
                    f"?month={yyyy_mm}&slot_id={s.slot_id}'"
                    " style='display:inline;white-space:nowrap'>"
                    "<select name='user_id' style='font-size:0.85em;padding:2px'"
                    " onchange=\"if(this.value){document.getElementById('loading').style.display='flex';this.form.submit()}\">"
                    f"<option value=''>+ add</option>{opts}</select></form>"
                    + bulk_btns
                )
        slot_actions = ""
        if editable:
            slot_actions = (
                f"<form method='post' action='/api/slots/cancel"
                f"?month={yyyy_mm}&slot_id={s.slot_id}&cancel=1'"
                " style='display:inline'"
                " onsubmit=\"return confirmSubmit(this,"
                "'Cancel this event?','Cancel event','#a80',"
                "'Do not cancel')\">"
                "<button type='submit' style='font-size:0.75em;cursor:pointer;"
                "color:#a80;background:none;border:none;text-decoration:underline;"
                "padding:0'>cancel</button></form>"
            )
        extra_tds = ""
        if editable:
            extra_tds = (f"<td style='padding:6px 12px'>{assign_cell}</td>"
                         f"<td style='padding:6px 12px;white-space:nowrap'>"
                         f"{slot_actions}</td>")
        rows.append(
            "<tr>"
            f"<td style='padding:6px 12px;padding-left:32px;color:#666;"
            f"white-space:nowrap'>{_fmt_time(s.start_time)}</td>"
            f"<td style='padding:6px 12px'>{html.escape(s.name)}</td>"
            f"<td style='padding:6px 12px;color:{count_color};text-align:right;"
            f"white-space:nowrap'>{filled}/{s.required_volunteers}</td>"
            f"<td style='padding:6px 12px'>{names_html}</td>"
            f"{extra_tds}"
            "</tr>"
        )
    extra_ths = ("<th style='padding:6px 12px'>Add</th>"
                 "<th style='padding:6px 12px'></th>") if editable else ""
    return (
        "<table style='border-collapse:collapse;width:100%;font-size:0.95em;"
        "text-align:left;margin-top:16px'>"
        "<thead><tr style='color:#888;border-bottom:1px solid #ddd'>"
        "<th style='padding:6px 12px;padding-left:32px;white-space:nowrap'>Start</th>"
        "<th style='padding:6px 12px'>Mass</th>"
        "<th style='padding:6px 12px;text-align:right;white-space:nowrap'>Filled</th>"
        f"<th style='padding:6px 12px'>Assigned</th>{extra_ths}"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def _stub_user(user_id: str) -> User:
    return User(community_id="?", email="?", name="Deleted user")


def _emails_page(event: dict, user: User, community: Community | None,
                 app: Application, membership: Membership | None) -> dict:
    org_name = app.name if app else (community.name if community else user.community_id)
    if not _is_admin(user, membership):
        return _html(403, _page(
            f"<h1>{html.escape(org_name)}</h1><p>Admins only.</p>"
            "<p><a href='/'>Back</a></p>",
            title=org_name,
        ))
    qs = event.get("queryStringParameters") or {}
    direction = qs.get("direction") or ""
    kind = qs.get("kind") or ""
    outcome = qs.get("outcome") or ""
    before = qs.get("before") or None
    page_size = 50
    # Pull one more than page_size to detect whether there's a next page
    raw = list(db.list_email_logs(user.community_id, limit=page_size + 1,
                                  before_sk=before))
    has_more = len(raw) > page_size
    page_records = raw[:page_size]
    # #190: same scope leak as the home-page widget — full /admin/emails
    # listed every email in the community, not just THIS app's. AAs
    # of one app could read another app's subjects, recipient
    # addresses, and member activity. Apply the app filter alongside
    # the existing direction/kind/outcome filters. Untagged emails
    # (no related_app_id) are dropped from the per-app view; if/when
    # that becomes important we can surface them on a CA-only page.
    filtered = [
        e for e in page_records
        if e.related_app_id == app.app_id
        and (not direction or e.direction == direction)
        and (not kind or e.kind == kind)
        and (not outcome or e.outcome == outcome)
    ]
    next_cursor = None
    if has_more and page_records:
        last = page_records[-1]
        next_cursor = f"EMAIL#{last.ts}#{last.email_id}"
    nav_links = []
    if before:
        params = {k: v for k, v in [("direction", direction), ("kind", kind),
                                    ("outcome", outcome)] if v}
        first_url = ("/admin/emails?" + urllib.parse.urlencode(params)
                     if params else "/admin/emails")
        nav_links.append(f"<a href='{first_url}'>← first page</a>")
    if next_cursor:
        params = {k: v for k, v in [("direction", direction), ("kind", kind),
                                    ("outcome", outcome),
                                    ("before", next_cursor)] if v}
        next_url = "/admin/emails?" + urllib.parse.urlencode(params)
        nav_links.append(f"<a href='{next_url}'>next page →</a>")
    page_nav = (
        f"<p style='margin-top:16px;text-align:center'>"
        f"{' &nbsp;&middot;&nbsp; '.join(nav_links)}</p>"
        if nav_links else ""
    )
    body = (
        f"<h1>{html.escape(org_name)}</h1>"
        f"<p style='color:#888'>Email activity</p>"
        + _emails_filter_form(direction, kind, outcome, total=len(filtered))
        + _emails_table(filtered)
        + page_nav
        + _admin_nav_bar("emails", app=app)
    )
    return _html(200, _page(body, narrow=False, title=org_name,
                            ))


_EMAIL_DIRECTIONS = ["", "outbound", "inbound"]
_EMAIL_KINDS = ["", "publish_broadcast", "reminder", "change_notification",
                "swap_request", "admin_command", "command_reply",
                "bounce", "auto_reply", "smoke_test", "other"]
_EMAIL_OUTCOMES = ["", "accepted", "delivered", "bounced",
                   "rejected_dmarc", "rejected_allowlist", "error"]


def _options(values: list[str], selected: str) -> str:
    return "".join(
        f"<option value='{html.escape(v)}'{' selected' if v == selected else ''}>"
        f"{html.escape(v or '(any)')}</option>"
        for v in values
    )


def _emails_filter_form(direction: str, kind: str, outcome: str,
                        total: int) -> str:
    return (
        "<form method='get' action='/admin/emails' "
        "style='margin:16px 0;display:flex;gap:12px;align-items:center;"
        "flex-wrap:wrap'>"
        "<label>Direction "
        f"<select name='direction'>{_options(_EMAIL_DIRECTIONS, direction)}</select>"
        "</label>"
        "<label>Kind "
        f"<select name='kind'>{_options(_EMAIL_KINDS, kind)}</select></label>"
        "<label>Outcome "
        f"<select name='outcome'>{_options(_EMAIL_OUTCOMES, outcome)}</select></label>"
        "<button type='submit' style='padding:4px 12px'>Filter</button>"
        f"<span style='color:#888;font-size:0.9em'>{total} shown on this page</span>"
        "</form>"
    )


def _emails_table(emails: list[EmailLog]) -> str:
    if not emails:
        return "<p style='color:#888'>No matching emails.</p>"
    rows = "".join(
        "<tr>"
        f"<td style='padding:6px 12px;color:#666;font-size:0.85em;"
        f"white-space:nowrap'>{html.escape(e.ts[:16].replace('T', ' '))}</td>"
        f"<td style='padding:6px 12px;color:#888;font-size:0.85em'>{html.escape(e.direction)}</td>"
        f"<td style='padding:6px 12px;font-size:0.9em'>{html.escape(e.kind)}</td>"
        f"<td style='padding:6px 12px'>{html.escape(e.to_addr)}</td>"
        f"<td style='padding:6px 12px'>{html.escape(e.subject)}</td>"
        f"<td style='padding:6px 12px;color:{_OUTCOME_COLORS.get(e.outcome, '#888')};"
        f"font-size:0.9em'>{html.escape(e.outcome)}</td>"
        + (f"<td style='padding:6px 12px;color:#c33;font-size:0.85em'>"
           f"{html.escape((e.error_detail or '')[:80])}</td>"
           if any(x.error_detail for x in emails) else "")
        + "</tr>"
        for e in emails
    )
    err_col = ("<th style='text-align:left;padding:6px 12px;"
               "background:white;position:sticky;top:0'>Error</th>"
               if any(e.error_detail for e in emails) else "")
    scroll_style = ("max-height:500px;overflow-y:auto;border:1px solid #eee;"
                    "border-radius:4px" if len(emails) > 10 else "")
    return (
        f"<div style='{scroll_style};margin-top:8px'>"
        "<table style='border-collapse:collapse;width:100%;font-size:0.95em;"
        "text-align:left'>"
        "<thead><tr style='color:#888;border-bottom:1px solid #ddd'>"
        "<th style='padding:6px 12px;background:white;position:sticky;top:0'>When (UTC)</th>"
        "<th style='padding:6px 12px;background:white;position:sticky;top:0'>Dir</th>"
        "<th style='padding:6px 12px;background:white;position:sticky;top:0'>Kind</th>"
        "<th style='padding:6px 12px;background:white;position:sticky;top:0'>To</th>"
        "<th style='padding:6px 12px;background:white;position:sticky;top:0'>Subject</th>"
        "<th style='padding:6px 12px;background:white;position:sticky;top:0'>Outcome</th>"
        f"{err_col}"
        "</tr></thead><tbody>"
        + rows
        + "</tbody></table></div>"
    )


def _pending_trades_html(user: User, app: Application) -> str:
    pending = [s for s in db.list_swaps_for_user(user.user_id) if s.state == "pending"]
    if not pending:
        return ""
    items = []
    for swap in pending:
        slot = db.find_slot_in_month(app.app_id, swap.yyyy_mm, swap.release_slot_id)
        if not slot:
            continue
        release_desc = (f"{html.escape(slot.name)} on "
                        f"{_pretty_date(slot.local_date)}")
        wanted_descs = []
        for sid in swap.preferred_slot_ids:
            ws = db.find_slot_in_month(app.app_id, swap.yyyy_mm, sid)
            if ws:
                wanted_descs.append(
                    f"{html.escape(ws.name)} on {_pretty_date(ws.local_date)}")
        if wanted_descs:
            want_html = ", ".join(wanted_descs)
        else:
            want_html = "any available slot"
        mode = "hard drop" if swap.released else "keeping slot"
        items.append(
            f"<li style='padding:8px 0;border-bottom:1px solid #eee'>"
            f"<span style='color:#a80'>Pending trade:</span> "
            f"giving up <strong>{release_desc}</strong>, "
            f"want <strong>{want_html}</strong> "
            f"<span style='color:#888;font-size:0.85em'>({mode})</span>"
            f" <form method='post' action='/api/swap/cancel"
            f"?swap_id={swap.swap_id}&month={swap.yyyy_mm}' style='display:inline'"
            f" onsubmit=\"return confirmSubmit(this,"
            f"'Cancel this trade request?','Cancel trade','#c33')\">"
            "<button type='submit' style='font-size:0.8em;cursor:pointer;"
            "color:#c33;background:none;border:none;text-decoration:underline;"
            "padding:0'>cancel trade</button></form></li>"
        )
    if not items:
        return ""
    return (
        "<div style='margin:16px 0;padding:12px;border:1px solid #f0d080;"
        "border-radius:8px;background:#fffbe6'>"
        "<h3 style='font-size:1em;color:#a80;margin:0 0 8px 0'>Pending trades</h3>"
        "<ul style='list-style:none;padding:0;margin:0'>"
        + "".join(items)
        + "</ul></div>"
    )


def _volunteer_home(user: User, community: Community | None,
                    app: Application, *, event: dict, org_name: str) -> str:
    tz_name = (user.preferred_tz or
               (community.default_timezone if community else "America/New_York"))
    tz = ZoneInfo(tz_name)
    today = dt.datetime.now(tz).date().isoformat()
    asgns = sorted(
        db.list_assignments_for_user(user.user_id, since_date=today),
        key=lambda a: a.local_date,
    )
    # Pre-compute which months are currently published so the Trade
    # button is hidden for assignments whose schedule has been
    # unpublished (there'd be nothing to trade into).
    published_months = {s.yyyy_mm for s in db.list_schedules(app.app_id)
                        if s.state == "published"}
    items = []
    for a in asgns:
        slot = db.find_slot_in_month(app.app_id, a.yyyy_mm, a.slot_id)
        if slot is None or slot.cancelled:
            continue
        items.append(_upcoming_item(slot,
                                    tradable=a.yyyy_mm in published_months))
    listing = (
        "<p style='color:#888'>Nothing scheduled yet.</p>"
        if not items else
        "<ul style='list-style:none;padding:0;text-align:left;margin-top:16px'>"
        + "".join(items)
        + "</ul>"
    )
    pending_html = _pending_trades_html(user, app)
    role_label = _ROLE_LABEL.get(user.community_role, user.community_role)
    body = (
        f"<h1>{html.escape(org_name)}</h1>"
        + _flash_banner_html(event)
        + f"<p>Hello, {html.escape(user.name)}.</p>"
        f"<p style=\"color:#666;font-size:0.9em\">"
        f"{html.escape(role_label)}</p>"
        + pending_html
        + f"<h2 style='font-size:1.1em;color:#444;margin-top:32px'>Your upcoming "
        f"{html.escape(app.event_noun_plural or _pluralize(app.event_noun or 'event'))}</h2>"
        + listing
        + "<p style='margin-top:24px'><a href='/your-schedule'>"
        "View schedule, add yourself to slots, mark unavailable days</a>"
        " &middot; <form method='post' action='/api/schedules/email-me' "
        "style='display:inline'><button type='submit' style='font-size:1em;"
        "cursor:pointer;color:#2a7;background:none;border:none;"
        "text-decoration:underline;padding:0'>Send schedule to me by email"
        "</button></form></p>"
        + "<p style='margin-top:8px'><a href='/settings'>Your cohort(s) and notification settings</a></p>"
    )
    return _page(body, narrow=False, title=org_name)


def _upcoming_item(slot: Slot, *, tradable: bool = True) -> str:
    """Render one slot in the "Your upcoming events" list.

    ``tradable`` controls whether the Trade button is shown. Set False
    when the underlying Schedule is not currently published — there's
    no live schedule to trade into, so the modal would have no
    destination slots. Withdraw is always available: even if the
    schedule has been unpublished, the assignment still exists in
    the DB and the user can still release it.
    """
    arrival_local = ""
    if slot.arrival_offset_minutes:
        arrival_local = f" &middot; please arrive by {_fmt_arrival(slot)}"
    trade_btn = (
        f"<a href='/swap/new?slot_id={slot.slot_id}&month={slot.yyyy_mm}' "
        "style='padding:4px 0;font-size:0.85em;min-width:80px;text-align:center;"
        "cursor:pointer;color:white;background:#2a7;border:1px solid #2a7;"
        "border-radius:4px;text-decoration:none;display:inline-block'>Trade</a>"
        if tradable else ""
    )
    return (
        "<li style='padding:12px 0;border-bottom:1px solid #eee;"
        "display:flex;justify-content:space-between;align-items:center'>"
        "<div>"
        f"<div style='font-weight:600'>{html.escape(slot.name)}</div>"
        f"<div style='color:#666;font-size:0.9em'>"
        f"{_DAY_LABEL[slot.day_of_week]} {html.escape(slot.local_date)} "
        f"&middot; {_fmt_time(slot.start_time)}{arrival_local}"
        "</div></div>"
        f"<div style='display:flex;gap:8px;flex-shrink:0'>"
        f"{trade_btn}"
        f"<button onclick=\"showReleaseModal('/api/assignments/release"
        f"?slot_id={slot.slot_id}&month={slot.yyyy_mm}')\" "
        "style='padding:4px 0;font-size:0.85em;min-width:80px;text-align:center;"
        "cursor:pointer;color:#c33;border:1px solid #c33;background:white;"
        "border-radius:4px'>Withdraw</button>"
        "</div></li>"
    )


def _advance_start(day_of_week: int, hhmm: str,
                   minutes: int) -> tuple[int, str]:
    """Advance ``(day_of_week, hhmm)`` by ``minutes``, bumping the day
    when the time crosses midnight.

    Adoration schedules cross midnight (Wed 23:00 → Thu 00:00) so we
    can't just wrap the time; we must also bump the day select.
    day_of_week wraps Mon..Sun (0..6) → back to Mon.
    """
    h, m = (int(x) for x in hhmm.split(":"))
    total = h * 60 + m + minutes
    day_offset, in_day = divmod(total, 24 * 60)
    return (
        (day_of_week + day_offset) % 7,
        f"{in_day // 60:02d}:{in_day % 60:02d}",
    )


def _slot_cap(slot: Slot) -> int | None:
    """Return the effective signup cap for a slot, or None if uncapped.

    Three cases worth pinning explicitly:
      - max_volunteers is None → the admin opted into "no cap"
        (e.g. adoration apps). The caller should treat this as
        unlimited; comparing filled >= None would crash.
      - max_volunteers is 0 → legacy or accidental zero. Fall back to
        required_volunteers so old data behaves the same as it did
        before the int|None refactor.
      - max_volunteers is a positive int → use it directly.
    """
    if slot.max_volunteers is None:
        return None
    if slot.max_volunteers == 0:
        return slot.required_volunteers
    return slot.max_volunteers


def _slot_full(slot: Slot, filled: int) -> bool:
    """True iff the slot has hit its cap. Uncapped slots are never full."""
    cap = _slot_cap(slot)
    return cap is not None and filled >= cap


def _fmt_arrival(slot: Slot) -> str:
    h, m = (int(x) for x in slot.start_time.split(":"))
    start = dt.datetime(2000, 1, 1, h, m)
    arrival = start - dt.timedelta(minutes=slot.arrival_offset_minutes)
    return _fmt_time(f"{arrival.hour:02d}:{arrival.minute:02d}")


def _admin_home(user: User, community: Community | None,
                app: Application, membership: Membership | None, *,
                event: dict,
                org_name: str,
                templates: list[SlotTemplate],
                schedules: list[tuple[Schedule, int, int, int, int]],
                recent_emails: list[EmailLog]) -> str:
    role_label = _ROLE_LABEL.get(
        membership.app_role if membership else user.community_role,
        "Admin")
    body = (
        f"<h1>{html.escape(org_name)}</h1>"
        + _flash_banner_html(event) +
        f"<p>Hello, {html.escape(user.name)}.</p>"
        f"<p style=\"color:#666;font-size:0.9em\">"
        f"{html.escape(role_label)}</p>"
        f"<p style=\"color:#999;font-size:0.8em\">{html.escape(user.email)}</p>"
    )
    body += _my_assignments_section(user, community, app)
    body += _schedules_section(schedules)
    body += _users_summary_section(user.community_id, app.app_id)
    body += _emails_section(recent_emails)
    body += _templates_section(templates)
    body += _settings_summary_section(app, community, user)
    body += _admin_nav_bar("home", app=app)
    return _page(body, narrow=False, title=org_name)


def _users_summary_section(community_id: str, app_id: str) -> str:
    """Render the home-page "Member management" widget for an App Admin.

    Scoped to THIS app's members — the count and the recent-additions
    list both come from the per-app Membership set. Pre-fix (#185) the
    widget walked db.list_users(community_id) and showed every
    community user, so an AA of one small app saw a count like "32
    users total" and the most-recent additions from completely
    unrelated apps. That broke the privacy model (#181) on the home
    page even though the in-app /admin/users page had already been
    scoped (#169 HIGH-1).
    """
    member_ids = {m.user_id for m in db.list_memberships_for_app(app_id)}
    all_users = sorted(
        (u for u in db.list_users(community_id) if u.user_id in member_ids),
        key=lambda u: u.created_at or "", reverse=True)
    recent = all_users[:5]
    total = len(all_users)
    if not recent:
        return (
            "<section style='margin-top:32px;text-align:left'>"
            "<h2 style='font-size:1.1em;color:#444'>Member management "
            "<a href='/admin/users' style='font-size:0.75em;"
            "font-weight:400;margin-left:8px'>manage members</a> "
            "<a href='/admin/cohorts' style='font-size:0.75em;"
            "font-weight:400;margin-left:8px'>manage cohorts</a></h2>"
            "<p style='color:#888'>No users yet.</p>"
            "</section>"
        )
    rows = "".join(
        "<tr>"
        f"<td style='padding:4px 12px'>{html.escape(u.name)}</td>"
        f"<td style='padding:4px 12px;font-size:0.9em;color:#666'>"
        f"{html.escape(u.email)}</td>"
        "</tr>"
        for u in recent
    )
    return (
        "<section style='margin-top:32px;text-align:left'>"
        "<h2 style='font-size:1.1em;color:#444'>Member management "
        "<a href='/admin/users' style='font-size:0.75em;"
        "font-weight:400;margin-left:8px'>manage members</a> "
        "<a href='/admin/cohorts' style='font-size:0.75em;"
        "font-weight:400;margin-left:8px'>manage cohorts</a></h2>"
        f"<p style='color:#888;font-size:0.9em'>{total} users total. "
        "Most recently added:</p>"
        "<table style='border-collapse:collapse;width:100%;font-size:0.95em'>"
        f"<tbody>{rows}</tbody></table>"
        "</section>"
    )


def _my_assignments_section(user: User, community: Community | None,
                            app: Application) -> str:
    tz_name = (user.preferred_tz or
               (community.default_timezone if community else "America/New_York"))
    tz = ZoneInfo(tz_name)
    today = dt.datetime.now(tz).date().isoformat()
    asgns = sorted(
        db.list_assignments_for_user(user.user_id, since_date=today),
        key=lambda a: a.local_date,
    )
    # Same Trade-vs-Withdraw gating as the volunteer home — see
    # _volunteer_home for the rationale.
    published_months = {s.yyyy_mm for s in db.list_schedules(app.app_id)
                        if s.state == "published"}
    items = []
    for a in asgns:
        slot = db.find_slot_in_month(app.app_id, a.yyyy_mm, a.slot_id)
        if slot is None or slot.cancelled:
            continue
        items.append(_upcoming_item(slot,
                                    tradable=a.yyyy_mm in published_months))
    if not items:
        listing = "<p style='color:#888'>No upcoming assignments.</p>"
    else:
        listing = (
            "<ul style='list-style:none;padding:0;text-align:left;margin-top:8px'>"
            + "".join(items) + "</ul>"
        )
    event_noun = app.event_noun or "event"
    return (
        "<section style='margin-top:24px;text-align:left'>"
        + _pending_trades_html(user, app)
        + f"<h2 style='font-size:1.1em;color:#444'>Your upcoming {html.escape(app.event_noun_plural or _pluralize(event_noun))}</h2>"
        + listing
        + "<p style='margin-top:8px'><a href='/your-schedule'>"
        "View schedule, add yourself to slots, mark unavailable days</a>"
        " &middot; <form method='post' action='/api/schedules/email-me' "
        "style='display:inline'><button type='submit' style='font-size:1em;"
        "cursor:pointer;color:#2a7;background:none;border:none;"
        "text-decoration:underline;padding:0'>"
        "Send schedule to me by email</button></form></p>"
        "<p style='margin-top:4px'><a href='/settings'>"
        "Your cohort(s) and notification settings</a></p>"
        "</section>"
    )


_OUTCOME_COLORS = {
    "accepted": "#2a7", "delivered": "#2a7",
    "bounced": "#c33", "rejected_dmarc": "#c33",
    "rejected_allowlist": "#a80", "error": "#c33",
}


def _emails_section(emails: list[EmailLog]) -> str:
    header = (
        "<h2 style='font-size:1.1em;color:#444'>Email "
        "<a href='/admin/send-email' style='font-size:0.75em;font-weight:400;"
        "margin-left:8px'>compose and send email</a>"
        " <span style='font-size:0.75em;font-weight:400;color:#888'>&middot;</span>"
        " <a href='/admin/emails' style='font-size:0.75em;font-weight:400;"
        "margin-left:4px'>view all activity</a></h2>"
    )
    if not emails:
        return (
            "<section style='margin-top:32px;text-align:left'>"
            + header
            + "<p style='color:#888;font-size:0.9em'>Recent email activity:</p>"
            "<p style='color:#888'>No emails sent yet.</p>"
            "</section>"
        )
    rows = "".join(
        "<tr>"
        f"<td style='padding:6px 12px;color:#666;font-size:0.85em;"
        f"white-space:nowrap'>{html.escape(e.ts[:16].replace('T', ' '))}</td>"
        f"<td style='padding:6px 12px'>{html.escape(e.to_addr)}</td>"
        f"<td style='padding:6px 12px'>{html.escape(e.subject)}</td>"
        f"<td style='padding:6px 12px;color:{_OUTCOME_COLORS.get(e.outcome, '#888')};"
        f"font-size:0.9em'>{html.escape(e.outcome)}</td>"
        "</tr>"
        for e in emails
    )
    return (
        "<section style='margin-top:32px;text-align:left'>"
        + header
        + "<p style='color:#888;font-size:0.9em'>Recent email activity:</p>"
        "<table style='border-collapse:collapse;width:100%;font-size:0.95em'>"
        "<thead><tr style='color:#888;border-bottom:1px solid #ddd'>"
        "<th style='text-align:left;padding:6px 12px'>When (UTC)</th>"
        "<th style='text-align:left;padding:6px 12px'>To</th>"
        "<th style='text-align:left;padding:6px 12px'>Subject</th>"
        "<th style='text-align:left;padding:6px 12px'>Outcome</th>"
        "</tr></thead>"
        f"<tbody>{rows}</tbody>"
        "</table>"
        "</section>"
    )


def _lead_summary(minutes: list[int]) -> str:
    """Compact human-readable rendering of lead times for a settings summary."""
    if not minutes:
        return "none"
    parts = []
    for m in sorted(minutes, reverse=True):
        if m >= 1440:
            d = m // 1440
            parts.append(f"{d} day" + ("s" if d > 1 else ""))
        elif m >= 60:
            h = m // 60
            parts.append(f"{h} hour" + ("s" if h > 1 else ""))
        else:
            parts.append(f"{m} min")
    return ", ".join(parts)


def _settings_summary_section(app: Application,
                              community: Community | None,
                              user: User) -> str:
    plural_event = (app.event_noun_plural or
                    _pluralize(app.event_noun or "event"))
    plural_term = (app.terminology_plural or
                   _pluralize(app.terminology or "volunteer"))
    trade_label = ("release immediately" if app.trade_default_release
                   else "keep slot while looking")
    group_label = "on" if app.group_email_mode else "off"
    rows = [
        ("App", html.escape(app.name)),
        ("Event / volunteer", f"{html.escape(plural_term)} covering "
                              f"{html.escape(plural_event)}"),
        ("Default reminders", _lead_summary(app.default_lead_times or [])),
        ("Trade default", trade_label),
        ("Group cohort emails", group_label),
    ]
    if user.community_role == "ca" and community:
        rows.insert(0, ("Community", f"{html.escape(community.name)} "
                                     f"({html.escape(community.default_timezone)})"))
    rendered = "".join(
        f"<tr><td style='padding:3px 12px 3px 0;color:#888;"
        f"font-size:0.9em;white-space:nowrap'>{label}</td>"
        f"<td style='padding:3px 0;font-size:0.9em'>{value}</td></tr>"
        for label, value in rows
    )
    return (
        "<section style='margin-top:32px;text-align:left'>"
        "<h2 style='font-size:1.1em;color:#444'>Settings and reminders "
        "<a href='/admin/settings' style='font-size:0.75em;"
        "font-weight:400;margin-left:8px'>edit</a></h2>"
        "<table style='border-collapse:collapse;margin-top:4px'>"
        f"<tbody>{rendered}</tbody></table>"
        "</section>"
    )


def _templates_section(templates: list[SlotTemplate]) -> str:
    if not templates:
        return (
            "<section style='margin-top:32px;text-align:left'>"
            "<h2 style='font-size:1.1em;color:#444'>Schedule template "
            "<a href='/admin/templates' style='font-size:0.75em;"
            "font-weight:400;margin-left:8px'>edit</a></h2>"
            "<p style='color:#888'>No templates yet.</p>"
            "</section>"
        )
    rows = "".join(
        "<tr>"
        f"<td style='padding:6px 12px'>{html.escape(t.name)}</td>"
        f"<td style='padding:6px 12px'>{_DAY_LABEL[t.day_of_week]}</td>"
        f"<td style='padding:6px 12px'>{_fmt_time(t.start_time)}</td>"
        f"<td style='padding:6px 12px;color:#888'>{t.required_volunteers}</td>"
        "</tr>"
        for t in templates
    )
    return (
        "<section style='margin-top:32px;text-align:left'>"
        "<h2 style='font-size:1.1em;color:#444'>Schedule template and reminders "
        "<a href='/admin/templates' style='font-size:0.75em;"
        "font-weight:400;margin-left:8px'>edit</a></h2>"
        "<table style='border-collapse:collapse;width:100%;font-size:0.95em'>"
        "<thead><tr style='color:#888;border-bottom:1px solid #ddd'>"
        "<th style='text-align:left;padding:6px 12px'>Name</th>"
        "<th style='text-align:left;padding:6px 12px'>Day</th>"
        "<th style='text-align:left;padding:6px 12px'>Start</th>"
        "<th style='text-align:left;padding:6px 12px'>Need</th>"
        "</tr></thead>"
        f"<tbody>{rows}</tbody>"
        "</table>"
        "</section>"
    )


def _schedule_action(sch: Schedule) -> str:
    if sch.state == "draft":
        return (
            f"<form method='post' action='/api/schedules/publish"
            f"?month={sch.yyyy_mm}' style='display:inline'>"
            "<button type='submit' style='font-size:0.85em;cursor:pointer;"
            "color:#2a7;background:none;border:none;text-decoration:underline;"
            "padding:0'>Make active</button></form>"
            " &middot; "
            f"<form method='post' action='/api/schedules/delete"
            f"?month={sch.yyyy_mm}' style='display:inline'"
            f" onsubmit=\"return confirmSubmit(this,"
            f"'Delete this draft schedule ({sch.yyyy_mm})? "
            f"This cannot be undone.','Delete','#c33')\">"
            "<button type='submit' style='font-size:0.85em;cursor:pointer;"
            "color:#c33;background:none;border:none;text-decoration:underline;"
            "padding:0'>Delete</button></form>"
        )
    if sch.state == "published":
        return (
            f"<form method='post' action='/api/schedules/archive"
            f"?month={sch.yyyy_mm}' style='display:inline'"
            " onsubmit=\"return confirmSubmit(this,"
            f"'Move the {_month_label(sch.yyyy_mm)} schedule to history? It "
            "stays viewable and any calendar invites keep working \\u2014 it "
            "just leaves the default schedule and email screens until you "
            "reactivate it.','Move to history','#2a7')\">"
            "<button type='submit' style='font-size:0.85em;cursor:pointer;"
            "color:#2a7;background:none;border:none;text-decoration:underline;"
            "padding:0'>Archive</button></form>"
            " &middot; "
            f"<form method='post' action='/api/schedules/unpublish"
            f"?month={sch.yyyy_mm}' style='display:inline'"
            " onsubmit=\"return confirmSubmit(this,"
            "'Return this schedule to draft? Members will no longer see it and any reminder emails not yet sent will be cancelled. Assignments are preserved.',"
            "'Return to draft','#a80')\">"
            "<button type='submit' style='font-size:0.85em;cursor:pointer;"
            "color:#a80;background:none;border:none;text-decoration:underline;"
            "padding:0'>Return to draft</button></form>"
        )
    if sch.state == "archived":
        return (
            f"<form method='post' action='/api/schedules/reactivate"
            f"?month={sch.yyyy_mm}' style='display:inline'>"
            "<button type='submit' style='font-size:0.85em;cursor:pointer;"
            "color:#2a7;background:none;border:none;text-decoration:underline;"
            "padding:0'>Reactivate</button></form>"
        )
    if sch.state == "publishing":
        # Lock held — no activate/return actions while in flight.
        return ("<span style='color:#c80;font-size:0.85em'>"
                "activating…</span>")
    return ""


def _increment_month(yyyy_mm: str) -> str:
    y, m = (int(x) for x in yyyy_mm.split("-"))
    if m == 12:
        return f"{y + 1}-01"
    return f"{y}-{m + 1:02d}"


def _month_label(yyyy_mm: str) -> str:
    y, m = yyyy_mm.split("-")
    return f"{_MONTH_LABEL[int(m)]} {y}"


def _find_next_uncreated(existing: set[str]) -> str:
    today = dt.date.today()
    if today.month == 12:
        candidate = f"{today.year + 1}-01"
    else:
        candidate = f"{today.year}-{today.month + 1:02d}"
    for _ in range(24):
        if candidate not in existing:
            return candidate
        candidate = _increment_month(candidate)
    return candidate


def _schedules_section(schedules: list[tuple[Schedule, int, int, int, int]]) -> str:
    existing_months = {sch.yyyy_mm for sch, *_ in schedules}
    nm = _find_next_uncreated(existing_months)
    existing_js = ",".join(f"'{m}'" for m in sorted(existing_months))
    quick_create = (
        f"<form method='post' action='/api/schedules/create?month={nm}' "
        "style='margin:8px 0'>"
        f"<button type='submit' style='cursor:pointer;font-size:0.95em;"
        "color:#2a7;background:none;border:none;text-decoration:underline;"
        f"padding:0'>Create {_month_label(nm)} schedule</button></form>"
    )
    create_form = (
        "<details style='margin:8px 0'>"
        "<summary style='cursor:pointer;color:#666;font-size:0.9em'>"
        "Create a different month...</summary>"
        "<form method='post' action='/api/schedules/create' "
        "style='margin:8px 0;display:flex;gap:8px;align-items:center'"
        f" onsubmit=\"var m=this.month.value;var ex=[{existing_js}];"
        "if(ex.indexOf(m)>=0){alert('A schedule for '+m+' already exists."
        " Delete it first.');return false;}\">"
        "<label>Month <input type='month' name='month' "
        f"value='{nm}' style='padding:4px'></label>"
        "<button type='submit' style='padding:4px 12px;cursor:pointer'>"
        "Create</button></form></details>"
    )
    # Archived months are admin-declared history — keep them out of the
    # dashboard's default view (they live behind "Past schedules" on the
    # /schedules page). existing_months above still counts them so the
    # create-form can't collide with an archived month.
    n_archived = sum(1 for sch, *_ in schedules if sch.state == "archived")
    active_scheds = [t for t in schedules if t[0].state != "archived"]
    sorted_scheds = sorted(active_scheds, key=lambda x: x[0].yyyy_mm,
                           reverse=True)
    display_scheds = sorted_scheds[:6]
    show_all = len(sorted_scheds) > 6
    archived_note = (
        f"<p style='margin:8px 0'><a href='/schedules' style='color:#888;"
        f"font-size:0.9em'>Past schedules ({n_archived}) &rarr;</a></p>"
        if n_archived else "")

    if not sorted_scheds:
        return (
            "<section style='margin-top:32px;text-align:left'>"
            "<h2 style='font-size:1.1em;color:#444'>Schedules</h2>"
            + quick_create + create_form + archived_note
            + "</section>"
        )
    rows = "".join(
        "<tr>"
        f"<td style='padding:6px 12px'>"
        f"<a href='/schedules/{html.escape(sch.yyyy_mm)}'>{html.escape(sch.yyyy_mm)}</a>"
        "</td>"
        f"<td style='padding:6px 12px;color:{_STATE_COLORS.get(sch.state, '#888')}'>"
        f"{html.escape(_state_label(sch.state))}</td>"
        f"<td style='padding:6px 12px'>{covered}/{event_count}</td>"
        f"<td style='padding:6px 12px'>{filled_slots}/{total_slots}</td>"
        f"<td style='padding:6px 12px'>{_schedule_action(sch)}</td>"
        "</tr>"
        for sch, event_count, covered, total_slots, filled_slots in display_scheds
    )
    return (
        "<section style='margin-top:32px;text-align:left'>"
        "<h2 style='font-size:1.1em;color:#444'>Schedules</h2>"
        "<table style='border-collapse:collapse;width:100%;font-size:0.95em'>"
        "<thead><tr style='color:#888;border-bottom:1px solid #ddd'>"
        "<th style='text-align:left;padding:6px 12px'>Month</th>"
        "<th style='text-align:left;padding:6px 12px'>State</th>"
        "<th style='text-align:left;padding:6px 12px'>Covered/Total</th>"
        "<th style='text-align:left;padding:6px 12px'>Filled/Total</th>"
        "<th style='text-align:left;padding:6px 12px'>Action</th>"
        "</tr></thead>"
        f"<tbody>{rows}</tbody>"
        "</table>"
        "<p style='margin:8px 0'><a href='/schedules' style='color:#2a7;"
        "font-size:0.95em'>View all active schedules</a></p>"
        + archived_note
        + quick_create + create_form
        + "</section>"
    )


def _unprovisioned(email: str) -> str:
    return _page(
        "<h1>Community Organizer</h1>"
        "<p>You signed in, but your account isn't provisioned in this community yet.</p>"
        f"<p style=\"color:#999;font-size:0.85em\">{html.escape(email)}</p>"
        "<p><a href=\"/auth/logout\">Sign out</a></p>",
        title="Community Organizer",
    )


def _no_application(user: User, community: Community | None) -> str:
    # `app` is not in scope here (the whole point of this page is that
    # no Application is provisioned). The original copy referenced
    # `app.name` which would NameError at runtime and surface a 500
    # with a stack trace (security fix M2).
    org_name = community.name if community else user.community_id
    return _page(
        f"<h1>{html.escape(org_name)}</h1>"
        f"<p>Hello, {html.escape(user.name)}.</p>"
        "<p>No applications are configured for this community yet.</p>"
        "<p style='color:#888;font-size:0.9em'>Ask a community admin to create one.</p>"
        "<p><a href=\"/auth/logout\">Sign out</a></p>",
        title=org_name,
    )


def _coverage_message(slot: Slot, remaining: int) -> str:
    if remaining >= slot.required_volunteers:
        return "This event still has full coverage."
    elif remaining >= slot.min_volunteers:
        return (f"Coverage is now {remaining}/{slot.required_volunteers} "
                f"(below the desired level of {slot.required_volunteers}).")
    elif remaining > 0:
        return (f"WARNING: Coverage is now {remaining}/{slot.required_volunteers} "
                f"(at minimum level).")
    else:
        return "URGENT: This event has NO coverage!"


def _send_removal_notifications(
    removed_user: User, remover: User | None, community: Community,
    app: Application, slot: Slot, yyyy_mm: str, self_release: bool,
    notify_self: bool = False,
    schedule_visible: bool | None = None,
) -> None:
    sch = db.get_schedule(app.app_id, yyyy_mm)
    if not sch:
        return
    # Determine whether the schedule is publicly visible. If the caller
    # didn't pass it (older call sites), fall back to checking the
    # state we just loaded.
    if schedule_visible is None:
        schedule_visible = _schedule_visible(sch)
    # For non-visible schedules, we still want to send the self-release
    # confirmation (so the user has a record), but admin-driven
    # removals stay silent — admins don't typically remove someone
    # from an unpublished month, and if they do they shouldn't surprise
    # the user with an email referencing a schedule they can't see.
    if not schedule_visible and not self_release:
        return
    from community_organizer.providers.email import get_email_provider
    provider = get_email_provider()
    admin_name = remover.name if remover and not self_release else None
    from_addr = _from_addr(admin_name, app.name if app else community.name)
    domain = DOMAIN_NAME
    when = _pretty_date(slot.local_date)
    # An admin removing their OWN slot (remover == removed) reads as a
    # self-removal even though it came through the admin screen. House style:
    # NEVER use a reflexive / singular-"they" ("removed themselves") and don't
    # guess gender — name the person or use a no-pronoun verb ("released").
    self_removal = self_release or (
        remover is not None and remover.user_id == removed_user.user_id)
    co_asgns = list(db.list_assignments_for_slot(
        app.app_id, yyyy_mm, slot.slot_id))
    remaining = len(co_asgns)
    coverage = _coverage_message(slot, remaining)
    users_by_id = {u.user_id: u for u in db.list_users(community.community_id)}
    co_names = sorted(
        users_by_id[a.user_id].name
        for a in co_asgns
        if a.user_id != removed_user.user_id and a.user_id in users_by_id
    )
    co_line = (f"  Still covering: {', '.join(co_names)}\n"
               if co_names else "")

    event_type = app.event_noun or "event"
    if self_release:
        subject = f"{app.name} -- you released: {slot.name} on {when}"
        if schedule_visible:
            tail = (
                f"{coverage}\n\n"
                f"Others covering your {event_type} will be notified, as well as "
                f"others from your cohort, encouraging someone to sign up in your "
                f"place. If you haven't already, we encourage you also to sign up "
                f"for an alternative slot at the same time on another day:\n"
                f"  https://{domain}/your-schedule\n\n"
            )
        else:
            # Schedule has been unpublished — no live page to direct
            # them to, no cohort notification fired. Set expectations
            # clearly instead of leaving them with a dead link.
            tail = (
                f"This schedule is not currently active, so no other members "
                f"are being notified. If a schedule for {_month_label(yyyy_mm)} "
                f"becomes active later, you'll receive it by email and can sign "
                f"up at that time.\n\n"
            )
        body = (
            f"Hi {removed_user.name},\n\n"
            f"You released your slot:\n\n"
            f"  {slot.name}\n"
            f"  {when} -- {_fmt_time(slot.start_time)}\n"
            f"{co_line}\n"
            f"{tail}"
            f"-- {app.name if app else community.name}\n"
        )
    else:
        admin_name = remover.name if remover else "An admin"
        subject = f"{app.name} -- removed from: {slot.name} on {when}"
        body = (
            f"Hi {removed_user.name},\n\n"
            f"{admin_name} removed you from:\n\n"
            f"  {slot.name}\n"
            f"  {when} -- {_fmt_time(slot.start_time)}\n"
            f"{co_line}\n"
            f"No action needed. However, if you want to volunteer for another "
            f"slot, feel free to visit https://{domain}/your-schedule to see "
            f"what's needed.\n\n"
            f"-- {app.name if app else community.name}\n"
        )

    send_to_user = (not self_release) or notify_self
    if send_to_user and removed_user.email and not removed_user.email_undeliverable:
        tz_name = (app.default_timezone or community.default_timezone
                   or "America/New_York")
        cancel_ics = ical.make_cancel_ics(
            slot, removed_user.user_id, removed_user.email,
            domain=domain, timezone=tz_name,
        )
        provider.send(
            community_id=community.community_id,
            from_addr=from_addr, to_addr=removed_user.email,
            subject=subject, body_text=body,
            kind="change_notification",
            related_user_id=removed_user.user_id,
            related_app_id=app.app_id,
            related_slot_id=slot.slot_id,
            related_yyyy_mm=yyyy_mm,
            ics_content=cancel_ics,
        )

    # Symmetric to the signup fan-out (the notifier sends co-assignees an
    # "X joined" note on INSERT): tell the people still on this slot that
    # someone who was sharing it — possibly just a quick sign-up they were
    # already told about — is no longer covering it with them. Without
    # this, a sign-up-then-release leaves co-assignees (and admins, below)
    # with a stale "X signed up" notice and no correction. That was the
    # usher-app bug: Gmail signed up for a Jun 28 slot, two co-ushers were
    # told, then Gmail released it and nobody heard (coverage stayed at the
    # required level, so even the under-coverage alert stayed silent).
    # Coverage apps only — recurring apps surface a peer's departure through
    # the cohort-opening email's in-slot branch.
    if schedule_visible and app.app_type == "coverage":
        verb = "released" if self_removal else "was removed from"
        for a in co_asgns:
            co = users_by_id.get(a.user_id)
            if (not co or not co.email or co.email_undeliverable
                    or co.channel == "none"):
                continue
            others = [n for n in co_names if n != co.name]
            still = (f"  Still covering with you: {', '.join(others)}\n"
                     if others else "")
            co_body = (
                f"Hi {co.name},\n\n"
                f"{removed_user.name} {verb} a slot you're also covering:\n\n"
                f"  {slot.name}\n"
                f"  {when} -- {_fmt_time(slot.start_time)}\n"
                f"{still}\n"
                f"{coverage}\n\n"
                f"-- {app.name if app else community.name}\n"
            )
            provider.send(
                community_id=community.community_id,
                from_addr=from_addr, to_addr=co.email,
                subject=f"{app.name} -- {removed_user.name} {verb}: {slot.name}",
                body_text=co_body,
                kind="change_notification",
                related_user_id=co.user_id,
                related_app_id=app.app_id,
                related_slot_id=slot.slot_id,
                related_yyyy_mm=yyyy_mm,
            )

    # Notify App Admins. Coverage releases/removals ALWAYS ping the AAs
    # (symmetric to the notifier's "X signed up" admin note on signup) so
    # the schedule's owners never end up with a one-sided record. Recurring
    # apps keep the original under-coverage gate here — they get an
    # always-on admin ping from _notify_admins_of_release instead.
    if app.app_type == "coverage" or remaining < slot.required_volunteers:
        aa_ids = {m.user_id for m in db.list_memberships_for_app(app.app_id)
                  if m.app_role == "aa" and m.user_id != removed_user.user_id}
        if remover:
            aa_ids.discard(remover.user_id)
        users_by_id = {u.user_id: u for u in db.list_users(community.community_id)}
        for aa_id in aa_ids:
            aa = users_by_id.get(aa_id)
            if not aa or not aa.email or aa.email_undeliverable:
                continue
            who = removed_user.name
            # Name the actor; no reflexive / singular-they. Self-removal (incl.
            # an admin removing their own slot) -> "released this slot".
            if self_removal:
                how = "released this slot"
            elif remover:
                how = f"was removed by {remover.name}"
            else:
                how = "was removed"
            still = (f"  Still covering: {', '.join(co_names)}\n"
                     if co_names else "")
            aa_body = (
                f"Hi {aa.name},\n\n"
                f"{who} {how}:\n\n"
                f"  {slot.name}\n"
                f"  {when} -- {_fmt_time(slot.start_time)}\n"
                f"{still}\n"
                f"{coverage}\n\n"
                f"View and manage the schedule at:\n"
                f"  https://{domain}/schedules/{yyyy_mm}\n\n"
                f"-- {app.name if app else community.name}\n"
            )
            provider.send(
                community_id=community.community_id,
                from_addr=from_addr, to_addr=aa.email,
                subject=f"{app.name} -- {who} {how}: {slot.name}",
                body_text=aa_body,
                kind="change_notification",
                related_user_id=aa_id,
                related_app_id=app.app_id,
                related_slot_id=slot.slot_id,
                related_yyyy_mm=yyyy_mm,
            )


def _slot_end_time(slot: Slot) -> str:
    """Return slot.start_time + duration_minutes as a local ``HH:MM``
    string, wrapping at 24h. Used to find adjacent slots in the
    "Take it or Split it" notifier."""
    h, m = (int(x) for x in slot.start_time.split(":"))
    total = (h * 60 + m + slot.duration_minutes) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def _find_adjacent_assignees(app: Application, slot: Slot,
                             yyyy_mm: str) -> tuple[list[User], list[User]]:
    """Find the people scheduled in the slot immediately before and
    immediately after ``slot``.

    Returns ``(prior_assignees, next_assignees)`` — each a list of
    User objects deduplicated. Empty lists when there's no adjacent
    slot (first/last of the day) or the adjacent slot has no one
    assigned.

    "Adjacent" means: same local_date, and end_time/start_time
    exactly meet. A 1 PM 60-min slot is the prior of a 2 PM slot.
    A 30-minute gap between slots disqualifies them — they're
    neighbors in the schedule but not "the person right before you"
    in the adoration sense.
    """
    same_day = [s for s in db.list_slots(app.app_id, yyyy_mm)
                if s.local_date == slot.local_date and s.slot_id != slot.slot_id
                and not s.cancelled]
    end_of_this = _slot_end_time(slot)
    prior_slots = [s for s in same_day
                   if _slot_end_time(s) == slot.start_time]
    next_slots = [s for s in same_day
                  if s.start_time == end_of_this]

    users_by_id = {u.user_id: u for u in db.list_users(slot.community_id)}

    def _assignees(slots: list[Slot]) -> list[User]:
        seen: dict[str, User] = {}
        for s in slots:
            for a in db.list_assignments_for_slot(
                    app.app_id, yyyy_mm, s.slot_id):
                u = users_by_id.get(a.user_id)
                if u is not None and u.user_id not in seen:
                    seen[u.user_id] = u
        return list(seen.values())

    return _assignees(prior_slots), _assignees(next_slots)


def _send_take_or_split_emails(
        releaser: User, community: Community, app: Application,
        slot: Slot, yyyy_mm: str) -> None:
    """Email the two adjacent assignees with one-click "Take it" or
    "Split it" actions.

    Each neighbor gets their own email with:
      - "Take 2 PM" link → /assignments/cover?...&mode=take
      - "Split with <other>" link → /assignments/cover?...&mode=split
        (only when there IS an "other" neighbor; otherwise just Take)

    Both links go through a confirmation page rather than acting on
    GET — calendar/preview bots can't accidentally claim slots.

    Fails soft: any provider exception is logged. The Assignment
    delete from the release already happened.
    """
    if app.app_type != "recurring_commitments":
        return
    if not community:
        return
    prior_users, next_users = _find_adjacent_assignees(app, slot, yyyy_mm)
    # Don't try to email the releaser themselves (e.g., they released
    # two consecutive slots).
    prior_users = [u for u in prior_users if u.user_id != releaser.user_id]
    next_users = [u for u in next_users if u.user_id != releaser.user_id]
    if not (prior_users or next_users):
        return
    domain = os.environ.get("DOMAIN_NAME", "community.example.org")
    from_addr = f"organizer@{domain}"
    when = _pretty_date(slot.local_date)
    slot_time = _fmt_time(slot.start_time)
    from community_organizer.providers.email import get_email_provider
    provider = get_email_provider()

    def _send_to(target: User, other_side: list[User]) -> None:
        if not target.email or target.email_undeliverable:
            return
        base = (f"https://{domain}/assignments/cover"
                f"?slot_id={slot.slot_id}&month={yyyy_mm}")
        take_link = f"{base}&mode=take"
        # Split offer only when there IS someone on the other side.
        # Pass their user_id so the action handler knows who to also
        # assign — and so we can name them in the confirmation page.
        if other_side:
            other = other_side[0]
            split_link = f"{base}&mode=split&with={other.user_id}"
            split_label = f"Split with {other.name}"
            split_note = (
                f"\n\nIf you click 'Split with {other.name}', you'll BOTH "
                "be assigned to the 2 PM slot. Only do this if you have "
                f"reason to believe {other.name} is willing — they'll be "
                f"signed up automatically and notified. {other.name} can "
                "decline by releasing the slot if it doesn't actually "
                "work for them."
            )
        else:
            split_link = None
            split_label = ""
            split_note = ""

        body = (
            f"Hi {target.name},\n\n"
            f"{releaser.name} just released:\n\n"
            f"  {slot.name}\n"
            f"  {when} -- {slot_time}\n\n"
            "You're scheduled right next to this slot. If you can "
            "stay, please consider helping cover.\n\n"
            f"  Take the whole hour: {take_link}\n"
        )
        if split_link:
            body += f"  {split_label}: {split_link}\n"
        body += split_note
        body += (
            "\n\nIf neither works, that's fine — cohort members and "
            "the admins have also been notified.\n\n"
            f"-- {app.name}\n"
        )
        try:
            provider.send(
                community_id=community.community_id,
                from_addr=from_addr, to_addr=target.email,
                subject=f"{app.name} -- can you cover {slot.name} "
                        f"on {when}?",
                body_text=body, kind="change_notification",
                related_user_id=target.user_id,
                related_app_id=app.app_id,
                related_slot_id=slot.slot_id,
                related_yyyy_mm=yyyy_mm,
            )
        except Exception as e:    # noqa: BLE001
            log.warning("take-or-split notify failed user=%s slot=%s: %s",
                        target.user_id, slot.slot_id, e)

    # Prior neighbor's "other" is the next neighbor; next's "other" is
    # the prior neighbor.
    for u in prior_users:
        _send_to(u, next_users)
    for u in next_users:
        _send_to(u, prior_users)


def _send_pickup_invite(target_user: User, community: Community,
                        app: Application, slot: Slot, yyyy_mm: str) -> None:
    """Email a one-off VEVENT .ics for a single occurrence the user
    just picked up.

    Used in recurring_commitments after _signup_assignment / admin
    one-off assignment. Skips silently if no email / bouncing / no
    community. Failures are logged, not raised — the Assignment row
    is already written and is the source of truth.
    """
    if not target_user.email or target_user.email_undeliverable:
        return
    domain = os.environ.get("DOMAIN_NAME", "community.example.org")
    tz_name = (app.default_timezone or community.default_timezone
               or "America/New_York")
    arrival_text = (f"please arrive by {_fmt_arrival(slot)}"
                    if slot.arrival_offset_minutes else None)
    body_ics = ical.make_event_ics(
        slot, target_user.user_id, target_user.email,
        domain=domain, community_name=community.name,
        timezone=tz_name, arrival_text=arrival_text,
        alarm_minutes=target_user.calendar_alarm_minutes,
    )
    when = _pretty_date(slot.local_date)
    text = (
        f"Hi {target_user.name},\n\n"
        f"Thanks for picking up {slot.name} on {when} at "
        f"{_fmt_time(slot.start_time)}. The calendar invite is "
        "attached.\n\n"
        f"-- {community.name}\n"
    )
    from community_organizer.providers.email import get_email_provider
    provider = get_email_provider()
    from_addr = f"organizer@{domain}"
    try:
        provider.send(
            community_id=community.community_id,
            from_addr=from_addr, to_addr=target_user.email,
            subject=f"{community.name} -- {slot.name} on {when}",
            body_text=text, kind="change_notification",
            related_user_id=target_user.user_id,
            related_app_id=app.app_id,
            related_slot_id=slot.slot_id,
            related_yyyy_mm=yyyy_mm,
            ics_content=body_ics,
        )
    except Exception as e:    # noqa: BLE001
        log.warning("pickup invite send failed user=%s slot=%s: %s",
                    target_user.user_id, slot.slot_id, e)


def _notify_admins_of_release(releaser: User, community: Community,
                              app: Application, slot: Slot,
                              yyyy_mm: str) -> None:
    """Notify every App Admin (and CA/UA) that a slot was released.

    For recurring_commitments apps the AAs are the operational pivot
    when a cohort can't fill a slot. We notify them in coverage too
    so a release that drops the slot below required gets human eyes.
    """
    if not community:
        return
    aa_ids = {m.user_id for m in db.list_memberships_for_app(app.app_id)
              if m.app_role == "aa" and m.user_id != releaser.user_id}
    # Plus community CAs/UAs (they sit above any specific membership).
    for u in db.list_users(community.community_id):
        if u.community_role in ("ca", "ua") and u.user_id != releaser.user_id:
            aa_ids.add(u.user_id)
    if not aa_ids:
        return
    users_by_id = {u.user_id: u for u in db.list_users(community.community_id)}
    domain = os.environ.get("DOMAIN_NAME", "community.example.org")
    from_addr = f"organizer@{domain}"
    when = _pretty_date(slot.local_date)
    body_text = (
        "Hi,\n\n"
        f"{releaser.name} released:\n\n"
        f"  {slot.name}\n"
        f"  {when} -- {_fmt_time(slot.start_time)}\n\n"
        "The slot is now open. Cohort members have been notified; "
        "if no one picks it up, you may want to step in.\n\n"
        f"-- {app.name}\n"
    )
    from community_organizer.providers.email import get_email_provider
    provider = get_email_provider()
    for aa_id in aa_ids:
        aa = users_by_id.get(aa_id)
        if not aa or not aa.email or aa.email_undeliverable:
            continue
        try:
            provider.send(
                community_id=community.community_id,
                from_addr=from_addr, to_addr=aa.email,
                subject=f"{app.name} -- {releaser.name} released "
                        f"{slot.name} on {when}",
                body_text=body_text, kind="change_notification",
                related_user_id=aa.user_id,
                related_app_id=app.app_id,
                related_slot_id=slot.slot_id,
                related_yyyy_mm=yyyy_mm,
            )
        except Exception as e:    # noqa: BLE001
            log.warning("admin-release-notify failed user=%s slot=%s: %s",
                        aa.user_id, slot.slot_id, e)


def _notify_cohort_of_opening(releaser: User, community: Community,
                              app: Application, slot: Slot, yyyy_mm: str) -> None:
    sch = db.get_schedule(app.app_id, yyyy_mm)
    if not _schedule_visible(sch):
        return
    remaining = sum(1 for _ in db.list_assignments_for_slot(app.app_id, yyyy_mm, slot.slot_id))
    if remaining >= slot.required_volunteers:
        return
    cohort = db.get_cohort_by_template(app.app_id, slot.template_id) if slot.template_id != "one-off" else None
    if not cohort:
        return
    cmems = list(db.list_cohort_members(cohort.cohort_id))
    if not cmems:
        return
    assigned_ids = {a.user_id for a in db.list_assignments_for_slot(
        app.app_id, yyyy_mm, slot.slot_id)}
    users_by_id = {u.user_id: u for u in db.list_users(community.community_id)}
    from community_organizer.providers.email import get_email_provider
    provider = get_email_provider()
    from_addr = f"organizer@{os.environ.get('DOMAIN_NAME', 'community.example.org')}"
    domain = os.environ.get("DOMAIN_NAME", "community.example.org")
    when = _pretty_date(slot.local_date)
    coverage = _coverage_message(slot, remaining)
    # Who's still on the slot (names the people still signed up, so the cohort
    # knows who they'd be joining).
    still_names = sorted(users_by_id[uid].name for uid in assigned_ids
                         if uid in users_by_id)
    still_line = (f"  Still covering: {', '.join(still_names)}\n"
                  if still_names else "")
    # Split cohort members into "already in slot" vs "available to cover"
    in_slot_targets: list[User] = []
    available_targets: list[User] = []
    for cm in cmems:
        target = users_by_id.get(cm.user_id)
        if not target or target.user_id == releaser.user_id:
            continue
        if target.email_undeliverable or not target.email or target.channel == "none":
            continue
        if target.user_id in assigned_ids:
            in_slot_targets.append(target)
        else:
            available_targets.append(target)

    def _coverage_body(intro: str) -> str:
        return (
            f"{intro}\n\n"
            f"  {slot.name}\n"
            f"  {when} -- {_fmt_time(slot.start_time)}\n"
            f"{still_line}\n"
            f"{coverage}\n\n"
            f"If you can cover, sign up at:\n"
            f"  https://{domain}/your-schedule\n\n"
            f"-- {app.name if app else community.name}\n"
        )

    def _update_body(intro: str) -> str:
        return (
            f"{intro}\n\n"
            f"  {slot.name}\n"
            f"  {when} -- {_fmt_time(slot.start_time)}\n\n"
            f"{coverage}\n\n"
            f"If you know someone who can help, ask them to sign up at:\n"
            f"  https://{domain}/your-schedule\n\n"
            f"-- {app.name if app else community.name}\n"
        )

    notified = 0
    if app.group_email_mode:
        aa_ids = {m.user_id for m in db.list_memberships_for_app(app.app_id)
                  if m.app_role == "aa" and m.user_id != releaser.user_id}
        aa_emails = [users_by_id[aa_id].email for aa_id in aa_ids
                     if aa_id in users_by_id
                     and users_by_id[aa_id].email
                     and not users_by_id[aa_id].email_undeliverable]
        if available_targets:
            recipients = [t.email for t in available_targets] + aa_emails
            recipients = sorted(set(recipients))
            provider.send(
                community_id=community.community_id,
                from_addr=from_addr, to_addr=recipients[0],
                to_addrs=recipients,
                subject=f"{app.name} -- opening: {slot.name} on {when}",
                body_text=_coverage_body("Hi all,\n\nA slot opened up:"),
                kind="change_notification",
                related_app_id=app.app_id,
                related_slot_id=slot.slot_id,
                related_yyyy_mm=yyyy_mm,
            )
            notified += len(available_targets)
        # In-slot peers: only for NON-coverage apps. Coverage apps already send
        # each co-assignee the dedicated "X released/was removed from a slot
        # you're also covering" note in _send_removal_notifications — firing
        # this too would double-notify them (and any admin on the slot). See
        # that function's co-assignee loop.
        if in_slot_targets and app.app_type != "coverage":
            recipients = [t.email for t in in_slot_targets] + aa_emails
            recipients = sorted(set(recipients))
            provider.send(
                community_id=community.community_id,
                from_addr=from_addr, to_addr=recipients[0],
                to_addrs=recipients,
                subject=f"{app.name} -- coverage update: {slot.name} on {when}",
                body_text=_update_body(
                    f"Hi all,\n\n{releaser.name} released a slot "
                    f"you are also assigned to:"),
                kind="change_notification",
                related_app_id=app.app_id,
                related_slot_id=slot.slot_id,
                related_yyyy_mm=yyyy_mm,
            )
            notified += len(in_slot_targets)
    else:
        for target in available_targets:
            provider.send(
                community_id=community.community_id,
                from_addr=from_addr, to_addr=target.email,
                subject=f"{app.name} -- opening: {slot.name} on {when}",
                body_text=_coverage_body(f"Hi {target.name},\n\nA slot opened up:"),
                kind="change_notification",
                related_user_id=target.user_id,
                related_app_id=app.app_id,
                related_slot_id=slot.slot_id,
                related_yyyy_mm=yyyy_mm,
            )
            notified += 1
        # See the group-mode note above: coverage apps notify in-slot peers via
        # the dedicated co-assignee note, so skip them here to avoid duplicates.
        for target in (in_slot_targets if app.app_type != "coverage" else []):
            provider.send(
                community_id=community.community_id,
                from_addr=from_addr, to_addr=target.email,
                subject=f"{app.name} -- coverage update: {slot.name} on {when}",
                body_text=_update_body(
                    f"Hi {target.name},\n\n{releaser.name} released a slot "
                    f"you are also assigned to:"),
                kind="change_notification",
                related_user_id=target.user_id,
                related_app_id=app.app_id,
                related_slot_id=slot.slot_id,
                related_yyyy_mm=yyyy_mm,
            )
            notified += 1
    log.info("notified %d cohort members of opening for slot %s (remaining=%d/%d) group=%s",
             notified, slot.slot_id, remaining, slot.required_volunteers,
             app.group_email_mode)


def _release_assignment(event: dict, user: User, community: Community | None,
                        app: Application, membership: Membership | None) -> dict:
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method != "POST":
        return _text(405, "POST required")
    qs = event.get("queryStringParameters") or {}
    slot_id = qs.get("slot_id")
    yyyy_mm = qs.get("month")
    if not slot_id or not yyyy_mm:
        return _error_redirect("/your-schedule", "Missing slot id or month.")
    notify_me = _get_param(event, "notify_me") == "1"
    slot = db.find_slot_in_month(app.app_id, yyyy_mm, slot_id)
    # Verify the actor actually had an assignment on this slot BEFORE
    # firing cohort emails — otherwise any member can spam every cohort
    # member by hitting this endpoint with arbitrary slot_ids
    # (security fix M1).
    existing = [a for a in db.list_assignments_for_slot(app.app_id, yyyy_mm, slot_id)
                if a.user_id == user.user_id]
    if not existing:
        # Silent no-op — matches the historic delete behavior, just
        # without the email storm.
        if _is_admin(user, membership):
            return _redirect("/")
        return _redirect("/your-schedule")
    db.delete_assignment(app.app_id, yyyy_mm, slot_id, user.user_id)
    log.info("user %s released assignment slot=%s month=%s notify_me=%s",
             user.user_id, slot_id, yyyy_mm, notify_me)
    # If the schedule has been unpublished, the assignment can still be
    # released (the row exists in DDB) but the cohort-opening email
    # would point at a slot no recipient can see. Skip the cohort
    # notification in that case; the actor still gets their personal
    # confirmation (with adjusted copy — see _send_removal_notifications).
    sch = db.get_schedule(app.app_id, yyyy_mm)
    schedule_visible = _schedule_visible(sch)
    if slot and community:
        _send_removal_notifications(
            user, None, community, app, slot, yyyy_mm,
            self_release=True, notify_self=notify_me,
            schedule_visible=schedule_visible,
        )
        if schedule_visible:
            _notify_cohort_of_opening(user, community, app, slot, yyyy_mm)
            # Recurring-app admins need a direct ping so a no-show
            # cohort slot doesn't quietly go unfilled.
            if app.app_type == "recurring_commitments":
                _notify_admins_of_release(user, community, app, slot, yyyy_mm)
                # Adjacent assignees get a separate "Take it or Split
                # it" email — the two people on either side are by
                # far the most likely to absorb the open hour.
                _send_take_or_split_emails(
                    user, community, app, slot, yyyy_mm)
    if _is_admin(user, membership):
        return _redirect("/")
    return _redirect("/your-schedule")


def _signup_assignment(event: dict, user: User, community: Community | None,
                       app: Application, membership: Membership | None) -> dict:
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method != "POST":
        return _text(405, "POST required")
    qs = event.get("queryStringParameters") or {}
    slot_id = qs.get("slot_id")
    yyyy_mm = qs.get("month")
    if not slot_id or not yyyy_mm:
        return _error_redirect("/", "Missing slot id or month.")
    slot = db.find_slot_in_month(app.app_id, yyyy_mm, slot_id)
    if not slot:
        return _error_redirect("/", "Slot not found.")
    sch = db.get_schedule(app.app_id, yyyy_mm)
    if not _schedule_visible(sch):
        return _error_redirect("/", "Schedule is not visible.")
    # Atomic signup: increment the slot's assignment_count AND write
    # the Assignment row in a single DDB TransactWriteItems. The
    # previous read-then-write pattern raced; two members clicking
    # "Sign me up" at the same time could both pass the capacity
    # check and both write, taking the slot over max_volunteers
    # (security fix D12).
    try:
        db.atomic_signup_assignment(
            slot,
            user_id=user.user_id,
            community_id=user.community_id,
        )
    except db.CapacityExceeded:
        log.info("user %s lost capacity race for slot=%s month=%s",
                 user.user_id, slot_id, yyyy_mm)
        return _redirect("/your-schedule")
    log.info("user %s signed up for slot=%s month=%s",
             user.user_id, slot_id, yyyy_mm)
    # For recurring_commitments apps the new assignee likely isn't in
    # the slot's cohort (cohort members got the RRULE at join time).
    # Send them a one-off .ics for THIS occurrence so their calendar
    # picks it up. No-op for coverage apps — the broadcast at publish
    # time already covered them.
    if app.app_type == "recurring_commitments" and community is not None:
        _send_pickup_invite(user, community, app, slot, yyyy_mm)
    return _redirect("/your-schedule")


def _cover_released_page(event: dict, user: User,
                         community: Community | None,
                         app: Application,
                         membership: Membership | None) -> dict:
    """Confirmation page for the "Take it" / "Split it" email links.

    URL: ``GET /assignments/cover?slot_id=&month=&mode=take|split[&with=<uid>]``

    Shows the slot details, what's about to happen, and a single
    POST button. Cancel link goes back to the home page. Renders an
    error if the slot was already filled or no longer exists.
    """
    slot_id = _get_param(event, "slot_id")
    yyyy_mm = _get_param(event, "month")
    mode = (_get_param(event, "mode") or "").strip()
    with_uid = (_get_param(event, "with") or "").strip()
    if not slot_id or not yyyy_mm or mode not in ("take", "split"):
        return _error_redirect("/", "Missing slot id, month, or mode.")
    slot = db.find_slot_in_month(app.app_id, yyyy_mm, slot_id)
    if not slot:
        return _html(404, _page(
            "<h2>Slot not found</h2>"
            "<p>The slot may have been removed. "
            "<a href='/'>Back to Home</a></p>"))
    other_user: User | None = None
    if mode == "split":
        if not with_uid:
            return _error_redirect("/", "Split mode requires a partner.")
        other_user = db.get_user(user.community_id, with_uid)
        if other_user is None:
            return _text(404, "named partner not found")
        # PRIVACY-AUDIT LOW-2: clamp the partner to "someone who has
        # an assignment in an adjacent slot in this period." Without
        # this, an AA could craft a URL with any user_id from the
        # community and render that user's name on the confirmation
        # page. Adjacency is computed at email-send time too, so
        # legitimate links from the take-or-split email always pass.
        prior, nxt = _find_adjacent_assignees(app, slot, yyyy_mm)
        if other_user.user_id not in {u.user_id for u in prior + nxt}:
            return _text(404, "named partner not found")
    # If the caller is already on the slot, that's a no-op; bail early.
    existing = [a for a in
                db.list_assignments_for_slot(app.app_id, yyyy_mm, slot_id)
                if a.user_id == user.user_id]
    already = bool(existing)

    when = _pretty_date(slot.local_date)
    if mode == "take":
        title = f"Cover {html.escape(slot.name)}?"
        explanation = (
            f"<p>You'll be assigned to <b>{html.escape(slot.name)}</b> "
            f"on <b>{html.escape(when)}</b>, starting "
            f"{html.escape(_fmt_time(slot.start_time))}. "
            "A calendar invite will be emailed to you.</p>"
        )
        btn_label = "Yes, take it"
    else:
        title = (f"Split {html.escape(slot.name)} with "
                 f"{html.escape(other_user.name)}?")
        explanation = (
            "<div style='padding:10px 14px;border:1px solid #a80;"
            "background:#fff8eb;border-radius:6px;margin-bottom:12px'>"
            f"<b>Heads up:</b> {html.escape(other_user.name)} will be "
            f"automatically assigned to {html.escape(slot.name)} on "
            f"{html.escape(when)} alongside you. "
            "<b>Only do this if you have reason to believe "
            f"{html.escape(other_user.name)} is willing</b> — for "
            "example, you've already chatted or you know they "
            "regularly cover for each other. They'll be emailed and "
            "can decline by releasing the slot if it doesn't work."
            "</div>"
        )
        btn_label = f"Yes, split it with {html.escape(other_user.name)}"

    already_banner = ""
    if already:
        already_banner = (
            "<div style='padding:10px 14px;border:1px solid #2a7;"
            "background:#f0fff5;border-radius:6px;margin-bottom:12px'>"
            "You're already assigned to this slot. Submitting will "
            "just ensure your partner is also assigned (split mode), "
            "or be a no-op (take mode)."
            "</div>"
        )

    hidden = (
        f"<input type='hidden' name='slot_id' value='{slot.slot_id}'>"
        f"<input type='hidden' name='month' value='{yyyy_mm}'>"
        f"<input type='hidden' name='mode' value='{mode}'>"
    )
    if mode == "split":
        hidden += (
            f"<input type='hidden' name='with' value='{other_user.user_id}'>"
        )

    body = (
        f"<h1>{title}</h1>"
        f"{already_banner}{explanation}"
        "<form method='post' action='/api/assignments/cover' "
        "style='display:flex;gap:12px;align-items:center;margin-top:16px'>"
        f"{hidden}"
        f"<button type='submit' style='padding:8px 18px;cursor:pointer;"
        "background:#2a7;color:white;border:none;border-radius:4px;"
        f"font-size:1em'>{btn_label}</button>"
        "<a href='/' style='color:#888'>Cancel</a>"
        "</form>"
    )
    return _html(200, _page(body, narrow=True))


def _api_cover_released(event: dict, user: User,
                        community: Community | None,
                        app: Application,
                        membership: Membership | None) -> dict:
    """Execute the take/split assignment from the confirmation page.

    Take: caller gets an Assignment row for the slot. Pickup .ics
    email goes out via the existing _send_pickup_invite (recurring
    apps only).

    Split: caller AND the partner (?with=<uid>) both get assigned.
    Both get the pickup .ics. Partner also gets a heads-up that
    they were signed up.

    Both modes are idempotent — already-assigned users don't create
    duplicate rows. Capacity races are rare here (max_volunteers is
    None for adoration; for capped slots we use atomic_signup which
    raises CapacityExceeded cleanly).
    """
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method != "POST":
        return _text(405, "POST required")
    slot_id = _get_param(event, "slot_id")
    yyyy_mm = _get_param(event, "month")
    mode = (_get_param(event, "mode") or "").strip()
    with_uid = (_get_param(event, "with") or "").strip()
    if not slot_id or not yyyy_mm or mode not in ("take", "split"):
        return _error_redirect("/", "Missing slot id, month, or mode.")
    slot = db.find_slot_in_month(app.app_id, yyyy_mm, slot_id)
    if not slot:
        return _error_redirect("/", "Slot not found.")
    if not community:
        return _error_redirect("/", "Community not found.")

    def _assign(target: User) -> bool:
        """Idempotent assignment. Returns True if we wrote a new row."""
        existing = [a for a in
                    db.list_assignments_for_slot(app.app_id, yyyy_mm, slot_id)
                    if a.user_id == target.user_id]
        if existing:
            return False
        try:
            db.atomic_signup_assignment(
                slot, user_id=target.user_id,
                community_id=user.community_id,
                created_by=user.user_id,
            )
        except db.CapacityExceeded:
            log.info("cover-released hit capacity slot=%s user=%s",
                     slot_id, target.user_id)
            return False
        return True

    wrote_self = _assign(user)
    if wrote_self:
        _send_pickup_invite(user, community, app, slot, yyyy_mm)

    if mode == "split":
        if not with_uid:
            return _error_redirect("/", "Split mode requires a partner.")
        other = db.get_user(user.community_id, with_uid)
        # PRIVACY-AUDIT LOW-2: don't sign up arbitrary user_ids. The
        # named partner must already have an assignment in an
        # adjacent slot — same check the confirmation page uses.
        if other is not None:
            prior, nxt = _find_adjacent_assignees(app, slot, yyyy_mm)
            if other.user_id not in {u.user_id for u in prior + nxt}:
                other = None
        if other is not None:
            wrote_other = _assign(other)
            if wrote_other:
                _send_pickup_invite(other, community, app, slot, yyyy_mm)
                _send_split_heads_up(user, other, community, app,
                                     slot, yyyy_mm)
    log.info("cover-released by user=%s slot=%s mode=%s",
             user.user_id, slot_id, mode)
    return _redirect("/")


def _send_split_heads_up(initiator: User, partner: User,
                         community: Community, app: Application,
                         slot: Slot, yyyy_mm: str) -> None:
    """Email the split partner that they've been auto-assigned.

    Includes a release link so they can back out if the assumption
    was wrong. The pickup .ics from _send_pickup_invite already
    landed in their inbox separately — this is a plain-text note
    explaining WHO signed them up and giving them the out.
    """
    if not partner.email or partner.email_undeliverable:
        return
    domain = os.environ.get("DOMAIN_NAME", "community.example.org")
    from_addr = f"organizer@{domain}"
    when = _pretty_date(slot.local_date)
    body = (
        f"Hi {partner.name},\n\n"
        f"{initiator.name} just signed you up to split "
        f"{slot.name} on {when} -- the assumption is the two of you "
        "will informally divide the hour (first half / second half).\n\n"
        f"If that doesn't actually work for you, please withdraw at "
        f"https://{domain}/ -- the cohort and admins will be notified "
        "so someone else can step in.\n\n"
        f"-- {app.name}\n"
    )
    from community_organizer.providers.email import get_email_provider
    provider = get_email_provider()
    try:
        provider.send(
            community_id=community.community_id,
            from_addr=from_addr, to_addr=partner.email,
            subject=f"{app.name} -- {initiator.name} signed you up "
                    f"to split {slot.name}",
            body_text=body, kind="change_notification",
            related_user_id=partner.user_id,
            related_app_id=app.app_id,
            related_slot_id=slot.slot_id,
            related_yyyy_mm=yyyy_mm,
        )
    except Exception as e:    # noqa: BLE001
        log.warning("split heads-up send failed user=%s slot=%s: %s",
                    partner.user_id, slot.slot_id, e)


def _open_slots_page(event: dict, user: User, community: Community | None,
                     app: Application, membership: Membership | None) -> dict:
    org_name = app.name if app else (community.name if community else user.community_id)
    tz_name = (user.preferred_tz or
               (community.default_timezone if community else "America/New_York"))
    tz = ZoneInfo(tz_name)
    today = dt.datetime.now(tz).date().isoformat()
    users_by_id = {u.user_id: u for u in db.list_users(user.community_id)}

    # Schedules that should be reflected here: published for coverage,
    # materialized for recurring. _schedule_visible normalizes.
    visible = sorted(
        [s for s in db.list_schedules(app.app_id)
         if _schedule_visible(s)],
        key=lambda s: s.yyyy_mm,
    )
    all_slots: list[tuple[Slot, list[Assignment]]] = []
    for sch in visible:
        slots = sorted(db.list_slots(app.app_id, sch.yyyy_mm),
                       key=lambda s: (s.local_date, s.start_time))
        asgns_by_slot: dict[str, list[Assignment]] = {}
        for a in db.list_assignments_for_month(app.app_id, sch.yyyy_mm):
            asgns_by_slot.setdefault(a.slot_id, []).append(a)
        for s in slots:
            if s.local_date < today or s.cancelled:
                continue
            all_slots.append((s, asgns_by_slot.get(s.slot_id, [])))

    rows = ""
    current_date: str | None = None
    for s, asgns in all_slots:
        if s.local_date != current_date:
            current_date = s.local_date
            rows += (
                "<tr style='background:#f5f5f5'>"
                "<td colspan='5' style='padding:8px 12px;font-weight:600;"
                "color:#444;border-top:1px solid #ddd'>"
                f"{html.escape(_pretty_date(s.local_date))}"
                "</td></tr>"
            )
        filled = len(asgns)
        full = _slot_full(s, filled)
        user_assigned = any(a.user_id == user.user_id for a in asgns)
        name_parts = []
        for a in asgns:
            vol = users_by_id.get(a.user_id, _stub_user(a.user_id))
            if a.user_id == user.user_id:
                release_url = f"/api/assignments/release?slot_id={s.slot_id}&month={s.yyyy_mm}"
                confirm_self = ""
                if not a.confirmed_at:
                    confirm_self = (
                        f" | <form method='post' "
                        f"action='/api/assignments/confirm"
                        f"?slot_id={s.slot_id}&month={s.yyyy_mm}' "
                        "style='display:inline'>"
                        "<button type='submit' style='font-size:0.8em;"
                        "cursor:pointer;color:#2a7;background:none;border:none;"
                        "text-decoration:underline;padding:0'>"
                        "confirm</button></form>"
                    )
                name_parts.append(
                    f"{_confirm_name_html(vol.name, a)} ("
                    f"<a href='/swap/new?slot_id={s.slot_id}&month={s.yyyy_mm}'"
                    " style='font-size:0.8em;color:#2a7'>trade</a>"
                    f" | <a href='javascript:void(0)' onclick=\"showReleaseModal('{release_url}')\" "
                    "style='font-size:0.8em;color:#c33'>withdraw</a>"
                    f"{confirm_self})"
                )
            else:
                name_parts.append(_confirm_name_html(vol.name, a))
        if not full and not user_assigned:
            name_parts.append(
                f"<form method='post' action='/api/assignments/signup"
                f"?slot_id={s.slot_id}&month={s.yyyy_mm}' "
                "style='display:inline'>"
                "<button type='submit' style='font-size:0.85em;"
                "cursor:pointer;color:#2a7;background:none;border:none;"
                "text-decoration:underline;padding:0'>"
                "Sign me up</button></form>"
            )
        vol_html = ", ".join(name_parts) if name_parts else \
            "<span style='color:#aaa'>--</span>"
        count_color = "#2a7" if full else "#a80"
        rows += (
            "<tr>"
            f"<td style='padding:6px 12px;padding-left:32px;color:#666;"
            f"white-space:nowrap'>{_fmt_time(s.start_time)}</td>"
            f"<td style='padding:6px 12px'>{html.escape(s.name)}</td>"
            f"<td style='padding:6px 12px;color:{count_color};"
            f"white-space:nowrap'>{filled}/{s.required_volunteers}</td>"
            f"<td style='padding:6px 12px'>{vol_html}</td>"
            "</tr>"
        )

    if not rows:
        content = "<p style='color:#888'>No active schedule yet.</p>"
    else:
        content = (
            "<table style='border-collapse:collapse;width:100%;font-size:0.95em;"
            "text-align:left;margin-top:16px'>"
            "<thead><tr style='color:#888;border-bottom:1px solid #ddd'>"
            "<th style='padding:6px 12px;padding-left:32px;white-space:nowrap'>Start</th>"
            "<th style='padding:6px 12px'>Mass</th>"
            "<th style='padding:6px 12px;white-space:nowrap'>Filled</th>"
            f"<th style='padding:6px 12px'>{html.escape((app.terminology or 'volunteer').capitalize())}s</th>"
            "</tr></thead><tbody>" + rows + "</tbody></table>"
        )

    email_me = (
        "<form method='post' action='/api/schedules/email-me' "
        "style='display:inline;margin-left:16px'>"
        "<button type='submit' style='font-size:0.85em;cursor:pointer;"
        "color:#2a7;background:none;border:none;text-decoration:underline;"
        "padding:0'>Send this schedule to me by email</button></form>"
    )
    pending_html = _pending_trades_html(user, app)
    availability_link = (
        "<p style='margin-top:8px;font-size:0.95em'>"
        "<a href='/my-availability' style='color:#2a7'>"
        "Set days I'm unavailable &rarr;</a></p>"
    )
    body = (
        f"<h1>{html.escape(org_name)}</h1>"
        + _flash_banner_html(event)
        + pending_html
        + availability_link
        + "<h2 style='font-size:1.1em;color:#444'>Schedule</h2>"
        + content
        + f"<p style='margin-top:24px'>{email_me}</p>"
        + (_admin_nav_bar("my-schedule", app=app) if _is_admin(user, membership)
           else "<p style='margin-top:8px'><a href='/'>Back to Home</a></p>")
    )
    return _html(200, _page(body, narrow=False, title=org_name,
                            ))


def _my_availability_page(event: dict, user: User,
                          community: Community | None,
                          app: Application,
                          membership: Membership | None) -> dict:
    """Member-facing list of "I can't do this day" entries for this app.

    Forward-looking only: past dates are hidden. The admin's cohort
    pick-list reads these to fade-out blocked members on assignment
    pages. A block on a date the member is already assigned to is
    refused (member must release the assignment first).
    """
    if membership is None and not _is_admin(user, membership):
        # A user who isn't a member of any app and isn't a CA has no
        # app context — bounce them to the launcher.
        return _redirect("/launcher")
    org_name = app.name if app else (
        community.name if community else user.community_id)
    tz_name = (user.preferred_tz or
               (community.default_timezone if community else "America/New_York"))
    today = dt.datetime.now(ZoneInfo(tz_name)).date().isoformat()

    blocks = sorted(
        db.list_blocked_dates_for_user(app.app_id, user.user_id,
                                       since_date=today),
        key=lambda b: b.local_date,
    )
    if blocks:
        rows = "".join(
            "<tr>"
            f"<td style='padding:6px 12px'>{html.escape(_pretty_date(b.local_date))}</td>"
            "<td style='padding:6px 12px;text-align:right'>"
            "<form method='post' action='/api/blocked-dates/delete' "
            "style='display:inline' "
            f"onsubmit=\"return confirmSubmit(this,"
            f"'Remove the block for {html.escape(_pretty_date(b.local_date))}?',"
            "'Remove','#c33')\">"
            f"<input type='hidden' name='local_date' value='{b.local_date}'>"
            "<button type='submit' style='font-size:0.85em;cursor:pointer;"
            "color:#c33;background:none;border:none;text-decoration:underline;"
            "padding:0'>Remove</button></form>"
            "</td></tr>"
            for b in blocks
        )
        blocks_html = (
            "<table style='border-collapse:collapse;width:100%;font-size:0.95em;"
            "text-align:left;margin-top:8px'>"
            "<thead><tr style='color:#888;border-bottom:1px solid #ddd'>"
            "<th style='padding:6px 12px'>Date</th>"
            "<th style='padding:6px 12px;text-align:right'></th>"
            "</tr></thead><tbody>" + rows + "</tbody></table>"
        )
    else:
        blocks_html = ("<p style='color:#888;margin-top:8px'>"
                       "You have no upcoming blocked dates.</p>")

    add_form = (
        "<form method='post' action='/api/blocked-dates/add' "
        "style='margin-top:24px;padding:16px;background:#f8f8f8;"
        "border-radius:4px;border:1px solid #eee'>"
        "<label style='display:block;font-weight:600;color:#444;"
        "margin-bottom:6px'>Add a date you can't do:</label>"
        f"<input type='date' name='local_date' min='{today}' required "
        "style='padding:6px;font-size:1em'> "
        "<button type='submit' style='padding:6px 16px;cursor:pointer;"
        "font-size:1em;margin-left:8px'>Block this date</button>"
        "</form>"
    )

    intro = (
        "<p style='color:#555;line-height:1.5'>"
        "Days you list here are off-limits for the admin's pick list. "
        "If you're already assigned to a slot on a date you want to "
        "block, release the assignment first (from "
        "<a href='/your-schedule' style='color:#2a7'>My Schedule</a>)."
        "</p>"
    )

    body = (
        f"<h1>{html.escape(org_name)}</h1>"
        "<h2 style='font-size:1.1em;color:#444'>My availability</h2>"
        + _flash_banner_html(event)
        + intro
        + "<h3 style='font-size:1em;color:#555;margin-top:24px'>"
        "Upcoming blocked dates</h3>"
        + blocks_html
        + add_form
        + (_admin_nav_bar("my-availability", app=app)
           if _is_admin(user, membership)
           else "<p style='margin-top:24px'>"
                "<a href='/your-schedule'>Back to My Schedule</a></p>"
                "<p style='margin-top:8px'>"
                "<a href='/'>Back to Home</a></p>")
    )
    return _html(200, _page(body, title=org_name))


def _api_blocked_date_add(event: dict, user: User,
                          community: Community | None,
                          app: Application,
                          membership: Membership | None) -> dict:
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method != "POST":
        return _text(405, "POST required")
    if membership is None and not _is_admin(user, membership):
        return _error_redirect("/launcher", "Not a member of this app.")
    local_date = (_get_param(event, "local_date") or "").strip()
    if not _ISO_DATE_RE.match(local_date):
        return _error_redirect("/my-availability",
            "Invalid date — use the date picker.")
    tz_name = (user.preferred_tz or
               (community.default_timezone if community else "America/New_York"))
    today = dt.datetime.now(ZoneInfo(tz_name)).date().isoformat()
    if local_date < today:
        return _error_redirect("/my-availability",
            "Pick a future date — past blocks have no effect.")
    if db.is_user_assigned_on_date(app.app_id, user.user_id, local_date):
        return _error_redirect("/my-availability",
            f"You're already assigned to a slot on "
            f"{_pretty_date(local_date)}. Release that assignment from "
            "My Schedule first, then add the block.")
    db.put_blocked_date(BlockedDate(
        community_id=user.community_id, app_id=app.app_id,
        user_id=user.user_id, local_date=local_date,
    ))
    log.info("user %s blocked %s in app %s",
             user.user_id, local_date, app.app_id)
    return _redirect("/my-availability")


def _api_blocked_date_delete(event: dict, user: User,
                             community: Community | None,
                             app: Application,
                             membership: Membership | None) -> dict:
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method != "POST":
        return _text(405, "POST required")
    if membership is None and not _is_admin(user, membership):
        return _error_redirect("/launcher", "Not a member of this app.")
    local_date = (_get_param(event, "local_date") or "").strip()
    if not _ISO_DATE_RE.match(local_date):
        return _error_redirect("/my-availability", "Invalid date.")
    db.delete_blocked_date(app.app_id, user.user_id, local_date)
    log.info("user %s unblocked %s in app %s",
             user.user_id, local_date, app.app_id)
    return _redirect("/my-availability")


def _api_schedule_create(event: dict, user: User, community: Community | None,
                         app: Application, membership: Membership | None) -> dict:
    if not _is_admin(user, membership):
        return _text(403, "admins only")
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method != "POST":
        return _text(405, "POST required")
    yyyy_mm = _get_param(event, "month")
    if not yyyy_mm:
        return _error_redirect("/", "Missing month parameter.")
    if not community:
        return _error_redirect("/", "Community not found.")
    existing_sch = db.get_schedule(app.app_id, yyyy_mm)
    if existing_sch:
        return _error_redirect("/",
            f"A schedule for {yyyy_mm} already exists "
            f"({existing_sch.state}). Delete it first or edit the existing one.")
    tz = app.default_timezone or community.default_timezone
    templates = list(db.list_templates(app.app_id))
    if not templates:
        return _error_redirect("/admin/templates",
            "Create at least one event template before creating a schedule.")
    slots = scheduling.materialize(
        community.community_id, app.app_id, yyyy_mm, tz, templates,
        period_type=app.period_type)
    db.put_slots(slots)
    sch = Schedule(community_id=community.community_id, app_id=app.app_id, yyyy_mm=yyyy_mm)
    db.put_schedule(sch)
    log.info("admin %s created schedule %s (%d slots)", user.user_id, yyyy_mm, len(slots))
    return _redirect(f"/schedules/{yyyy_mm}")


def _api_schedule_publish(event: dict, user: User, community: Community | None,
                          app: Application, membership: Membership | None) -> dict:
    if not _is_admin(user, membership):
        return _text(403, "admins only")
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method != "POST":
        return _text(405, "POST required")
    yyyy_mm = _get_param(event, "month")
    if not yyyy_mm or not community:
        return _error_redirect("/schedules", "Missing month or community.")
    try:
        # Publish is now state-only (#215). Members can self-signup
        # against this schedule immediately; the broadcast email +
        # per-slot calendar invites are a separate admin action
        # via /admin/send-email.
        summary = publishing.publish_schedule(community, app, yyyy_mm)
    except ValueError as e:
        return _error_redirect(f"/schedules/{yyyy_mm}", str(e))
    log.info("admin %s published %s (state-only): %s",
             user.user_id, yyyy_mm, summary)
    _cleanup_stale_cohorts(app.app_id)
    # Land on the send-email page with a banner suggesting the
    # broadcast as a next step, since publish no longer fires it.
    msg = (f"{_month_label(yyyy_mm)} is now active — members can self-signup. "
           f"Send it out with an email when the schedule is ready.")
    return _redirect(
        "/admin/send-email?msg=" + urllib.parse.quote(msg))


def _api_schedule_unpublish(event: dict, user: User, community: Community | None,
                            app: Application, membership: Membership | None) -> dict:
    if not _is_admin(user, membership):
        return _text(403, "admins only")
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method != "POST":
        return _text(405, "POST required")
    yyyy_mm = _get_param(event, "month")
    if not yyyy_mm:
        return _error_redirect("/schedules", "Missing month.")
    try:
        publishing.unpublish_schedule(app, yyyy_mm)
    except ValueError as e:
        return _error_redirect(f"/schedules/{yyyy_mm}", str(e))
    log.info("admin %s unpublished %s", user.user_id, yyyy_mm)
    _notify_admins_of_unpublish(user, community, app, yyyy_mm)
    return _redirect("/")


def _api_schedule_archive(event: dict, user: User, community: Community | None,
                          app: Application, membership: Membership | None) -> dict:
    """Admin declares a published month 'history' (age-out). Non-destructive:
    the schedule stays live (reminders + .ics intact) but drops out of the
    default screens and the Send-Email audience. See publishing.archive_schedule."""
    if not _is_admin(user, membership):
        return _text(403, "admins only")
    if _http_method(event) != "POST":
        return _text(405, "POST required")
    yyyy_mm = _get_param(event, "month")
    if not yyyy_mm:
        return _error_redirect("/schedules", "Missing month.")
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    try:
        publishing.archive_schedule(app, yyyy_mm, archived_at=now)
    except ValueError as e:
        return _error_redirect(f"/schedules/{yyyy_mm}", str(e))
    log.info("admin %s archived %s", user.user_id, yyyy_mm)
    return _redirect("/schedules?notice=" + urllib.parse.quote(
        f"{_month_label(yyyy_mm)} moved to history. It stays viewable under "
        "Past schedules and keeps working for anyone who saved its invites."))


def _api_schedule_reactivate(event: dict, user: User, community: Community | None,
                             app: Application, membership: Membership | None) -> dict:
    """Bring an archived month back to active (the inverse of archive)."""
    if not _is_admin(user, membership):
        return _text(403, "admins only")
    if _http_method(event) != "POST":
        return _text(405, "POST required")
    yyyy_mm = _get_param(event, "month")
    if not yyyy_mm:
        return _error_redirect("/schedules", "Missing month.")
    try:
        publishing.reactivate_schedule(app, yyyy_mm)
    except ValueError as e:
        return _error_redirect(f"/schedules/{yyyy_mm}", str(e))
    log.info("admin %s reactivated %s", user.user_id, yyyy_mm)
    return _redirect("/schedules?notice=" + urllib.parse.quote(
        f"{_month_label(yyyy_mm)} is active again."))


def _notify_admins_of_unpublish(actor: User, community: Community | None,
                                app: Application, yyyy_mm: str) -> None:
    if not community:
        return
    aa_ids = {m.user_id for m in db.list_memberships_for_app(app.app_id)
              if m.app_role == "aa" and m.user_id != actor.user_id}
    if not aa_ids:
        return
    users_by_id = {u.user_id: u for u in db.list_users(community.community_id)}
    from community_organizer.providers.email import get_email_provider
    provider = get_email_provider()
    from_addr = _from_addr(actor.name, app.name)
    month_label = _month_label(yyyy_mm)
    for aa_id in aa_ids:
        aa = users_by_id.get(aa_id)
        if not aa or not aa.email or aa.email_undeliverable:
            continue
        body = (
            f"Hi {aa.name},\n\n"
            f"{actor.name} returned the {month_label} schedule to draft.\n\n"
            f"Members no longer see this schedule, and any reminder emails "
            f"not yet sent for it have been cancelled. Assignments are "
            f"preserved — the schedule can be made active again at any time.\n\n"
            f"View it at:\n"
            f"  https://{DOMAIN_NAME}/schedules/{yyyy_mm}\n\n"
            f"-- {app.name}\n"
        )
        provider.send(
            community_id=community.community_id,
            from_addr=from_addr, to_addr=aa.email,
            subject=f"{app.name} -- {actor.name} returned the "
                    f"{month_label} schedule to draft",
            body_text=body, kind="change_notification",
            related_user_id=aa_id, related_app_id=app.app_id,
            related_yyyy_mm=yyyy_mm,
        )


def _api_schedule_send_summary(event: dict, user: User, community: Community | None,
                               app: Application, membership: Membership | None) -> dict:
    if not _is_admin(user, membership):
        return _text(403, "admins only")
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method != "POST":
        return _text(405, "POST required")
    yyyy_mm = _get_param(event, "month")
    to_raw = _get_param(event, "to") or user.email
    if not yyyy_mm or not community:
        return _error_redirect("/schedules", "Missing month.")
    # Accept comma/semicolon-separated addresses
    addrs = [a.strip() for a in re.split(r"[;,]", to_raw) if a.strip()]
    if not addrs:
        return _error_redirect(f"/schedules/{yyyy_mm}",
            "Add at least one recipient.")
    subject, body_text, body_html = schedule_email.generate_schedule_email(
        community, app, yyyy_mm)
    from_addr = _from_addr(user.name, app.name if app else community.name)
    from community_organizer.providers.email import get_email_provider
    provider = get_email_provider()
    for addr in addrs:
        email_log = provider.send(
            community_id=community.community_id,
            from_addr=from_addr,
            to_addr=addr,
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            kind="other",
            related_yyyy_mm=yyyy_mm,
        )
        log.info("admin %s sent schedule summary for %s to %s, outcome=%s",
                 user.user_id, yyyy_mm, addr, email_log.outcome)
    return _redirect(f"/schedules/{yyyy_mm}")


def _serve_ics_for_assignment(event: dict, user: User,
                              community: Community | None,
                              app: Application,
                              membership: Membership | None) -> dict:
    """Serve a one-event .ics for the caller's own assignment.

    Route: ``GET /ics/<period_id>/<slot_id>``. The user must hold an
    Assignment on that slot (no admin override — admins don't need
    "someone else's" .ics from this endpoint; if they did we'd build
    a separate route). The published-state of the enclosing Schedule
    is intentionally NOT checked: this is exactly the affordance that
    lets a Recurring Commitments member add their commitment to
    their calendar without waiting for the admin to publish the week.
    """
    path = event.get("rawPath") or event.get("path") or "/"
    parts = path.split("/")
    # /ics/<period_id>/<slot_id> → ['', 'ics', '<pid>', '<sid>']
    if len(parts) != 4 or not parts[2] or not parts[3]:
        return _text(404, "not found")
    period_id, slot_id = parts[2], parts[3]

    slot = db.find_slot_in_month(app.app_id, period_id, slot_id)
    if not slot:
        return _text(404, "slot not found")
    # Confirm the user is actually on this slot.
    asgns = list(db.list_assignments_for_slot(app.app_id, period_id, slot_id))
    if not any(a.user_id == user.user_id for a in asgns):
        return _text(404, "no assignment")

    tz_name = (app.default_timezone
               or (community.default_timezone if community else None)
               or "America/New_York")
    domain = os.environ.get("DOMAIN_NAME", "community.example.org")
    cn = community.name if community else app.name
    arrival_text = (f"please arrive by {_fmt_arrival(slot)}"
                    if slot.arrival_offset_minutes else None)
    body = ical.make_event_ics(
        slot, user.user_id, user.email,
        domain=domain, community_name=cn,
        timezone=tz_name, arrival_text=arrival_text,
        alarm_minutes=user.calendar_alarm_minutes,
    )

    # File name uses the slot's date so the user's downloads folder
    # shows distinct filenames for repeat fetches across weeks.
    fname = f"{slot.local_date}-{slot.name.replace(' ', '-')}.ics"
    fname = re.sub(r"[^A-Za-z0-9._-]", "", fname) or "slot.ics"
    return {
        "statusCode": 200,
        "headers": {
            "Content-Type": "text/calendar; charset=utf-8",
            "Content-Disposition": f'attachment; filename="{fname}"',
        },
        "body": body,
    }


def _api_schedule_email_me(event: dict, user: User, community: Community | None,
                           app: Application, membership: Membership | None) -> dict:
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method != "POST":
        return _text(405, "POST required")
    if not community:
        return _text(500, "community not found")
    published = sorted(
        [s for s in db.list_schedules(app.app_id) if s.state == "published"],
        key=lambda s: s.yyyy_mm,
    )
    if not published:
        return _error_redirect("/your-schedule",
            "No active schedules to email yet.")
    from_addr = f"organizer@{os.environ.get('DOMAIN_NAME', 'community.example.org')}"
    from community_organizer.providers.email import get_email_provider
    provider = get_email_provider()
    for sch in published:
        subject, body_text, body_html = schedule_email.generate_schedule_email(
            community, app, sch.yyyy_mm)
        provider.send(
            community_id=community.community_id,
            from_addr=from_addr, to_addr=user.email,
            subject=subject, body_text=body_text, body_html=body_html,
            kind="other", related_yyyy_mm=sch.yyyy_mm,
        )
    log.info("member %s emailed schedule to self (%d months)", user.user_id, len(published))
    return _redirect("/your-schedule")


def _api_schedule_delete(event: dict, user: User, community: Community | None,
                         app: Application, membership: Membership | None) -> dict:
    if not _is_admin(user, membership):
        return _text(403, "admins only")
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method != "POST":
        return _text(405, "POST required")
    yyyy_mm = _get_param(event, "month")
    if not yyyy_mm:
        return _error_redirect("/schedules", "Missing month.")
    sch = db.get_schedule(app.app_id, yyyy_mm)
    if not sch:
        return _error_redirect("/schedules", "Schedule not found.")
    if sch.state == "published":
        return _error_redirect(f"/schedules/{yyyy_mm}",
            "Cannot delete an active schedule — return it to draft first.")
    db.delete_assignments_for_month(app.app_id, yyyy_mm)
    db.delete_slots(app.app_id, yyyy_mm)
    db.delete_notifications_for_schedule(app.app_id, yyyy_mm)
    db.delete_schedule(app.app_id, yyyy_mm)
    log.info("admin %s deleted schedule %s", user.user_id, yyyy_mm)
    return _redirect("/")


def _api_schedule_copy_from(event: dict, user: User, community: Community | None,
                            app: Application, membership: Membership | None) -> dict:
    if not _is_admin(user, membership):
        return _text(403, "admins only")
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method != "POST":
        return _text(405, "POST required")
    target_mm = _get_param(event, "month")
    source_mm = _get_param(event, "source")
    if not target_mm or not source_mm:
        return _error_redirect("/schedules", "Missing month or source.")
    from collections import defaultdict
    tgt_by_key: dict[tuple, list[Slot]] = defaultdict(list)
    for s in sorted(db.list_slots(app.app_id, target_mm),
                    key=lambda s: s.local_date):
        tgt_by_key[(s.template_id, s.day_of_week, s.start_time)].append(s)
    src_by_key: dict[tuple, list[Slot]] = defaultdict(list)
    for s in sorted(db.list_slots(app.app_id, source_mm),
                    key=lambda s: s.local_date):
        src_by_key[(s.template_id, s.day_of_week, s.start_time)].append(s)
    src_slot_map = {s.slot_id: s for s in db.list_slots(app.app_id, source_mm)}
    src_ordinal: dict[str, int] = {}
    for key, slot_list in src_by_key.items():
        for i, s in enumerate(slot_list):
            src_ordinal[s.slot_id] = i
    source_asgns = list(db.list_assignments_for_month(app.app_id, source_mm))
    copied = 0
    for a in source_asgns:
        src_slot = src_slot_map.get(a.slot_id)
        if not src_slot:
            continue
        key = (src_slot.template_id, src_slot.day_of_week, src_slot.start_time)
        ordinal = src_ordinal.get(a.slot_id, 0)
        tgt_list = tgt_by_key.get(key, [])
        if ordinal >= len(tgt_list):
            continue
        tgt_slot = tgt_list[ordinal]
        asg = Assignment(
            community_id=user.community_id, app_id=app.app_id,
            yyyy_mm=target_mm, slot_id=tgt_slot.slot_id,
            user_id=a.user_id, local_date=tgt_slot.local_date,
            created_by=user.user_id,
        )
        db.put_assignment(asg)
        copied += 1
    log.info("admin %s copied %d assignments from %s to %s",
             user.user_id, copied, source_mm, target_mm)
    return _redirect(f"/schedules/{target_mm}")


def _api_admin_assign(event: dict, user: User, community: Community | None,
                      app: Application, membership: Membership | None) -> dict:
    if not _is_admin(user, membership):
        return _text(403, "admins only")
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method != "POST":
        return _text(405, "POST required")
    yyyy_mm = _get_param(event, "month")
    slot_id = _get_param(event, "slot_id")
    user_id = _get_param(event, "user_id")
    if not (yyyy_mm and slot_id and user_id):
        return _error_redirect_or_next(event, "/",
            "Missing month, slot id, or user id.")
    slot = db.find_slot_in_month(app.app_id, yyyy_mm, slot_id)
    if not slot:
        return _error_redirect_or_next(event,
            f"/schedules/{yyyy_mm}", "Slot not found.")
    target = db.get_user(user.community_id, user_id)
    if not target:
        return _error_redirect_or_next(event,
            f"/schedules/{yyyy_mm}", "User not found.")
    # PRIVACY-AUDIT MED-3: target must be a member of this app.
    # Prevents an admin from assigning users who belong only to
    # other apps in the same community.
    if db.get_membership(app.app_id, user_id) is None:
        return _error_redirect_or_next(event,
            f"/schedules/{yyyy_mm}",
            "User is not a member of this app.")
    # Defense in depth — the UI option is already `disabled` for
    # blocked dates, but a hand-crafted POST should also be refused
    # so the admin can't override a member's declared unavailability.
    if user_id in db.list_blocked_users_on_date(app.app_id, slot.local_date):
        return _error_redirect_or_next(event,
            f"/schedules/{yyyy_mm}",
            f"{target.name} has marked {slot.local_date} as unavailable.")
    asg = Assignment(
        community_id=user.community_id, app_id=app.app_id,
        yyyy_mm=yyyy_mm, slot_id=slot_id, user_id=user_id,
        local_date=slot.local_date, created_by=user.user_id,
    )
    db.put_assignment(asg)
    log.info("admin %s assigned %s to slot %s", user.user_id, user_id, slot_id)
    # For recurring apps, ship the one-off .ics to the new assignee.
    # Mirrors _signup_assignment behavior so an admin-driven pickup
    # gets the same calendar treatment as a self-service one.
    if app.app_type == "recurring_commitments" and community is not None:
        _send_pickup_invite(target, community, app, slot, yyyy_mm)
    # The recurring home (=/) doesn't use the /schedules/ tab; the
    # admin assign picker passes next=/ so the page returns to its
    # context. Coverage flows still want the per-month schedule view.
    raw_next = _get_param(event, "next")
    if raw_next:
        return _redirect(_safe_next(raw_next))
    if app.app_type == "recurring_commitments":
        return _redirect("/")
    return _redirect(f"/schedules/{yyyy_mm}")


def _api_admin_bulk_assign(event: dict, user: User, community: Community | None,
                           app: Application, membership: Membership | None) -> dict:
    if not _is_admin(user, membership):
        return _text(403, "admins only")
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method != "POST":
        return _text(405, "POST required")
    yyyy_mm = _get_param(event, "month")
    slot_id = _get_param(event, "slot_id")
    mode = _get_param(event, "bulk_mode")
    if not (yyyy_mm and slot_id and mode):
        return _error_redirect(f"/schedules/{yyyy_mm}" if yyyy_mm
                               else "/schedules", "Missing required fields.")
    slot = db.find_slot_in_month(app.app_id, yyyy_mm, slot_id)
    if not slot:
        return _error_redirect(f"/schedules/{yyyy_mm}", "Slot not found.")
    current = list(db.list_assignments_for_slot(app.app_id, yyyy_mm, slot_id))
    assigned_ids = {a.user_id for a in current}
    cap = _slot_cap(slot)
    # Uncapped slots can always take more — skip the remaining check
    # entirely. Otherwise short-circuit when the cap is reached.
    if cap is not None and len(current) >= cap:
        return _redirect(f"/schedules/{yyyy_mm}")
    remaining = (cap - len(current)) if cap is not None else None

    # PRIVACY-AUDIT MED-4: cohort members could in theory reference
    # users outside this app — clamp targets to actual app members.
    app_member_ids = {m.user_id for m in
                      db.list_memberships_for_app(app.app_id)}
    # Honor member-declared unavailability — skip anyone who has
    # blocked this slot's date.
    blocked = db.list_blocked_users_on_date(app.app_id, slot.local_date)
    # only "cohort:<id>" mode is supported now. The
    # legacy "all" mode (add every app member) was a UI experiment
    # that turned out to add clutter without solving a real workflow;
    # the form-element and server-side branch were both removed.
    if not mode.startswith("cohort:"):
        return _error_redirect(f"/schedules/{yyyy_mm}",
            "Unsupported bulk-assign mode.")
    cohort_id = mode.split(":", 1)[1]
    # Also verify the cohort belongs to THIS app — accepting an
    # arbitrary cohort_id would let an admin enumerate or recruit
    # another app's cohort members.
    cohort = db.get_cohort(app.app_id, cohort_id)
    if cohort is None:
        return _error_redirect(f"/schedules/{yyyy_mm}",
            "Cohort not found in this app.")
    target_ids = [cm.user_id for cm in db.list_cohort_members(cohort_id)
                  if cm.user_id not in assigned_ids
                  and cm.user_id in app_member_ids
                  and cm.user_id not in blocked]

    added = 0
    for uid in target_ids:
        # remaining=None means uncapped; never short-circuit.
        if remaining is not None and added >= remaining:
            break
        target = db.get_user(user.community_id, uid)
        if not target:
            continue
        asg = Assignment(
            community_id=user.community_id, app_id=app.app_id,
            yyyy_mm=yyyy_mm, slot_id=slot_id, user_id=uid,
            local_date=slot.local_date, created_by=user.user_id,
        )
        db.put_assignment(asg)
        added += 1
    log.info("admin %s bulk-assigned %d users to slot %s (mode=%s)",
             user.user_id, added, slot_id, mode)
    return _redirect(f"/schedules/{yyyy_mm}")


def _api_admin_unassign(event: dict, user: User, community: Community | None,
                        app: Application, membership: Membership | None) -> dict:
    if not _is_admin(user, membership):
        return _text(403, "admins only")
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method != "POST":
        return _text(405, "POST required")
    yyyy_mm = _get_param(event, "month")
    slot_id = _get_param(event, "slot_id")
    user_id = _get_param(event, "user_id")
    if not (yyyy_mm and slot_id and user_id):
        return _error_redirect("/schedules",
            "Missing month, slot id, or user id.")
    slot = db.find_slot_in_month(app.app_id, yyyy_mm, slot_id)
    removed_user = db.get_user(user.community_id, user_id)
    db.delete_assignment(app.app_id, yyyy_mm, slot_id, user_id)
    log.info("admin %s unassigned %s from slot %s", user.user_id, user_id, slot_id)
    if slot and removed_user and community:
        _send_removal_notifications(removed_user, user, community, app, slot, yyyy_mm,
                                    self_release=False)
        _notify_cohort_of_opening(removed_user, community, app, slot, yyyy_mm)
    return _redirect(f"/schedules/{yyyy_mm}")


def _api_assignment_confirm(event: dict, user: User,
                            community: Community | None,
                            app: Application,
                            membership: Membership | None) -> dict:
    """Member confirms their own assignment from /your-schedule.

    Anyone can hit this endpoint, but the slot's assignment must
    actually belong to the calling user. Confirming someone else's
    assignment is silently no-op'd (no error leak about which
    assignments exist for other users).
    """
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method != "POST":
        return _text(405, "POST required")
    yyyy_mm = _get_param(event, "month")
    slot_id = _get_param(event, "slot_id")
    if not yyyy_mm or not slot_id:
        return _error_redirect("/your-schedule",
            "Missing month or slot id.")
    db.confirm_assignment(app.app_id, yyyy_mm, slot_id, user.user_id,
                          via="member_login")
    log.info("user %s confirmed own assignment slot=%s month=%s",
             user.user_id, slot_id, yyyy_mm)
    raw_next = _get_param(event, "next")
    if raw_next:
        return _redirect(_safe_next(raw_next))
    if app.app_type == "recurring_commitments":
        return _redirect("/")
    return _redirect("/your-schedule")


def _api_admin_confirm_assignment(event: dict, user: User,
                                  community: Community | None,
                                  app: Application,
                                  membership: Membership | None) -> dict:
    """Admin override: mark an assignment as confirmed.

    Used when a member confirmed out-of-band (text, call, hallway
    chat) but didn't accept the calendar invite or click Confirm in
    the app. The confirmation display design: this is the explicit, distinct
    extra step the admin takes — not the default path.
    """
    if not _is_admin(user, membership):
        return _text(403, "admins only")
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method != "POST":
        return _text(405, "POST required")
    yyyy_mm = _get_param(event, "month")
    slot_id = _get_param(event, "slot_id")
    user_id = _get_param(event, "user_id")
    if not (yyyy_mm and slot_id and user_id):
        return _error_redirect("/schedules",
            "Missing month, slot id, or user id.")
    db.confirm_assignment(app.app_id, yyyy_mm, slot_id, user_id,
                          via="admin_override")
    log.info("admin %s confirmed %s for slot %s",
             user.user_id, user_id, slot_id)
    raw_next = _get_param(event, "next")
    if raw_next:
        return _redirect(_safe_next(raw_next))
    return _redirect(f"/schedules/{yyyy_mm}")


_DAY_OPTIONS = [
    ("0", "Mon"), ("1", "Tue"), ("2", "Wed"), ("3", "Thu"),
    ("4", "Fri"), ("5", "Sat"), ("6", "Sun"),
]


def _template_form(*, action: str, tpl: SlotTemplate | None = None,
                   button_label: str = "Add event",
                   app: Application | None = None,
                   prefill_from: SlotTemplate | None = None) -> str:
    """Render the Add/Edit template form.

    Three modes share this rendering:
      - **Edit** (``tpl`` is the existing row): show its current
        values. App-level defaults are ignored.
      - **Successive add** (``prefill_from`` is the just-saved
        previous template): same day, start advanced by the prior
        duration, other values copied. App-level defaults are
        ignored — the previous row IS the source of truth here.
      - **First add** (no tpl, no prefill_from): use app-level
        Application.template_default_* if set, else hardcoded
        fallbacks. This is the very first time the admin lands on
        the Add form for a fresh app.
    """
    # Resolve each field by precedence: tpl (edit) > prefill (chain) >
    # app default > hardcoded fallback. Helper makes the chain explicit.
    def _pick(tpl_val, prefill_val, app_val, fallback):
        if tpl is not None:
            return tpl_val
        if prefill_from is not None:
            return prefill_val
        if app_val is not None:
            return app_val
        return fallback

    name_val = html.escape(tpl.name) if tpl else ""
    # Successive-add: compute the (day, start) pair together so a
    # midnight wrap also bumps the day select.
    prefill_day, prefill_start = (
        _advance_start(prefill_from.day_of_week,
                       prefill_from.start_time,
                       prefill_from.duration_minutes)
        if prefill_from else (None, None)
    )
    start_val = _pick(
        tpl.start_time if tpl else None,
        prefill_start,
        app.template_default_start_time if app else None,
        "08:00",
    )
    dur_val = _pick(
        tpl.duration_minutes if tpl else None,
        prefill_from.duration_minutes if prefill_from else None,
        app.template_default_duration_minutes if app else None,
        60,
    )
    arr_val = _pick(
        tpl.arrival_offset_minutes if tpl else None,
        prefill_from.arrival_offset_minutes if prefill_from else None,
        app.template_default_arrival_offset_minutes if app else None,
        10,
    )
    req_val = _pick(
        tpl.required_volunteers if tpl else None,
        prefill_from.required_volunteers if prefill_from else None,
        app.template_default_required_volunteers if app else None,
        2,
    )
    min_val = _pick(
        tpl.min_volunteers if tpl else None,
        prefill_from.min_volunteers if prefill_from else None,
        app.template_default_min_volunteers if app else None,
        1,
    )
    # max needs its own pick logic because None on the SlotTemplate is
    # a meaningful value (uncapped), distinct from "no source".
    if tpl is not None:
        max_val: str = "" if tpl.max_volunteers is None else str(tpl.max_volunteers)
    elif prefill_from is not None:
        max_val = ("" if prefill_from.max_volunteers is None
                   else str(prefill_from.max_volunteers))
    elif app is not None and app.template_default_max_volunteers is not None:
        max_val = str(app.template_default_max_volunteers)
    else:
        max_val = "5"
    day_val = str(_pick(
        tpl.day_of_week if tpl else None,
        prefill_day,
        app.template_default_day_of_week if app else None,
        6,
    ))
    day_opts = "".join(
        f"<option value='{v}'{' selected' if v == day_val else ''}>{lbl}</option>"
        for v, lbl in _DAY_OPTIONS
    )
    # Monthly apps need an additional "which occurrence?" picker so
    # admins can express "First Friday" / "Last Sunday" / etc. Default
    # to "First" for monthly, hidden field for weekly.
    is_monthly = app is not None and app.period_type == "monthly"
    ordinal_current = tpl.ordinal if tpl else None
    ordinal_options = [
        ("", "Every"),       # legacy ushers — every matching weekday
        ("1", "First"),
        ("2", "Second"),
        ("3", "Third"),
        ("4", "Fourth"),
        ("-1", "Last"),
    ]
    ordinal_str = ("" if ordinal_current is None else str(ordinal_current))
    ordinal_opts = "".join(
        f"<option value='{v}'"
        f"{' selected' if v == ordinal_str else ''}>{lbl}</option>"
        for v, lbl in ordinal_options
    )
    ordinal_field = (
        f"<label style='display:flex;flex-direction:column;font-size:0.85em;color:#666'>"
        f"When<select name='ordinal'>{ordinal_opts}</select></label>"
        if is_monthly else ""
    )
    tid = f"<input type='hidden' name='template_id' value='{tpl.template_id}'>" if tpl else ""
    # Preserve prefill_from so a duplicate-rejection round-trip can
    # bounce back to /admin/templates with the chain intact.
    prefill_hidden = (
        f"<input type='hidden' name='prefill_from' "
        f"value='{prefill_from.template_id}'>"
        if (tpl is None and prefill_from is not None) else ""
    )
    return (
        f"<form method='post' action='{action}' "
        "style='margin:16px 0;display:flex;gap:8px;align-items:end;flex-wrap:wrap'>"
        f"{tid}"
        f"{prefill_hidden}"
        f"<label style='display:flex;flex-direction:column;font-size:0.85em;color:#666'>"
        f"Name (optional)<input name='name' value='{name_val}' "
        "placeholder='auto-generated if blank' style='padding:4px;width:200px'></label>"
        f"{ordinal_field}"
        f"<label style='display:flex;flex-direction:column;font-size:0.85em;color:#666'>"
        f"Day<select name='day'>{day_opts}</select></label>"
        f"<label style='display:flex;flex-direction:column;font-size:0.85em;color:#666'>"
        f"Start<input type='time' name='start' value='{start_val}' required "
        "style='padding:4px'></label>"
        f"<label style='display:flex;flex-direction:column;font-size:0.85em;color:#666'>"
        f"Duration (min)<input type='number' name='duration' value='{dur_val}' "
        "min='1' style='padding:4px;width:60px'></label>"
        f"<label style='display:flex;flex-direction:column;font-size:0.85em;color:#666'>"
        f"Arrive early (min)<input type='number' name='arrival' value='{arr_val}' "
        "min='0' style='padding:4px;width:60px'></label>"
        # Required is a coverage concept (Ushers need ≥N volunteers per
        # slot). Recurring_commitments uses just "Minimum" — there's no
        # second cap above which we'd say "we have enough". Hide the
        # field; the API treats missing required as required = min_vol.
        + (f"<label style='display:flex;flex-direction:column;font-size:0.85em;color:#666'>"
           f"Required<input type='number' name='required' value='{req_val}' "
           "min='1' style='padding:4px;width:50px'></label>"
           if (app is None
               or app.app_type != "recurring_commitments")
           else "") +
        f"<label style='display:flex;flex-direction:column;font-size:0.85em;color:#666'>"
        f"Minimum<input type='number' name='min_vol' value='{min_val}' "
        "min='1' style='padding:4px;width:50px'></label>"
        f"<label style='display:flex;flex-direction:column;font-size:0.85em;color:#666' "
        "title='Leave blank for unlimited'>"
        f"Maximum<input type='number' name='max_vol' value='{max_val}' "
        "min='1' max='10' placeholder='(none)' "
        "style='padding:4px;width:60px'></label>"
        f"<button type='submit' style='padding:6px 16px;cursor:pointer'>"
        f"{button_label}</button></form>"
    )


def _bulk_template_form(app: Application) -> str:
    """Render the "Generate range of templates" collapsed fieldset
    that pre-optimizes for building long contiguous schedules (the
    Wed→Thu adoration use case)."""
    dur = app.template_default_duration_minutes or 60
    arr = app.template_default_arrival_offset_minutes or 10
    req = app.template_default_required_volunteers or 2
    mn = app.template_default_min_volunteers or 1
    mx_val = ("" if app.template_default_max_volunteers is None
              else str(app.template_default_max_volunteers))
    start_day_default = (app.template_default_day_of_week
                         if app.template_default_day_of_week is not None
                         else 6)
    start_time_default = app.template_default_start_time or "08:00"

    def _day_opts(sel: int) -> str:
        return "".join(
            f"<option value='{v}'"
            f"{' selected' if int(v) == sel else ''}>{lbl}</option>"
            for v, lbl in _DAY_OPTIONS
        )

    inp = "padding:4px"
    lbl = ("display:flex;flex-direction:column;font-size:0.85em;"
           "color:#666;gap:2px")
    return (
        "<details style='margin:16px 0;border:1px solid #ddd;"
        "border-radius:6px;background:#fafafa'>"
        "<summary style='padding:8px 12px;cursor:pointer;font-weight:600;"
        "color:#444'>"
        "Generate range of events"
        " <span style='font-weight:normal;color:#888;font-size:0.85em'>"
        "&mdash; quick-fill a long contiguous schedule</span>"
        "</summary>"
        "<form method='post' action='/api/templates/generate-range' "
        "style='padding:12px;display:flex;gap:10px;align-items:end;"
        "flex-wrap:wrap'>"
        f"<label style='{lbl}'>Start day<select name='start_day' "
        f"style='{inp}'>{_day_opts(start_day_default)}</select></label>"
        f"<label style='{lbl}'>Start time"
        f"<input type='time' name='start_time' "
        f"value='{start_time_default}' required style='{inp}'></label>"
        f"<label style='{lbl}'>End day<select name='end_day' "
        f"style='{inp}'>{_day_opts(start_day_default)}</select></label>"
        f"<label style='{lbl}'>End time"
        f"<input type='time' name='end_time' value='08:00' required "
        f"style='{inp}'></label>"
        f"<label style='{lbl}'>Each lasts (min)"
        f"<input type='number' name='length' value='{dur}' min='1' "
        f"style='{inp};width:70px'></label>"
        f"<label style='{lbl}' title='Extra minutes BETWEEN slot ends "
        "and the next start. Use for 1h-on / 30m-off patterns.'>"
        f"Gap (min)<input type='number' name='gap' value='0' min='0' "
        f"style='{inp};width:70px'></label>"
        f"<label style='{lbl}'>Arrive early"
        f"<input type='number' name='arrival' value='{arr}' min='0' "
        f"style='{inp};width:60px'></label>"
        + (f"<label style='{lbl}'>Required"
           f"<input type='number' name='required' value='{req}' min='1' "
           f"style='{inp};width:50px'></label>"
           if app.app_type != "recurring_commitments" else "") +
        f"<label style='{lbl}'>Minimum"
        f"<input type='number' name='min_vol' value='{mn}' min='1' "
        f"style='{inp};width:50px'></label>"
        f"<label style='{lbl}' title='Leave blank for unlimited'>"
        f"Maximum<input type='number' name='max_vol' value='{mx_val}' "
        f"min='1' max='99' placeholder='(none)' "
        f"style='{inp};width:60px'></label>"
        "<button type='submit' style='padding:6px 16px;cursor:pointer;"
        "background:#2a7;color:white;border:none;border-radius:4px'>"
        "Generate</button>"
        "</form></details>"
    )


def _templates_page(event: dict, user: User, community: Community | None,
                    app: Application, membership: Membership | None) -> dict:
    if not _is_admin(user, membership):
        return _html(403, _page("<p>Admins only.</p><p><a href='/'>Back</a></p>",
                                ))
    org_name = app.name if app else (community.name if community else user.community_id)
    templates = sorted(db.list_templates(app.app_id),
                       key=lambda t: (t.day_of_week, t.start_time))
    edit_id = _get_param(event, "edit")
    edit_tpl = None
    if edit_id:
        edit_tpl = db.get_template(app.app_id, edit_id)
    # ?prefill_from=<tid> seeds the Add form from the just-saved
    # template (chain mode); the _api_template_add handler redirects
    # here with that param set so a long schedule fills in fast.
    # If no explicit prefill_from but templates already exist, default
    # to the most-recently-created one so the admin can continue the
    # chain after leaving and returning. Falls back to None for a
    # truly empty app (first template lands with app/hardcoded defaults).
    prefill_id = _get_param(event, "prefill_from")
    prefill_tpl: SlotTemplate | None = None
    if prefill_id:
        prefill_tpl = db.get_template(app.app_id, prefill_id)
    elif templates:
        prefill_tpl = max(templates, key=lambda t: t.created_at)

    # "Need" column is a coverage concept; recurring apps collapse it
    # into "Min" (per-app config).
    show_required = app.app_type != "recurring_commitments"
    n_cols = 9 if show_required else 8
    rows = ""
    for t in templates:
        if edit_tpl and t.template_id == edit_tpl.template_id:
            rows += (
                f"<tr><td colspan='{n_cols}' style='padding:8px 12px;background:#f9f9f9'>"
                + _template_form(action="/api/templates/edit", tpl=edit_tpl,
                                 button_label="Save", app=app)
                + "</td></tr>"
            )
        else:
            rows += (
                "<tr>"
                f"<td style='padding:6px 12px'>{html.escape(t.name)}</td>"
                f"<td style='padding:6px 12px'>{_DAY_LABEL[t.day_of_week]}</td>"
                f"<td style='padding:6px 12px'>{_fmt_time(t.start_time)}</td>"
                f"<td style='padding:6px 12px;color:#888'>{t.duration_minutes} min</td>"
                f"<td style='padding:6px 12px;color:#888'>{t.arrival_offset_minutes} min</td>"
                + (f"<td style='padding:6px 12px;color:#888'>{t.required_volunteers}</td>"
                   if show_required else "") +
                f"<td style='padding:6px 12px;color:#888'>{t.min_volunteers}</td>"
                f"<td style='padding:6px 12px;color:#888'>"
                f"{t.max_volunteers if t.max_volunteers is not None else '&mdash;'}"
                f"</td>"
                f"<td style='padding:6px 12px;white-space:nowrap'>"
                f"<a href='/admin/templates?edit={t.template_id}' "
                "style='font-size:0.85em;color:#2a7'>edit</a>"
                " &middot; "
                f"<form method='post' action='/api/templates/delete"
                f"?template_id={t.template_id}' style='display:inline'"
                f" onsubmit=\"return confirmSubmit(this,"
                f"'Delete template: {html.escape(t.name)}?',"
                f"'Delete','#c33')\">"
                "<button type='submit' style='font-size:0.85em;cursor:pointer;"
                "color:#c33;background:none;border:none;text-decoration:underline;"
                "padding:0'>delete</button></form>"
                "</td></tr>"
            )
    event_noun = html.escape(app.event_noun) if app.event_noun else "event"
    table = ""
    if templates:
        table = (
            "<table style='border-collapse:collapse;width:100%;font-size:0.95em;"
            "margin-top:12px'>"
            "<thead><tr style='color:#888;border-bottom:1px solid #ddd'>"
            f"<th style='text-align:left;padding:6px 12px'>Name of {event_noun}</th>"
            "<th style='text-align:left;padding:6px 12px'>Day</th>"
            "<th style='text-align:left;padding:6px 12px'>Start</th>"
            "<th style='text-align:left;padding:6px 12px'>Dur</th>"
            "<th style='text-align:left;padding:6px 12px'>Early</th>"
            + ("<th style='text-align:left;padding:6px 12px'>Need</th>"
               if show_required else "") +
            "<th style='text-align:left;padding:6px 12px'>Min</th>"
            "<th style='text-align:left;padding:6px 12px'>Max</th>"
            "<th style='text-align:left;padding:6px 12px'></th>"
            "</tr></thead><tbody>" + rows + "</tbody></table>"
        )
    generated_banner = ""
    gen_raw = _get_param(event, "generated")
    if gen_raw is not None:
        try:
            n_gen = int(gen_raw)
            n_dups = int(_get_param(event, "dups") or "0")
        except ValueError:
            n_gen = n_dups = 0
        if n_gen or n_dups:
            dup_part = (f" {n_dups} duplicate"
                        f"{'s' if n_dups != 1 else ''} skipped."
                        if n_dups else "")
            generated_banner = (
                "<div style='margin:12px 0;padding:12px 16px;"
                "border:1px solid #2a7;border-radius:6px;"
                "background:#f0fff5;color:#155'>"
                f"<b>{n_gen} template{'s' if n_gen != 1 else ''} "
                "created.</b>"
                f"{dup_part}"
                "</div>"
            )

    deleted_all_banner = ""
    deleted_all_raw = _get_param(event, "deleted_all")
    if deleted_all_raw is not None:
        try:
            n_del = int(deleted_all_raw)
        except ValueError:
            n_del = 0
        deleted_all_banner = (
            "<div style='margin:12px 0;padding:12px 16px;"
            "border:1px solid #a80;border-radius:6px;background:#fffbe6;"
            "color:#704800'>"
            f"<b>{n_del} template{'s' if n_del != 1 else ''} deleted.</b> "
            "Cohorts, future slots, and assignments tied to them were "
            "also removed."
            "</div>"
        )

    dup_banner = ""
    if _get_param(event, "dup") == "1":
        dup_day_raw = _get_param(event, "dup_day") or ""
        dup_start = _get_param(event, "dup_start") or ""
        try:
            dup_day_label = _DAY_LABEL[int(dup_day_raw)]
        except (ValueError, KeyError):
            dup_day_label = dup_day_raw
        dup_banner = (
            "<div style='margin:12px 0;padding:12px 16px;border:1px solid #c33;"
            "border-radius:6px;background:#fff5f5;color:#900'>"
            f"<b>Duplicate not added.</b> A template already exists for "
            f"<b>{html.escape(dup_day_label)} {html.escape(_fmt_time(dup_start) if dup_start else '')}</b>. "
            "Edit the existing one if you need different settings."
            "</div>"
        )
    # "Delete all" button — only render when there's something to
    # delete. Two confirms (modal + native) feel right given how
    # destructive this is. Hidden silent=1 query param suppresses
    # CANCEL emails — useful during admin cleanup / testing.
    delete_all_block = ""
    n_templates = len(templates)
    if n_templates > 0:
        delete_all_block = (
            "<details style='margin-top:24px;border:1px solid #f0d080;"
            "border-radius:6px;background:#fffbe6;padding:8px 14px'>"
            "<summary style='cursor:pointer;color:#704800;"
            "font-weight:600'>Delete ALL templates</summary>"
            "<p style='color:#704800;font-size:0.85em;margin:8px 0'>"
            f"Removes every one of the {n_templates} templates in this "
            "app, plus their cohorts, cohort memberships, and any "
            "future slots + assignments tied to them. Past slots and "
            "past assignments are left alone. <b>This cannot be "
            "undone.</b></p>"
            "<form method='post' action='/api/templates/delete-all' "
            "style='display:flex;gap:14px;align-items:center;flex-wrap:wrap'"
            f" onsubmit=\"return confirmSubmit(this,"
            f"'Delete ALL {n_templates} templates in this app? "
            "This will cancel every cohort member\\'s recurring "
            "calendar invite (unless you check Silent) and remove "
            "all future slots and assignments. This cannot be "
            "undone.','Delete all','#c33')\">"
            "<label style='font-size:0.9em'>"
            "<input type='checkbox' name='silent' value='1'> "
            "Silent (skip cancellation emails — for testing)</label>"
            f"<button type='submit' style='padding:6px 16px;cursor:pointer;"
            "background:#c33;color:white;border:none;border-radius:4px'>"
            f"Delete all {n_templates}</button>"
            "</form></details>"
        )

    body = (
        f"<h1>{html.escape(org_name)}</h1>"
        "<h2 style='font-size:1.1em;color:#444'>Edit schedule template</h2>"
        "<p style='color:#888;font-size:0.85em;margin-top:4px'>"
        "Changes here apply to newly created schedules. "
        "Existing schedules can only be modified from their edit page.</p>"
        + _flash_banner_html(event)
        + generated_banner
        + deleted_all_banner
        + dup_banner
        + table
        # Bulk Generate Range is only useful for long contiguous
        # schedules (weekly recurring apps). Monthly apps typically
        # need 1-2 templates a year — the single-Add form is enough.
        + (_bulk_template_form(app) if app.period_type == "weekly" else "")
        + f"<h3 style='font-size:1em;color:#444;margin-top:24px'>Add new {event_noun}</h3>"
        + _template_form(action="/api/templates/add",
                         app=app, prefill_from=prefill_tpl)
        + delete_all_block
        + _admin_nav_bar("templates", app=app)
    )
    return _html(200, _page(body, narrow=False, title=org_name,
                            ))


def _admin_settings_page(event: dict, user: User, community: Community | None,
                         app: Application, membership: Membership | None) -> dict:
    # AA, or a CA/UA pivoted into the app. CA/UA admittance preserves the
    # access they had on the former standalone Share page (now folded in
    # here); the reminders form below stays AA-only on save.
    is_app_admin = _is_admin(user, membership)
    if not is_app_admin and user.community_role not in ("ca", "ua"):
        return _html(403, _page("<p>Admins only.</p><p><a href='/'>Back</a></p>"))
    org_name = app.name if app else (community.name if community else user.community_id)
    conflict_banner = ""
    if _get_param(event, "conflict") == "settings":
        conflict_banner = (
            "<div style='margin:12px 0;padding:12px 16px;border:1px solid #c33;"
            "border-radius:6px;background:#fff5f5;color:#900'>"
            "<b>Your settings change was not saved.</b> Another admin "
            "modified these settings while you were editing. The current "
            "values are shown below — please review and try again.</div>"
        )
    # Public-page block (link + slug + description + card image), folded in
    # from the former standalone Share tab. _ensure_slug materialises a URL
    # the first time an app's config is viewed.
    slug = _ensure_slug(app)
    share_url = f"https://{DOMAIN_NAME}/home/{slug}"
    public_section = _public_page_section(app, slug, share_url)
    # The coverage reminders/terminology form applies only to coverage-style
    # apps and stays AA-only. Event apps (date-poll / standing) show just the
    # public-page section here. CA/UA-but-not-AA viewers see the public page
    # only (as they did on the old Share tab).
    reminders = (
        _default_reminders_form(app, user=user, community=community)
        if is_app_admin and app.app_type not in EVENT_APP_TYPES else "")
    body = (
        f"<h1>{html.escape(org_name)}</h1>"
        "<h2 style='font-size:1.1em;color:#444'>Settings</h2>"
        + _flash_banner_html(event)
        + conflict_banner
        + public_section
        + reminders
        + _admin_nav_bar("settings", app=app)
    )
    return _html(200, _page(body, narrow=False, title=org_name))


def _api_template_add(event: dict, user: User, community: Community | None,
                      app: Application, membership: Membership | None) -> dict:
    if not _is_admin(user, membership):
        return _text(403, "admins only")
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method != "POST":
        return _text(405, "POST required")
    name = _get_param(event, "name") or ""
    day = _get_param(event, "day")
    start = _get_param(event, "start")
    duration = _get_param(event, "duration")
    arrival = _get_param(event, "arrival")
    required = _get_param(event, "required")
    min_vol = _get_param(event, "min_vol")
    raw_ordinal = _get_param(event, "ordinal")
    ordinal_val: int | None = None
    if raw_ordinal:
        try:
            ordinal_val = int(raw_ordinal)
        except ValueError:
            return _error_redirect("/admin/templates",
                "Ordinal must be 1, 2, 3, 4, -1 (last), or blank.")
        if ordinal_val not in (1, 2, 3, 4, -1):
            return _error_redirect("/admin/templates",
                "Ordinal must be 1, 2, 3, 4, or -1 (last).")
    if not all([day, start, duration]):
        return _error_redirect("/admin/templates", "Missing required fields.")
    if not name.strip():
        name = _auto_event_name(app.terminology or "volunteer",
                                int(day), start, ordinal=ordinal_val)
    raw_max = (_get_param(event, "max_vol") or "").strip()
    # Blank max_vol → None (uncapped). Adoration apps set it once at
    # template-creation time; coverage apps keep the existing default.
    max_vol_val: int | None
    if not raw_max:
        max_vol_val = None
    else:
        try:
            max_vol_val = int(raw_max)
        except ValueError:
            return _error_redirect("/admin/templates",
                "Max volunteers must be blank or a positive integer.")
    day_int = int(day)
    # Block accidental duplicates: same (day_of_week, start_time)
    # within this app. Prefill chains can make it easy to hit Add
    # twice for the same hour, especially when the admin is heads-down
    # building out an 18-hour schedule. Refuse and redirect back with
    # the prefill preserved so the chain doesn't break.
    if _find_existing_template_at(
            app.app_id, day_int, start, ordinal=ordinal_val) is not None:
        prefill = _get_param(event, "prefill_from") or ""
        qs = f"dup=1&dup_day={day_int}&dup_start={start}"
        if prefill:
            qs += f"&prefill_from={prefill}"
        return _redirect(f"/admin/templates?{qs}")
    # Recurring apps don't render the Required field — collapse to
    # min_volunteers. Coverage keeps the legacy default of 2.
    min_int = int(min_vol or 1)
    if required:
        req_int = int(required)
    elif app.app_type == "recurring_commitments":
        req_int = min_int
    else:
        req_int = 2
    tpl, _cohort = _create_template_with_cohort(
        community_id=user.community_id, app=app, name=name,
        day_of_week=day_int, start_time=start,
        duration_minutes=int(duration),
        arrival_offset_minutes=int(arrival or 10),
        required_volunteers=req_int,
        min_volunteers=min_int,
        max_volunteers=max_vol_val,
        ordinal=ordinal_val,
    )
    log.info("admin %s added template %s", user.user_id, tpl.template_id)
    # Successive-add chain: redirect with prefill_from set so the
    # next Add-form lands pre-populated from this template (start
    # advanced by duration, day bumped on midnight wrap).
    return _redirect(f"/admin/templates?prefill_from={tpl.template_id}")


def _find_existing_template_at(
        app_id: str, day_of_week: int, start_time: str,
        ordinal: int | None = None,
) -> SlotTemplate | None:
    """Return any template in ``app_id`` matching (day, start_time, ordinal),
    or None.

    Shared by single-Add and bulk-generate so they detect dups the
    same way. Linear scan is fine — a parish has at most ~30 templates.

    For monthly apps, ordinal participates in the match — "First Fri 7 PM"
    and "Last Fri 7 PM" coexist without colliding. For weekly apps
    callers pass ordinal=None and existing templates also have
    ordinal=None, so the comparison still works.
    """
    for existing in db.list_templates(app_id):
        if (existing.day_of_week == day_of_week
                and existing.start_time == start_time
                and existing.ordinal == ordinal):
            return existing
    return None


def _create_template_with_cohort(
    *, community_id: str, app: Application, name: str,
    day_of_week: int, start_time: str, duration_minutes: int,
    arrival_offset_minutes: int, required_volunteers: int,
    min_volunteers: int, max_volunteers: int | None,
    skip_backfill: bool = False, ordinal: int | None = None,
) -> tuple[SlotTemplate, "Cohort"]:
    """Persist a SlotTemplate and its auto-cohort. Caller is
    responsible for duplicate-checking before invoking.

    The cohort is the "people willing to take this slot" group.
    Single-Add and bulk-Generate both call this so the two paths
    can't drift on cohort naming or linkage.

    auto_reminders is forced False for recurring_commitments apps:
    cohort members already get the RRULE invite at join time, and
    materialization writes an Assignment row per week — the default
    reminder lead-times would blast every cohort member with two
    emails per week per slot. Coverage apps keep the dataclass
    default (True) so the existing Ushers behavior is preserved.
    """
    from community_organizer.core.models import Cohort
    if not (name or "").strip():
        name = _auto_event_name(
            app.terminology or "volunteer", day_of_week, start_time,
            ordinal=ordinal)
    auto_reminders = (app.app_type != "recurring_commitments")
    tpl = SlotTemplate(
        community_id=community_id, app_id=app.app_id, name=name,
        day_of_week=day_of_week, start_time=start_time,
        duration_minutes=duration_minutes,
        arrival_offset_minutes=arrival_offset_minutes,
        required_volunteers=required_volunteers,
        min_volunteers=min_volunteers,
        max_volunteers=max_volunteers,
        auto_reminders=auto_reminders,
        ordinal=ordinal,
    )
    db.put_template(tpl)
    # Cohort naming reflects ordinal so a "First Fri 7 PM" template
    # gets a cohort named after the same pattern, not just "Fri 7 PM".
    if ordinal is None:
        cohort_name = (
            f"{_DAY_SHORT[tpl.day_of_week]} {_fmt_time(tpl.start_time)}")
    else:
        cohort_name = (
            f"{_ORDINAL_SHORT.get(ordinal, '')} "
            f"{_DAY_SHORT[tpl.day_of_week]} "
            f"{_fmt_time(tpl.start_time)}").strip()
    cohort = Cohort(community_id=community_id, app_id=app.app_id,
                    name=cohort_name, linked_template_id=tpl.template_id)
    db.put_cohort(cohort)
    # For recurring apps: back-fill Slot rows in periods that have
    # already been materialized so the new template doesn't only
    # affect future un-touched periods. Coverage apps are no-ops.
    # The bulk-generate path skips this per-template and calls
    # _backfill_templates_into_materialized_periods once at the end —
    # otherwise 19 templates × N periods × ~2 scans/slot blows the
    # API Gateway 30s timeout (real production 504 hit on a fresh
    # 18-hour adoration schedule).
    if not skip_backfill:
        _backfill_templates_into_materialized_periods(
            community_id, app, [tpl])
    return tpl, cohort


def _backfill_templates_into_materialized_periods(
        community_id: str, app: Application,
        templates: list[SlotTemplate]) -> int:
    """Add Slot rows for every ``templates[i]`` to every already-
    materialized period in one batched pass.

    Skips periods in the past and existing (template_id, local_date)
    pairs (idempotent). Returns the count of slots created.

    Only operates on recurring_commitments apps; coverage apps still
    require an explicit Create-Schedule click for each month.

    Per-period work: ONE list_slots query (to build the dedup set),
    then a batch_writer flushes all new slots 25 at a time. With 19
    templates × 4 materialized periods, this is ~4 queries plus
    ~3-4 batch writes instead of the previous ~300 individual
    queries/writes that timed out at the API Gateway 30-second
    boundary.
    """
    if app.app_type != "recurring_commitments":
        return 0
    if not templates:
        return 0
    tz_name = (app.default_timezone or "UTC")
    today_str = dt.date.today().isoformat()
    created = 0
    for sch in db.list_schedules(app.app_id):
        if sch.state != "materialized":
            continue
        existing_keys = {
            (s.template_id, s.local_date)
            for s in db.list_slots(app.app_id, sch.yyyy_mm)
        }
        new_slots = scheduling.materialize(
            community_id, app.app_id, sch.yyyy_mm, tz_name,
            templates, period_type=app.period_type,
        )
        to_write = [
            s for s in new_slots
            if s.local_date >= today_str
            and (s.template_id, s.local_date) not in existing_keys
        ]
        if to_write:
            db.put_slots(to_write)
            created += len(to_write)
    if created:
        tids = ",".join(t.template_id for t in templates[:5])
        more = ("…" if len(templates) > 5 else "")
        log.info("back-filled %d slots for %d new template(s) [%s%s]",
                 created, len(templates), tids, more)
    return created


def _api_template_generate_range(event: dict, user: User,
                                 community: Community | None,
                                 app: Application,
                                 membership: Membership | None) -> dict:
    """Bulk-create a contiguous range of templates in one click.

    Inputs (form fields):
      start_day, start_time  — beginning of the range
      end_day, end_time      — exclusive end of the range; can be on
                                a later day (Wed→Thu adoration)
      length                 — minutes each generated slot lasts
      gap                    — minutes between successive slot starts
                                beyond the slot's own length (default 0)
      arrival, required, min_vol, max_vol — per-slot settings; same
                                meaning as the single-Add form

    Walk: slot starts at offset 0, then offset = length + gap, etc.
    The total range in minutes is
        ((end_day - start_day) mod 7) * 1440 + (end_minutes - start_minutes)
    Where end_day == start_day we treat end_time <= start_time as a
    full 7-day wrap (the admin probably meant "this time next week"
    but that's a bad UX — reject with a 400 to force them to pick a
    later end day).

    Duplicates (same day_of_week, start_time as an existing template)
    are silently skipped, NOT errored. The redirect includes counts
    so the page can render a banner.
    """
    if not _is_admin(user, membership):
        return _text(403, "admins only")
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method != "POST":
        return _text(405, "POST required")

    try:
        start_day = int(_get_param(event, "start_day") or "")
        start_time = (_get_param(event, "start_time") or "").strip()
        end_day = int(_get_param(event, "end_day") or "")
        end_time = (_get_param(event, "end_time") or "").strip()
        length = int(_get_param(event, "length") or "60")
        gap = int(_get_param(event, "gap") or "0")
    except ValueError:
        return _error_redirect("/admin/templates",
            "Numeric fields could not be parsed.")

    if not (0 <= start_day <= 6 and 0 <= end_day <= 6):
        return _error_redirect("/admin/templates",
            "Start day and end day must be Mon-Sun.")
    if not (start_time and end_time):
        return _error_redirect("/admin/templates",
            "Start and end time are required.")
    if length <= 0 or gap < 0:
        return _error_redirect("/admin/templates",
            "Length must be positive and gap must be 0 or greater.")

    try:
        sh, sm = (int(x) for x in start_time.split(":"))
        eh, em = (int(x) for x in end_time.split(":"))
    except ValueError:
        return _error_redirect("/admin/templates",
            "Start and end time must be HH:MM.")
    start_total = sh * 60 + sm
    end_total = eh * 60 + em

    # Total span in minutes. Same-day with end <= start is ambiguous
    # — refuse rather than guess 7-day wrap.
    day_offset = (end_day - start_day) % 7
    if day_offset == 0 and end_total <= start_total:
        return _error_redirect("/admin/templates",
            "End must be after start. For a same-day end, pick a later "
            "end time — or pick a later end day.")
    span = day_offset * 24 * 60 + (end_total - start_total)
    step = length + gap

    arr = int(_get_param(event, "arrival") or
              app.template_default_arrival_offset_minutes or 10)
    min_vol = int(_get_param(event, "min_vol") or
                  app.template_default_min_volunteers or 1)
    # Recurring apps don't render Required — it's collapsed to min_vol.
    raw_req = _get_param(event, "required")
    if raw_req:
        req = int(raw_req)
    elif app.app_type == "recurring_commitments":
        req = min_vol
    else:
        req = app.template_default_required_volunteers or 2
    raw_max = (_get_param(event, "max_vol") or "").strip()
    if not raw_max:
        max_val: int | None = app.template_default_max_volunteers
    else:
        try:
            max_val = int(raw_max)
        except ValueError:
            return _error_redirect("/admin/templates",
                "Max volunteers must be blank or a positive integer.")

    created = 0
    skipped_dups = 0
    offset = 0
    last_template_id: str | None = None
    new_templates: list[SlotTemplate] = []
    # Pre-cache existing (day, start_time) for dup detection so the
    # loop doesn't re-scan db.list_templates(app.app_id) every iter.
    existing_keys = {
        (t.day_of_week, t.start_time)
        for t in db.list_templates(app.app_id)
    }
    # Iterate until the NEXT slot would extend past end. A slot
    # starting at `offset` runs through `offset + length`; that must
    # be <= span.
    while offset + length <= span:
        slot_day, slot_hhmm = _advance_start(start_day, start_time, offset)
        if (slot_day, slot_hhmm) in existing_keys:
            skipped_dups += 1
        else:
            # skip_backfill=True so we batch-backfill ONCE at the end —
            # otherwise the per-template backfill is O(N²) and hits the
            # API Gateway 30s timeout on real 18-hour schedules.
            tpl, _cohort = _create_template_with_cohort(
                community_id=user.community_id, app=app, name="",
                day_of_week=slot_day, start_time=slot_hhmm,
                duration_minutes=length,
                arrival_offset_minutes=arr,
                required_volunteers=req,
                min_volunteers=min_vol,
                max_volunteers=max_val,
                skip_backfill=True,
            )
            new_templates.append(tpl)
            last_template_id = tpl.template_id
            existing_keys.add((slot_day, slot_hhmm))
            created += 1
        offset += step

    # One bulk backfill after all templates are written.
    _backfill_templates_into_materialized_periods(
        user.community_id, app, new_templates)

    log.info("admin %s bulk-created %d templates (%d dups skipped)",
             user.user_id, created, skipped_dups)
    qs = f"generated={created}&dups={skipped_dups}"
    # If at least one was created, set prefill_from on the last so the
    # single-Add form still has a sensible chain.
    if last_template_id:
        qs += f"&prefill_from={last_template_id}"
    return _redirect(f"/admin/templates?{qs}")


def _api_template_edit(event: dict, user: User, community: Community | None,
                       app: Application, membership: Membership | None) -> dict:
    if not _is_admin(user, membership):
        return _text(403, "admins only")
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method != "POST":
        return _text(405, "POST required")
    tid = _get_param(event, "template_id")
    if not tid:
        return _error_redirect_or_next(event, "/admin/templates",
            "Missing template id.")
    tpl = db.get_template(app.app_id, tid)
    if not tpl:
        return _error_redirect_or_next(event, "/admin/templates",
            "Template not found.")
    new_name = _get_param(event, "name")
    day = _get_param(event, "day")
    if day is not None:
        tpl.day_of_week = int(day)
    start = _get_param(event, "start")
    if start:
        tpl.start_time = start
    if new_name is not None and not new_name.strip():
        tpl.name = _auto_event_name(app.terminology or "volunteer",
                                    tpl.day_of_week, tpl.start_time)
    elif new_name:
        tpl.name = new_name
    dur = _get_param(event, "duration")
    if dur:
        tpl.duration_minutes = int(dur)
    arr = _get_param(event, "arrival")
    if arr:
        tpl.arrival_offset_minutes = int(arr)
    req = _get_param(event, "required")
    if req:
        tpl.required_volunteers = int(req)
    mv = _get_param(event, "min_vol")
    if mv:
        tpl.min_volunteers = int(mv)
    # max_vol presence is significant: blank string explicitly clears
    # the cap; absent param leaves the value alone. _get_param returns
    # None if the field wasn't in the form at all.
    mx = _get_param(event, "max_vol")
    if mx is not None:
        stripped = mx.strip()
        if not stripped:
            tpl.max_volunteers = None
        else:
            try:
                tpl.max_volunteers = int(stripped)
            except ValueError:
                return _error_redirect_or_next(event, "/admin/templates",
                    "Max volunteers must be blank or a positive integer.")
    raw_ordinal = _get_param(event, "ordinal")
    if raw_ordinal is not None:
        s = raw_ordinal.strip()
        if not s:
            tpl.ordinal = None
        else:
            try:
                ord_val = int(s)
            except ValueError:
                return _error_redirect_or_next(event, "/admin/templates",
                    "Ordinal must be 1, 2, 3, 4, -1 (last), or blank.")
            if ord_val not in (1, 2, 3, 4, -1):
                return _error_redirect_or_next(event, "/admin/templates",
                    "Ordinal must be 1, 2, 3, 4, -1 (last), or blank.")
            tpl.ordinal = ord_val
    db.put_template(tpl)
    log.info("admin %s edited template %s", user.user_id, tid)
    return _redirect_next(event, "/admin/templates")


def _cascade_delete_template_data(
        community: Community | None, app: Application,
        template_id: str, *, send_cancel_emails: bool = True,
        users_by_id: dict[str, User] | None = None,
) -> None:
    """Run the full delete-cascade for a single template.

    For recurring apps: cancel emails (optional), drop cohort
    memberships, drop the cohort, remove future Slots + their
    Assignments. For coverage apps: no-op (cleanup is admin's call
    via the per-schedule edit page). Finally deletes the template
    row itself.

    ``users_by_id`` lets callers (e.g. bulk-delete) pass a single
    pre-built lookup so we don't list_users per template.
    """
    if app.app_type == "recurring_commitments" and community is not None:
        cohort = db.get_cohort_by_template(app.app_id, template_id)
        if cohort is not None:
            if users_by_id is None:
                users_by_id = {
                    u.user_id: u
                    for u in db.list_users(community.community_id)}
            for cm in list(db.list_cohort_members(cohort.cohort_id)):
                target = users_by_id.get(cm.user_id)
                if target is not None and send_cancel_emails:
                    _send_cohort_cancel(community, app, cohort, target)
                db.delete_cohort_membership(cohort.cohort_id, cm.user_id)
            db.delete_cohort(app.app_id, cohort.cohort_id)
        today_str = dt.date.today().isoformat()
        for sch in db.list_schedules(app.app_id):
            if sch.state != "materialized":
                continue
            for slot in list(db.list_slots(app.app_id, sch.yyyy_mm)):
                if slot.template_id != template_id:
                    continue
                if slot.local_date < today_str:
                    continue
                for a in db.list_assignments_for_slot(
                        app.app_id, sch.yyyy_mm, slot.slot_id):
                    db.delete_assignment(
                        app.app_id, sch.yyyy_mm,
                        slot.slot_id, a.user_id)
                db.delete_slot(app.app_id, sch.yyyy_mm,
                               slot.local_date, slot.slot_id)
    db.delete_template(app.app_id, template_id)


def _api_template_delete(event: dict, user: User, community: Community | None,
                         app: Application, membership: Membership | None) -> dict:
    if not _is_admin(user, membership):
        return _text(403, "admins only")
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method != "POST":
        return _text(405, "POST required")
    tid = _get_param(event, "template_id")
    if not tid:
        return _error_redirect_or_next(event, "/admin/templates",
            "Missing template id.")
    _cascade_delete_template_data(community, app, tid)
    log.info("admin %s deleted template %s (recurring cascade=%s)",
             user.user_id, tid,
             app.app_type == "recurring_commitments")
    return _redirect_next(event, "/admin/templates")


def _api_templates_delete_all(event: dict, user: User,
                              community: Community | None,
                              app: Application,
                              membership: Membership | None) -> dict:
    """Wipe every template in the app via the full cascade.

    Optional ``silent=1`` query param suppresses CANCEL emails — handy
    during admin cleanup / testing where the cohort members are
    test-only and emailing them is just noise.

    All-or-nothing per the simplest implementation: loops through and
    cascades each template. With many templates × many cohort
    members, the work can be substantial — the cancel-email skip is
    the primary lever for speed.
    """
    if not _is_admin(user, membership):
        return _text(403, "admins only")
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method != "POST":
        return _text(405, "POST required")
    silent = _get_param(event, "silent") == "1"
    templates = list(db.list_templates(app.app_id))
    users_by_id: dict[str, User] | None = None
    if community is not None:
        users_by_id = {
            u.user_id: u for u in db.list_users(community.community_id)}
    for tpl in templates:
        _cascade_delete_template_data(
            community, app, tpl.template_id,
            send_cancel_emails=not silent, users_by_id=users_by_id)
    log.info("admin %s deleted ALL %d templates in app %s (silent=%s)",
             user.user_id, len(templates), app.app_id, silent)
    return _redirect(f"/admin/templates?deleted_all={len(templates)}")


def _api_slot_cancel(event: dict, user: User, community: Community | None,
                     app: Application, membership: Membership | None) -> dict:
    if not _is_admin(user, membership):
        return _text(403, "admins only")
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method != "POST":
        return _text(405, "POST required")
    yyyy_mm = _get_param(event, "month")
    slot_id = _get_param(event, "slot_id")
    cancel = _get_param(event, "cancel")
    if not (yyyy_mm and slot_id):
        return _error_redirect("/schedules", "Missing month or slot id.")
    slot = db.find_slot_in_month(app.app_id, yyyy_mm, slot_id)
    if not slot:
        return _error_redirect(f"/schedules/{yyyy_mm}", "Slot not found.")
    slot.cancelled = (cancel != "0")
    db.put_slot(slot)
    log.info("admin %s set slot %s cancelled=%s", user.user_id, slot_id, slot.cancelled)
    return _redirect(f"/schedules/{yyyy_mm}")


def _api_slot_add(event: dict, user: User, community: Community | None,
                  app: Application, membership: Membership | None) -> dict:
    if not _is_admin(user, membership):
        return _text(403, "admins only")
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method != "POST":
        return _text(405, "POST required")
    yyyy_mm = _get_param(event, "month")
    name = _get_param(event, "name")
    date_str = _get_param(event, "date")
    start = _get_param(event, "start")
    required = _get_param(event, "required")
    min_vol = _get_param(event, "min_vol")
    duration = _get_param(event, "duration")
    arrival = _get_param(event, "arrival")
    if not all([yyyy_mm, date_str, start]):
        return _error_redirect(f"/schedules/{yyyy_mm}" if yyyy_mm
                               else "/schedules",
            "Missing required fields.")
    from zoneinfo import ZoneInfo
    tz_name = (app.default_timezone or
               (community.default_timezone if community else "America/New_York"))
    tz = ZoneInfo(tz_name)
    y, mo, d = (int(x) for x in date_str.split("-"))
    local = dt.datetime(y, mo, d, int(start.split(":")[0]),
                        int(start.split(":")[1]), tzinfo=tz)
    utc = local.astimezone(dt.timezone.utc)
    if not (name or "").strip():
        name = _auto_event_name(app.terminology or "volunteer", local.weekday(), start)
    raw_max = (_get_param(event, "max_vol") or "").strip()
    one_off_max: int | None
    if not raw_max:
        one_off_max = None
    else:
        try:
            one_off_max = int(raw_max)
        except ValueError:
            return _error_redirect(f"/schedules/{yyyy_mm}",
                "Max volunteers must be blank or a positive integer.")
    slot = Slot(
        community_id=user.community_id, app_id=app.app_id,
        yyyy_mm=yyyy_mm, template_id="one-off", name=name,
        day_of_week=local.weekday(), start_time=start,
        arrival_offset_minutes=int(arrival or 10),
        duration_minutes=int(duration or 60),
        required_volunteers=int(required or 2),
        min_volunteers=int(min_vol or 1),
        max_volunteers=one_off_max,
        concrete_date=utc.isoformat(), local_date=date_str,
    )
    db.put_slot(slot)
    log.info("admin %s added one-off slot %s on %s", user.user_id, slot.slot_id, date_str)
    return _redirect(f"/schedules/{yyyy_mm}")


def _api_slot_edit(event: dict, user: User, community: Community | None,
                   app: Application, membership: Membership | None) -> dict:
    if not _is_admin(user, membership):
        return _text(403, "admins only")
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method != "POST":
        return _text(405, "POST required")
    yyyy_mm = _get_param(event, "month")
    slot_id = _get_param(event, "slot_id")
    if not (yyyy_mm and slot_id):
        return _error_redirect("/schedules", "Missing month or slot id.")
    slot = db.find_slot_in_month(app.app_id, yyyy_mm, slot_id)
    if not slot:
        return _error_redirect(f"/schedules/{yyyy_mm}", "Slot not found.")
    name = _get_param(event, "name")
    if name:
        slot.name = name
    start = _get_param(event, "start")
    if start:
        slot.start_time = start
    req = _get_param(event, "required")
    if req:
        slot.required_volunteers = int(req)
    notes = _get_param(event, "notes")
    if notes is not None:
        slot.notes = notes or None
    db.put_slot(slot)
    log.info("admin %s edited slot %s", user.user_id, slot_id)
    return _redirect(f"/schedules/{yyyy_mm}")


def _short_lead_desc(leads: list[int] | None) -> str:
    if not leads:
        return "none"
    parts = []
    for m in sorted(leads, reverse=True):
        if m >= 1440:
            parts.append(f"{m // 1440}d")
        elif m >= 60:
            parts.append(f"{m // 60}h")
        else:
            parts.append(f"{m}m")
    return ", ".join(parts)


_REMINDER_OPTIONS = [
    (1440, "1 day before"),
    (720, "12 hours before"),
    (120, "2 hours before"),
    (60, "1 hour before"),
    (30, "30 minutes before"),
]


def _settings_page(event: dict, user: User, community: Community | None,
                   app: Application, membership: Membership | None) -> dict:
    org_name = app.name if app else (community.name if community else user.community_id)
    saved = _get_param(event, "saved")
    if _get_param(event, "nophone"):
        saved_msg = ("<p style='color:#a80;margin-bottom:16px'>"
                     "Settings saved. Text reminders need a mobile number, so "
                     "delivery was left on email.</p>")
    elif saved:
        saved_msg = ("<p style='color:#2a7;margin-bottom:16px'>"
                     "Settings saved.</p>")
    else:
        saved_msg = ""

    # ---- Cohort self-service section (#219) ----
    # Members of a coverage / recurring app pick their own cohort
    # affinity here. Server-side guards in _api_cohort_add_member /
    # _api_cohort_remove_member enforce self-only and the
    # "never remove your last cohort" rule — the UI just hides
    # the affordance the same way for clarity.
    cohorts_section = ""
    if app:
        # Sort by the linked template's (day_of_week, start_time) so the
        # list reads in time order, not alpha order. Cohort names often
        # ARE the time ("Sun 8 AM"), and alpha sort puts "Sun 8 AM"
        # after "Sun 10 AM" (because "1" < "8"). See
        # feedback_event_times_chronological_not_alpha. Cohorts without
        # a linked template fall to the bottom, sorted by name.
        _LATE = (99, "99:99")  # sentinel for unlinked cohorts
        templates_by_id = {t.template_id: t
                           for t in db.list_templates(app.app_id)}
        def _time_key(c):
            tpl = templates_by_id.get(c.linked_template_id or "")
            if tpl is None:
                return (_LATE, c.name.lower())
            return ((tpl.day_of_week, tpl.start_time), c.name.lower())
        all_cohorts = sorted(db.list_cohorts(app.app_id), key=_time_key)
        my_cohort_ids = {
            cm.cohort_id for cm in db.list_cohorts_for_user(user.user_id)
        }
        my_cohort_ids &= {c.cohort_id for c in all_cohorts}
        in_count = len(my_cohort_ids)
        # Pre-fetch users + cohort members. Show all
        # cohort members under each row so people can see who's where
        # — useful context when negotiating a swap. Privacy is the
        # same as the schedule edit view (which already lists names).
        users_by_id = {u.user_id: u
                       for u in db.list_users(user.community_id)}
        cohort_member_names: dict[str, list[str]] = {}
        for c in all_cohorts:
            names = []
            for cm in db.list_cohort_members(c.cohort_id):
                m = users_by_id.get(cm.user_id)
                if m is not None:
                    names.append(m.name)
            names.sort(key=lambda n: n.lower())
            cohort_member_names[c.cohort_id] = names
        if all_cohorts:
            rows = ""
            for c in all_cohorts:
                is_in = c.cohort_id in my_cohort_ids
                if is_in:
                    status = ("<span style='color:#2a7;font-weight:600'>"
                              "&check; You are in this cohort</span>")
                    if in_count > 1:
                        action = (
                            f"<form method='post' "
                            f"action='/api/cohorts/remove-member"
                            f"?cohort_id={c.cohort_id}"
                            f"&user_id={user.user_id}&next=/settings' "
                            "style='display:inline'>"
                            "<button type='submit' style='font-size:0.85em;"
                            "cursor:pointer;color:#a80;background:none;"
                            "border:none;text-decoration:underline;"
                            "padding:0'>Remove yourself</button></form>"
                        )
                    else:
                        action = ("<span style='color:#888;font-size:0.8em'>"
                                  "(remove disabled &mdash; you must be in "
                                  "at least one cohort)</span>")
                else:
                    status = "<span style='color:#888'>&mdash;</span>"
                    action = (
                        f"<form method='post' "
                        f"action='/api/cohorts/add-member"
                        f"?cohort_id={c.cohort_id}"
                        f"&user_id={user.user_id}&next=/settings' "
                        "style='display:inline'>"
                        "<button type='submit' style='font-size:0.85em;"
                        "cursor:pointer;color:#2a7;background:none;"
                        "border:none;text-decoration:underline;"
                        "padding:0'>Add yourself</button></form>"
                    )
                names = cohort_member_names.get(c.cohort_id, [])
                if names:
                    members_text = ", ".join(html.escape(n) for n in names)
                else:
                    members_text = ("<span style='color:#bbb'>"
                                    "no members yet</span>")
                rows += (
                    f"<tr><td style='padding:6px 12px;font-weight:500;"
                    f"vertical-align:top'>{html.escape(c.name)}</td>"
                    f"<td style='padding:6px 12px;vertical-align:top'>"
                    f"{status}</td>"
                    f"<td style='padding:6px 12px;text-align:right;"
                    f"vertical-align:top'>{action}</td></tr>"
                    # Members sub-row, indented under the cohort name.
                    "<tr><td colspan='3' style='padding:0 12px 10px 28px;"
                    "font-size:0.85em;color:#666;"
                    "border-bottom:1px solid #f0f0f0'>"
                    f"<i>Members:</i> {members_text}</td></tr>"
                )
            cohorts_section = (
                "<h2 style='font-size:1.1em;color:#444'>"
                "Cohorts and their members</h2>"
                "<p style='color:#666;font-size:0.9em;margin:4px 0 12px 0'>"
                "Cohorts group members by which slots they normally serve. "
                "Joining a cohort lets the admin pick you from a shorter "
                "list when filling that slot's date. You can be in more "
                "than one — and you can see everyone else's cohort here, "
                "which is handy when you need to swap a slot with someone."
                "</p>"
                "<table style='border-collapse:collapse;width:100%;"
                "font-size:0.95em;margin-bottom:24px'>"
                f"<tbody>{rows}</tbody></table>"
            )

    current_leads = set(user.lead_times_minutes or [1440, 120])
    checks = "".join(
        f"<label style='display:block;margin:6px 0'>"
        f"<input type='checkbox' name='lead' value='{mins}'"
        f"{' checked' if mins in current_leads else ''}> "
        f"{label}</label>"
        for mins, label in _REMINDER_OPTIONS
    )
    alarm_options = [
        (None, "None"),
        (15, "15 minutes before"),
        (30, "30 minutes before"),
        (60, "1 hour before"),
        (120, "2 hours before"),
        (1440, "1 day before"),
    ]
    current_alarm = user.calendar_alarm_minutes
    alarm_select = "".join(
        "<option value='{}'{}>{}</option>".format(
            mins if mins is not None else "",
            " selected" if mins == current_alarm else "",
            label,
        )
        for mins, label in alarm_options
    )
    # How-we-reach-you: phone + channel. SMS is reminders-only (calendar
    # invites + scheduling always go by email since .ics doesn't work over
    # text). Whether a chosen "text" actually sends is also gated server-side
    # by the rollout allowlist — the UI shows the option regardless and the
    # notifier falls back to email when SMS isn't available, so a member is
    # never left without a reminder.
    chan = user.channel if user.channel in ("email", "sms", "both") else "email"
    channel_radios = "".join(
        "<label style='display:block;margin:4px 0'>"
        f"<input type='radio' name='channel' value='{val}'"
        f"{' checked' if chan == val else ''}> {lbl}</label>"
        for val, lbl in (("email", "Email only"),
                         ("sms", "Text message (SMS)"),
                         ("both", "Both email and text"))
    )
    contact_section = (
        "<h3 style='font-size:1em;color:#555'>How we reach you</h3>"
        "<label style='display:block;margin:4px 0'>Mobile number "
        f"<input type='tel' name='phone' value='{html.escape(user.phone or '')}' "
        "placeholder='(703) 555-1234' style='padding:6px;width:200px'></label>"
        "<p style='color:#888;font-size:0.85em;margin:4px 0 8px 0'>"
        "Needed only if you want text reminders. US numbers.</p>"
        "<div style='margin-top:8px'>Send my reminders by:</div>"
        + channel_radios
    )
    # Submit-time validation, two parts:
    #  (1) Coherence guard (ALL users): you can't choose Text/Both without a
    #      phone — block the submit. Server re-checks (see _api_settings_save).
    #  (2) Member nudge (non-admins only): when a member ADDS a phone
    #      (empty->filled) but leaves delivery on email, pop an OPTIONAL
    #      reminder that SMS exists. Only fires on the empty->filled transition
    #      (defaultValue = saved value), so it never nags an email-on-purpose
    #      user. Admins skip the nudge (they set this up).
    is_admin = _is_admin(user, membership)
    settings_check_script = (
        "<script>var _memberNudge=" + ("false" if is_admin else "true") + ";"
        "function _settingsCheck(f){try{"
        "var p=f.phone,c=f.querySelector('input[name=channel]:checked');"
        "var ch=c?c.value:'email',ph=p?p.value.trim():'';"
        "if((ch==='sms'||ch==='both')&&!ph){"
        "alert('Add a mobile number above to receive text reminders.');"
        "return false;}"
        "if(_memberNudge&&ph&&p&&!p.defaultValue.trim()&&ch==='email'){"
        "alert('You added a phone number. To get text (SMS) reminders, set "
        "\\u201cSend my reminders by\\u201d to \\u201cText message\\u201d or "
        "\\u201cBoth\\u201d, then Save again. This is optional \\u2014 you can "
        "leave it on email.');}"
        "}catch(e){}return true;}</script>"
    )
    form_open = ("<form method='post' action='/api/settings/save' "
                 "style='text-align:left;margin-top:16px' "
                 "onsubmit='return _settingsCheck(this)'>")
    body = (
        f"<h1>{html.escape(org_name)}</h1>"
        + _flash_banner_html(event)
        + saved_msg
        + cohorts_section
        + settings_check_script
        + "<h2 style='font-size:1.1em;color:#444'>Your notification settings</h2>"
        + form_open
        + contact_section
        + "<h3 style='font-size:1em;color:#555;margin-top:20px'>"
        "Reminders before each event:</h3>"
        + checks
        + "<h3 style='font-size:1em;color:#555;margin-top:20px'>"
        "Calendar pop-up reminder:</h3>"
        "<p style='color:#888;font-size:0.85em;margin:4px 0 8px 0'>"
        "Built into each calendar invitation. Useful if you don't want email "
        "reminders, or as a backup.</p>"
        f"<select name='alarm' style='padding:4px;font-size:1em'>"
        f"{alarm_select}</select>"
        + "<p style='margin-top:16px'>"
        "<button type='submit' style='padding:8px 24px;cursor:pointer;"
        "font-size:1em'>Save settings</button></p>"
        "</form>"
        + (_admin_nav_bar("my-settings", app=app) if _is_admin(user, membership)
           else "<p style='margin-top:24px'><a href='/'>Back to Home</a></p>")
    )
    return _html(200, _page(body, title=org_name,
                            ))


def _api_settings_save(event: dict, user: User, community: Community | None,
                       app: Application, membership: Membership | None) -> dict:
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method != "POST":
        return _text(405, "POST required")
    body_str = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body_str = base64.b64decode(body_str).decode("utf-8", errors="replace")
    parsed = urllib.parse.parse_qs(body_str)
    selected = [int(v) for v in parsed.get("lead", [])]
    user.lead_times_minutes = sorted(selected, reverse=True) if selected else []
    alarm_raw = parsed.get("alarm", [""])[0]
    user.calendar_alarm_minutes = int(alarm_raw) if alarm_raw else None
    # Contact + delivery channel. Phone stored as entered (the notifier
    # normalizes to E.164 at send time); blank clears it.
    phone = (parsed.get("phone", [""])[0] or "").strip()
    user.phone = phone or None
    channel = parsed.get("channel", [""])[0]
    # Coherence guard: text delivery requires a deliverable number. If sms/both
    # is chosen without a valid phone, downgrade to email so we never persist an
    # SMS preference that can't be honored (matches the notifier's gate). The
    # client blocks this first; this is the no-JS / bypass backstop.
    downgraded = False
    if channel in ("sms", "both") and not to_e164(user.phone):
        channel = "email"
        downgraded = True
    if channel in ("email", "sms", "both"):
        user.channel = channel
    db.put_user(user)
    log.info("user %s updated lead_times=%s alarm=%s channel=%s phone=%s",
             user.user_id, user.lead_times_minutes, user.calendar_alarm_minutes,
             user.channel, bool(user.phone))
    return _redirect("/settings?saved=1" + ("&nophone=1" if downgraded else ""))


def _template_defaults_fieldset(app: Application) -> str:
    """Render the "Defaults for new event templates" section of the
    Settings form.

    Each field shows the app's stored default if any, with a blank
    placeholder otherwise. A blank submit clears the default (back
    to the hardcoded form fallback). The form posts to the same
    /api/settings/defaults endpoint as the rest of Settings.
    """
    def _val(v) -> str:
        return "" if v is None else str(v)

    day_opts = "".join(
        f"<option value='{v}'"
        f"{' selected' if app.template_default_day_of_week == v else ''}>"
        f"{lbl}</option>"
        for v, lbl in _DAY_OPTIONS
    )
    return (
        "<fieldset style='border:1px solid #ddd;border-radius:8px;"
        "padding:16px;margin-top:24px;margin-bottom:24px'>"
        "<legend style='font-weight:600;color:#444;padding:0 8px'>"
        "Defaults for new event templates</legend>"
        "<p style='color:#888;font-size:0.85em;margin:0 0 8px 0'>"
        "Pre-fill the Add Template form. Leave any field blank for "
        "no app-level default (the hardcoded fallback takes over). "
        "After you create the first template, each subsequent Add "
        "form pre-fills from the previous template — these defaults "
        "only seed the very first one.</p>"
        "<label style='display:block;margin:8px 0'>Day of week<br>"
        "<select name='tpl_default_day' style='padding:6px;width:160px'>"
        "<option value=''>(no default)</option>"
        f"{day_opts}"
        "</select></label>"
        "<label style='display:block;margin:8px 0'>Start time (HH:MM)<br>"
        f"<input type='time' name='tpl_default_start' "
        f"value='{_val(app.template_default_start_time)}' "
        "style='padding:6px;width:120px'></label>"
        "<label style='display:block;margin:8px 0'>Duration (min)<br>"
        f"<input type='number' name='tpl_default_duration' "
        f"value='{_val(app.template_default_duration_minutes)}' "
        "min='1' style='padding:6px;width:80px'></label>"
        "<label style='display:block;margin:8px 0'>Arrive early (min)<br>"
        f"<input type='number' name='tpl_default_arrival' "
        f"value='{_val(app.template_default_arrival_offset_minutes)}' "
        "min='0' style='padding:6px;width:80px'></label>"
        + ("<label style='display:block;margin:8px 0'>Required volunteers<br>"
           f"<input type='number' name='tpl_default_required' "
           f"value='{_val(app.template_default_required_volunteers)}' "
           "min='1' style='padding:6px;width:80px'></label>"
           if app.app_type != "recurring_commitments" else "") +
        "<label style='display:block;margin:8px 0'>Minimum volunteers<br>"
        f"<input type='number' name='tpl_default_min' "
        f"value='{_val(app.template_default_min_volunteers)}' "
        "min='1' style='padding:6px;width:80px'></label>"
        "<label style='display:block;margin:8px 0'>Maximum volunteers<br>"
        f"<input type='number' name='tpl_default_max' "
        f"value='{_val(app.template_default_max_volunteers)}' "
        "min='1' style='padding:6px;width:80px'>"
        " <span style='color:#888;font-size:0.85em'>"
        "Leave blank for hardcoded fallback (5). To default to "
        "uncapped (no max), clear the field manually on the first "
        "template you add and successive prefill carries it forward."
        "</span></label>"
        "</fieldset>"
    )


def _default_reminders_form(app: Application,
                            user: User | None = None,
                            community: Community | None = None) -> str:
    current = set(app.default_lead_times or [1440, 120])
    checks = "".join(
        f"<label style='display:inline-block;margin:4px 12px 4px 0'>"
        f"<input type='checkbox' name='lead' value='{mins}'"
        f"{' checked' if mins in current else ''}> "
        f"{label}</label>"
        for mins, label in _REMINDER_OPTIONS
    )
    trade_checked = "checked" if app.trade_default_release else ""
    trade_unchecked = "" if app.trade_default_release else "checked"

    derived_plural = _pluralize(app.event_noun or "event")
    plural_placeholder = (f"derived: {derived_plural}"
                          if not app.event_noun_plural else "")
    derived_term_plural = _pluralize(app.terminology or "volunteer")
    term_plural_placeholder = (f"derived: {derived_term_plural}"
                               if not app.terminology_plural else "")

    # Task #168: CA viewing inside an app sees the same screens an
    # AA would (per user direction: "identical screens help with
    # support / troubleshooting"). Community name, timezone, and app
    # name editing all move to CA mode (/admin/apps and
    # /admin/community-users). What was once an in-app CA-only
    # widget block becomes a one-line pointer.
    community_section = (
        "<p style='color:#888;font-size:0.85em;margin:8px 0 16px 0'>"
        "Community name, timezone, and app name are edited from "
        "<a href='/admin/apps' style='color:#2a7'>Community admin "
        "&rarr; Apps</a> (CA / UA only)."
        "</p>"
        if (user and user.community_role in ("ca", "ua") and community)
        else ""
    )

    return (
        "<form method='post' action='/api/settings/defaults' "
        "style='margin:8px 0'>"
        f"<input type='hidden' name='version' value='{app.version or 0}'>"
        + community_section
        + "<fieldset style='border:1px solid #ddd;border-radius:8px;"
        "padding:16px;margin-bottom:24px'>"
        "<legend style='font-weight:600;color:#444;padding:0 8px'>"
        "Application settings</legend>"
        "<p style='color:#888;font-size:0.85em;margin:0 0 8px 0'>"
        f"How this application <b>{html.escape(app.name)}</b> refers to "
        "its events and volunteers in the UI and in emails.</p>"
        "<label style='display:block;margin:8px 0'>Description<br>"
        f"<textarea name='app_description' rows='2' "
        "style='padding:6px;width:480px;max-width:100%;font-size:1em;"
        "border:1px solid #ccc;border-radius:4px;font-family:inherit' "
        "placeholder='Short blurb shown on the launcher when a "
        "user has multiple apps.'>"
        f"{html.escape(app.description or '')}</textarea>"
        " <span style='color:#888;font-size:0.85em'>"
        "Optional. Shown on the cross-app launcher.</span></label>"
        "<label style='display:block;margin:8px 0'>Event name (singular)<br>"
        f"<input type='text' name='event_noun' "
        f"value='{html.escape(app.event_noun or '')}' "
        "placeholder='event' "
        "style='padding:6px;width:240px;font-size:1em;border:1px solid #ccc;"
        "border-radius:4px'></label>"
        "<label style='display:block;margin:8px 0'>Event name (plural)<br>"
        f"<input type='text' name='event_noun_plural' "
        f"value='{html.escape(app.event_noun_plural or '')}' "
        f"placeholder='{html.escape(plural_placeholder)}' "
        "style='padding:6px;width:240px;font-size:1em;border:1px solid #ccc;"
        "border-radius:4px'>"
        " <span style='color:#888;font-size:0.85em'>"
        "Leave blank to auto-derive.</span></label>"
        "<label style='display:block;margin:8px 0'>Volunteer terminology "
        "(singular)<br>"
        f"<input type='text' name='terminology' "
        f"value='{html.escape(app.terminology or '')}' "
        "placeholder='volunteer' "
        "style='padding:6px;width:240px;font-size:1em;border:1px solid #ccc;"
        "border-radius:4px'></label>"
        "<label style='display:block;margin:8px 0'>Volunteer terminology "
        "(plural)<br>"
        f"<input type='text' name='terminology_plural' "
        f"value='{html.escape(app.terminology_plural or '')}' "
        f"placeholder='{html.escape(term_plural_placeholder)}' "
        "style='padding:6px;width:240px;font-size:1em;border:1px solid #ccc;"
        "border-radius:4px'>"
        " <span style='color:#888;font-size:0.85em'>"
        "Leave blank to auto-derive.</span></label>"
        "<label style='display:block;margin:8px 0'>Arrival label "
        "(e.g. 'please arrive by')<br>"
        f"<input type='text' name='arrival_label' "
        f"value='{html.escape(app.arrival_label or '')}' "
        "placeholder='please arrive by' "
        "style='padding:6px;width:300px;font-size:1em;border:1px solid #ccc;"
        "border-radius:4px'></label>"
        "</fieldset>"
        + "<h3 style='font-size:1em;color:#444;margin-top:24px'>"
        "Default reminder settings for new members</h3>"
        "<p style='color:#888;font-size:0.85em'>Existing members keep their "
        "own settings. This only affects newly added members.</p>"
        + checks
        + "<h3 style='font-size:1em;color:#444;margin-top:24px'>"
        "Trade default behavior</h3>"
        "<p style='color:#888;font-size:0.85em'>When a member initiates a trade, "
        "which option is pre-selected?</p>"
        f"<label style='display:block;margin:4px 0'>"
        f"<input type='radio' name='trade_default' value='release' {trade_checked}> "
        "Release slot immediately and offer to trade (recommended)</label>"
        f"<label style='display:block;margin:4px 0'>"
        f"<input type='radio' name='trade_default' value='keep' {trade_unchecked}> "
        "Keep slot while looking for a trade</label>"
        + _template_defaults_fieldset(app)
        + "<h3 style='font-size:1em;color:#444;margin-top:24px'>"
        "Automated cohort emails</h3>"
        "<p style='color:#888;font-size:0.85em'>When the system "
        "automatically sends the same email to multiple cohort members "
        "(e.g., a slot opening), should everyone be on the same email so "
        "they can see each other and reply-all? Otherwise each person "
        "receives a separate email. (Admin Send Email always uses "
        "multi-recipient mode for cohort or individual sends, regardless "
        "of this setting.)</p>"
        f"<label style='display:block;margin:4px 0'>"
        f"<input type='checkbox' name='group_email_mode' value='1'"
        f"{' checked' if app.group_email_mode else ''}> "
        "Send one shared email with all recipients visible "
        "(includes App Admins for reply-all)</label>"
        " <button type='submit' style='padding:4px 12px;cursor:pointer;"
        "margin-top:12px'>Save settings</button></form>"
    )


def _api_settings_defaults(event: dict, user: User, community: Community | None,
                           app: Application, membership: Membership | None) -> dict:
    if not _is_admin(user, membership):
        return _text(403, "admins only")
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method != "POST":
        return _text(405, "POST required")
    body_str = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body_str = base64.b64decode(body_str).decode("utf-8", errors="replace")
    parsed = urllib.parse.parse_qs(body_str, keep_blank_values=True)
    expected_version_raw = parsed.get("version", ["0"])[0]
    try:
        expected_version = int(expected_version_raw)
    except ValueError:
        expected_version = 0
    selected = [int(v) for v in parsed.get("lead", [])]
    app.default_lead_times = sorted(selected, reverse=True) if selected else []
    trade_val = parsed.get("trade_default", ["release"])[0]
    app.trade_default_release = (trade_val == "release")
    app.group_email_mode = parsed.get("group_email_mode", [""])[0] == "1"
    # Application identity fields
    new_event_noun = parsed.get("event_noun", [""])[0].strip()
    if new_event_noun:
        app.event_noun = new_event_noun
    app.event_noun_plural = parsed.get("event_noun_plural", [""])[0].strip()
    # Optional admin-supplied description; surfaced on /launcher.
    # No length cap server-side — the form's textarea is 2 rows so
    # admins self-limit; if they paste a paragraph it'll just wrap.
    app.description = parsed.get("app_description", [""])[0].strip()
    new_terminology = parsed.get("terminology", [""])[0].strip()
    if new_terminology:
        app.terminology = new_terminology
    app.terminology_plural = parsed.get("terminology_plural", [""])[0].strip()
    new_arrival_label = parsed.get("arrival_label", [""])[0].strip()
    if new_arrival_label:
        app.arrival_label = new_arrival_label
    # Task #168: app_name editing moved to CA mode (/admin/apps).
    # The form no longer surfaces this field; ignore any value
    # submitted from a stale client.

    # Per-app template defaults. Each field maps blank → None
    # (no app default; form falls back to hardcoded). int parse errors
    # silently drop to None for now — settings save is best-effort.
    def _maybe_int(key: str) -> int | None:
        raw = parsed.get(key, [""])[0].strip()
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    def _maybe_str(key: str) -> str | None:
        raw = parsed.get(key, [""])[0].strip()
        return raw or None

    app.template_default_day_of_week = _maybe_int("tpl_default_day")
    app.template_default_start_time = _maybe_str("tpl_default_start")
    app.template_default_duration_minutes = _maybe_int("tpl_default_duration")
    app.template_default_arrival_offset_minutes = _maybe_int("tpl_default_arrival")
    app.template_default_min_volunteers = _maybe_int("tpl_default_min")
    # Recurring apps don't render the Required field; mirror min into
    # required so the underlying data stays consistent.
    if app.app_type == "recurring_commitments":
        app.template_default_required_volunteers = (
            app.template_default_min_volunteers)
    else:
        app.template_default_required_volunteers = (
            _maybe_int("tpl_default_required"))
    app.template_default_max_volunteers = _maybe_int("tpl_default_max")

    try:
        db.put_application(app, expected_version=expected_version)
    except db.ConcurrencyConflict:
        return _redirect("/admin/settings?conflict=settings")
    # Task #168: community name + timezone editing moved to CA mode.
    # The in-app settings form no longer renders those fields.
    log.info("admin %s updated app settings: noun=%s plural=%s leads=%s "
             "trade_release=%s group_email=%s",
             user.user_id, app.event_noun, app.event_noun_plural,
             app.default_lead_times, app.trade_default_release,
             app.group_email_mode)
    return _redirect("/admin/settings")


def _users_page(event: dict, user: User, community: Community | None,
                app: Application, membership: Membership | None) -> dict:
    if not _is_admin(user, membership):
        return _html(403, _page("<p>Admins only.</p><p><a href='/'>Back</a></p>",
                                ))
    org_name = app.name if app else (community.name if community else user.community_id)
    conflict_name = _get_param(event, "conflict")
    conflict_banner = (
        f"<div style='margin:12px 0;padding:12px 16px;border:1px solid #c33;"
        f"border-radius:6px;background:#fff5f5;color:#900'>"
        f"<b>Your edit was not saved.</b> "
        f"Another admin changed <b>{html.escape(conflict_name)}</b> while "
        f"you were editing. The current values are shown below — please "
        f"review and try your edit again.</div>"
        if conflict_name else ""
    )
    # Filter to actual app members. The previous version listed every
    # user in the community and showed a per-user "role" column with
    # add/promote/remove affordances — that surfaced names + emails
    # of users in OTHER apps to this app's AAs (see PRIVACY-AUDIT.md
    # HIGH-1). Now this page shows ONLY users with a Membership row
    # for app.app_id. CAs/UAs use /admin/community-users to do
    # cross-app roster work.
    memberships = {m.user_id: m for m in db.list_memberships_for_app(app.app_id)}
    all_users = sorted(
        (u for u in db.list_users(user.community_id)
         if u.user_id in memberships),
        key=lambda u: u.name.lower(),
    )
    # Household (couple/family) visibility for AAs — read-only. Show, per
    # member, the OTHER members of their household who are ALSO in THIS app,
    # scoped to app members so an AA never sees a tie to someone outside their
    # app (households are mild PII). Creating/editing links stays a CA action
    # on /admin/households.
    hh_app: dict[str, list[User]] = {}
    for u in all_users:
        if u.household_id:
            hh_app.setdefault(u.household_id, []).append(u)

    def _household_note(u: User) -> str:
        if not u.household_id:
            return ""
        others = sorted(m.name for m in hh_app.get(u.household_id, [])
                        if m.user_id != u.user_id)
        if not others:
            return ""
        return ("<div style='font-size:0.8em;color:#2a7;margin-top:2px;"
                "white-space:normal'>Household: "
                + html.escape(", ".join(others)) + "</div>")
    # Event apps (standing/flexible) don't use cohorts or per-member lead-time
    # reminders — hide those columns/affordances. Skipping the cohort fetch
    # also leaves user_cohorts empty, which suppresses the cohort chips below.
    is_event = app.app_type in EVENT_APP_TYPES
    cohorts = [] if is_event else list(db.list_cohorts(app.app_id))
    cohorts_by_id = {c.cohort_id: c for c in cohorts}
    user_cohorts: dict[str, list[str]] = {}
    for c in cohorts:
        for cm in db.list_cohort_members(c.cohort_id):
            user_cohorts.setdefault(cm.user_id, []).append(c.name)
    edit_id = _get_param(event, "edit")

    rows = ""
    for idx, u in enumerate(all_users):
        # Anchor the post-save scroll at the row N rows above (see
        # _NEXT_ROW_OFFSET) so the edited row lands a bit lower in the
        # viewport, clear of the corner overlay.
        anchor_uid = all_users[max(0, idx - _NEXT_ROW_OFFSET)].user_id
        mem = memberships.get(u.user_id)
        app_role = mem.app_role if mem else "member"
        if edit_id and u.user_id == edit_id:
            rows += (
                f"<tr id='user-{u.user_id}' "
                "style='scroll-margin-top:120px'>"
                "<td colspan='6' style='padding:8px 12px;background:#f9f9f9'>"
                "<form method='post' action='/api/users/edit' "
                "style='display:flex;gap:8px;align-items:end;flex-wrap:wrap'>"
                f"<input type='hidden' name='user_id' value='{u.user_id}'>"
                f"<input type='hidden' name='version' value='{u.version or 0}'>"
                f"<input type='hidden' name='next' "
                f"value='/admin/users#user-{anchor_uid}'>"
                "<label style='display:flex;flex-direction:column;font-size:0.85em;color:#666'>"
                f"Name<input name='name' value='{html.escape(u.name)}' required "
                "style='padding:4px;width:140px'></label>"
                "<label style='display:flex;flex-direction:column;font-size:0.85em;color:#666'>"
                f"Email<input name='email' value='{html.escape(u.email)}' required "
                "style='padding:4px;width:180px'></label>"
                "<label style='display:flex;flex-direction:column;font-size:0.85em;color:#666'>"
                f"Phone<input name='phone' value='{html.escape(u.phone or '')}' "
                "style='padding:4px;width:120px'></label>"
                "<label style='display:flex;flex-direction:column;font-size:0.85em;color:#666'>"
                f"Notes<input name='notes' value='{html.escape(u.notes or '')}' "
                "style='padding:4px;width:140px'></label>"
                + ("" if is_event else (
                    "<div style='display:flex;flex-direction:column;font-size:0.85em;color:#666'>"
                    "Reminders<div style='display:flex;gap:8px;flex-wrap:wrap;margin-top:2px'>"
                    + "".join(
                        f"<label><input type='checkbox' name='lead' value='{mins}'"
                        f"{' checked' if mins in (u.lead_times_minutes or []) else ''}> "
                        f"{lbl}</label>"
                        for mins, lbl in _REMINDER_OPTIONS
                    )
                    + "</div></div>"))
                + "<button type='submit' style='padding:4px 12px;cursor:pointer'>"
                "Save</button>"
                " <a href='/admin/users' style='font-size:0.85em'>cancel</a>"
                "</form></td></tr>"
            )
        else:
            bounce_cell = ""
            if u.email_undeliverable:
                # Use a POST form (button styled as a link) — clearing the
                # bounce is a state-changing action and must not be CSRF-able
                # via <img src> on any other site (security fix M3).
                bounce_cell = (
                    " <span style='color:#c33;font-size:0.8em'>(bouncing "
                    f"<form method='post' action='/api/users/clear-bounce?user_id={u.user_id}' "
                    "style='display:inline'>"
                    f"<input type='hidden' name='next' "
                    f"value='/admin/users#user-{anchor_uid}'>"
                    "<button type='submit' style='background:none;border:0;color:#2a7;"
                    "cursor:pointer;font:inherit;padding:0;text-decoration:underline'>"
                    "clear</button></form>)</span>"
                )
            is_self = (u.user_id == user.user_id)
            role_label = _ROLE_LABEL.get(app_role, app_role)
            if is_self:
                role_action = " <span style='font-size:0.75em;color:#999'>(you)</span>"
            elif app_role == "member":
                role_action = (
                    f" <form method='post' action='/api/users/toggle-membership"
                    f"?user_id={u.user_id}&app_role=aa' style='display:inline'>"
                    f"<input type='hidden' name='next' "
                    f"value='/admin/users#user-{anchor_uid}'>"
                    "<button type='submit' style='font-size:0.75em;cursor:pointer;"
                    "color:#2a7;background:none;border:none;text-decoration:underline;"
                    "padding:0'>make admin</button></form>"
                )
            else:
                role_action = (
                    f" <form method='post' action='/api/users/toggle-membership"
                    f"?user_id={u.user_id}&app_role=member' style='display:inline'>"
                    f"<input type='hidden' name='next' "
                    f"value='/admin/users#user-{anchor_uid}'>"
                    "<button type='submit' style='font-size:0.75em;cursor:pointer;"
                    "color:#a80;background:none;border:none;text-decoration:underline;"
                    "padding:0'>demote</button></form>"
                )
            # Community-admin (CA) badge only — informational. The
            # promote/demote-CA ACTION is deliberately NOT offered on this
            # per-app roster; it lives exclusively on /admin/community-users
            # (the CA-only member list). Making someone a Community Admin is a
            # sensitive, community-wide change and must not be one click away
            # on an app member list, where it's easy to hit by accident
            # for a sensitive community-wide role change.
            if u.community_role == "ca" and not is_self:
                role_label = f"{role_label} + CA"
            _act = "font-size:0.85em;cursor:pointer;background:none;border:none;text-decoration:underline;padding:0"
            actions = [
                f"<a href='/admin/users?edit={u.user_id}#user-{anchor_uid}' "
                f"style='font-size:0.85em;color:#2a7'>edit</a>",
            ]
            if not is_self and u.cognito_sub:
                actions.append(
                    f"<form method='post' action='/api/users/reset-access"
                    f"?user_id={u.user_id}' style='display:inline'"
                    f" onsubmit=\"return confirmSubmit(this,"
                    f"'Reset access for {html.escape(u.name)}? Their "
                    f"password will be reset and they will be signed out "
                    f"of all devices. They can recover access via the "
                    f"\\x27New user or forgot your password?\\x27 link.',"
                    f"'Reset access','#a80')\">"
                    f"<input type='hidden' name='next' "
                    f"value='/admin/users#user-{anchor_uid}'>"
                    f"<button type='submit' style='{_act};color:#a80'>"
                    "reset access</button></form>"
                )
            if not is_self:
                actions.append(
                    f"<form method='post' action='/api/users/remove-from-app"
                    f"?user_id={u.user_id}' style='display:inline'"
                    f" onsubmit=\"return confirmSubmit(this,"
                    f"'Remove {html.escape(u.name)} from this app? "
                    f"They will remain in the community.','Remove','#a80')\">"
                    f"<input type='hidden' name='next' "
                    f"value='/admin/users#user-{anchor_uid}'>"
                    f"<button type='submit' style='{_act};color:#a80'>"
                    "remove from app</button></form>"
                )
                # Task #168: delete-from-community is a CA-mode action
                # only. Removed from this in-app screen — CAs use
                # /admin/community-users.
            actions_html = "<br>".join(actions)
            rows += (
                f"<tr id='user-{u.user_id}' style='scroll-margin-top:120px'>"
                f"<td style='padding:6px 12px;white-space:nowrap;text-align:left'>"
                f"{html.escape(u.name)}{bounce_cell}{_household_note(u)}</td>"
                f"<td style='padding:6px 12px;font-size:0.9em;text-align:left'>"
                f"{html.escape(u.email)}</td>"
                f"<td style='padding:6px 12px;font-size:0.9em;text-align:center'>"
                f"{html.escape(role_label)}{role_action}"
                + (f"<div style='font-size:0.8em;color:#999;margin-top:2px'>"
                   f"{', '.join('<span style=white-space:nowrap>' + html.escape(c).replace(' ', '&nbsp;') + '</span>' for c in user_cohorts.get(u.user_id, []))}"
                   "</div>" if u.user_id in user_cohorts else "")
                + "</td>"
                f"<td style='padding:6px 12px;font-size:0.85em;color:#666;text-align:center'>"
                f"{html.escape(u.phone or '')}</td>"
                f"<td style='padding:6px 12px;font-size:0.85em;color:#666;text-align:center'>"
                f"{html.escape(u.notes or '')}"
                + ("" if is_event else
                   "<div style='font-size:0.8em;color:#999;margin-top:2px'>"
                   f"Reminders: {_short_lead_desc(u.lead_times_minutes)}</div>")
                + "</td>"
                f"<td style='padding:6px 12px;font-size:0.85em'>{actions_html}</td>"
                "</tr>"
            )

    # AAs and CAs both get the Add Member form. New users land in
    # the community + get a Membership in THIS app only. AAs aren't
    # exposed to other apps' rosters because the page filter above
    # already scopes to app_member_ids.
    add_form = (
        "<h3 style='font-size:1em;color:#444;margin-top:24px'>Add new member</h3>"
        "<form method='post' action='/api/users/add' "
        "style='margin:8px 0;display:flex;gap:8px;align-items:end;flex-wrap:wrap'>"
        "<label style='display:flex;flex-direction:column;font-size:0.85em;color:#666'>"
        "Name (required)<input name='name' required "
        "style='padding:4px;width:140px'></label>"
        "<label style='display:flex;flex-direction:column;font-size:0.85em;color:#666'>"
        "Email (required)<input type='email' name='email' required "
        "style='padding:4px;width:180px'></label>"
        "<label style='display:flex;flex-direction:column;font-size:0.85em;color:#666'>"
        "Phone (optional)<input name='phone' style='padding:4px;width:120px'></label>"
        "<label style='display:flex;flex-direction:column;font-size:0.85em;color:#666'>"
        "Notes (optional)<input name='notes' style='padding:4px;width:160px'></label>"
        "<button type='submit' style='padding:6px 16px;cursor:pointer'>"
        "Add member</button>"
        "</form>"
    )
    if user.community_role in ("ca", "ua"):
        add_form += (
            "<p style='color:#888;font-size:0.85em;margin-top:8px'>"
            "Cross-app roster management: "
            "<a href='/admin/community-users' style='color:#2a7'>"
            "Community users</a>."
            "</p>"
        )

    table = ""
    if all_users:
        scroll_style = ("max-height:500px;overflow-y:auto;border:1px solid #eee;"
                        "border-radius:4px" if len(all_users) > 10 else "")
        table = (
            f"<div style='{scroll_style};margin-top:12px'>"
            "<table style='border-collapse:collapse;width:100%;font-size:0.95em'>"
            "<thead><tr style='color:#888;border-bottom:1px solid #ddd;"
            "position:sticky;top:0;background:white'>"
            "<th style='text-align:left;padding:6px 12px;white-space:nowrap'>Name</th>"
            "<th style='text-align:left;padding:6px 12px'>Email</th>"
            f"<th style='text-align:center;padding:6px 12px'>{'Role' if is_event else 'Role/Cohorts'}</th>"
            "<th style='text-align:center;padding:6px 12px'>Phone</th>"
            "<th style='text-align:center;padding:6px 12px'>Notes</th>"
            "<th style='text-align:center;padding:6px 12px'>Actions</th>"
            "</tr></thead><tbody>" + rows + "</tbody></table></div>"
        )

    body = (
        f"<h1>{html.escape(org_name)}</h1>"
        "<h2 style='font-size:1.1em;color:#444'>Manage members</h2>"
        + _flash_banner_html(event)
        + conflict_banner
        + table + add_form
        + _admin_nav_bar("members", app=app)
    )
    return _html(200, _page(body, narrow=False, title=org_name))


def _api_user_add(event: dict, user: User, community: Community | None,
                  app: Application, membership: Membership | None) -> dict:
    # AAs can add users to THEIR app. Three cases (#172):
    #   1. New community email → create User + Cognito + Membership
    #      (the original flow).
    #   2. Existing community user not yet in this app → just write
    #      the Membership row. No duplicate User, no second Cognito
    #      provision. Pre-fix, this case fired Cognito's
    #      UsernameExistsException and returned an opaque 500 to the
    #      AA. Now it does the obvious right thing.
    #   3. Existing community user already in this app → friendly
    #      "already a member" banner; no state changes.
    # AAs still cannot enumerate or add to other apps (corner + user
    # list filter to this app's roster). CA/UA cross-app work goes
    # through /admin/community-users.
    if not _is_admin(user, membership):
        return _text(403, "admins only")
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method != "POST":
        return _text(405, "POST required")
    name = _get_param(event, "name")
    email = _get_param(event, "email")
    phone = _get_param(event, "phone") or None
    notes = _get_param(event, "notes") or None
    if not name or not email:
        return _error_redirect("/admin/users",
            "Name and email are both required.")
    # Case 2 or 3: detect an existing community user with this email.
    # `get_user_by_email` already does case-insensitive matching, so
    # "User@example.com" finds "user@example.com" without local normalization.
    existing = db.get_user_by_email(user.community_id, email)
    if existing is not None:
        if db.get_membership(app.app_id, existing.user_id) is not None:
            # Case 3.
            return _error_redirect("/admin/users",
                f"{existing.name} is already a member of this app.")
        # Case 2: just grant membership.
        db.put_membership(Membership(
            community_id=user.community_id, app_id=app.app_id,
            user_id=existing.user_id))
        log.info("admin %s granted existing user %s membership in app %s",
                 user.user_id, existing.user_id, app.app_id)
        # #198: surface a notice when the typed name doesn't match the
        # existing user's name. Without this the AA never learns the
        # name they typed was discarded — for example, typing "Joe B"
        # when the existing user was named
        # "Joe Bennett". Case-insensitive, whitespace-trimmed compare.
        typed_norm = " ".join(name.split()).lower()
        existing_norm = " ".join((existing.name or "").split()).lower()
        if typed_norm and existing_norm and typed_norm != existing_norm:
            notice = (
                f"Granted membership to {existing.name} (the existing "
                f"community user at {existing.email}). The name you "
                f'typed ("{name}") was not used. If you meant a '
                f"different person, please double-check the email."
            )
            return _redirect(
                "/admin/users?notice=" + urllib.parse.quote(notice))
        return _redirect("/admin/users")
    # Case 1: net-new community user.
    new_user = User(community_id=user.community_id, email=email, name=name,
                    community_role="member", phone=phone, notes=notes,
                    lead_times_minutes=list(app.default_lead_times or [1440, 120]))
    # Auto-provision the Cognito identity so the unified
    # new-user/forgot-password flow works immediately.
    cognito_sub = _create_cognito_user(email, name)
    if cognito_sub:
        new_user.cognito_sub = cognito_sub
    db.put_user(new_user)
    mem = Membership(community_id=user.community_id, app_id=app.app_id,
                     user_id=new_user.user_id)
    db.put_membership(mem)
    log.info("admin %s added user %s (%s) cognito=%s",
             user.user_id, new_user.user_id, email, bool(cognito_sub))
    return _redirect("/admin/users")


def _api_user_edit(event: dict, user: User, community: Community | None,
                   app: Application, membership: Membership | None) -> dict:
    # Admit App Admins of the active app AND community-level CAs/UAs. This
    # handler is the shared save target for both the per-app Members page
    # (/admin/users) and the CA community-users page (/admin/community-users);
    # the latter POSTs here while the active app may be one the CA only
    # *belongs to* as a plain member (app_role != "aa"). _is_admin() looks
    # solely at app_role, so without the community_role clause a CA editing a
    # user while parked in such an app would be wrongly 403'd and the save
    # would silently no-op (the per-CA vs per-AA scoping below still applies).
    if not _is_admin(user, membership) and user.community_role not in ("ca", "ua"):
        return _text(403, "admins only")
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method != "POST":
        return _text(405, "POST required")
    uid = _get_param(event, "user_id")
    if not uid:
        return _error_redirect_or_next(event, "/admin/users",
            "Missing user id.")
    target = db.get_user(user.community_id, uid)
    if not target:
        return _error_redirect_or_next(event, "/admin/users",
            "User not found.")
    # PRIVACY-AUDIT HIGH-2: AAs can only edit users who are members
    # of their own app. CAs/UAs can edit any user in the community.
    if user.community_role not in ("ca", "ua"):
        if db.get_membership(app.app_id, uid) is None:
            return _error_redirect_or_next(event, "/admin/users",
                "App admins can only edit members of their own app.")
    expected_version_raw = _get_param(event, "version")
    try:
        expected_version = int(expected_version_raw) if expected_version_raw else 0
    except ValueError:
        expected_version = 0
    target.name = _get_param(event, "name") or target.name
    target.email = _get_param(event, "email") or target.email
    # community_role editing lives on /admin/community-users only
    # (per task #168). The per-app /admin/users edit form previously
    # rendered a CA-only community_role select; that's been removed.
    # The CA users page still accepts this field and continues to
    # use this same handler — we keep the gate here so CAs can edit
    # role from THAT screen, but the per-app form simply never posts it.
    role = _get_param(event, "community_role")
    if role and user.community_role == "ca":
        if role in ("ca", "ua", "member"):
            if target.user_id == user.user_id and role != "ca":
                return _error_redirect_or_next(event,
                    "/admin/community-users",
                    "You cannot change your own community role.")
            target.community_role = role
    phone = _get_param(event, "phone")
    if phone is not None:
        target.phone = phone or None
    notes = _get_param(event, "notes")
    if notes is not None:
        target.notes = notes or None
    body_str = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body_str = base64.b64decode(body_str).decode("utf-8", errors="replace")
    parsed = urllib.parse.parse_qs(body_str)
    if "lead" in parsed:
        target.lead_times_minutes = sorted([int(v) for v in parsed["lead"]], reverse=True)
    elif body_str and "lead" not in parsed:
        target.lead_times_minutes = []
    raw_next = _get_param(event, "next")
    next_url = _safe_next(raw_next) if raw_next else "/admin/users"
    try:
        db.put_user(target, expected_version=expected_version)
    except db.ConcurrencyConflict:
        # Inject conflict marker just before any fragment so the
        # banner shows AND the page scrolls to the row.
        sep = "&" if ("?" in next_url and "#" not in next_url.split("?", 1)[1]) else "?"
        base, frag = (next_url.split("#", 1) + [""])[:2]
        sep_base = "&" if "?" in base else "?"
        conflict_q = f"conflict={urllib.parse.quote(target.name)}"
        redirect = (f"{base}{sep_base}{conflict_q}"
                    + (f"#{frag}" if frag else ""))
        return _redirect(redirect)
    log.info("admin %s edited user %s", user.user_id, uid)
    return _redirect(next_url)


def _api_user_delete(event: dict, user: User, community: Community | None,
                     app: Application, membership: Membership | None) -> dict:
    if user.community_role not in ("ca", "ua"):
        return _text(403, "only Community Admins or User Admins can delete users")
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method != "POST":
        return _text(405, "POST required")
    uid = _get_param(event, "user_id")
    if not uid:
        return _error_redirect_or_next(event,
            "/admin/community-users", "Missing user id.")
    if uid == user.user_id:
        return _error_redirect_or_next(event,
            "/admin/community-users",
            "You cannot delete yourself. Ask another admin.")
    target = db.get_user(user.community_id, uid)
    if target and target.cognito_sub and USER_POOL_ID:
        try:
            _get_cognito().admin_delete_user(
                UserPoolId=USER_POOL_ID, Username=target.email)
            log.info("deleted Cognito identity for %s", target.email)
        except Exception as e:
            log.warning("Cognito admin_delete_user failed for %s: %s", target.email, e)
    for mem in db.list_memberships_for_user(uid):
        db.delete_membership(mem.app_id, uid)
    for a in db.list_assignments_for_user(uid):
        db.delete_assignment(a.app_id, a.yyyy_mm, a.slot_id, a.user_id)
    # CohortMemberships aren't reachable by user_id directly without a
    # GSI scan; walk every app's every cohort. Slow for big communities
    # but acceptable for a destructive op that requires admin confirm.
    for app_row in db.list_applications(user.community_id):
        for cohort in db.list_cohorts(app_row.app_id):
            for cm in db.list_cohort_members(cohort.cohort_id):
                if cm.user_id == uid:
                    db.delete_cohort_membership(cohort.cohort_id, uid)
    db.delete_user(user.community_id, uid)
    log.info("CA/UA %s deleted user %s from community (+ Cognito)", user.user_id, uid)
    # Honor ?next= so the delete-form on the CA users page redirects
    # back there instead of bouncing to the app-scoped users page.
    raw_next = _get_param(event, "next")
    return _redirect(_safe_next(raw_next) if raw_next else "/admin/users")


def _api_user_reset_access(event: dict, user: User, community: Community | None,
                           app: Application, membership: Membership | None) -> dict:
    # Admit App Admins of the active app AND community CAs/UAs — same shared-
    # target reasoning as _api_user_edit (the CA community-users page POSTs
    # here while parked in an app the CA may only be a plain member of).
    if not _is_admin(user, membership) and user.community_role not in ("ca", "ua"):
        return _text(403, "admins only")
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method != "POST":
        return _text(405, "POST required")
    uid = _get_param(event, "user_id")
    if not uid:
        return _error_redirect_or_next(event, "/admin/users",
            "Missing user id.")
    if uid == user.user_id:
        return _error_redirect_or_next(event, "/admin/users",
            "Use the password page to reset your own access.")
    target = db.get_user(user.community_id, uid)
    if not target:
        return _error_redirect_or_next(event, "/admin/users",
            "User not found.")
    # PRIVACY-AUDIT HIGH-2: AAs limited to their own app's members.
    if user.community_role not in ("ca", "ua"):
        if db.get_membership(app.app_id, uid) is None:
            return _error_redirect_or_next(event, "/admin/users",
                "App admins can only reset access for members of "
                "their own app.")
    if not target.cognito_sub:
        return _error_redirect_or_next(event, "/admin/users",
            "User has no login identity to reset.")
    import secrets
    random_pw = secrets.token_urlsafe(24) + "A1!"
    cognito = _get_cognito()
    try:
        cognito.admin_set_user_password(
            UserPoolId=USER_POOL_ID,
            Username=target.email,
            Password=random_pw,
            Permanent=True,
        )
    except Exception as e:
        log.warning("reset password failed for %s: %s", target.email, e)
        return _error_redirect_or_next(event, "/admin/users",
            f"Failed to reset password: {e}")
    try:
        cognito.admin_user_global_sign_out(
            UserPoolId=USER_POOL_ID,
            Username=target.email,
        )
    except Exception as e:
        # Sign-out is a best effort; log but don't fail because the password
        # was already rotated.
        log.warning("global sign-out failed for %s: %s", target.email, e)
    log.info("admin %s reset access for user %s (%s)",
             user.user_id, uid, target.email)
    return _redirect_next(event, "/admin/users")


def _api_user_remove_from_app(event: dict, user: User, community: Community | None,
                              app: Application, membership: Membership | None) -> dict:
    # AAs can remove members from THEIR app. The delete is scoped to
    # app.app_id (see body); cross-app removal isn't reachable from
    # this endpoint.
    if not _is_admin(user, membership):
        return _text(403, "admins only")
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method != "POST":
        return _text(405, "POST required")
    uid = _get_param(event, "user_id")
    if not uid:
        return _error_redirect_or_next(event, "/admin/users",
            "Missing user id.")
    if uid == user.user_id:
        return _error_redirect_or_next(event, "/admin/users",
            "You cannot remove yourself from the app. Ask another admin.")
    db.delete_membership(app.app_id, uid)
    for a in db.list_assignments_for_user(uid):
        if a.app_id == app.app_id:
            db.delete_assignment(a.app_id, a.yyyy_mm, a.slot_id, a.user_id)
    log.info("admin %s removed user %s from app %s", user.user_id, uid, app.app_id)
    return _redirect_next(event, "/admin/users")


def _api_user_toggle_membership(event: dict, user: User, community: Community | None,
                                app: Application, membership: Membership | None) -> dict:
    # AAs can promote/demote within THEIR app. The handler only writes
    # to membership rows for the current app_id (see body below), so
    # this can't bleed into other apps.
    if not _is_admin(user, membership):
        return _text(403, "admins only")
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method != "POST":
        return _text(405, "POST required")
    uid = _get_param(event, "user_id")
    app_role = _get_param(event, "app_role")
    if not uid:
        return _error_redirect_or_next(event, "/admin/users",
            "Missing user id.")
    if uid == user.user_id and app_role == "member":
        return _error_redirect_or_next(event, "/admin/users",
            "You cannot demote yourself. Ask another admin to change your role.")
    if uid == user.user_id and not app_role:
        return _error_redirect_or_next(event, "/admin/users",
            "You cannot remove yourself from the app. Ask another admin.")
    existing = db.get_membership(app.app_id, uid)
    if app_role and existing:
        existing.app_role = app_role
        db.put_membership(existing)
        log.info("admin %s set app_role=%s for %s", user.user_id, app_role, uid)
    elif existing and not app_role:
        db.delete_membership(app.app_id, uid)
        log.info("admin %s removed membership for %s", user.user_id, uid)
    else:
        mem = Membership(community_id=user.community_id, app_id=app.app_id,
                         user_id=uid, app_role=app_role or "member")
        db.put_membership(mem)
        log.info("admin %s added membership for %s", user.user_id, uid)
    return _redirect_next(event, "/admin/users")


def _api_user_set_community_role(event: dict, user: User,
                                 community: Community | None,
                                 app: Application,
                                 membership: Membership | None) -> dict:
    """Set another user's community_role. CA-only.

    The richer community_role editing flow lives on
    /admin/community-users; this is the per-app Members shortcut so a
    CA viewing the app roster can promote a member to community admin
    without pivoting screens.
    """
    if user.community_role != "ca":
        return _text(403, "only Community Admins can set community roles")
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method != "POST":
        return _text(405, "POST required")
    uid = _get_param(event, "user_id")
    role = _get_param(event, "role")
    if not uid or role not in ("ca", "ua", "member"):
        return _error_redirect_or_next(event, "/admin/users",
            "Missing user id or invalid role.")
    if uid == user.user_id:
        return _error_redirect_or_next(event, "/admin/users",
            "You cannot change your own community role. Ask another CA.")
    target = db.get_user(user.community_id, uid)
    if target is None:
        return _error_redirect_or_next(event, "/admin/users",
            "User not found.")
    target.community_role = role
    try:
        db.put_user(target, expected_version=target.version or 0)
    except db.ConcurrencyConflict:
        return _error_redirect_or_next(event, "/admin/users",
            f"Someone else just edited {target.name}. Try again.")
    log.info("CA %s set community_role=%s for %s",
             user.user_id, role, uid)
    return _redirect_next(event, "/admin/users")


def _api_user_clear_bounce(event: dict, user: User, community: Community | None,
                           app: Application, membership: Membership | None) -> dict:
    if not _is_admin(user, membership):
        return _text(403, "admins only")
    # Require POST. SameSite=Lax cookies still flow with top-level
    # GETs, so any page could `<img src=…clear-bounce?user_id=X>` and
    # silently clear a flag on an admin's browser visit (CSRF via GET
    # — security fix M3).
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method != "POST":
        return _text(405, "POST required")
    uid = _get_param(event, "user_id")
    if not uid:
        return _error_redirect_or_next(event, "/admin/users",
            "Missing user id.")
    target = db.get_user(user.community_id, uid)
    if not target:
        return _error_redirect_or_next(event, "/admin/users",
            "User not found.")
    # PRIVACY-AUDIT HIGH-2: AAs limited to their own app's members.
    if user.community_role not in ("ca", "ua"):
        if db.get_membership(app.app_id, uid) is None:
            return _error_redirect_or_next(event, "/admin/users",
                "App admins can only clear bounce flags for members of their own app.")
    target.email_undeliverable = False
    db.put_user(target)
    log.info("admin %s cleared bounce for %s", user.user_id, uid)
    return _redirect_next(event, "/admin/users")


def _swap_new_page(event: dict, user: User, community: Community | None,
                   app: Application, membership: Membership | None) -> dict:
    org_name = app.name if app else (community.name if community else user.community_id)
    slot_id = _get_param(event, "slot_id")
    yyyy_mm = _get_param(event, "month")
    if not slot_id or not yyyy_mm:
        return _error_redirect("/your-schedule",
            "Missing slot id or month.")
    slot = db.find_slot_in_month(app.app_id, yyyy_mm, slot_id)
    if not slot:
        return _error_redirect("/your-schedule", "Slot not found.")
    sch = db.get_schedule(app.app_id, yyyy_mm)
    if not _schedule_visible(sch):
        return _error_redirect("/your-schedule",
            "Schedule is not visible.")
    user_cohort_ids = {cm.cohort_id for cm in db.list_cohorts_for_user(user.user_id)}
    cohort_template_ids: set[str] = set()
    for c in db.list_cohorts(app.app_id):
        if c.cohort_id in user_cohort_ids and c.linked_template_id:
            cohort_template_ids.add(c.linked_template_id)
    if slot.template_id and slot.template_id != "one-off":
        cohort_template_ids.add(slot.template_id)
    all_slots = sorted(db.list_slots(app.app_id, yyyy_mm),
                       key=lambda s: (s.local_date, s.start_time))
    user_assigned_slots = {a.slot_id for a in db.list_assignments_for_user(
        user.user_id, since_date=None) if a.yyyy_mm == yyyy_mm}
    users_by_id = {u.user_id: u for u in db.list_users(user.community_id)}
    asgns_by_slot: dict[str, list] = {}
    for a in db.list_assignments_for_month(app.app_id, yyyy_mm):
        asgns_by_slot.setdefault(a.slot_id, []).append(a)
    options = []
    current_date: str | None = None
    for s in all_slots:
        if s.slot_id == slot_id or s.cancelled:
            continue
        if s.template_id not in cohort_template_ids and s.template_id != "one-off":
            continue
        if s.slot_id in user_assigned_slots:
            continue
        asgns = asgns_by_slot.get(s.slot_id, [])
        names = [users_by_id.get(a.user_id, _stub_user(a.user_id)).name for a in asgns]
        date_header = ""
        if s.local_date != current_date:
            current_date = s.local_date
            date_header = f"<div style='margin-top:12px;font-weight:600;color:#444'>{_pretty_date(s.local_date)}</div>"
        options.append(
            f"{date_header}"
            f"<label style='display:block;margin:4px 0 4px 16px'>"
            f"<input type='checkbox' name='preferred' value='{s.slot_id}'> "
            f"{_fmt_time(s.start_time)} -- {html.escape(s.name)} "
            f"<span style='color:#888;font-size:0.9em'>({', '.join(html.escape(n) for n in names) or 'open'})</span>"
            "</label>"
        )
    when = _pretty_date(slot.local_date)
    body = (
        f"<h1>{html.escape(org_name)}</h1>"
        f"<h2 style='color:#444'>Trade your slot</h2>"
        f"<p>You want to trade out of:</p>"
        f"<div style='padding:12px;background:#f5f5f5;border-radius:8px;margin:12px 0'>"
        f"<b>{html.escape(slot.name)}</b><br>"
        f"{when} -- {_fmt_time(slot.start_time)}</div>"
        f"<form method='post' action='/api/swap/create'>"
        f"<input type='hidden' name='slot_id' value='{slot_id}'>"
        f"<input type='hidden' name='month' value='{yyyy_mm}'>"
        f"<div style='margin:16px 0;padding:12px;border:1px solid #ddd;"
        f"border-radius:8px;background:#fafafa'>"
        "<label style='display:block;margin:6px 0;cursor:pointer'>"
        f"<input type='radio' name='release_now' value='1'"
        f"{' checked' if app.trade_default_release else ''} "
        "style='margin-right:6px'>"
        "Release my slot now and offer to trade "
        "<span style='color:#888;font-size:0.9em'>(I definitely can't make this date)</span>"
        "</label>"
        "<label style='display:block;margin:6px 0;cursor:pointer'>"
        f"<input type='radio' name='release_now' value='0'"
        f"{' checked' if not app.trade_default_release else ''} "
        "style='margin-right:6px'>"
        "Keep my slot while looking for a trade "
        "<span style='color:#888;font-size:0.9em'>(I can still cover if needed)</span>"
        "</label></div>"
        f"<p>Select one or more dates you'd accept instead:</p>"
        + "".join(options)
        + ("<p style='color:#888;margin-top:12px'>No tradeable slots available.</p>"
           if not options else
           "<p style='margin-top:16px'>"
           "<button type='submit' style='padding:8px 24px;cursor:pointer;"
           "font-size:1em;color:white;background:#2a7;border:none;"
           "border-radius:4px'>Request trade</button></p>")
        + "</form>"
        + "<p style='margin-top:24px'><a href='/your-schedule'>Back to schedule</a></p>"
    )
    return _html(200, _page(body, title=org_name))


def _api_swap_create(event: dict, user: User, community: Community | None,
                     app: Application, membership: Membership | None) -> dict:
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method != "POST":
        return _text(405, "POST required")
    slot_id = _get_param(event, "slot_id")
    yyyy_mm = _get_param(event, "month")
    if not slot_id or not yyyy_mm:
        return _error_redirect("/your-schedule", "Missing required fields.")
    body_str = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body_str = base64.b64decode(body_str).decode("utf-8", errors="replace")
    import urllib.parse as up
    parsed = up.parse_qs(body_str, keep_blank_values=True)
    preferred = parsed.get("preferred", [])
    release_now = parsed.get("release_now", ["0"])[0] == "1"
    if not preferred:
        return _error_redirect(
            f"/trade?slot_id={slot_id}&month={yyyy_mm}",
            "Select at least one alternative date.")
    # Verify the actor has an assignment on the release slot. Without
    # this, anyone can create a swap request for any slot and trigger
    # cohort-wide spam notifications (security fix M1).
    has_assignment = any(
        a.user_id == user.user_id
        for a in db.list_assignments_for_slot(app.app_id, yyyy_mm, slot_id)
    )
    if not has_assignment:
        return _text(403, "you are not assigned to that slot")
    from community_organizer.core.models import SwapRequest
    swap = SwapRequest(
        community_id=user.community_id, app_id=app.app_id, yyyy_mm=yyyy_mm,
        requester_user_id=user.user_id, release_slot_id=slot_id,
        preferred_slot_ids=preferred, released=release_now,
    )
    if release_now and community:
        slot = db.find_slot_in_month(app.app_id, yyyy_mm, slot_id)
        db.delete_assignment(app.app_id, yyyy_mm, slot_id, user.user_id)
        log.info("user %s released slot %s (hard trade)", user.user_id, slot_id)
        if slot:
            _send_removal_notifications(user, None, community, app, slot, yyyy_mm,
                                        self_release=True, notify_self=False)
            _notify_cohort_of_opening(user, community, app, slot, yyyy_mm)
    db.put_swap(swap)
    log.info("user %s created swap request %s for slot %s (released=%s)",
             user.user_id, swap.swap_id, slot_id, release_now)
    _notify_swap_request(user, community, app, swap)
    return _redirect("/your-schedule")


def _notify_swap_request(requester: User, community: Community,
                         app: Application, swap) -> None:
    from community_organizer.providers.email import get_email_provider
    provider = get_email_provider()
    from_addr = f"organizer@{os.environ.get('DOMAIN_NAME', 'community.example.org')}"
    domain = os.environ.get("DOMAIN_NAME", "community.example.org")
    release_slot = db.find_slot_in_month(app.app_id, swap.yyyy_mm, swap.release_slot_id)
    if not release_slot:
        return
    when = _pretty_date(release_slot.local_date)
    preferred_slots = [db.find_slot_in_month(app.app_id, swap.yyyy_mm, sid)
                       for sid in swap.preferred_slot_ids]
    preferred_slots = [s for s in preferred_slots if s]
    alt_lines = "\n".join(
        f"  - {_pretty_date(s.local_date)} -- {_fmt_time(s.start_time)}"
        for s in preferred_slots
    )
    cohort = db.get_cohort_by_template(app.app_id, release_slot.template_id) if release_slot.template_id != "one-off" else None
    notify_ids: set[str] = set()
    if cohort:
        for cm in db.list_cohort_members(cohort.cohort_id):
            notify_ids.add(cm.user_id)
    pref_assigned: dict[str, set[str]] = {}
    for s in preferred_slots:
        for a in db.list_assignments_for_slot(app.app_id, swap.yyyy_mm, s.slot_id):
            pref_assigned.setdefault(a.user_id, set()).add(s.slot_id)
            notify_ids.add(a.user_id)
    notify_ids.discard(requester.user_id)
    users_by_id = {u.user_id: u for u in db.list_users(community.community_id)}
    for uid in notify_ids:
        target = users_by_id.get(uid)
        if not target or not target.email or target.email_undeliverable or target.channel == "none":
            continue
        can_accept = uid in pref_assigned
        if can_accept:
            action = (
                f"You are assigned to one of the dates {requester.name} can accept. "
                f"To trade, visit:\n"
                f"  https://{domain}/swap/{swap.swap_id}/accept?month={swap.yyyy_mm}\n"
            )
        else:
            action = (
                f"If you can cover {when}, sign up at:\n"
                f"  https://{domain}/your-schedule\n"
            )
        body_text = (
            f"Hi {target.name},\n\n"
            f"{requester.name} would like to trade their slot:\n\n"
            f"  {release_slot.name}\n"
            f"  {when} -- {_fmt_time(release_slot.start_time)}\n\n"
            f"They can take any of these dates instead:\n"
            f"{alt_lines}\n\n"
            f"{action}\n"
            f"-- {app.name if app else community.name}\n"
        )
        provider.send(
            community_id=community.community_id,
            from_addr=from_addr, to_addr=target.email,
            subject=f"{app.name} -- trade request: {release_slot.name} on {when}",
            body_text=body_text,
            kind="swap_request",
            related_user_id=uid,
            related_app_id=app.app_id,
            related_slot_id=release_slot.slot_id,
            related_yyyy_mm=swap.yyyy_mm,
        )
    aa_ids = {m.user_id for m in db.list_memberships_for_app(app.app_id)
              if m.app_role == "aa" and m.user_id != requester.user_id}
    for aa_id in aa_ids:
        aa = users_by_id.get(aa_id)
        if not aa or not aa.email or aa.email_undeliverable:
            continue
        mode = "released their slot and is" if swap.released else "is"
        aa_body = (
            f"Hi {aa.name},\n\n"
            f"{requester.name} {mode} looking to trade:\n\n"
            f"  {release_slot.name}\n"
            f"  {when} -- {_fmt_time(release_slot.start_time)}\n\n"
            f"They can take any of these dates instead:\n"
            f"{alt_lines}\n\n"
            f"{'The slot has been released and the cohort has been notified.' if swap.released else 'They are keeping the slot until someone accepts the trade.'}\n\n"
            f"-- {app.name if app else community.name}\n"
        )
        provider.send(
            community_id=community.community_id,
            from_addr=from_addr, to_addr=aa.email,
            subject=f"{app.name} -- trade request from {requester.name}: {release_slot.name}",
            body_text=aa_body, kind="swap_request",
            related_user_id=aa_id, related_app_id=app.app_id,
            related_slot_id=release_slot.slot_id, related_yyyy_mm=swap.yyyy_mm,
        )
    log.info("notified %d cohort + %d AAs of swap request %s",
             len(notify_ids), len(aa_ids), swap.swap_id)


def _swap_accept_page(event: dict, user: User, community: Community | None,
                      app: Application, membership: Membership | None) -> dict:
    org_name = app.name if app else (community.name if community else user.community_id)
    path = event.get("rawPath") or event.get("path") or ""
    import re as _re
    m = _re.search(r"/swap/([a-f0-9]+)/accept", path)
    if not m:
        return _text(404, "not found")
    swap_id = m.group(1)
    yyyy_mm = _get_param(event, "month")
    if not yyyy_mm:
        return _error_redirect("/your-schedule", "Missing month.")
    swap = db.get_swap(app.app_id, yyyy_mm, swap_id)
    if not swap:
        return _error_redirect("/your-schedule",
            "Trade request not found.")
    if swap.state != "pending":
        return _html(200, _page(
            f"<h1>{html.escape(org_name)}</h1>"
            f"<p>This trade request has already been {swap.state}.</p>"
            "<p><a href='/your-schedule'>Back to schedule</a></p>",
            title=org_name))
    release_slot = db.find_slot_in_month(app.app_id, yyyy_mm, swap.release_slot_id)
    requester = db.get_user(user.community_id, swap.requester_user_id)
    if not release_slot or not requester:
        return _text(404, "data not found")
    user_assigned = {a.slot_id for a in db.list_assignments_for_user(
        user.user_id, since_date=None) if a.yyyy_mm == yyyy_mm}
    tradeable = [sid for sid in swap.preferred_slot_ids if sid in user_assigned]
    if not tradeable:
        return _html(200, _page(
            f"<h1>{html.escape(org_name)}</h1>"
            f"<p>{html.escape(requester.name)} wants to trade out of "
            f"{html.escape(release_slot.name)} on {_pretty_date(release_slot.local_date)}, "
            f"but you're not assigned to any of their preferred alternatives.</p>"
            "<p><a href='/your-schedule'>Back to schedule</a></p>",
            title=org_name))
    when = _pretty_date(release_slot.local_date)
    if len(tradeable) == 1:
        s = db.find_slot_in_month(app.app_id, yyyy_mm, tradeable[0])
        your_when = _pretty_date(s.local_date) if s else "?"
        your_time = _fmt_time(s.start_time) if s else "?"
        body = (
            f"<h1>{html.escape(org_name)}</h1>"
            f"<h2 style='color:#444'>Accept a trade</h2>"
            f"<p>{html.escape(requester.name)} wants to trade:</p>"
            f"<div style='padding:12px;background:#f5f5f5;border-radius:8px;margin:12px 0'>"
            f"<b>They give up:</b> {html.escape(release_slot.name)} on {when}<br>"
            f"<b>They take yours:</b> {html.escape(s.name if s else '?')} on {your_when}"
            "</div>"
            f"<form method='post' action='/api/swap/accept'>"
            f"<input type='hidden' name='swap_id' value='{swap_id}'>"
            f"<input type='hidden' name='month' value='{yyyy_mm}'>"
            f"<input type='hidden' name='accept_slot_id' value='{tradeable[0]}'>"
            "<p style='margin-top:16px'>"
            "<button type='submit' style='padding:10px 32px;cursor:pointer;"
            "font-size:1.1em;color:white;background:#2a7;border:none;"
            "border-radius:4px'>Accept this trade</button></p>"
            "</form>"
            "<p style='margin-top:24px'><a href='/your-schedule'>Back to schedule</a></p>"
        )
    else:
        options = ""
        for sid in tradeable:
            s = db.find_slot_in_month(app.app_id, yyyy_mm, sid)
            if s:
                options += (
                    f"<label style='display:block;margin:8px 0'>"
                    f"<input type='radio' name='accept_slot_id' value='{sid}' required> "
                    f"{_pretty_date(s.local_date)} -- {_fmt_time(s.start_time)} "
                    f"({html.escape(s.name)})</label>"
                )
        body = (
            f"<h1>{html.escape(org_name)}</h1>"
            f"<h2 style='color:#444'>Accept a trade</h2>"
            f"<p>{html.escape(requester.name)} wants to trade out of:</p>"
            f"<div style='padding:12px;background:#f5f5f5;border-radius:8px;margin:12px 0'>"
            f"<b>{html.escape(release_slot.name)}</b><br>"
            f"{when} -- {_fmt_time(release_slot.start_time)}</div>"
            f"<p>In exchange, they'll take your slot on one of these dates. "
            f"Select which one to trade:</p>"
            f"<form method='post' action='/api/swap/accept'>"
            f"<input type='hidden' name='swap_id' value='{swap_id}'>"
            f"<input type='hidden' name='month' value='{yyyy_mm}'>"
            + options
            + "<p style='margin-top:16px'>"
            "<button type='submit' style='padding:8px 24px;cursor:pointer;"
            "font-size:1em;color:white;background:#2a7;border:none;"
            "border-radius:4px'>Accept trade</button></p>"
            "</form>"
            "<p style='margin-top:24px'><a href='/your-schedule'>Back to schedule</a></p>"
        )
    return _html(200, _page(body, title=org_name))


def _api_swap_accept(event: dict, user: User, community: Community | None,
                     app: Application, membership: Membership | None) -> dict:
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method != "POST":
        return _text(405, "POST required")
    swap_id = _get_param(event, "swap_id")
    yyyy_mm = _get_param(event, "month")
    accept_slot_id = _get_param(event, "accept_slot_id")
    if not all([swap_id, yyyy_mm, accept_slot_id]):
        return _error_redirect("/your-schedule",
            "Missing required fields.")
    swap = db.get_swap(app.app_id, yyyy_mm, swap_id)
    if not swap or swap.state != "pending":
        return _error_redirect("/your-schedule",
            "Trade request is no longer available.")
    if accept_slot_id not in swap.preferred_slot_ids:
        return _error_redirect("/your-schedule",
            "Slot is not in the preferred list.")
    release_slot = db.find_slot_in_month(app.app_id, yyyy_mm, swap.release_slot_id)
    accept_slot = db.find_slot_in_month(app.app_id, yyyy_mm, accept_slot_id)
    if not release_slot or not accept_slot:
        return _text(404, "slots not found")
    requester = db.get_user(user.community_id, swap.requester_user_id)
    if not requester:
        return _text(404, "requester not found")
    if not swap.released:
        db.delete_assignment(app.app_id, yyyy_mm, swap.release_slot_id, swap.requester_user_id)
    db.delete_assignment(app.app_id, yyyy_mm, accept_slot_id, user.user_id)
    from community_organizer.core.models import Assignment
    a1 = Assignment(community_id=user.community_id, app_id=app.app_id,
                    yyyy_mm=yyyy_mm, slot_id=accept_slot_id,
                    user_id=swap.requester_user_id, local_date=accept_slot.local_date)
    a2 = Assignment(community_id=user.community_id, app_id=app.app_id,
                    yyyy_mm=yyyy_mm, slot_id=swap.release_slot_id,
                    user_id=user.user_id, local_date=release_slot.local_date)
    db.put_assignment(a1)
    db.put_assignment(a2)
    import datetime as _dt
    swap.state = "completed"
    swap.accepter_user_id = user.user_id
    swap.accepted_slot_id = accept_slot_id
    swap.completed_at = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    db.put_swap(swap)
    log.info("swap %s completed: %s takes %s, %s takes %s",
             swap.swap_id, requester.name, accept_slot_id, user.name, swap.release_slot_id)
    _notify_swap_completed(requester, user, community, app, swap, release_slot, accept_slot)
    return _redirect("/your-schedule")


def _notify_swap_completed(requester: User, accepter: User, community: Community,
                           app: Application, swap, release_slot, accept_slot) -> None:
    from community_organizer.providers.email import get_email_provider
    provider = get_email_provider()
    domain = os.environ.get("DOMAIN_NAME", "community.example.org")
    from_addr = f"organizer@{domain}"
    cn = app.name if app else community.name
    tz_name = (app.default_timezone or community.default_timezone
               or "America/New_York")
    rel_when = _pretty_date(release_slot.local_date)
    acc_when = _pretty_date(accept_slot.local_date)
    for target, their_new, their_old in [
        (requester, accept_slot, release_slot),
        (accepter, release_slot, accept_slot),
    ]:
        if not target.email or target.email_undeliverable:
            continue
        new_when = _pretty_date(their_new.local_date)
        old_when = _pretty_date(their_old.local_date)
        arrival_text = None
        if their_new.arrival_offset_minutes:
            arrival_text = f"please arrive by {_fmt_arrival(their_new)}"
        new_ics = ical.make_event_ics(
            their_new, target.user_id, target.email,
            domain=domain, community_name=cn,
            timezone=tz_name, arrival_text=arrival_text,
            alarm_minutes=target.calendar_alarm_minutes,
        )
        body = (
            f"Hi {target.name},\n\n"
            f"Trade completed!\n\n"
            f"You are now assigned to:\n"
            f"  {their_new.name} on {new_when} -- {_fmt_time(their_new.start_time)}\n\n"
            f"You are no longer assigned to:\n"
            f"  {their_old.name} on {old_when}\n\n"
            f"-- {cn}\n"
        )
        provider.send(
            community_id=community.community_id,
            from_addr=from_addr, to_addr=target.email,
            subject=f"{cn} -- trade completed: {their_new.name} on {new_when}",
            body_text=body, kind="swap_request",
            related_user_id=target.user_id, related_app_id=app.app_id,
            related_yyyy_mm=swap.yyyy_mm,
            ics_content=new_ics,
        )
    aa_ids = {m.user_id for m in db.list_memberships_for_app(app.app_id)
              if m.app_role == "aa"
              and m.user_id != requester.user_id
              and m.user_id != accepter.user_id}
    users_by_id = {u.user_id: u for u in db.list_users(community.community_id)}
    for aa_id in aa_ids:
        aa = users_by_id.get(aa_id)
        if not aa or not aa.email or aa.email_undeliverable:
            continue
        body = (
            f"Hi {aa.name},\n\n"
            f"A trade was completed:\n\n"
            f"  {requester.name} traded {release_slot.name} on {rel_when}\n"
            f"  for {accept_slot.name} on {acc_when} (with {accepter.name})\n\n"
            f"-- {cn}\n"
        )
        provider.send(
            community_id=community.community_id,
            from_addr=from_addr, to_addr=aa.email,
            subject=f"{cn} -- trade completed: {requester.name} and {accepter.name}",
            body_text=body, kind="swap_request",
            related_user_id=aa_id, related_app_id=app.app_id,
            related_yyyy_mm=swap.yyyy_mm,
        )


def _api_swap_cancel(event: dict, user: User, community: Community | None,
                     app: Application, membership: Membership | None) -> dict:
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method != "POST":
        return _text(405, "POST required")
    swap_id = _get_param(event, "swap_id")
    yyyy_mm = _get_param(event, "month")
    if not swap_id or not yyyy_mm:
        return _error_redirect("/your-schedule", "Missing required fields.")
    swap = db.get_swap(app.app_id, yyyy_mm, swap_id)
    if not swap or swap.state != "pending":
        return _error_redirect("/your-schedule",
            "Trade request is no longer available.")
    if swap.requester_user_id != user.user_id:
        return _error_redirect("/your-schedule",
            "Only the requester can cancel this trade.")
    swap.state = "cancelled"
    db.put_swap(swap)
    log.info("user %s cancelled swap %s", user.user_id, swap_id)
    return _redirect("/your-schedule")


def _cleanup_stale_cohorts(app_id: str) -> int:
    current_month = f"{dt.date.today().year}-{dt.date.today().month:02d}"
    template_ids = {t.template_id for t in db.list_templates(app_id)}
    future_template_ids: set[str] = set()
    for sch in db.list_schedules(app_id):
        if sch.yyyy_mm >= current_month:
            for s in db.list_slots(app_id, sch.yyyy_mm):
                future_template_ids.add(s.template_id)
    cleaned = 0
    for cohort in list(db.list_cohorts(app_id)):
        if not cohort.linked_template_id:
            continue
        if cohort.linked_template_id in template_ids:
            continue
        if cohort.linked_template_id in future_template_ids:
            continue
        for cm in db.list_cohort_members(cohort.cohort_id):
            db.delete_cohort_membership(cohort.cohort_id, cm.user_id)
        db.delete_cohort(app_id, cohort.cohort_id)
        cleaned += 1
        log.info("auto-deleted stale cohort %s (%s)", cohort.cohort_id, cohort.name)
    return cleaned


def _pick_next_draft_schedule(app_id: str,
                              today: dt.date | None = None) -> "Schedule | None":
    """Return the draft schedule closest in time to ``today`` for compose+publish.

    "Closest" = the earliest draft yyyy_mm at or after the current month,
    falling back to the most recent past draft if none are upcoming. The
    fallback exists for stale drafts (admin forgot to publish a past
    month) — we'd rather flag that on the page than silently no-op.
    Returns None if there are no draft schedules at all.

    ``today`` defaults to ``dt.date.today()``; callers pass it in for
    deterministic tests.
    """
    drafts = [s for s in db.list_schedules(app_id) if s.state == "draft"]
    if not drafts:
        return None
    if today is None:
        today = dt.date.today()
    current_key = f"{today.year:04d}-{today.month:02d}"
    upcoming = [s for s in drafts if s.yyyy_mm >= current_key]
    if upcoming:
        return min(upcoming, key=lambda s: s.yyyy_mm)
    return max(drafts, key=lambda s: s.yyyy_mm)


# Sentinel picker value meaning "every active (published) month".
_ALL_ACTIVE_MONTHS = "__all_active__"


def _schedule_copy_month_options(app_id: str,
                                 today: dt.date | None = None) -> list[dict]:
    """Month options for the send-email schedule pickers: active (published)
    months first (nearest-upcoming flagged default), then archived (history)
    months, each labelled. Callers add an 'All active months' choice on the
    full-schedule pickers (not the per-cohort ones)."""
    scheds = list(db.list_schedules(app_id))
    pub = [s for s in scheds if s.state == "published"]
    arch = sorted([s for s in scheds if s.state == "archived"],
                  key=lambda s: s.yyyy_mm, reverse=True)
    if today is None:
        today = dt.date.today()
    current_key = f"{today.year:04d}-{today.month:02d}"
    upcoming = sorted([s for s in pub if s.yyyy_mm >= current_key],
                      key=lambda s: s.yyyy_mm)
    past = sorted([s for s in pub if s.yyyy_mm < current_key],
                  key=lambda s: s.yyyy_mm, reverse=True)
    default_key = (upcoming[0].yyyy_mm if upcoming
                   else (past[0].yyyy_mm if past else None))
    opts = [{"value": s.yyyy_mm, "label": _month_label(s.yyyy_mm),
             "archived": False, "is_default": s.yyyy_mm == default_key}
            for s in upcoming + past]
    opts += [{"value": s.yyyy_mm,
              "label": f"{_month_label(s.yyyy_mm)} (history)",
              "archived": True, "is_default": False}
             for s in arch]
    return opts


def _resolve_copy_months(app: Application, selection: str) -> list[str]:
    """Turn a picker value into concrete month keys. ``_ALL_ACTIVE_MONTHS``
    expands to every published month; a ``yyyy_mm`` returns ``[that]`` iff the
    schedule exists and is published or archived (both are real, sendable)."""
    scheds = {s.yyyy_mm: s for s in db.list_schedules(app.app_id)}
    if selection == _ALL_ACTIVE_MONTHS:
        return sorted(k for k, s in scheds.items() if s.state == "published")
    s = scheds.get(selection)
    return [selection] if (s and s.state in ("published", "archived")) else []


def _render_schedule_cells(community: Community | None, app: Application,
                           cells: set, cache: dict) -> str:
    """Render + concatenate a set of schedule "cells" for one recipient.

    A cell is ``(yyyy_mm, template_ids_or_None)``: ``None`` = whole month,
    a frozenset = a cohort slice (that template's slots only). Results are
    cached by cell so shared content (e.g. the full month everyone gets) is
    rendered once per send, not once per recipient."""
    parts = []
    for cell in sorted(cells,
                       key=lambda c: (c[0], "" if c[1] is None
                                      else ",".join(sorted(c[1])))):
        if cell not in cache:
            m, tids = cell
            cache[cell] = schedule_email.generate_schedule_table_html(
                community, app, m,
                template_ids=(set(tids) if tids else None))
        parts.append(cache[cell])
    return "<br><br>".join(parts)


def _send_email_page(event: dict, user: User, community: Community | None,
                     app: Application, membership: Membership | None) -> dict:
    if not _is_admin(user, membership):
        return _html(403, _page("<p>Admins only.</p><p><a href='/'>Back</a></p>"))
    org_name = app.name if app else (community.name if community else user.community_id)
    sent = _get_param(event, "sent")
    sent_msg = (f"<p style='color:#2a7;margin-bottom:16px'>"
                f"Email sent to {html.escape(sent)} recipients.</p>" if sent else "")
    templates_by_id = {t.template_id: t for t in db.list_templates(app.app_id)}
    def _cohort_sort_key(c):
        tpl = templates_by_id.get(c.linked_template_id or "")
        if tpl:
            return (tpl.day_of_week, tpl.start_time)
        return (99, c.name)
    # Event apps (standing/flexible) have no cohorts and no active schedules,
    # so the cohort picker and the "copy of the schedule" options are hidden.
    is_event = app.app_type in EVENT_APP_TYPES
    cohorts = ([] if is_event
               else sorted(db.list_cohorts(app.app_id), key=_cohort_sort_key))
    members_by_app = {m.user_id for m in db.list_memberships_for_app(app.app_id)}
    users_by_id = {u.user_id: u
                   for u in db.list_users(user.community_id)
                   if u.user_id in members_by_app}
    members = sorted(
        [(uid, users_by_id[uid].name) for uid in members_by_app
         if uid in users_by_id],
        key=lambda x: x[1],
    )

    # Schedule-copy month options (active + archived). Full-schedule pickers
    # also get an "All active months" choice; per-cohort pickers don't.
    month_opts = [] if is_event else _schedule_copy_month_options(app.app_id)
    have_sched = bool(month_opts)

    def _opt(o, selected=False):
        return (f"<option value='{html.escape(o['value'])}'"
                + (" selected" if selected else "")
                + f">{html.escape(o['label'])}</option>")

    full_opts = (
        f"<option value='{_ALL_ACTIVE_MONTHS}' selected>All active months</option>"
        + "".join(_opt(o) for o in month_opts))
    cohort_opts = "".join(_opt(o, selected=o["is_default"]) for o in month_opts)

    # Each cohort: a recipient checkbox + (if it maps to a template) a
    # "send this cohort their schedule" slice sub-option with its own month.
    cohort_rows = ""
    for c in cohorts:
        slice_ui = ""
        if c.linked_template_id and have_sched:
            slice_ui = (
                "<div style='margin-left:24px;margin-top:2px'>"
                "<label style='font-size:0.85em;color:#666'>"
                f"<input type='checkbox' name='cohort_sched_{c.cohort_id}' "
                f"value='1' class='cohort-sched' data-cohort='{c.cohort_id}'> "
                "Send this cohort their schedule</label>"
                f"<span class='cohort-month' id='cm-{c.cohort_id}' "
                "style='display:none'> &rarr; "
                f"<select name='cohort_month_{c.cohort_id}' "
                f"style='font-size:0.85em;padding:3px'>{cohort_opts}</select>"
                "</span></div>"
            )
        cohort_rows += (
            "<div style='margin:6px 0 6px 24px'>"
            "<label style='display:block'>"
            f"<input type='checkbox' name='cohort' value='{c.cohort_id}' "
            f"class='cohort-cb' id='cohort-{c.cohort_id}'> "
            f"{html.escape(c.name)}</label>{slice_ui}</div>"
        )

    # Schedule-include controls for each audience.
    all_sched_block = "" if not have_sched else (
        "<div id='all-sched' style='margin:4px 0 8px 24px'>"
        "<label style='font-size:0.9em;color:#444'>"
        "<input type='checkbox' name='all_include_schedule' value='1' "
        "id='all-inc'> Include a copy of the schedule</label>"
        "<span id='all-month-wrap' style='display:none'> &rarr; "
        f"<select name='all_copy_month' style='font-size:0.9em;padding:4px'>"
        f"{full_opts}</select></span></div>"
    )
    sel_full_block = "" if not have_sched else (
        "<div id='sel-full' style='margin-bottom:10px'>"
        "<label style='font-size:0.9em;color:#444'>"
        "<input type='checkbox' name='sel_include_full' value='1' id='sel-inc'> "
        "Include the full schedule</label>"
        "<span id='sel-month-wrap' style='display:none'> &rarr; "
        f"<select name='sel_full_month' style='font-size:0.9em;padding:4px'>"
        f"{full_opts}</select></span></div>"
    )

    member_opts = "".join(
        f"<option value='{uid}'>{html.escape(name)}</option>"
        for uid, name in members
    )

    # Quick-pick data. "Never logged in" = members who've never signed in
    # (welcome-email helper); available for every app type.
    never_logged_in = [
        {"uid": m_uid, "name": users_by_id[m_uid].name}
        for m_uid, _ in members
        if not (users_by_id[m_uid].login_count or 0)
        and not users_by_id[m_uid].last_login_at
    ]
    # For a flexible_event app with an open poll, ALSO offer "haven't
    # responded" (the primary shortcut for a poll) — members answer via a
    # login-free magic link, so "never logged in" would wrongly include people
    # who DID respond. Both buttons are shown so "never logged in" stays handy.
    not_responded = []
    if app.app_type == "flexible_event":
        _open_polls = [e for e in db.list_flexible_events(app.app_id)
                       if e.state == "poll"]
        if _open_polls:
            _poll = max(_open_polls, key=lambda e: e.created_at)
            _rsvps = list(db.list_flexible_rsvps(app.app_id, _poll.event_id))
            _responded = {r.user_id for r in _rsvps}
            # Household size = number of THIS app's members sharing a household_id.
            _hh_size: dict[str, int] = {}
            for m_uid, _ in members:
                hid = users_by_id[m_uid].household_id
                if hid:
                    _hh_size[hid] = _hh_size.get(hid, 0) + 1
            # A household is "fully covered" when a responder in it reported a
            # party_size that EXACTLY equals that household's member count — the
            # whole family is then accounted for, so we skip its other members.
            # If the numbers don't match (e.g. "2 of 3"), we can't tell who was
            # covered, so fall back to reminding that household's non-responders.
            _covered_households = set()
            for r in _rsvps:
                ru = users_by_id.get(r.user_id)
                hid = ru.household_id if ru else None
                if hid and r.party_size and r.party_size == _hh_size.get(hid):
                    _covered_households.add(hid)
            not_responded = [
                {"uid": m_uid, "name": users_by_id[m_uid].name}
                for m_uid, _ in members
                if m_uid not in _responded
                and users_by_id[m_uid].household_id not in _covered_households
            ]
    import json as _json
    # json.dumps does NOT escape "</" — neutralize for inline <script>.
    never_logged_in_json = _json.dumps(never_logged_in).replace("</", "<\\/")
    not_responded_json = _json.dumps(not_responded).replace("</", "<\\/")
    _qp_style = ("font-size:0.85em;padding:2px 10px;cursor:pointer;color:#2a7;"
                 "background:none;border:1px solid #2a7;border-radius:4px")
    # "haven't responded" first (primary), then "never logged in".
    quick_pick_buttons = ""
    if not_responded:
        quick_pick_buttons += (
            f" <button type='button' id='qp-not-responded' style='{_qp_style}'>"
            f"+ haven't responded ({len(not_responded)})</button>")
    if never_logged_in:
        quick_pick_buttons += (
            f" <button type='button' id='qp-never-logged-in' style='{_qp_style}'>"
            f"+ never logged in ({len(never_logged_in)})</button>")

    cohorts_section = ("" if is_event else (
        "<div style='margin-bottom:12px'>"
        "<p style='font-size:0.9em;color:#666;margin:0 0 4px 0'>Cohorts</p>"
        + (cohort_rows if cohort_rows else
           "<span style='color:#aaa;font-size:0.85em'>No cohorts defined.</span>")
        + "</div>"))

    body = (
        f"<h1>{html.escape(org_name)}</h1>"
        "<h2 style='font-size:1.1em;color:#444'>Compose and send email</h2>"
        + _flash_banner_html(event)
        + sent_msg
        + "<form method='post' action='/api/admin/send-email' "
        "style='text-align:left;max-width:640px;margin:0 auto' id='send-form'>"
        "<fieldset style='border:1px solid #ddd;border-radius:8px;padding:16px;"
        "margin-bottom:16px'>"
        "<legend style='font-weight:600;color:#444;padding:0 8px'>Recipients</legend>"
        "<label style='display:block;margin:8px 0'>"
        "<input type='radio' name='mode' value='all' id='mode-all' checked> "
        "All members</label>"
        + all_sched_block +
        "<label style='display:block;margin:8px 0'>"
        "<input type='radio' name='mode' value='select' id='mode-select'> "
        "Select recipients</label>"
        "<div id='select-section' style='display:none;margin-left:24px;"
        "margin-top:8px'>"
        + sel_full_block
        + cohorts_section
        + "<div style='margin-bottom:12px'>"
        "<p style='font-size:0.9em;color:#666;margin:0 0 4px 0'>Individual members</p>"
        "<div id='selected-users' style='margin-bottom:8px'></div>"
        "<select id='user-picker' style='font-size:0.9em;padding:4px'>"
        f"<option value=''>+ add recipient</option>{member_opts}</select>"
        + quick_pick_buttons
        + "</div>"
        "<div>"
        "<p style='font-size:0.9em;color:#666;margin:0 0 4px 0'>"
        "Additional email addresses (semicolon-separated)</p>"
        "<textarea name='extra_emails' rows='2' "
        "placeholder='alice@example.com; bob@example.com' "
        "style='width:100%;padding:6px;font-size:0.9em;border:1px solid #ccc;"
        "border-radius:4px;box-sizing:border-box;font-family:inherit'></textarea>"
        "</div></div>"
        "</fieldset>"
        "<div style='margin-bottom:16px'>"
        "<label style='display:block;font-weight:600;color:#444;margin-bottom:4px'>"
        "Subject</label>"
        f"<input type='text' name='subject' id='subject-input' required "
        f"value='{html.escape(org_name)} -- ' "
        f"data-default='{html.escape(org_name)} -- ' "
        "style='width:100%;padding:8px;font-size:1em;border:1px solid #ccc;"
        "border-radius:4px;box-sizing:border-box'>"
        "</div>"
        "<div style='margin-bottom:16px'>"
        "<label style='display:block;font-weight:600;color:#444;margin-bottom:4px'>"
        "Message</label>"
        "<textarea name='body' required rows='10' "
        "style='width:100%;padding:8px;font-size:1em;border:1px solid #ccc;"
        "border-radius:4px;box-sizing:border-box;font-family:inherit'></textarea>"
        "</div>"
        "<button type='button' id='send-btn' style='padding:12px 28px;"
        "cursor:pointer;font-size:1.05em;color:white;background:#2a7;"
        "border:none;border-radius:4px;min-width:220px;"
        "font-weight:600'>Send email</button>"
        "</form>"
        "<script>"
        "var modeAll=document.getElementById('mode-all');"
        "var modeSelect=document.getElementById('mode-select');"
        "var selectSection=document.getElementById('select-section');"
        "var allSched=document.getElementById('all-sched');"
        "var selectedDiv=document.getElementById('selected-users');"
        "var picker=document.getElementById('user-picker');"
        "function schedOn(){"
        "if(modeAll.checked){var a=document.getElementById('all-inc');"
        "return !!(a&&a.checked);}"
        "var s=document.getElementById('sel-inc');if(s&&s.checked)return true;"
        "var any=false;document.querySelectorAll('.cohort-sched').forEach("
        "function(cb){if(cb.checked)any=true;});return any;}"
        "function updateBtn(){var b=document.getElementById('send-btn');"
        "if(b)b.textContent=schedOn()?'Send email + schedule':'Send email';}"
        "function updateMode(){"
        "selectSection.style.display=modeSelect.checked?'block':'none';"
        "if(allSched)allSched.style.display=modeAll.checked?'block':'none';"
        "updateBtn();}"
        "function bindInc(cbId,wrapId){"
        "var cb=document.getElementById(cbId),w=document.getElementById(wrapId);"
        "if(cb&&w){cb.addEventListener('change',function(){"
        "w.style.display=cb.checked?'inline':'none';updateBtn();});}}"
        "bindInc('all-inc','all-month-wrap');"
        "bindInc('sel-inc','sel-month-wrap');"
        "document.querySelectorAll('.cohort-sched').forEach(function(cb){"
        "cb.addEventListener('change',function(){"
        "var cid=cb.dataset.cohort;"
        "var sp=document.getElementById('cm-'+cid);"
        "if(sp)sp.style.display=cb.checked?'inline':'none';"
        "var rc=document.getElementById('cohort-'+cid);"
        "if(cb.checked&&rc)rc.checked=true;updateBtn();});});"
        "var modeRadios=document.querySelectorAll('input[name=mode]');"
        "modeRadios.forEach(function(r){r.onchange=updateMode});"
        "updateMode();"
        "function addRecipient(uid,name){"
        "if(document.getElementById('u-'+uid))return;"
        "var sp=document.createElement('span');"
        "sp.id='u-'+uid;"
        "sp.style.cssText='display:inline-block;margin:2px 8px 2px 0;"
        "padding:2px 8px;background:#f0f0f0;border-radius:12px;font-size:0.9em';"
        "sp.innerHTML=name+' <input type=\"hidden\" name=\"user_id\" value=\"'+uid+'\">"
        "<button type=\"button\" onclick=\"this.parentElement.remove()\" "
        "style=\"font-size:0.8em;cursor:pointer;color:#c33;background:none;"
        "border:none;padding:0\">x</button>';"
        "selectedDiv.appendChild(sp);"
        "}"
        "picker.onchange=function(){"
        "if(!this.value)return;"
        "var uid=this.value,name=this.options[this.selectedIndex].text;"
        "this.value='';"
        "addRecipient(uid,name);"
        "};"
        f"var neverLoggedIn={never_logged_in_json};"
        "var qpBtn=document.getElementById('qp-never-logged-in');"
        "if(qpBtn){qpBtn.onclick=function(){"
        "neverLoggedIn.forEach(function(u){addRecipient(u.uid,u.name)});"
        "var subj=document.getElementById('subject-input');"
        "if(subj && subj.value===subj.dataset.default){"
        f"subj.value='Welcome to {html.escape(org_name)}';"
        "}"
        "this.disabled=true;"
        "this.style.color='#aaa';"
        "this.style.borderColor='#ccc';"
        "this.style.cursor='default';"
        "};}"
        f"var notResponded={not_responded_json};"
        "var qpNR=document.getElementById('qp-not-responded');"
        "if(qpNR){qpNR.onclick=function(){"
        "notResponded.forEach(function(u){addRecipient(u.uid,u.name)});"
        "this.disabled=true;"
        "this.style.color='#aaa';"
        "this.style.borderColor='#ccc';"
        "this.style.cursor='default';"
        "};}"
        "document.getElementById('send-btn').onclick=function(){"
        "var form=document.getElementById('send-form');"
        "if(!form.reportValidity())return;"
        "var sched=schedOn();"
        "var confirmPrompt=sched?"
        "'Send this email with the schedule to the selected recipients?':"
        "'Send this email to the selected recipients?';"
        "var confirmBtnLabel=sched?'Send email + schedule':'Send';"
        "var d=document.createElement('div');"
        "d.style.cssText='position:fixed;top:0;left:0;width:100%;height:100%;"
        "background:rgba(0,0,0,0.4);z-index:10000;display:flex;"
        "justify-content:center;align-items:center';"
        "d.innerHTML='<div style=\"background:white;padding:24px 32px;"
        "border-radius:8px;max-width:360px;text-align:center;"
        "font-family:-apple-system,BlinkMacSystemFont,sans-serif\">"
        "<p style=\"font-size:1.1em;margin-bottom:16px\">'"
        "+confirmPrompt+'</p>"
        "<button id=\"confirm-send\" style=\"padding:8px 20px;"
        "cursor:pointer;color:white;background:#2a7;border:none;"
        "border-radius:4px;font-size:1em;margin-right:8px\">'"
        "+confirmBtnLabel+'</button>"
        "<button onclick=\"this.closest(\\x27div\\x27).parentElement.remove()\" "
        "style=\"padding:8px 20px;cursor:pointer;background:#eee;border:none;"
        "border-radius:4px;font-size:1em\">Cancel</button></div>';"
        "document.body.appendChild(d);"
        "document.getElementById('confirm-send').onclick=function(){"
        "d.remove();"
        "document.getElementById('loading').style.display='flex';"
        "form.submit();};"
        "};"
        "</script>"
        + _admin_nav_bar("send-email", app=app)
    )
    return _html(200, _page(body, narrow=False, title=org_name))


def _api_send_email(event: dict, user: User, community: Community | None,
                    app: Application, membership: Membership | None) -> dict:
    """Compose + send an email, optionally attaching a copy of the schedule.

    Audience is 'all' (all members) or 'select' (chosen cohorts + individual
    members + raw addresses). A schedule copy can ride along:

      - "Include a copy of the schedule" (either audience) -> the FULL month
        table (or all active months) goes to everyone targeted.
      - Per selected cohort, "Send this cohort their schedule" -> that
        cohort's members get just THEIR slice (their template's slots).

    Precedence (give more, never less), one email per recipient:
      1. full include -> everyone gets the full table;
      2. else individually-added members + raw addresses get the UNION of all
         selected cohort slices (a "cc"); cohort-only members get their own
         slice(s). See the plan in the send-email rework.
    """
    if not _is_admin(user, membership):
        return _text(403, "admins only")
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method != "POST":
        return _text(405, "POST required")

    raw_body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw_body = base64.b64decode(raw_body).decode("utf-8", errors="replace")
    parsed = urllib.parse.parse_qs(raw_body, keep_blank_values=True)

    def _p(name: str) -> str:
        vals = parsed.get(name) or [""]
        return vals[0] if vals else ""

    mode = _p("mode") or "all"
    # Strip CR/LF from the subject — defense in depth against header
    # injection at the provider layer (security fix H4).
    subject = _safe_header(_p("subject"))
    body_text = _p("body")
    if not subject or not body_text:
        return _error_redirect("/admin/send-email",
            "Subject and message are both required.")

    is_event = app.app_type in EVENT_APP_TYPES
    users_by_id = {u.user_id: u for u in db.list_users(user.community_id)}
    # Opted-out members are not in the recipient universe at all: this set
    # backs "everyone", the cohort intersection, AND the validation of
    # individually-picked ids, so filtering here covers every path. Someone
    # who asked to stop hearing from this group must not keep getting
    # broadcasts from it -- the poll sender has always honoured this
    # (_api_flex_event_send_poll), and this page previously did not.
    member_ids = {m.user_id for m in db.list_memberships_for_app(app.app_id)
                  if not m.opted_out}

    # ---- schedule directives ------------------------------------------
    if mode == "all":
        include_full = bool(_p("all_include_schedule")) and not is_event
        full_selection = _p("all_copy_month")
    else:
        include_full = bool(_p("sel_include_full")) and not is_event
        full_selection = _p("sel_full_month")
    full_months = _resolve_copy_months(app, full_selection) if include_full else []
    if include_full and not full_months:
        return _error_redirect("/admin/send-email",
            "No active schedule available for the month you chose.")

    # Selected cohorts (select mode) + their optional per-cohort slice.
    selected_cohort_ids = ([] if (mode == "all" or is_event)
                           else parsed.get("cohort", []))
    cohorts_by_id = {c.cohort_id: c for c in db.list_cohorts(app.app_id)}
    cohort_members: dict[str, set[str]] = {}
    slice_cell: dict[str, tuple] = {}   # cid -> (yyyy_mm, frozenset({template_id}))
    for cid in selected_cohort_ids:
        c = cohorts_by_id.get(cid)
        if not c:
            continue
        cohort_members[cid] = {cm.user_id for cm in db.list_cohort_members(cid)
                               if cm.user_id in member_ids}
        if _p(f"cohort_sched_{cid}") and c.linked_template_id:
            months = _resolve_copy_months(app, _p(f"cohort_month_{cid}"))
            if months:
                slice_cell[cid] = (months[0], frozenset({c.linked_template_id}))
    union_cells = set(slice_cell.values())   # for individually-added + raw cc

    # ---- recipients ---------------------------------------------------
    individual_uids: set[str] = set()
    cohort_only_uids: set[str] = set()
    if mode == "all":
        recipient_uids = set(member_ids)
    else:
        for cid in selected_cohort_ids:
            cohort_only_uids |= cohort_members.get(cid, set())
        for uid in parsed.get("user_id", []):
            if uid in member_ids:
                individual_uids.add(uid)
        recipient_uids = cohort_only_uids | individual_uids
    raw_extras = _p("extra_emails")
    extra_emails = [a.strip() for a in re.split(r"[;,\s]+", raw_extras)
                    if a.strip() and "@" in a]

    if not recipient_uids and not extra_emails:
        return _error_redirect("/admin/send-email",
            "Pick at least one recipient or enter an email address.")

    has_schedule = bool(full_months) or bool(union_cells)

    from community_organizer.providers.email import get_email_provider
    provider = get_email_provider()
    from_addr = _from_addr(user.name, app.name if app else community.name)
    org = app.name if app else (community.name if community else "")

    # ---- no schedule attached: the plain-announcement path (unchanged
    # behavior: 'all' personalizes per member; 'select' sends one grouped
    # email so recipients can reply-all) --------------------------------
    if not has_schedule:
        sent = 0
        if mode == "select":
            recipients = []
            for uid in recipient_uids:
                u = users_by_id.get(uid)
                if not u or not u.email or u.email_undeliverable or u.channel == "none":
                    continue
                recipients.append(u.email)
            recipients.extend(extra_emails)
            if user.email and user.email not in recipients:
                recipients.append(user.email)
            recipients = sorted(set(recipients))
            if recipients:
                full_body = f"Hi all,\n\n{body_text.strip()}\n\n-- {org}\n"
                provider.send(
                    community_id=community.community_id, from_addr=from_addr,
                    to_addr=recipients[0], to_addrs=recipients,
                    subject=subject, body_text=full_body, kind="other",
                    related_app_id=app.app_id)
                sent = len(recipients)
        else:
            for uid in sorted(recipient_uids):
                u = users_by_id.get(uid)
                if not u or not u.email or u.email_undeliverable or u.channel == "none":
                    continue
                full_body = f"Hi {u.name},\n\n{body_text.strip()}\n\n-- {org}\n"
                provider.send(
                    community_id=community.community_id, from_addr=from_addr,
                    to_addr=u.email, subject=subject, body_text=full_body,
                    kind="other", related_user_id=uid, related_app_id=app.app_id)
                sent += 1
            for addr in extra_emails:
                full_body = f"Hello,\n\n{body_text.strip()}\n\n-- {org}\n"
                provider.send(
                    community_id=community.community_id, from_addr=from_addr,
                    to_addr=addr, subject=subject, body_text=full_body,
                    kind="other", related_app_id=app.app_id)
                sent += 1
        log.info("admin %s sent email to %d recipients (mode=%s), subject=%s",
                 user.user_id, sent, mode, subject)
        return _redirect(f"/admin/send-email?sent={sent}")

    # ---- schedule attached --------------------------------------------
    render_cache: dict = {}

    def _deliverable(u) -> bool:
        return bool(u and u.email and not u.email_undeliverable
                    and u.channel != "none")

    def _html_body(msg: str, cells: set) -> str:
        section = (_render_schedule_cells(community, app, cells, render_cache)
                   if cells else "")
        return ('<div style="font-family:Arial,sans-serif;font-size:14px">'
                f"{_text_to_html_paragraphs(msg)}"
                f"{('<br>' + section) if section else ''}</div>")

    sent = 0
    if full_months:
        # FULL case: personalized, one email per recipient (add-ins included),
        # everyone getting the whole month table(s). No giant CC.
        full_cells = {(m, None) for m in full_months}
        for uid in sorted(recipient_uids):
            u = users_by_id.get(uid)
            if not _deliverable(u):
                continue
            msg = f"Hi {u.name},\n\n{body_text.strip()}\n\n-- {org}\n"
            provider.send(
                community_id=community.community_id, from_addr=from_addr,
                to_addr=u.email, subject=subject, body_text=msg,
                body_html=_html_body(msg, full_cells),
                kind="publish_broadcast", related_user_id=uid,
                related_app_id=app.app_id)
            sent += 1
        for addr in extra_emails:
            msg = f"Hello,\n\n{body_text.strip()}\n\n-- {org}\n"
            provider.send(
                community_id=community.community_id, from_addr=from_addr,
                to_addr=addr, subject=subject, body_text=msg,
                body_html=_html_body(msg, full_cells),
                kind="publish_broadcast", related_app_id=app.app_id)
            sent += 1
    else:
        # COHORT case: one GROUP email per sliced cohort so reply-all reaches
        # the whole cohort. Individually-added members + raw addresses are
        # CC'd on each cohort email, and the sender rides
        # along so reply-all includes them.
        addin_emails = [users_by_id[uid].email for uid in sorted(individual_uids)
                        if _deliverable(users_by_id.get(uid))]
        addin_emails += extra_emails
        emailed_uids: set[str] = set()
        for cid, cell in slice_cell.items():
            members = [uid for uid in sorted(cohort_members.get(cid, set()))
                       if _deliverable(users_by_id.get(uid))]
            to = [users_by_id[uid].email for uid in members] + list(addin_emails)
            if user.email:
                to.append(user.email)
            to = sorted(set(to))
            if not to:
                continue
            msg = f"Hi all,\n\n{body_text.strip()}\n\n-- {org}\n"
            provider.send(
                community_id=community.community_id, from_addr=from_addr,
                to_addr=to[0], to_addrs=to, subject=subject, body_text=msg,
                body_html=_html_body(msg, {cell}),
                kind="publish_broadcast", related_app_id=app.app_id)
            sent += len(to)
            emailed_uids.update(members)
            emailed_uids.update(individual_uids)
        # Members of a selected cohort with NO slice checked still asked to be
        # recipients — send them the message (no schedule) as one group so
        # nobody is silently dropped. Individuals/raw were CC'd above.
        leftover = [uid for uid in sorted(recipient_uids)
                    if uid not in emailed_uids and uid not in individual_uids
                    and _deliverable(users_by_id.get(uid))]
        if leftover:
            to = sorted({users_by_id[uid].email for uid in leftover}
                        | ({user.email} if user.email else set()))
            msg = f"Hi all,\n\n{body_text.strip()}\n\n-- {org}\n"
            provider.send(
                community_id=community.community_id, from_addr=from_addr,
                to_addr=to[0], to_addrs=to, subject=subject, body_text=msg,
                body_html=_html_body(msg, set()),
                kind="other", related_app_id=app.app_id)
            sent += len(to)
    log.info("admin %s sent schedule email (mode=%s, cohort_group=%s) to %d, subj=%s",
             user.user_id, mode, bool(slice_cell) and not full_months, sent, subject)
    return _redirect(f"/admin/send-email?sent={sent}")


def _cohorts_page(event: dict, user: User, community: Community | None,
                  app: Application, membership: Membership | None) -> dict:
    if not _is_admin(user, membership):
        return _html(403, _page("<p>Admins only.</p><p><a href='/'>Back</a></p>"))
    _cleanup_stale_cohorts(app.app_id)
    org_name = app.name if app else (community.name if community else user.community_id)
    templates_by_id = {t.template_id: t for t in db.list_templates(app.app_id)}
    def _cohort_sort_key(c):
        tpl = templates_by_id.get(c.linked_template_id or "")
        if tpl:
            return (tpl.day_of_week, tpl.start_time)
        return (99, c.name)
    cohorts = sorted(db.list_cohorts(app.app_id), key=_cohort_sort_key)
    # PRIVACY-AUDIT HIGH-3: scope users_by_id to this app's members
    # only. Cohort memberships can in theory reference any
    # community_user_id, but the only way a non-member can land in
    # a cohort here is via DB cruft — we render those as
    # "(unknown user)" to avoid leaking names from other apps.
    members_by_app = {m.user_id for m in db.list_memberships_for_app(app.app_id)}
    users_by_id = {u.user_id: u
                   for u in db.list_users(user.community_id)
                   if u.user_id in members_by_app}

    sections = ""
    for c in cohorts:
        cmems = list(db.list_cohort_members(c.cohort_id))
        cmem_ids = {cm.user_id for cm in cmems}
        member_rows = ""
        for cm in cmems:
            u = users_by_id.get(cm.user_id)
            name = u.name if u else "Unknown"
            member_rows += (
                f"<span style='display:inline-block;margin:2px 8px 2px 0;"
                f"padding:2px 8px;background:#f0f0f0;border-radius:12px;font-size:0.9em'>"
                f"{html.escape(name)} "
                f"<form method='post' action='/api/cohorts/remove-member"
                f"?cohort_id={c.cohort_id}&user_id={cm.user_id}' style='display:inline'>"
                "<button type='submit' style='font-size:0.8em;cursor:pointer;"
                "color:#c33;background:none;border:none;padding:0'>x</button>"
                "</form></span>"
            )
        if not member_rows:
            member_rows = "<span style='color:#aaa;font-size:0.9em'>No members yet</span>"
        available = sorted(
            [(uid, users_by_id[uid].name) for uid in members_by_app
             if uid in users_by_id and uid not in cmem_ids],
            key=lambda x: x[1],
        )
        add_form = ""
        if available:
            opts = "".join(
                f"<option value='{uid}'>{html.escape(name)}</option>"
                for uid, name in available
            )
            add_form = (
                f"<form method='post' action='/api/cohorts/add-member"
                f"?cohort_id={c.cohort_id}' style='display:inline;margin-left:8px'>"
                "<select name='user_id' style='font-size:0.85em;padding:2px'"
                " onchange=\"if(this.value){document.getElementById('loading')"
                ".style.display='flex';this.form.submit()}\">"
                f"<option value=''>+ add</option>{opts}</select></form>"
            )
        delete_link = ""
        if user.community_role in ("ca", "ua"):
            delete_link = (
                f" <form method='post' action='/api/cohorts/delete"
                f"?cohort_id={c.cohort_id}' style='display:inline'"
                f" onsubmit=\"return confirmSubmit(this,"
                f"'Delete cohort: {html.escape(c.name)}?','Delete','#c33')\">"
                "<button type='submit' style='font-size:0.75em;cursor:pointer;"
                "color:#c33;background:none;border:none;text-decoration:underline;"
                "padding:0'>delete</button></form>"
            )
        sections += (
            f"<div style='margin:16px 0;padding:12px;border:1px solid #eee;"
            f"border-radius:8px'>"
            f"<h3 style='font-size:1em;color:#444;margin:0 0 8px 0'>"
            f"{html.escape(c.name)}{delete_link}</h3>"
            f"<div>{member_rows}{add_form}</div>"
            f"</div>"
        )

    body = (
        f"<h1>{html.escape(org_name)}</h1>"
        "<h2 style='font-size:1.1em;color:#444'>Manage cohorts</h2>"
        "<p style='color:#888;font-size:0.85em'>Cohorts are groups of "
        "users who typically cover the same events. When someone releases "
        "a slot, their cohort is notified.</p>"
        + _flash_banner_html(event)
        + (sections or "<p style='color:#888'>No cohorts yet. "
           "Cohorts are auto-created when you add events to the template.</p>")
        + _admin_nav_bar("cohorts", app=app)
    )
    return _html(200, _page(body, narrow=False, title=org_name))


def _schedule_visible(sch: "Schedule | None") -> bool:
    """True if the schedule is in a state where users see the slots
    and can act on them.

    For coverage apps that's ``state == "published"``; for
    recurring_commitments apps the analog is ``state ==
    "materialized"`` (no publish event happens — lazy materialization
    makes the slots visible the moment the Schedule row appears).

    Callsites that need to gate user-facing actions (signup,
    release, cohort-opening notifications) should use this helper
    rather than hard-coding "published" so they work in both app types.
    """
    return bool(sch and sch.state in ("published", "materialized"))


def _next_occurrence_on_or_after(today: dt.date, day_of_week: int) -> dt.date:
    """Return the next date on/after ``today`` that lands on
    ``day_of_week`` (Python convention: Mon=0..Sun=6)."""
    delta = (day_of_week - today.weekday()) % 7
    return today + dt.timedelta(days=delta)


def _fill_future_cohort_assignments(community: Community,
                                    app: Application, cohort: Cohort,
                                    user_id: str) -> int:
    """When a user joins a cohort AFTER one or more periods have
    already been materialized, retro-fill their Assignment row for
    each of those slots.

    Returns the count of Assignments created. Only operates on
    recurring_commitments apps. Idempotent (skips slots they're
    already assigned to).
    """
    if app.app_type != "recurring_commitments":
        return 0
    if not cohort.linked_template_id:
        return 0
    added = 0
    for sch in db.list_schedules(app.app_id):
        if sch.state != "materialized":
            continue
        for slot in db.list_slots(app.app_id, sch.yyyy_mm):
            if slot.template_id != cohort.linked_template_id:
                continue
            existing = any(
                a.user_id == user_id for a in
                db.list_assignments_for_slot(app.app_id, sch.yyyy_mm,
                                             slot.slot_id))
            if existing:
                continue
            db.put_assignment(Assignment(
                community_id=community.community_id, app_id=app.app_id,
                yyyy_mm=sch.yyyy_mm, slot_id=slot.slot_id,
                user_id=user_id, local_date=slot.local_date,
                created_by="cohort-join",
            ))
            added += 1
    return added


def _delete_future_cohort_assignments(app: Application, cohort: Cohort,
                                      user_id: str) -> int:
    """Inverse of _fill_future_cohort_assignments: when a user leaves
    a cohort, remove their Assignment rows for every materialized
    future slot tied to the cohort's linked template.

    Only operates on recurring_commitments apps. Past slots are left
    untouched — the assignment history stays intact.
    """
    if app.app_type != "recurring_commitments":
        return 0
    if not cohort.linked_template_id:
        return 0
    today_str = dt.date.today().isoformat()
    removed = 0
    for sch in db.list_schedules(app.app_id):
        if sch.state != "materialized":
            continue
        for slot in db.list_slots(app.app_id, sch.yyyy_mm):
            if slot.template_id != cohort.linked_template_id:
                continue
            if slot.local_date < today_str:
                continue
            for a in db.list_assignments_for_slot(
                    app.app_id, sch.yyyy_mm, slot.slot_id):
                if a.user_id == user_id:
                    db.delete_assignment(
                        app.app_id, sch.yyyy_mm,
                        slot.slot_id, user_id)
                    removed += 1
    return removed


def _send_cohort_recurring_invite(community: Community, app: Application,
                                  cohort: Cohort, target_user: User) -> None:
    """Email the joining user a single RRULE-based VCALENDAR so their
    calendar fills in every future occurrence in one click.

    Silently no-ops if the user has no email, is bounced/complained,
    if the cohort isn't linked to a template, or if the email
    provider raises — we don't want a failed invite to block the
    join. The log captures any provider error.
    """
    if app.app_type != "recurring_commitments":
        return
    if not target_user.email or target_user.email_undeliverable:
        return
    if not cohort.linked_template_id:
        return
    tpl = db.get_template(app.app_id, cohort.linked_template_id)
    if tpl is None:
        return

    tz_name = (app.default_timezone or community.default_timezone
               or "America/New_York")
    domain = os.environ.get("DOMAIN_NAME", "community.example.org")
    today = dt.date.today()
    first_date = _next_occurrence_on_or_after(today, tpl.day_of_week)
    horizon_months = app.visible_horizon_months or 6
    # Approximate; calendar app doesn't need precision past the day.
    until_date = today + dt.timedelta(days=horizon_months * 30)

    description = community.name if community else app.name
    if tpl.arrival_offset_minutes:
        h, m = (int(x) for x in tpl.start_time.split(":"))
        arrival = (dt.datetime(2000, 1, 1, h, m)
                   - dt.timedelta(minutes=tpl.arrival_offset_minutes))
        description += (f" — {app.arrival_label or 'please arrive by'} "
                        f"{_fmt_time(f'{arrival.hour:02d}:{arrival.minute:02d}')}")

    body = ical.make_recurring_event_ics(
        cohort_id=cohort.cohort_id, user_id=target_user.user_id,
        user_email=target_user.email,
        summary=tpl.name, description=description,
        day_of_week=tpl.day_of_week, start_time=tpl.start_time,
        duration_minutes=tpl.duration_minutes,
        first_date=first_date, until_date=until_date,
        domain=domain, timezone=tz_name,
        alarm_minutes=target_user.calendar_alarm_minutes,
    )

    text = (
        f"Hi {target_user.name},\n\n"
        f"Thanks for committing to {tpl.name}! The attached calendar "
        "invite covers every future occurrence so you can add it "
        "to your calendar once.\n\n"
        "If you ever need to release a specific week, return to "
        f"https://{domain}/ and use the Withdraw button on that "
        "occurrence; the rest of your commitments stay intact.\n\n"
        f"-- {community.name if community else app.name}\n"
    )
    from community_organizer.providers.email import get_email_provider
    provider = get_email_provider()
    from_addr = f"organizer@{domain}"
    try:
        provider.send(
            community_id=community.community_id,
            from_addr=from_addr, to_addr=target_user.email,
            subject=f"{community.name if community else app.name}"
                    f" -- recurring calendar invite: {tpl.name}",
            body_text=text, kind="change_notification",
            related_user_id=target_user.user_id,
            related_app_id=app.app_id,
            ics_content=body,
        )
    except Exception as e:    # noqa: BLE001
        log.warning("recurring invite send failed for user=%s cohort=%s: %s",
                    target_user.user_id, cohort.cohort_id, e)


def _send_cohort_cancel(community: Community, app: Application,
                        cohort: Cohort, target_user: User) -> None:
    """Email the leaving user a METHOD:CANCEL .ics so their calendar
    removes the recurring series.

    Same fail-soft contract as _send_cohort_recurring_invite.
    """
    if app.app_type != "recurring_commitments":
        return
    if not target_user.email or target_user.email_undeliverable:
        return
    if not cohort.linked_template_id:
        return
    tpl = db.get_template(app.app_id, cohort.linked_template_id)
    if tpl is None:
        return

    tz_name = (app.default_timezone or community.default_timezone
               or "America/New_York")
    domain = os.environ.get("DOMAIN_NAME", "community.example.org")
    today = dt.date.today()
    # The first_date the original invite used is unknowable here, so
    # use any matching weekday on/after today. The UID is what
    # matters for calendar identity; the DTSTART is mostly decorative
    # on a CANCEL.
    first_date = _next_occurrence_on_or_after(today, tpl.day_of_week)

    body = ical.make_recurring_cancel_ics(
        cohort_id=cohort.cohort_id, user_id=target_user.user_id,
        user_email=target_user.email,
        summary=tpl.name, day_of_week=tpl.day_of_week,
        start_time=tpl.start_time, duration_minutes=tpl.duration_minutes,
        first_date=first_date,
        domain=domain, timezone=tz_name,
    )
    text = (
        f"Hi {target_user.name},\n\n"
        f"You've been removed from {tpl.name}. The attached cancellation "
        "tells your calendar to drop the recurring entry.\n\n"
        f"-- {community.name if community else app.name}\n"
    )
    from community_organizer.providers.email import get_email_provider
    provider = get_email_provider()
    from_addr = f"organizer@{domain}"
    try:
        provider.send(
            community_id=community.community_id,
            from_addr=from_addr, to_addr=target_user.email,
            subject=f"{community.name if community else app.name}"
                    f" -- cancelled: {tpl.name}",
            body_text=text, kind="change_notification",
            related_user_id=target_user.user_id,
            related_app_id=app.app_id,
            ics_content=body,
        )
    except Exception as e:    # noqa: BLE001
        log.warning("recurring cancel send failed for user=%s cohort=%s: %s",
                    target_user.user_id, cohort.cohort_id, e)


def _notify_admins_of_cohort_change(community: Community | None,
                                    app: Application,
                                    actor: User,
                                    target: User,
                                    cohort,
                                    action: str) -> None:
    """Send an at-a-glance heads-up to every app/community admin when
    cohort membership changes. Skips the actor (no point telling the
    admin who did it). Fail-soft: any send error is logged, not raised.

    ``action`` is "joined" or "left".
    """
    if community is None or app is None:
        return
    try:
        users_by_id = {u.user_id: u
                       for u in db.list_users(community.community_id)}
        admin_ids: set[str] = set()
        for m in db.list_memberships_for_app(app.app_id):
            if m.app_role == "aa":
                admin_ids.add(m.user_id)
        for u in users_by_id.values():
            if u.community_role in ("ca", "ua"):
                admin_ids.add(u.user_id)
        admin_ids.discard(actor.user_id)
        recipients = sorted(
            users_by_id[uid].email
            for uid in admin_ids
            if uid in users_by_id
            and users_by_id[uid].email
            and not users_by_id[uid].email_undeliverable
        )
        if not recipients:
            return
        from community_organizer.providers.email import get_email_provider
        provider = get_email_provider()
        from_addr = _from_addr(None, app.name)
        # Body covers self-service vs admin-acted-on-member symmetrically;
        # the difference shows in the "by" line so admins can tell at a
        # glance whether to follow up with the affected member.
        if actor.user_id == target.user_id:
            actor_line = "(self-service)"
        else:
            actor_line = f"(by admin: {actor.name})"
        subject = (f"{app.name} -- {target.name} {action} cohort "
                   f"{cohort.name}")
        body = (
            f"Cohort change in {app.name}:\n\n"
            f"  {target.name} <{target.email or '?'}>\n"
            f"  {action} cohort: {cohort.name}\n"
            f"  {actor_line}\n\n"
            f"You're receiving this because you are an admin of "
            f"{app.name} or of {community.name}.\n"
        )
        provider.send(
            community_id=community.community_id,
            from_addr=from_addr,
            to_addr=recipients[0],
            to_addrs=recipients,
            subject=subject,
            body_text=body,
            kind="change_notification",
            related_app_id=app.app_id,
            related_user_id=target.user_id,
        )
        log.info("notified %d admin(s) of cohort %s by %s for %s",
                 len(recipients), action, actor.user_id, target.user_id)
    except Exception as e:  # noqa: BLE001
        log.warning("admin cohort-change notification failed: %s", e)


def _api_cohort_add_member(event: dict, user: User, community: Community | None,
                           app: Application, membership: Membership | None) -> dict:
    """Add a user to a cohort.

    Auth: admins can add anyone; non-admin members can add **themselves
    only** (self-service cohort join — security fix for the Recurring
    Commitments app type where cohort signup is user-driven, not
    admin-curated).

    Also verifies the cohort belongs to the caller's current
    Application — closes the cross-app cohort manipulation gap from
    the original audit's #14 finding.
    """
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method != "POST":
        return _text(405, "POST required")
    cohort_id = _get_param(event, "cohort_id")
    uid = _get_param(event, "user_id")
    if not cohort_id or not uid:
        return _error_redirect_or_next(event, "/admin/cohorts",
            "Missing cohort id or user id.")
    is_admin = _is_admin(user, membership)
    is_self = uid == user.user_id
    if not (is_admin or is_self):
        return _error_redirect_or_next(event, "/admin/cohorts",
            "You can only add yourself to a cohort.")
    cohort = db.get_cohort(app.app_id, cohort_id)
    if cohort is None or cohort.app_id != app.app_id:
        # Either doesn't exist or belongs to another app — same
        # response either way to avoid leaking which.
        return _error_redirect_or_next(event, "/admin/cohorts",
            "Cohort not found.")
    from community_organizer.core.models import CohortMembership
    cm = CohortMembership(cohort_id=cohort_id, user_id=uid)
    db.put_cohort_membership(cm)
    # Recurring-app onboarding side effects (no-ops for coverage apps):
    # auto-create Membership if absent (cohort commitment implies app
    # membership), back-fill Assignment rows for any already-
    # materialized future periods, then ship the RRULE invite email.
    # All are fail-soft; CohortMembership is the source of truth.
    if community is not None:
        target_user = db.get_user(user.community_id, uid)
        cohort_full = db.get_cohort(app.app_id, cohort_id)
        if target_user is not None and cohort_full is not None:
            if (app.app_type == "recurring_commitments"
                    and db.get_membership(app.app_id, uid) is None):
                db.put_membership(Membership(
                    community_id=user.community_id, app_id=app.app_id,
                    user_id=uid, app_role="member",
                ))
                log.info("auto-membership for user %s in app %s "
                         "(via cohort join)", uid, app.app_id)
            n_added = _fill_future_cohort_assignments(
                community, app, cohort_full, uid)
            if n_added:
                log.info("cohort-join back-filled %d assignments for %s",
                         n_added, uid)
            _send_cohort_recurring_invite(
                community, app, cohort_full, target_user)
    # Heads-up to every other admin so they see the roster change
    # without having to poll /admin/cohorts. Self-service AND
    # admin-driven (skip the actor either way).
    target_for_email = (db.get_user(user.community_id, uid)
                        if not is_self else user)
    if target_for_email is not None:
        _notify_admins_of_cohort_change(
            community, app, actor=user,
            target=target_for_email, cohort=cohort,
            action="joined",
        )
    if is_admin and not is_self:
        log.info("admin %s added user %s to cohort %s",
                 user.user_id, uid, cohort_id)
        return _redirect_next(event, "/admin/cohorts")
    log.info("user %s self-joined cohort %s", user.user_id, cohort_id)
    return _redirect(_safe_next(_get_param(event, "next")) or "/your-schedule")


def _api_cohort_remove_member(event: dict, user: User, community: Community | None,
                              app: Application, membership: Membership | None) -> dict:
    """Remove a user from a cohort.

    Auth + redirect semantics mirror ``_api_cohort_add_member``.
    """
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method != "POST":
        return _text(405, "POST required")
    cohort_id = _get_param(event, "cohort_id")
    uid = _get_param(event, "user_id")
    if not cohort_id or not uid:
        return _error_redirect_or_next(event, "/admin/cohorts",
            "Missing cohort id or user id.")
    is_admin = _is_admin(user, membership)
    is_self = uid == user.user_id
    if not (is_admin or is_self):
        return _error_redirect_or_next(event, "/admin/cohorts",
            "You can only remove yourself from a cohort.")
    cohort = db.get_cohort(app.app_id, cohort_id)
    if cohort is None or cohort.app_id != app.app_id:
        return _error_redirect_or_next(event, "/admin/cohorts",
            "Cohort not found.")
    # A member cannot remove themselves
    # from their LAST cohort in the app — leaves no way for the
    # admin to assign them. UI hides the button in this case;
    # the server-side check is defense against hand-crafted POSTs.
    # Admin-driven removals are unaffected (admin can fully detach
    # someone if needed).
    if is_self and not is_admin:
        in_app_cohort_ids = {
            c.cohort_id for c in db.list_cohorts(app.app_id)
        }
        my_app_cohort_ids = {
            cm.cohort_id for cm in db.list_cohorts_for_user(uid)
            if cm.cohort_id in in_app_cohort_ids
        }
        if cohort_id in my_app_cohort_ids and len(my_app_cohort_ids) <= 1:
            return _error_redirect_or_next(event, "/settings",
                "You must be in at least one cohort. Join another "
                "cohort first if you want to leave this one.")
    db.delete_cohort_membership(cohort_id, uid)
    # Recurring-app cleanup side effects: delete future Assignment
    # rows for slots tied to this cohort's template, and send the
    # METHOD:CANCEL .ics so the user's calendar removes the series.
    # Past Assignments stay so the history is preserved.
    if community is not None:
        target_user = db.get_user(user.community_id, uid)
        cohort_full = db.get_cohort(app.app_id, cohort_id)
        if target_user is not None and cohort_full is not None:
            n_removed = _delete_future_cohort_assignments(
                app, cohort_full, uid)
            if n_removed:
                log.info("cohort-leave deleted %d future assignments for %s",
                         n_removed, uid)
            _send_cohort_cancel(community, app, cohort_full, target_user)
    # Heads-up to every other admin (skip the actor).
    target_for_email = (db.get_user(user.community_id, uid)
                        if not is_self else user)
    if target_for_email is not None:
        _notify_admins_of_cohort_change(
            community, app, actor=user,
            target=target_for_email, cohort=cohort,
            action="left",
        )
    if is_admin and not is_self:
        log.info("admin %s removed user %s from cohort %s",
                 user.user_id, uid, cohort_id)
        return _redirect_next(event, "/admin/cohorts")
    log.info("user %s self-left cohort %s", user.user_id, cohort_id)
    return _redirect(_safe_next(_get_param(event, "next")) or "/your-schedule")


def _api_cohort_delete(event: dict, user: User, community: Community | None,
                       app: Application, membership: Membership | None) -> dict:
    if user.community_role not in ("ca", "ua"):
        return _text(403, "only Community Admins can delete cohorts")
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method != "POST":
        return _text(405, "POST required")
    cohort_id = _get_param(event, "cohort_id")
    if not cohort_id:
        return _error_redirect("/admin/cohorts", "Missing cohort id.")
    for cm in db.list_cohort_members(cohort_id):
        db.delete_cohort_membership(cohort_id, cm.user_id)
    db.delete_cohort(app.app_id, cohort_id)
    log.info("CA %s deleted cohort %s", user.user_id, cohort_id)
    return _redirect("/admin/cohorts")


_HELP_FILES = {
    "admin": "help-admin.md",
    "member": "help-member.md",
}
_HELP_TITLES = {
    "admin": "App Admin Guide",
    "member": "Member Guide",
}


def _md_inline(s: str) -> str:
    """HTML-escape, then re-apply bold/italic/code markdown.

    The docs are static markdown shipped in the repo, but we substitute
    placeholders ({{app_name}} etc.) with admin-controllable values
    BEFORE this conversion runs. Without escaping, a CA who sets the
    app name to ``<img src=x onerror=…>`` would XSS every visitor of
    the **public** /help/* pages. So we escape the entire line first,
    then re-introduce safe formatting via regex on the already-escaped
    text.
    """
    import re
    s = html.escape(s, quote=True)
    s = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', s)
    s = re.sub(r'\*(.+?)\*', r'<i>\1</i>', s)
    s = re.sub(r'`(.+?)`', r'<code>\1</code>', s)
    return s


def _md_to_html(md: str) -> str:
    lines = md.split("\n")
    out: list[str] = []
    in_table = False
    in_list = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("# "):
            continue
        if stripped.startswith("## "):
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<h2 style='color:#444;margin-top:32px;border-bottom:1px solid #eee;"
                       f"padding-bottom:8px'>{_md_inline(stripped[3:])}</h2>")
            continue
        if stripped.startswith("### "):
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<h3 style='color:#444;margin-top:24px'>{_md_inline(stripped[4:])}</h3>")
            continue
        if stripped.startswith("---"):
            continue
        if stripped.startswith("| ") and "---" not in stripped:
            if not in_table:
                out.append("<table style='border-collapse:collapse;width:100%;"
                           "font-size:0.9em;margin:12px 0'>")
                in_table = True
            cells = [c.strip() for c in stripped.split("|")[1:-1]]
            tag = "th" if in_table and not any("<td" in o for o in out[-3:]) else "td"
            row = "".join(f"<{tag} style='padding:6px 8px;border:1px solid #ddd;"
                          f"text-align:left'>{_md_inline(c)}</{tag}>" for c in cells)
            out.append(f"<tr>{row}</tr>")
            continue
        if stripped.startswith("|") and "---" in stripped:
            continue
        if in_table and not stripped.startswith("|"):
            out.append("</table>")
            in_table = False
        if stripped.startswith("- **") or stripped.startswith("- "):
            if not in_list:
                out.append("<ul style='line-height:1.8;padding-left:24px'>")
                in_list = True
            out.append(f"<li>{_md_inline(stripped[2:])}</li>")
            continue
        if stripped.startswith("  - "):
            out.append(f"<li style='margin-left:16px'>{_md_inline(stripped[4:])}</li>")
            continue
        if in_list and not stripped:
            out.append("</ul>")
            in_list = False
        if stripped:
            out.append(f"<p style='color:#555;line-height:1.6;margin:8px 0'>{_md_inline(stripped)}</p>")
    if in_list:
        out.append("</ul>")
    if in_table:
        out.append("</table>")
    return "\n".join(out)


def _substitute_doc_placeholders(md: str, community, app) -> str:
    event_s = (app.event_noun if app else "") or "event"
    event_p = (app.event_noun_plural if app else "") or _pluralize(event_s)
    vol_s = (app.terminology if app else "") or "volunteer"
    vol_p = (app.terminology_plural if app else "") or _pluralize(vol_s)
    arrival = (app.arrival_label if app else "") or "please arrive by"
    app_name = (app.name if app else "") or "your application"
    comm_name = (community.name if community else "") or "your community"
    from_addr = f"organizer@{DOMAIN_NAME}"
    subs = {
        "{{event}}": event_s, "{{events}}": event_p,
        "{{Event}}": event_s.capitalize(), "{{Events}}": event_p.capitalize(),
        "{{volunteer}}": vol_s, "{{volunteers}}": vol_p,
        "{{Volunteer}}": vol_s.capitalize(), "{{Volunteers}}": vol_p.capitalize(),
        "{{app_name}}": app_name, "{{community_name}}": comm_name,
        "{{arrival_label}}": arrival, "{{from_address}}": from_addr,
    }
    for k, v in subs.items():
        md = md.replace(k, v)
    return md


def _help_page(role: str) -> dict:
    filename = _HELP_FILES.get(role)
    if not filename:
        return _text(404, "not found")
    import pathlib
    md_path = pathlib.Path(__file__).parent.parent / filename
    try:
        md = md_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _text(404, "help file not found")
    # PRIVACY-AUDIT MED-1: don't load the first community Application
    # for an UNAUTHENTICATED help page — that revealed an app name
    # before login. The help content is generic enough to render with
    # just the community name as branding.
    community_id = os.environ.get("COMMUNITY_ID", "")
    community = db.get_community(community_id) if community_id else None
    app = None
    org_name = community.name if community else "Community Organizer"
    title = _HELP_TITLES.get(role, "Help")
    md = _substitute_doc_placeholders(md, community, app)
    content_html = _md_to_html(md)
    safe_org = html.escape(org_name, quote=True)
    body = (
        f"<div style='text-align:left'>"
        f"<h1>{safe_org}</h1>"
        + content_html
        + "<p style='margin-top:32px'><a href='/'>Back to Home</a></p>"
        "</div>"
    )
    return _html(200, _page(body, narrow=False, title=f"{title} - {safe_org}"))


def _login_page(event: dict) -> dict:
    error = _get_param(event, "error") or ""
    msg = _get_param(event, "msg") or ""
    next_url = _safe_next(_get_param(event, "next"))
    community_id = os.environ.get("COMMUNITY_ID", "")
    community = db.get_community(community_id) if community_id else None
    org_name = community.name if community else "Community Organizer"
    banner = ""
    if error:
        banner = (f"<p style='color:#c33;margin-bottom:16px'>"
                  f"{html.escape(error)}</p>")
    elif msg:
        banner = (f"<p style='color:#2a7;margin-bottom:16px'>"
                  f"{html.escape(msg)}</p>")
    next_escaped = html.escape(next_url)
    body = (
        f"<h1 style='margin-bottom:8px'>{html.escape(org_name)}</h1>"
        "<p style='color:#888;margin-bottom:32px'>Sign in to continue</p>"
        + banner
        + "<div style='display:flex;flex-direction:column;gap:12px;"
        "align-items:center;margin-bottom:24px'>"
        # Google — same blue button as before.
        f"<a href='/login/google?next={urllib.parse.quote(next_url)}' "
        "style='display:inline-block;padding:12px 24px;"
        "background:#4285f4;color:white;text-decoration:none;"
        "border-radius:4px;font-size:1em;min-width:240px;"
        "text-align:center'>Sign in with Google</a>"
        # Apple — black per Apple HIG. Apple's icon would normally
        # appear but we keep text-only to avoid licensing the SVG.
        f"<a href='/login/apple?next={urllib.parse.quote(next_url)}' "
        "style='display:inline-block;padding:12px 24px;"
        "background:#000;color:white;text-decoration:none;"
        "border-radius:4px;font-size:1em;min-width:240px;"
        "text-align:center'>Sign in with Apple</a>"
        # Pre-click warning re: Hide My Email. Apple persists the
        # privacy choice for this Services ID + Apple ID, so a
        # user who picks Hide My Email and then wants to switch
        # later has to revoke the app at appleid.apple.com. Better
        # to warn before they click than to recover after.
        # Use stronger language here: if the user picks
        # Hide My Email, the relay address we receive won't match
        # the one their admin already added, so they can't be
        # matched to the right account.
        "<p style='font-size:0.85em;color:#a00;max-width:300px;"
        "margin:0;line-height:1.4;text-align:center;font-weight:600'>"
        "When asked by Apple, you <u>must</u> share your real email "
        "address. Your administrator added that address to the system "
        "when they invited you, and Hide My Email won't match."
        "</p>"
        "</div>"
        "<div style='color:#888;margin:24px 0'>or sign in with email</div>"
        "<form method='post' action='/login/password' "
        "style='display:flex;flex-direction:column;gap:12px;align-items:center;"
        "max-width:300px;margin:0 auto'>"
        f"<input type='hidden' name='next' value='{next_escaped}'>"
        "<input type='email' name='email' placeholder='Email' required "
        "style='padding:10px;width:100%;font-size:1em;border:1px solid #ccc;"
        "border-radius:4px'>"
        "<input type='password' name='password' placeholder='Password' required "
        "style='padding:10px;width:100%;font-size:1em;border:1px solid #ccc;"
        "border-radius:4px'>"
        "<button type='submit' style='padding:10px 24px;cursor:pointer;"
        "font-size:1em;width:100%;background:#2a7;color:white;border:none;"
        "border-radius:4px'>Sign in</button>"
        "<p style='margin-top:12px'><a href='/login/forgot' "
        "style='font-size:0.9em;color:#888'>New user or forgot your password?</a></p>"
        "</form>"
    )
    return _html(200, _page(body, title=org_name))


def _login_password(event: dict) -> dict:
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method != "POST":
        return _redirect("/login")
    email = _get_param(event, "email")
    password = _get_param(event, "password")
    # _safe_next constrains to local paths; rejects off-host targets
    # (open-redirect / phishing prevention — security fix H3).
    next_url = _safe_next(_get_param(event, "next"))
    if not email or not password:
        return _redirect("/login?error=Email+and+password+required")
    try:
        resp = _get_cognito().admin_initiate_auth(
            UserPoolId=USER_POOL_ID,
            ClientId=auth.USER_POOL_CLIENT_ID,
            AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": email, "PASSWORD": password},
        )
    except _get_cognito().exceptions.NotAuthorizedException:
        return _redirect("/login?error=Incorrect+email+or+password")
    except _get_cognito().exceptions.UserNotFoundException:
        return _redirect("/login?error=Incorrect+email+or+password")
    except Exception as e:
        log.warning("login failed for %s: %s", email, e)
        return _redirect("/login?error=Login+failed")
    if resp.get("ChallengeName") == "NEW_PASSWORD_REQUIRED":
        session = resp["Session"]
        return _new_password_page(email, session)
    result = resp.get("AuthenticationResult") or {}
    id_token = result.get("IdToken")
    refresh_token = result.get("RefreshToken")
    if not id_token:
        return _redirect("/login?error=Login+failed")
    try:
        claims = auth.verify_id_token(id_token)
        _record_login(claims)
    except Exception as e:
        log.warning("could not record login after password auth: %s", e)
    cookies = [
        auth.set_cookie(auth.ID_COOKIE, id_token,
                        max_age=result.get("ExpiresIn", 3600)),
    ]
    if refresh_token:
        cookies.append(auth.set_cookie(auth.REFRESH_COOKIE, refresh_token,
                                       max_age=30 * 24 * 3600))
    return {"statusCode": 302, "headers": {"Location": next_url},
            "cookies": cookies, "body": ""}


def _new_password_page(email: str, session: str, error: str = "") -> dict:
    community_id = os.environ.get("COMMUNITY_ID", "")
    community = db.get_community(community_id) if community_id else None
    org_name = community.name if community else "Community Organizer"
    error_html = (f"<p style='color:#c33;margin-bottom:16px'>"
                  f"{html.escape(error)}</p>" if error else "")
    body = (
        f"<h1 style='margin-bottom:8px'>{html.escape(org_name)}</h1>"
        "<p style='color:#888;margin-bottom:24px'>Welcome! Please set a new password.</p>"
        + error_html
        + "<form method='post' action='/login/new-password' "
        "style='display:flex;flex-direction:column;gap:12px;align-items:center;"
        "max-width:300px;margin:0 auto'>"
        f"<input type='hidden' name='email' value='{html.escape(email)}'>"
        f"<input type='hidden' name='session' value='{html.escape(session)}'>"
        "<input type='password' name='new_password' placeholder='New password' "
        "required minlength='8' "
        "style='padding:10px;width:100%;font-size:1em;border:1px solid #ccc;"
        "border-radius:4px'>"
        "<input type='password' name='confirm_password' placeholder='Confirm password' "
        "required minlength='8' "
        "style='padding:10px;width:100%;font-size:1em;border:1px solid #ccc;"
        "border-radius:4px'>"
        "<button type='submit' style='padding:10px 24px;cursor:pointer;"
        "font-size:1em;width:100%;background:#2a7;color:white;border:none;"
        "border-radius:4px'>Set password &amp; sign in</button>"
        "<p style='font-size:0.85em;color:#888'>Minimum 8 characters, "
        "including uppercase, lowercase, and a number.</p>"
        "</form>"
    )
    return _html(200, _page(body, title=org_name))


def _login_new_password(event: dict) -> dict:
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method != "POST":
        return _redirect("/login")
    email = _get_param(event, "email")
    session = _get_param(event, "session")
    new_password = _get_param(event, "new_password")
    confirm = _get_param(event, "confirm_password")
    if not all([email, session, new_password, confirm]):
        return _redirect("/login?error=Missing+fields")
    if new_password != confirm:
        return _new_password_page(email, session, error="Passwords do not match")
    try:
        resp = _get_cognito().admin_respond_to_auth_challenge(
            UserPoolId=USER_POOL_ID,
            ClientId=auth.USER_POOL_CLIENT_ID,
            ChallengeName="NEW_PASSWORD_REQUIRED",
            ChallengeResponses={
                "USERNAME": email,
                "NEW_PASSWORD": new_password,
            },
            Session=session,
        )
    except _get_cognito().exceptions.InvalidPasswordException as e:
        return _new_password_page(email, session,
                                  error="Password does not meet requirements")
    except Exception as e:
        log.warning("new password failed for %s: %s", email, e)
        return _new_password_page(email, session, error=f"Failed: {e}")
    result = resp.get("AuthenticationResult") or {}
    id_token = result.get("IdToken")
    refresh_token = result.get("RefreshToken")
    if not id_token:
        return _redirect("/login?error=Login+failed")
    try:
        claims = auth.verify_id_token(id_token)
        _record_login(claims)
    except Exception as e:
        log.warning("could not record login after new-password: %s", e)
    cookies = [
        auth.set_cookie(auth.ID_COOKIE, id_token,
                        max_age=result.get("ExpiresIn", 3600)),
    ]
    if refresh_token:
        cookies.append(auth.set_cookie(auth.REFRESH_COOKIE, refresh_token,
                                       max_age=30 * 24 * 3600))
    return {"statusCode": 302, "headers": {"Location": "/"},
            "cookies": cookies, "body": ""}


def _forgot_password_page(error: str = "", success: str = "") -> dict:
    community_id = os.environ.get("COMMUNITY_ID", "")
    community = db.get_community(community_id) if community_id else None
    org_name = community.name if community else "Community Organizer"
    msg = ""
    if error:
        msg = (f"<p style='color:#c33;margin-bottom:16px'>"
               f"{html.escape(error)}</p>")
    elif success:
        msg = (f"<p style='color:#2a7;margin-bottom:16px'>"
               f"{html.escape(success)}</p>")
    body = (
        f"<h1 style='margin-bottom:8px'>{html.escape(org_name)}</h1>"
        "<p style='color:#888;margin-bottom:24px'>"
        "New user setup or password reset</p>"
        + msg
        + "<p style='color:#666;font-size:0.9em;max-width:340px;margin:0 auto 16px'>"
        "Enter your email address and we'll send you a verification code "
        "if your email is in the system. Use this whether you're new to "
        "the system or just forgot your password.</p>"
        + "<form method='post' action='/login/forgot' "
        "style='display:flex;flex-direction:column;gap:12px;align-items:center;"
        "max-width:300px;margin:0 auto'>"
        "<input type='email' name='email' placeholder='Email address' "
        "required autofocus "
        "style='padding:10px;width:100%;font-size:1em;border:1px solid #ccc;"
        "border-radius:4px'>"
        "<button type='submit' style='padding:10px 24px;cursor:pointer;"
        "font-size:1em;width:100%;background:#2a7;color:white;border:none;"
        "border-radius:4px'>Send login code</button>"
        "<p style='margin-top:8px'><a href='/login' "
        "style='font-size:0.9em;color:#888'>Back to sign in</a></p>"
        "</form>"
    )
    return _html(200, _page(body, title=org_name))


def _reset_password_page(email: str, error: str = "") -> dict:
    community_id = os.environ.get("COMMUNITY_ID", "")
    community = db.get_community(community_id) if community_id else None
    org_name = community.name if community else "Community Organizer"
    error_html = (f"<p style='color:#c33;margin-bottom:16px'>"
                  f"{html.escape(error)}</p>" if error else "")
    body = (
        f"<h1 style='margin-bottom:8px'>{html.escape(org_name)}</h1>"
        "<p style='color:#888;margin-bottom:24px'>"
        "Create or reset password</p>"
        "<p style='color:#666;font-size:0.9em;max-width:340px;"
        "margin:0 auto 16px'>If you have an account with that email "
        "address created for you by the administrator, enter the code "
        "you received and choose a password.</p>"
        + error_html
        + "<form method='post' action='/login/reset-password' "
        "style='display:flex;flex-direction:column;gap:12px;align-items:center;"
        "max-width:300px;margin:0 auto'>"
        f"<input type='hidden' name='email' value='{html.escape(email)}'>"
        "<input type='text' name='code' placeholder='Verification code' "
        "required autofocus inputmode='numeric' autocomplete='one-time-code' "
        "style='padding:10px;width:100%;font-size:1em;border:1px solid #ccc;"
        "border-radius:4px'>"
        "<input type='password' name='new_password' placeholder='New password' "
        "required minlength='8' "
        "style='padding:10px;width:100%;font-size:1em;border:1px solid #ccc;"
        "border-radius:4px'>"
        "<input type='password' name='confirm_password' "
        "placeholder='Confirm password' required minlength='8' "
        "style='padding:10px;width:100%;font-size:1em;border:1px solid #ccc;"
        "border-radius:4px'>"
        "<button type='submit' style='padding:10px 24px;cursor:pointer;"
        "font-size:1em;width:100%;background:#2a7;color:white;border:none;"
        "border-radius:4px'>Reset password</button>"
        "<p style='font-size:0.85em;color:#888'>Minimum 8 characters, "
        "including uppercase, lowercase, and a number.</p>"
        "<p style='margin-top:8px'><a href='/login' "
        "style='font-size:0.9em;color:#888'>Back to sign in</a></p>"
        "</form>"
    )
    return _html(200, _page(body, title=org_name))


def _login_forgot(event: dict) -> dict:
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method != "POST":
        return _forgot_password_page()
    email = _get_param(event, "email")
    if not email:
        return _forgot_password_page(error="Please enter your email address")
    try:
        _get_cognito().forgot_password(
            ClientId=auth.USER_POOL_CLIENT_ID,
            Username=email,
        )
    except _get_cognito().exceptions.UserNotFoundException:
        pass
    except _get_cognito().exceptions.LimitExceededException:
        return _forgot_password_page(
            error="Too many attempts. Please try again later.")
    except Exception as e:
        log.warning("forgot_password failed for %s: %s", email, e)
    return _reset_password_page(email)


def _login_reset_password(event: dict) -> dict:
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method != "POST":
        qs = event.get("queryStringParameters") or {}
        email = qs.get("email", "")
        if not email:
            return _redirect("/login/forgot")
        return _reset_password_page(email)
    email = _get_param(event, "email")
    code = _get_param(event, "code")
    new_password = _get_param(event, "new_password")
    confirm = _get_param(event, "confirm_password")
    if not all([email, code, new_password, confirm]):
        return _reset_password_page(email or "", error="All fields are required")
    if new_password != confirm:
        return _reset_password_page(email, error="Passwords do not match")
    try:
        _get_cognito().confirm_forgot_password(
            ClientId=auth.USER_POOL_CLIENT_ID,
            Username=email,
            ConfirmationCode=code,
            Password=new_password,
        )
    except _get_cognito().exceptions.CodeMismatchException:
        return _reset_password_page(email,
                                    error="Invalid or expired code")
    except _get_cognito().exceptions.InvalidPasswordException:
        return _reset_password_page(email,
                                    error="Password does not meet requirements")
    except _get_cognito().exceptions.ExpiredCodeException:
        return _reset_password_page(email,
                                    error="Code has expired. Please request a new one.")
    except Exception as e:
        log.warning("confirm_forgot_password failed for %s: %s", email, e)
        return _reset_password_page(email, error=f"Failed: {e}")
    return _redirect("/login?msg=Password+reset+successfully.+Please+sign+in.")


def _record_login(claims: dict) -> None:
    """Bump last_login_at and login_count for the user identified by claims."""
    sub = claims.get("sub", "") if claims else ""
    if not sub:
        return
    try:
        community_id = os.environ.get("COMMUNITY_ID", "")
        user = db.get_user_by_cognito_sub(
            sub, community_id=community_id or None)
        if user is None:
            email = claims.get("email", "")
            # Same trust gate as _route's auto-link (D2): verified email or
            # a Google federation. Login stats are low-impact, but keep the
            # posture consistent with the auth path.
            if email and community_id and _login_email_trusted(claims):
                user = db.get_user_by_email(community_id, email)
        if user is None:
            return
        import datetime as _dt
        user.last_login_at = _dt.datetime.now(_dt.timezone.utc).isoformat(
            timespec="seconds")
        user.login_count = (user.login_count or 0) + 1
        db.put_user(user)
    except Exception as e:
        log.warning("failed to record login for sub=%s: %s", sub, e)


def _auth_callback(event: dict) -> dict:
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    qs = dict(event.get("queryStringParameters") or {})

    # Apple's OAuth flow uses response_mode=form_post — the IdP POSTs
    # to Cognito's idpresponse endpoint, and Cognito's final hop to
    # our /auth/callback can be either a GET (Google-style) or a POST
    # with a form-encoded body (Apple-style, when Cognito passes the
    # response_mode through). Read query string AND POST body so we
    # don't miss the code/error in either shape.
    if method == "POST" and event.get("body"):
        import base64 as _b64
        body = event["body"]
        if event.get("isBase64Encoded"):
            body = _b64.b64decode(body).decode("utf-8")
        parsed = dict(urllib.parse.parse_qsl(body))
        # Form body wins over query params if both somehow carry the
        # same key — Apple's POST is the authoritative response.
        qs.update(parsed)

    code = qs.get("code")
    error = qs.get("error")
    error_description = qs.get("error_description", "")
    log.info("auth callback method=%s have_code=%s have_error=%s "
             "error=%r error_description=%r qs_keys=%s",
             method, bool(code), bool(error),
             error, error_description, sorted(qs.keys()))

    if error:
        # Cognito or upstream IdP returned an OAuth error. Surface it.
        safe_err = html.escape(error)
        safe_desc = html.escape(error_description)
        log.warning("oauth callback returned error=%r description=%r",
                    error, error_description)
        return _html(400, _page(
            f"<h1>Sign-in error</h1>"
            f"<p style='color:#c33'><strong>{safe_err}</strong></p>" +
            (f"<p>{safe_desc}</p>" if safe_desc else "") +
            "<p style='margin-top:24px'><a href='/login'>Back to login</a></p>",
            title="Sign-in error"
        ))
    if not code:
        return _text(400, "missing code")
    cookies_in = auth.parse_cookies(event)
    # OAuth state CSRF check — must come BEFORE exchanging the code so
    # an attacker can't use a forged callback to consume a code we
    # didn't initiate (security fix D1). The state cookie was set at
    # /login/google; if either side is missing or they don't match,
    # this isn't our flow.
    state_qs = qs.get("state")
    state_cookie = cookies_in.get(auth.OAUTH_STATE_COOKIE)
    if not auth.validate_oauth_state(state_qs, state_cookie):
        log.warning("oauth state mismatch (qs=%r, cookie=%s)",
                    state_qs, "present" if state_cookie else "absent")
        # Clear the cookie defensively in case it lingered from a
        # half-completed flow.
        return {
            "statusCode": 400,
            "headers": {"Content-Type": "text/plain"},
            "cookies": [auth.clear_cookie(auth.OAUTH_STATE_COOKIE)],
            "body": ("OAuth state validation failed — your sign-in session "
                     "expired or this callback didn't originate from your "
                     "browser. Start sign-in again from the login page."),
        }
    try:
        tokens = auth.exchange_code(code)
    except Exception as e:
        log.exception("token exchange failed")
        return _text(502, f"token exchange failed: {e}")
    try:
        claims = auth.verify_id_token(tokens["id_token"])
    except Exception as e:
        log.exception("id_token verify failed after exchange")
        return _text(502, f"id_token verify failed: {e}")

    # Apple "Hide My Email" — log but accept. We warn users up
    # front on the login page (see _login_page) so they're informed
    # before they click; if they ignore the warning and pick the
    # relay anyway, we accept it. Trade-off: calendar reply flow
    # may break for relay users on non-Apple-Mail clients (Outlook,
    # Gmail web) because the reply leaves their real account with
    # a `From` we don't recognise. Apple Mail users round-trip
    # through the relay correctly. Earlier auto-reject was worse
    # UX (Apple persists the privacy choice, leaving users stuck
    # in a loop until they revoke at appleid.apple.com).
    email = claims.get("email", "")
    if auth.is_apple_private_relay_email(email):
        log.warning("Apple sign-in via private-relay address %s "
                    "— calendar reply flow may not work for this "
                    "user on non-Apple-Mail clients", email)

    _record_login(claims)
    # The scheduler_next cookie is set by /login at user request, but
    # cookies are still attacker-influenced (via /login?next=…) — pin
    # the destination to a safe local path (security fix H3).
    next_url = _safe_next(urllib.parse.unquote(
        cookies_in.get("scheduler_next", "/")))
    set_cookies = [
        auth.set_cookie(auth.ID_COOKIE, tokens["id_token"],
                        max_age=tokens.get("expires_in", 3600)),
        auth.set_cookie(auth.REFRESH_COOKIE, tokens["refresh_token"],
                        max_age=30 * 24 * 3600),
        "scheduler_next=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0",
        # Clear the one-shot state cookie — it's done its job.
        auth.clear_cookie(auth.OAUTH_STATE_COOKIE),
    ]
    return {"statusCode": 302, "headers": {"Location": next_url},
            "cookies": set_cookies, "body": ""}


def _logout() -> dict:
    # Clear BOTH the domain-scoped and host-only variant of each session
    # cookie. A browser that signed in before COOKIE_DOMAIN was introduced
    # (2026-06-04) still holds host-only cookies; clearing only the current
    # domain-scoped variant left those behind and logout appeared to fail
    # (while a fresh private window logged out fine). See
    # auth.clear_cookie_variants.
    cookies: list[str] = []
    for name in (auth.ID_COOKIE, auth.REFRESH_COOKIE, ACTIVE_APP_COOKIE):
        cookies.extend(auth.clear_cookie_variants(name))
    return {"statusCode": 302,
            "headers": {"Location": auth.logout_redirect_url()},
            "cookies": cookies,
            "body": ""}


_APP_TYPE_LABEL = {
    "coverage": "Coverage (Ushers-style rotation)",
    "recurring_commitments": "Recurring Commitments (same person, same slot)",
    "standing_event": "Standing Event (recurring meeting)",
    "flexible_event": "Flexible Event (one-off; date poll or direct)",
}


def _launcher_page(event: dict, user: User,
                   community: Community | None) -> dict:
    """Cross-app landing page for users who belong to more than one
    app (or any CA/UA). Lists the apps they belong to with the
    admin-supplied description and a role badge; CA/UA viewers also
    see a "Community admin" tile linking to /admin/apps.

    Reached when:
      - User logs in fresh and has 2+ apps (no active-app cookie)
      - Active-app cookie expired (>4 hours since last activity) and
        they have 2+ apps
      - They navigate directly to /launcher

    Single-app users never see this — _route() flows straight to
    their one app.
    """
    org_name = community.name if community else user.community_id
    is_ca_ua = user.community_role in ("ca", "ua")

    # The user's visible app list. CAs/UAs see every app in the
    # community (matches the corner-menu policy). Plain members + AAs
    # see only apps they hold a Membership in.
    all_apps = sorted(db.list_applications(user.community_id),
                      key=lambda a: a.name.lower())
    member_apps = {m.app_id: m for m in
                   db.list_memberships_for_user(user.user_id)}
    if is_ca_ua:
        visible_apps = all_apps
    else:
        visible_apps = [a for a in all_apps if a.app_id in member_apps]

    tiles = ""
    for app_obj in visible_apps:
        mem = member_apps.get(app_obj.app_id)
        if mem is not None:
            role_label = _ROLE_LABEL.get(mem.app_role, mem.app_role)
        elif is_ca_ua:
            role_label = "Community Admin"
        else:
            role_label = ""
        desc_html = (
            f"<p style='color:#666;font-size:0.9em;margin:4px 0 0 0'>"
            f"{html.escape(app_obj.description)}</p>"
            if app_obj.description else
            "<p style='color:#bbb;font-style:italic;font-size:0.85em;"
            "margin:4px 0 0 0'>No description set.</p>"
        )
        tiles += (
            "<a href='/?app_id=" + app_obj.app_id + "' "
            "style='display:block;text-decoration:none;color:inherit;"
            "border:1px solid #ddd;border-radius:8px;padding:16px 20px;"
            "margin-bottom:12px;background:white;"
            "transition:border-color 0.15s'>"
            f"<div style='font-size:1.15em;font-weight:600;color:#2a7'>"
            f"{html.escape(app_obj.name)}</div>"
            + (f"<div style='color:#888;font-size:0.8em;margin-top:2px'>"
               f"{html.escape(role_label)}</div>" if role_label else "")
            + desc_html
            + "</a>"
        )

    if not visible_apps:
        if is_ca_ua:
            tiles = (
                "<p style='color:#888'>No apps yet. "
                "<a href='/admin/apps' style='color:#2a7'>Create one</a> "
                "to get started.</p>"
            )
        else:
            tiles = (
                "<p style='color:#888'>You haven't been added to "
                "any apps yet. Please contact a Community Admin.</p>"
            )

    ca_section = ""
    if is_ca_ua:
        ca_section = (
            "<h2 style='font-size:1.05em;color:#444;margin-top:32px'>"
            "Community admin</h2>"
            "<a href='/admin/apps' style='display:block;"
            "text-decoration:none;color:inherit;"
            "border:1px solid #f0d080;border-radius:8px;"
            "padding:16px 20px;background:#fffbe6'>"
            "<div style='font-size:1.05em;font-weight:600;color:#a80'>"
            "Manage apps and community-wide users &rarr;</div>"
            "<p style='color:#704800;font-size:0.9em;margin:4px 0 0 0'>"
            "Create or delete apps, edit community settings, "
            "manage who's in the community."
            "</p></a>"
        )

    body = (
        f"<h1 style='margin-bottom:4px'>Welcome to "
        f"{html.escape(org_name)}</h1>"
        f"<p style='color:#888;margin-top:0'>"
        f"Hello, {html.escape(user.name)}. Choose where to go:</p>"
        + tiles + ca_section
    )
    return _html(200, _page(body, narrow=True, title=org_name))


def _ca_landing_page(event: dict, user: User,
                     community: Community | None) -> dict:
    """The Community Admin landing page at /admin/apps.

    Lists every Application in the community with its app_type and
    period_type, links each to its in-app home (`/?app_id=...`), and
    surfaces a create form + delete-with-warning buttons. app_type is
    immutable once created — no edit affordance, by design.
    """
    # UA viewers see the same list but without the Delete affordance
    # — creating/deleting apps is CA-only structural work. They land
    # here so they can still pivot INTO any app to do roster work.
    can_create_delete_apps = (user.community_role == "ca")
    apps = sorted(db.list_applications(user.community_id),
                  key=lambda a: a.created_at)
    rows = ""
    if not apps:
        empty_copy = (
            "No apps yet. Create one below to get started."
            if can_create_delete_apps else
            "No apps yet. Ask a Community Admin to create one."
        )
        rows = ("<tr><td colspan='5' style='padding:14px;color:#888;"
                f"text-align:center'>{empty_copy}</td></tr>")
    # ?edit=<app_id> renders that row as an inline edit form for the
    # app's name + description. Posting updates both and redirects back
    # to /admin/apps. (Legacy ?edit_desc= still works — it predates
    # the name-editing affordance and only opens the description for
    # editing. Both query params route through the same form.)
    edit_app_id = _get_param(event, "edit") or _get_param(event, "edit_desc")
    for idx, a in enumerate(apps):
        # See _NEXT_ROW_OFFSET — anchor the post-save scroll a row above.
        anchor_aid = apps[max(0, idx - _NEXT_ROW_OFFSET)].app_id
        type_label = _APP_TYPE_LABEL.get(a.app_type, a.app_type)
        if edit_app_id and edit_app_id == a.app_id:
            rows += (
                f"<tr id='app-{a.app_id}' "
                "style='border-bottom:1px solid #eee;background:#fafafa;"
                "scroll-margin-top:120px'>"
                f"<td colspan='5' style='padding:14px'>"
                f"<form method='post' action='/api/apps/update' "
                "style='display:flex;flex-direction:column;gap:10px'>"
                f"<input type='hidden' name='app_id' value='{a.app_id}'>"
                f"<input type='hidden' name='next' "
                f"value='/admin/apps#app-{anchor_aid}'>"
                f"<label style='font-size:0.9em;color:#444'>"
                f"Name"
                "</label>"
                f"<input type='text' name='name' required "
                f"value='{html.escape(a.name)}' "
                "style='padding:6px;width:100%;box-sizing:border-box;"
                "font-family:inherit;font-size:1em;"
                "border:1px solid #ccc;border-radius:4px'>"
                f"<label style='font-size:0.9em;color:#444;margin-top:4px'>"
                f"Description "
                "<span style='color:#888;font-size:0.85em;font-weight:normal'>"
                "(shown on the launcher under each app's name)"
                "</span></label>"
                f"<textarea name='description' rows='3' "
                "style='padding:6px;width:100%;box-sizing:border-box;"
                "font-family:inherit;font-size:0.95em;"
                "border:1px solid #ccc;border-radius:4px'>"
                f"{html.escape(a.description or '')}</textarea>"
                "<div style='display:flex;gap:10px;align-items:center'>"
                "<button type='submit' style='padding:6px 16px;"
                "cursor:pointer;background:#2a7;color:white;"
                "border:none;border-radius:4px'>Save</button>"
                "<a href='/admin/apps' style='color:#888'>cancel</a>"
                "</div></form></td></tr>"
            )
            continue
        if can_create_delete_apps:
            delete_cell = (
                "<td style='padding:8px 10px;text-align:right'>"
                f"<button onclick=\"confirmModal("
                f"'Delete app {html.escape(a.name)}? '"
                f"+ 'EVERYTHING inside this app — templates, schedules, '"
                f"+ 'slots, assignments, cohorts, memberships, swaps, '"
                f"+ 'and queued notifications — will be permanently '"
                f"+ 'deleted. Users themselves stay in the community. '"
                f"+ 'This cannot be undone. Continue?',"
                f"function(){{"
                f"var f=document.createElement('form');"
                f"f.method='post';f.action='/api/apps/delete';"
                f"var i=document.createElement('input');"
                f"i.name='app_id';i.value='{a.app_id}';f.appendChild(i);"
                f"document.body.appendChild(f);f.submit();"
                f"}},'Delete','#c33')\" "
                "style='cursor:pointer;background:none;border:1px solid #c33;"
                "color:#c33;padding:4px 10px;border-radius:4px'>"
                "Delete</button></td>"
            )
        else:
            delete_cell = (
                "<td style='padding:8px 10px;text-align:right;"
                "color:#bbb;font-size:0.85em'>&mdash;</td>"
            )
        desc_block = (
            f"<div style='color:#666;font-size:0.9em;margin-top:4px'>"
            f"{html.escape(a.description)}</div>"
            if a.description else
            "<div style='color:#bbb;font-style:italic;"
            "font-size:0.85em;margin-top:4px'>(no description)</div>"
        )
        edit_link = (
            f"<a href='/admin/apps?edit={a.app_id}#app-{anchor_aid}' "
            "style='font-size:0.8em;color:#2a7;text-decoration:none'>"
            "edit name &amp; description</a>"
        )
        rows += (
            f"<tr id='app-{a.app_id}' "
            "style='border-bottom:1px solid #eee;scroll-margin-top:120px'>"
            f"<td style='padding:8px 10px'>"
            f"<a href='/?app_id={a.app_id}' "
            "style='color:#2a7;font-weight:600;text-decoration:none'>"
            f"{html.escape(a.name)}</a>"
            f"{desc_block}"
            f"<div style='margin-top:4px'>{edit_link}</div>"
            "</td>"
            f"<td style='padding:8px 10px;color:#555;vertical-align:top'>"
            f"{html.escape(type_label)}</td>"
            f"<td style='padding:8px 10px;color:#555;vertical-align:top'>"
            f"{html.escape(a.period_type)}</td>"
            f"<td style='padding:8px 10px;color:#888;font-size:0.9em;"
            "vertical-align:top'>"
            f"{html.escape(a.created_at[:10])}</td>"
            f"{delete_cell}"
            "</tr>"
        )

    app_type_options = "".join(
        f"<option value='{k}'>{html.escape(v)}</option>"
        for k, v in _APP_TYPE_LABEL.items()
    )

    role_label = ("Community Admin landing"
                  if user.community_role == "ca"
                  else "User Admin landing")

    # Apps section — section-style heading + table, matching AA home's
    # visual scheme.
    apps_section = (
        "<section style='margin-top:32px;text-align:left'>"
        "<h2 style='font-size:1.1em;color:#444'>Applications</h2>"
        "<table style='border-collapse:collapse;width:100%;"
        "font-size:0.95em;border:1px solid #eee'>"
        "<thead style='background:#fafafa'>"
        "<tr>"
        "<th style='text-align:left;padding:8px 10px'>Name</th>"
        "<th style='text-align:left;padding:8px 10px'>App type</th>"
        "<th style='text-align:left;padding:8px 10px'>Period</th>"
        "<th style='text-align:left;padding:8px 10px'>Created</th>"
        "<th></th>"
        "</tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        "</section>"
    )

    # Create new app section — was a separate centered block before
    # (#193). Now matches the AA-home section style: left-flush
    # heading at the standard h2 size, content sized to the page
    # width instead of a 480px centered island.
    create_section = (
        "<section style='margin-top:32px;text-align:left'>"
        "<h2 style='font-size:1.1em;color:#444'>Create new Application</h2>"
        "<p style='color:#888;font-size:0.9em;margin-top:0'>"
        "Application type and period are <b>locked at creation</b>. "
        "To change either later, delete the application and create a new one."
        "</p>"
        "<form method='post' action='/api/apps/create' "
        "style='display:flex;flex-direction:column;gap:10px;"
        "max-width:520px'>"
        "<label>Name "
        "<input name='name' required maxlength='80' "
        "style='width:100%;padding:6px;box-sizing:border-box'></label>"
        "<label>Description (optional) "
        "<textarea name='description' rows='2' "
        "style='width:100%;padding:6px;box-sizing:border-box;"
        "font-family:inherit;font-size:inherit' "
        "placeholder='Short blurb shown on the launcher. Editable "
        "later.'></textarea></label>"
        "<label>App type "
        "<select name='app_type' id='create-app-type' required "
        "style='width:100%;padding:6px' "
        # When the user picks an app type, auto-set period_type to
        # the natural default (monthly for coverage, weekly for
        # recurring_commitments). They can still override after.
        # Previous UX had a "(default for app type)" mystery option
        # that confused users who didn't know what the default was.
        "onchange=\"var pt=document.getElementById('create-period-type');"
        "if(this.value==='recurring_commitments')pt.value='weekly';"
        "else if(this.value==='flexible_event')pt.value='ad_hoc';"
        "else pt.value='monthly';\">"
        "<option value=''>-- pick one --</option>"
        f"{app_type_options}"
        "</select></label>"
        "<label>Period type "
        "<select name='period_type' id='create-period-type' required "
        "style='width:100%;padding:6px'>"
        "<option value='monthly'>monthly</option>"
        "<option value='weekly'>weekly</option>"
        "<option value='ad_hoc'>Ad hoc</option>"
        "</select></label>"
        "<button type='submit' "
        "style='padding:8px 16px;cursor:pointer;background:#2a7;"
        "color:white;border:none;border-radius:4px;align-self:flex-start'>"
        "Create app</button>"
        "</form>"
        "</section>"
    ) if can_create_delete_apps else ""

    body = (
        f"<h1 style='margin:0 0 4px 0'>Community: "
        f"{html.escape(community.name if community else user.community_id)}"
        f"</h1>"
        f"<p style='color:#888;margin-top:0'>{role_label}</p>"
        + _flash_banner_html(event)
        + apps_section
        + _ca_community_users_section(user.community_id)
        + create_section
        + _ca_nav_bar("apps")
    )
    return _html(200, _page(body, narrow=False, title="Community admin"))


def _ca_community_users_section(community_id: str) -> str:
    """Render the CA-landing "Member management" widget.

    Same visual shape as the AA home's `_users_summary_section` but
    community-scoped (no app filter) and the action link points at
    `/admin/community-users` instead of the per-app `/admin/users`.
    Built for #193 so the CA landing reads with the same section
    style as the AA home — the member-management section design.
    """
    all_users = sorted(db.list_users(community_id),
                       key=lambda u: u.created_at or "", reverse=True)
    recent = all_users[:5]
    total = len(all_users)
    heading = (
        "<h2 style='font-size:1.1em;color:#444'>Member management "
        "<a href='/admin/community-users' style='font-size:0.75em;"
        "font-weight:400;margin-left:8px'>manage all users</a></h2>"
    )
    if not recent:
        return (
            "<section style='margin-top:32px;text-align:left'>"
            + heading
            + "<p style='color:#888'>No users yet.</p>"
            "</section>"
        )
    rows = "".join(
        "<tr>"
        f"<td style='padding:4px 12px'>{html.escape(u.name)}</td>"
        f"<td style='padding:4px 12px;font-size:0.9em;color:#666'>"
        f"{html.escape(u.email)}</td>"
        "</tr>"
        for u in recent
    )
    return (
        "<section style='margin-top:32px;text-align:left'>"
        + heading
        + f"<p style='color:#888;font-size:0.9em'>{total} users total. "
        "Most recently added:</p>"
        "<table style='border-collapse:collapse;width:100%;font-size:0.95em'>"
        f"<tbody>{rows}</tbody></table>"
        "</section>"
    )


def _api_app_create(event: dict, user: User,
                    community: Community | None) -> dict:
    # Creating an app is a community-structural action — CA only.
    # UAs handle roster management across apps but not the apps
    # themselves.
    if user.community_role != "ca":
        return _text(403,
                     "Only Community Admins can create apps.")
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method != "POST":
        return _text(405, "POST required")
    name = (_get_param(event, "name") or "").strip()
    app_type = (_get_param(event, "app_type") or "").strip()
    period_type = (_get_param(event, "period_type") or "").strip()
    description = (_get_param(event, "description") or "").strip()
    if not name:
        return _error_redirect("/admin/apps", "App name is required.")
    if app_type not in ("coverage", "recurring_commitments",
                        "standing_event", "flexible_event"):
        return _error_redirect("/admin/apps",
            f"Unsupported app type: {app_type!r}")
    if not period_type:
        # standing_event and flexible_event are date-centric; period_type
        # doesn't drive their UI but the field is non-null on Application
        # so we stamp the natural default. flexible_event gets "ad_hoc"
        # to signal "no fixed period" — events are created individually.
        if app_type == "recurring_commitments":
            period_type = "weekly"
        elif app_type == "flexible_event":
            period_type = "ad_hoc"
        else:
            period_type = "monthly"
    if period_type not in ("monthly", "weekly", "ad_hoc"):
        return _error_redirect("/admin/apps",
            f"Unsupported period type: {period_type!r}")
    app = Application(community_id=user.community_id, name=name,
                      app_type=app_type, period_type=period_type,
                      description=description)
    app.public_slug = _unique_slug(user.community_id, name)
    db.put_application(app)
    log.info("CA %s created app %s name=%r type=%s period=%s slug=%s",
             user.user_id, app.app_id, name, app_type, period_type,
             app.public_slug)
    return _redirect("/admin/apps")


def _render_user_apps_cell(target_user: User,
                           memberships_for_user: list[Membership],
                           all_apps: list[Application],
                           *, anchor_uid: str | None = None) -> str:
    """Render the per-user "Apps" cell on /admin/community-users.

    Layout, one row per membership:
        Ushers — App Admin  [demote] [×]
        Adoration — Member  [make admin] [×]

    Followed by an inline add form with an app picker (only apps the
    user isn't already in) + a Member/Admin radio + Add button.

    All three actions hit CA-route endpoints so they work on any app,
    not just the CA's currently-active one.

    ``anchor_uid`` is the user_id that the post-save scroll fragment
    should target (typically the row N-rows-above the edited row, see
    ``_NEXT_ROW_OFFSET``). Defaults to the row's own user_id, which
    matches the pre-#180 behavior.
    """
    if anchor_uid is None:
        anchor_uid = target_user.user_id
    apps_by_id = {a.app_id: a for a in all_apps}
    member_app_ids = {m.app_id for m in memberships_for_user}

    chip_style = (
        "display:flex;align-items:center;gap:6px;font-size:0.85em;"
        "padding:3px 0"
    )
    btn = ("font-size:0.78em;cursor:pointer;background:none;border:none;"
           "text-decoration:underline;padding:0")
    chips: list[str] = []
    for mem in sorted(memberships_for_user,
                      key=lambda m: apps_by_id.get(m.app_id).name.lower()
                      if apps_by_id.get(m.app_id) else ""):
        app_obj = apps_by_id.get(mem.app_id)
        if app_obj is None:
            # Orphan membership — app was deleted but row remained.
            # Surface it so the CA can clean up.
            chips.append(
                f"<div style='{chip_style};color:#c33' "
                f"title='Orphan: app no longer exists'>"
                f"(orphan) {html.escape(mem.app_id[:8])}…"
                f"<form method='post' action='/api/community-users/remove-membership' "
                "style='display:inline'>"
                f"<input type='hidden' name='user_id' value='{target_user.user_id}'>"
                f"<input type='hidden' name='target_app_id' value='{mem.app_id}'>"
                f"<button type='submit' style='{btn};color:#c33'>"
                "remove</button></form></div>"
            )
            continue
        role_label = _ROLE_LABEL.get(mem.app_role, mem.app_role)
        # Inline toggle: clicking flips member ↔ aa.
        new_role = "aa" if mem.app_role == "member" else "member"
        toggle_label = "make admin" if mem.app_role == "member" else "demote"
        toggle_color = "#2a7" if mem.app_role == "member" else "#a80"
        chips.append(
            f"<div style='{chip_style}'>"
            f"<span style='font-weight:600'>{html.escape(app_obj.name)}</span>"
            f"<span style='color:#666'>&mdash; {html.escape(role_label)}</span>"
            f"<form method='post' "
            "action='/api/community-users/toggle-membership' "
            "style='display:inline'>"
            f"<input type='hidden' name='user_id' value='{target_user.user_id}'>"
            f"<input type='hidden' name='target_app_id' value='{mem.app_id}'>"
            f"<input type='hidden' name='new_role' value='{new_role}'>"
            f"<input type='hidden' name='next' "
            f"value='/admin/community-users#user-{anchor_uid}'>"
            f"<button type='submit' style='{btn};color:{toggle_color}'>"
            f"{toggle_label}</button></form>"
            f"<form method='post' "
            "action='/api/community-users/remove-membership' "
            "style='display:inline'"
            f" onsubmit=\"return confirmSubmit(this,"
            f"'Remove {html.escape(target_user.name)} from "
            f"{html.escape(app_obj.name)}?','Remove','#c33')\">"
            f"<input type='hidden' name='user_id' value='{target_user.user_id}'>"
            f"<input type='hidden' name='target_app_id' value='{mem.app_id}'>"
            f"<input type='hidden' name='next' "
            f"value='/admin/community-users#user-{anchor_uid}'>"
            f"<button type='submit' style='{btn};color:#c33' "
            "title='Remove from this app'>&times;</button></form>"
            "</div>"
        )

    # Add form: shows only apps the user isn't already in.
    addable = [a for a in all_apps if a.app_id not in member_app_ids]
    if addable:
        opts = "".join(
            f"<option value='{a.app_id}'>{html.escape(a.name)}</option>"
            for a in sorted(addable, key=lambda a: a.name.lower())
        )
        add_form = (
            "<form method='post' "
            "action='/api/community-users/add-membership' "
            f"style='margin-top:6px;display:flex;gap:6px;align-items:center;"
            "flex-wrap:wrap;font-size:0.85em'>"
            f"<input type='hidden' name='user_id' "
            f"value='{target_user.user_id}'>"
            f"<input type='hidden' name='next' "
            f"value='/admin/community-users#user-{anchor_uid}'>"
            "<select name='target_app_id' required "
            "style='padding:2px;font-size:inherit'>"
            f"<option value=''>+ add to app…</option>{opts}"
            "</select>"
            "<select name='role' style='padding:2px;font-size:inherit' "
            "title='Role in this app'>"
            "<option value='member' selected>as Member</option>"
            "<option value='aa'>as Admin</option>"
            "</select>"
            "<button type='submit' style='font-size:inherit;cursor:pointer;"
            "padding:1px 8px'>Add</button>"
            "</form>"
        )
    else:
        add_form = (
            "<div style='font-size:0.8em;color:#999;margin-top:6px'>"
            "(in every app)</div>"
        )
    return "".join(chips) + add_form


def _ca_users_page(event: dict, user: User,
                   community: Community | None) -> dict:
    """Community-wide user manager — every User in the community
    listed in one table, unfiltered by app. Each row links to /edit
    (same per-user edit endpoint the app-scoped users page uses) and
    surfaces reset-access + delete-from-community. No per-app role
    toggle here (membership is an app-scoped concept); changes to
    community_role itself happen via the edit form.

    Add form creates a User WITHOUT a Membership — the CA can later
    open an app and grant membership from /admin/users there. (The
    user said: no invitations live at this level.)
    """
    org_name = community.name if community else user.community_id
    all_users = sorted(db.list_users(user.community_id),
                       key=lambda u: u.name.lower())
    all_apps = list(db.list_applications(user.community_id))
    # Pre-build per-user membership lists once so the row renderer
    # doesn't query DDB for every user.
    memberships_by_user: dict[str, list[Membership]] = {}
    for app_obj in all_apps:
        for mem in db.list_memberships_for_app(app_obj.app_id):
            memberships_by_user.setdefault(mem.user_id, []).append(mem)
    edit_id = _get_param(event, "edit")
    rows = ""
    _act = ("font-size:0.85em;cursor:pointer;background:none;border:none;"
            "text-decoration:underline;padding:0")
    for idx, u in enumerate(all_users):
        # See _NEXT_ROW_OFFSET — anchor the post-save scroll a row above.
        anchor_uid = all_users[max(0, idx - _NEXT_ROW_OFFSET)].user_id
        is_self = (u.user_id == user.user_id)
        crole_label = _ROLE_LABEL.get(u.community_role, u.community_role)
        if edit_id and u.user_id == edit_id and user.community_role == "ca":
            # CA-only: render community_role as a select. AAs editing
            # at app scope can't see this field; CAs at community scope
            # can promote / demote.
            role_options = "".join(
                f"<option value='{k}'"
                f"{' selected' if k == u.community_role else ''}>"
                f"{html.escape(v)}</option>"
                for k, v in (("member", "Member"),
                             ("ua", "User Admin"),
                             ("ca", "Community Admin"))
            )
            rows += (
                f"<tr id='user-{u.user_id}' "
                "style='scroll-margin-top:120px'>"
                "<td colspan='6' style='padding:8px 12px;background:#f9f9f9'>"
                "<form method='post' action='/api/users/edit' "
                "style='display:flex;gap:8px;align-items:end;flex-wrap:wrap'>"
                f"<input type='hidden' name='user_id' value='{u.user_id}'>"
                f"<input type='hidden' name='version' value='{u.version or 0}'>"
                f"<input type='hidden' name='next' "
                f"value='/admin/community-users#user-{anchor_uid}'>"
                "<label style='display:flex;flex-direction:column;font-size:0.85em;color:#666'>"
                f"Name<input name='name' value='{html.escape(u.name)}' "
                "required style='padding:4px;width:160px'></label>"
                "<label style='display:flex;flex-direction:column;font-size:0.85em;color:#666'>"
                f"Email<input name='email' value='{html.escape(u.email)}' "
                "required style='padding:4px;width:200px'></label>"
                "<label style='display:flex;flex-direction:column;font-size:0.85em;color:#666'>"
                f"Phone<input name='phone' value='{html.escape(u.phone or '')}' "
                "style='padding:4px;width:120px'></label>"
                "<label style='display:flex;flex-direction:column;font-size:0.85em;color:#666'>"
                f"Notes<input name='notes' value='{html.escape(u.notes or '')}' "
                "style='padding:4px;width:160px'></label>"
                "<label style='display:flex;flex-direction:column;font-size:0.85em;color:#666'>"
                f"Community role<select name='community_role' "
                "style='padding:4px;width:160px'>"
                f"{role_options}</select></label>"
                "<button type='submit' style='padding:4px 12px;cursor:pointer'>"
                "Save</button>"
                " <a href='/admin/community-users' style='font-size:0.85em'>"
                "cancel</a>"
                "</form></td></tr>"
            )
        else:
            actions = [
                f"<a href='/admin/community-users?edit={u.user_id}#user-{anchor_uid}' "
                f"style='font-size:0.85em;color:#2a7'>edit</a>",
            ]
            if not is_self and u.cognito_sub:
                actions.append(
                    f"<form method='post' action='/api/users/reset-access"
                    f"?user_id={u.user_id}' style='display:inline'"
                    f" onsubmit=\"return confirmSubmit(this,"
                    f"'Reset access for {html.escape(u.name)}? Their "
                    f"password will be reset and they will be signed out "
                    f"of all devices.',"
                    f"'Reset access','#a80')\">"
                    f"<button type='submit' style='{_act};color:#a80'>"
                    "reset access</button></form>"
                )
            if not is_self:
                actions.append(
                    f"<form method='post' action='/api/users/delete"
                    f"?user_id={u.user_id}&next=/admin/community-users' "
                    "style='display:inline'"
                    f" onsubmit=\"return confirmSubmit(this,"
                    f"'Permanently delete {html.escape(u.name)} from the "
                    f"entire community? This cannot be undone, and any "
                    f"app memberships, assignments, or schedule history "
                    f"tied to them will be orphaned.','Delete','#c33')\">"
                    f"<button type='submit' style='{_act};color:#c33'>"
                    "delete</button></form>"
                )
                # Promote/demote CA action — same handler the per-app
                # Members screen uses (CA-only, guarded server-side).
                if u.community_role == "ca":
                    actions.append(
                        f"<form method='post' "
                        f"action='/api/users/set-community-role"
                        f"?user_id={u.user_id}&role=member' "
                        "style='display:inline'"
                        f" onsubmit=\"return confirmSubmit(this,"
                        f"'Demote {html.escape(u.name)} from "
                        f"Community Admin?','Demote','#a80')\">"
                        f"<input type='hidden' name='next' "
                        f"value='/admin/community-users#user-{anchor_uid}'>"
                        f"<button type='submit' style='{_act};color:#a80'>"
                        "demote from CA</button></form>"
                    )
                else:
                    actions.append(
                        f"<form method='post' "
                        f"action='/api/users/set-community-role"
                        f"?user_id={u.user_id}&role=ca' "
                        "style='display:inline'"
                        f" onsubmit=\"return confirmSubmit(this,"
                        f"'Make {html.escape(u.name)} a Community Admin? "
                        f"They will be able to manage all apps, members, "
                        f"and settings community-wide.',"
                        f"'Make CA','#2a7')\">"
                        f"<input type='hidden' name='next' "
                        f"value='/admin/community-users#user-{anchor_uid}'>"
                        f"<button type='submit' style='{_act};color:#2a7'>"
                        "make CA</button></form>"
                    )
            actions_html = "<br>".join(actions)
            self_marker = (" <span style='font-size:0.75em;color:#999'>"
                           "(you)</span>" if is_self else "")
            apps_cell = _render_user_apps_cell(
                u, memberships_by_user.get(u.user_id, []), all_apps,
                anchor_uid=anchor_uid)
            rows += (
                f"<tr id='user-{u.user_id}' "
                "style='border-bottom:1px solid #f0f0f0;scroll-margin-top:120px'>"
                f"<td style='padding:6px 12px;white-space:nowrap;"
                "vertical-align:top'>"
                f"{html.escape(u.name)}{self_marker}</td>"
                f"<td style='padding:6px 12px;font-size:0.9em;"
                "vertical-align:top'>"
                f"{html.escape(u.email)}</td>"
                f"<td style='padding:6px 12px;font-size:0.9em;"
                "text-align:center;vertical-align:top'>"
                f"{html.escape(crole_label)}</td>"
                f"<td style='padding:6px 12px;vertical-align:top;"
                f"min-width:240px'>{apps_cell}</td>"
                f"<td style='padding:6px 12px;font-size:0.85em;color:#666;"
                "vertical-align:top'>"
                f"{html.escape(u.phone or '')}"
                f"<div style='color:#999'>{html.escape(u.notes or '')}</div>"
                "</td>"
                f"<td style='padding:6px 12px;font-size:0.85em;"
                f"vertical-align:top'>{actions_html}</td>"
                "</tr>"
            )

    # Long rosters scroll inside a fixed-height box with a sticky header —
    # consistent with the per-app Members page (_users_page).
    scroll_style = ("max-height:500px;overflow-y:auto;border:1px solid #eee;"
                    "border-radius:4px" if len(all_users) > 10 else "")
    table = (
        f"<div style='{scroll_style};margin-top:12px'>"
        "<table style='border-collapse:collapse;width:100%;font-size:0.95em'>"
        "<thead><tr style='color:#888;border-bottom:1px solid #ddd;"
        "position:sticky;top:0;background:white'>"
        "<th style='text-align:left;padding:6px 12px'>Name</th>"
        "<th style='text-align:left;padding:6px 12px'>Email</th>"
        "<th style='text-align:center;padding:6px 12px'>Community role</th>"
        "<th style='text-align:left;padding:6px 12px'>Apps &amp; roles</th>"
        "<th style='text-align:left;padding:6px 12px'>Phone / Notes</th>"
        "<th style='text-align:center;padding:6px 12px'>Actions</th>"
        "</tr></thead><tbody>" + rows + "</tbody></table></div>"
    ) if all_users else (
        "<p style='color:#888'>No users yet.</p>"
    )

    add_form = (
        "<h3 style='font-size:1em;color:#444;margin-top:24px'>Add user</h3>"
        "<p style='color:#888;font-size:0.85em'>This adds the user to the "
        "community only. App membership is granted from each app's "
        "<i>Manage members</i> page.</p>"
        "<form method='post' action='/api/community-users/add' "
        "style='margin:8px 0;display:flex;gap:8px;align-items:end;"
        "flex-wrap:wrap'>"
        "<label style='display:flex;flex-direction:column;font-size:0.85em;color:#666'>"
        "Name (required)<input name='name' required "
        "style='padding:4px;width:160px'></label>"
        "<label style='display:flex;flex-direction:column;font-size:0.85em;color:#666'>"
        "Email (required)<input type='email' name='email' required "
        "style='padding:4px;width:200px'></label>"
        "<label style='display:flex;flex-direction:column;font-size:0.85em;color:#666'>"
        "Phone (optional)<input name='phone' style='padding:4px;width:120px'></label>"
        "<label style='display:flex;flex-direction:column;font-size:0.85em;color:#666'>"
        "Notes (optional)<input name='notes' style='padding:4px;width:160px'></label>"
        "<button type='submit' style='padding:6px 16px;cursor:pointer'>"
        "Add user</button>"
        "</form>"
    )

    body = (
        f"<h1 style='margin-bottom:4px'>Community users</h1>"
        f"<p style='color:#888;margin-top:0'>"
        f"{html.escape(org_name)} — all users, all apps</p>"
        + _flash_banner_html(event)
        + table + add_form
        + _ca_nav_bar("community-users")
    )
    return _html(200, _page(body, narrow=False, title="Community users"))


def _api_ca_user_add(event: dict, user: User,
                     community: Community | None) -> dict:
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method != "POST":
        return _text(405, "POST required")
    name = (_get_param(event, "name") or "").strip()
    email = (_get_param(event, "email") or "").strip()
    phone = (_get_param(event, "phone") or "").strip() or None
    notes = (_get_param(event, "notes") or "").strip() or None
    if not name or not email:
        return _error_redirect("/admin/community-users",
            "Name and email are both required.")
    # mirror the dedup check that the per-app add
    # path got in #197. Previously the CA add-user path would happily
    # create a second User row for an email already in the community
    # — both rows ended up pointing at the same Cognito sub (since
    # _create_cognito_user errors silently on UsernameExistsException
    # and returns None), but the duplicate DDB User row was a real
    # orphan that confused downstream queries. Now: detect existing
    # community email and refuse with a clear message pointing the CA
    # at the existing user.
    existing = db.get_user_by_email(user.community_id, email)
    if existing is not None:
        return _error_redirect("/admin/community-users",
            f"{existing.name} <{existing.email}> is already in this "
            "community. Use the existing entry; you can grant them "
            "app memberships from their row.")
    new_user = User(community_id=user.community_id, email=email, name=name,
                    community_role="member", phone=phone, notes=notes)
    cognito_sub = _create_cognito_user(email, name)
    if cognito_sub:
        new_user.cognito_sub = cognito_sub
    db.put_user(new_user)
    log.info("CA %s added user %s (%s) cognito=%s — no app membership",
             user.user_id, new_user.user_id, email, bool(cognito_sub))
    return _redirect("/admin/community-users")


def _ca_membership_args(event: dict, user: User
                        ) -> tuple[User | None, Application | None,
                                   str | None]:
    """Shared input parsing for the three CA-level membership APIs.

    Returns (target_user, target_app, error_response). Validates that
    both target_user and target_app belong to the CA's own community
    so an attacker who guesses ids from another tenant can't act on
    them. None tuple elements indicate validation failure; the error
    string is the response body the caller should surface.
    """
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method != "POST":
        return None, None, "POST required"
    uid = (_get_param(event, "user_id") or "").strip()
    aid = (_get_param(event, "target_app_id") or "").strip()
    if not uid or not aid:
        return None, None, "user_id and target_app_id required"
    target_user = db.get_user(user.community_id, uid)
    target_app = db.get_application(user.community_id, aid)
    if target_user is None or target_app is None:
        return None, None, "user or app not found in this community"
    return target_user, target_app, None


def _api_ca_membership_add(event: dict, user: User,
                           community: Community | None) -> dict:
    target_user, target_app, err = _ca_membership_args(event, user)
    if err:
        if err == "POST required":
            return _text(405, err)
        return _error_redirect_or_next(event,
            "/admin/community-users", err.capitalize() + ".")
    role = (_get_param(event, "role") or "member").strip()
    if role not in ("member", "aa"):
        return _error_redirect_or_next(event,
            "/admin/community-users",
            "Role must be Member or App Admin.")
    if db.get_membership(target_app.app_id, target_user.user_id):
        # Already a member — idempotent. Don't clobber the existing role.
        return _redirect_next(event, "/admin/community-users")
    db.put_membership(Membership(
        community_id=user.community_id, app_id=target_app.app_id,
        user_id=target_user.user_id, app_role=role,
    ))
    log.info("CA %s added user %s to app %s as %s",
             user.user_id, target_user.user_id, target_app.app_id, role)
    return _redirect_next(event, "/admin/community-users")


def _api_ca_membership_remove(event: dict, user: User,
                              community: Community | None) -> dict:
    target_user, target_app, err = _ca_membership_args(event, user)
    if err:
        if err == "POST required":
            return _text(405, err)
        return _error_redirect_or_next(event,
            "/admin/community-users", err.capitalize() + ".")
    db.delete_membership(target_app.app_id, target_user.user_id)
    log.info("CA %s removed user %s from app %s",
             user.user_id, target_user.user_id, target_app.app_id)
    return _redirect_next(event, "/admin/community-users")


def _api_ca_membership_toggle(event: dict, user: User,
                              community: Community | None) -> dict:
    target_user, target_app, err = _ca_membership_args(event, user)
    if err:
        if err == "POST required":
            return _text(405, err)
        return _error_redirect_or_next(event,
            "/admin/community-users", err.capitalize() + ".")
    new_role = (_get_param(event, "new_role") or "").strip()
    if new_role not in ("member", "aa"):
        return _error_redirect_or_next(event,
            "/admin/community-users",
            "Role must be Member or App Admin.")
    mem = db.get_membership(target_app.app_id, target_user.user_id)
    if mem is None:
        return _error_redirect_or_next(event,
            "/admin/community-users",
            "Membership not found.")
    mem.app_role = new_role
    db.put_membership(mem)
    log.info("CA %s set %s in app %s to role=%s",
             user.user_id, target_user.user_id, target_app.app_id, new_role)
    return _redirect_next(event, "/admin/community-users")


# ---- Households (community-level spousal/family grouping) ------------------

def _ca_households_page(event: dict, user: User,
                        community: Community | None) -> dict:
    """View + manage household (spousal/family) links for the whole
    community. household_id lives on the User, so a link spans EVERY app —
    a couple stays paired across the couples club, men's club, women's club,
    etc. The flexible_event "someone in your household already replied"
    warning reads these."""
    org = community.name if community else user.community_id
    users = sorted(db.list_users(user.community_id),
                   key=lambda u: (u.name or u.email).lower())
    by_hh: dict[str, list[User]] = {}
    ungrouped: list[User] = []
    for u in users:
        (by_hh.setdefault(u.household_id, []) if u.household_id
         else ungrouped).append(u)

    btn = ("font-size:0.8em;cursor:pointer;background:none;border:none;"
           "color:#c33;text-decoration:underline;padding:0;margin-left:6px")
    hh_html = ""
    for hid, members in sorted(by_hh.items(),
                               key=lambda kv: (kv[1][0].name or "").lower()):
        lis = ""
        for m in members:
            lis += (f"<li>{html.escape(m.name or m.email)} "
                    f"<span style='color:#888'>&lt;{html.escape(m.email)}&gt;</span>"
                    "<form method='post' action='/api/households/unpair' "
                    "style='display:inline'>"
                    f"<input type='hidden' name='user_id' value='{m.user_id}'>"
                    f"<button type='submit' style='{btn}'>remove</button>"
                    "</form></li>")
        warn = (" <span style='color:#a80;font-size:0.85em'>(only one person "
                "— pair someone in, or remove)</span>" if len(members) == 1 else "")
        hh_html += ("<div style='margin:8px 0;padding:8px 12px;border:1px solid "
                    "#e3efe3;border-radius:6px'>"
                    f"<ul style='margin:0;padding-left:18px'>{lis}</ul>{warn}</div>")

    opts = "".join(
        f"<option value='{u.user_id}'>{html.escape(u.name or u.email)} "
        f"({html.escape(u.email)})</option>" for u in users)

    body = (
        "<h1>Households</h1>"
        f"<p style='color:#666;margin-top:-6px'>{html.escape(org)}</p>"
        + _flash_banner_html(event)
        + "<p style='color:#555;max-width:640px;margin:0 auto 16px'>Spousal / "
          "family links are community-wide — they apply across <b>every</b> app "
          "(couples club, men's, women's, …), because they live on the person, "
          "not the app. A household can hold more than two people (e.g. grown "
          "children with their own email). The poll's “someone in your "
          "household already replied” warning uses these.</p>"
          "<h2 style='font-size:1.1em'>Pair two people</h2>"
          "<form method='post' action='/api/households/pair' "
          "style='margin-bottom:6px'>"
          f"<select name='user_a' style='padding:6px'>{opts}</select> "
          "<b>+</b> "
          f"<select name='user_b' style='padding:6px'>{opts}</select> "
          "<button type='submit' style='padding:6px 16px;cursor:pointer'>"
          "Pair</button></form>"
          "<p style='color:#888;font-size:0.85em;margin:0 0 18px'>"
          "Pairing someone who's already in a household just adds the other "
          "person to it (e.g. a child).</p>"
          "<h2 style='font-size:1.1em'>Households</h2>"
        + (hh_html or "<p style='color:#888'>No households yet.</p>")
        + (f"<h2 style='font-size:1.1em'>Not in a household "
           f"({len(ungrouped)})</h2>"
           "<ul style='columns:2;color:#666;max-width:640px;margin:0 auto;"
           "text-align:left;display:inline-block'>"
           + "".join(f"<li>{html.escape(u.name or u.email)}</li>"
                     for u in ungrouped) + "</ul>" if ungrouped else "")
        + _ca_nav_bar("households")
    )
    return _html(200, _page(body, narrow=False, title="Households"))


def _api_ca_household_pair(event: dict, user: User,
                           community: Community | None) -> dict:
    if _http_method(event) != "POST":
        return _text(405, "POST required")
    a = (_get_param(event, "user_a") or "").strip()
    b = (_get_param(event, "user_b") or "").strip()
    if not a or not b or a == b:
        return _error_redirect("/admin/households",
                               "Pick two different people.")
    ua = db.get_user(user.community_id, a)
    ub = db.get_user(user.community_id, b)
    if ua is None or ub is None:
        return _error_redirect("/admin/households", "User not found.")
    hid = ua.household_id or ub.household_id or ("hh-" + secrets.token_hex(6))
    for u in (ua, ub):
        if u.household_id != hid:
            u.household_id = hid
            db.put_user(u, expected_version=u.version)
    log.info("CA %s paired %s + %s household=%s", user.user_id, a, b, hid)
    return _redirect("/admin/households?notice=" + urllib.parse.quote(
        f"Paired {ua.name or ua.email} and {ub.name or ub.email}."))


def _api_ca_household_unpair(event: dict, user: User,
                             community: Community | None) -> dict:
    if _http_method(event) != "POST":
        return _text(405, "POST required")
    uid = (_get_param(event, "user_id") or "").strip()
    u = db.get_user(user.community_id, uid)
    if u is None:
        return _error_redirect("/admin/households", "User not found.")
    if u.household_id is not None:
        u.household_id = None
        db.put_user(u, expected_version=u.version)
    return _redirect("/admin/households?notice=" + urllib.parse.quote(
        f"Removed {u.name or u.email} from their household."))


def _api_app_update(event: dict, user: User,
                    community: Community | None) -> dict:
    """Update an Application's name and/or description from the CA landing page.

    Accessible to CA and UA (these are per-app metadata, not structural
    ops). Description is also editable inside the app via /admin/settings
    by AAs — both paths write to the same field; whichever change
    happens last wins. Name editing is CA-mode only; the in-app
    settings page doesn't expose it.

    Name is required if present in the form; description is optional.
    A missing ``name`` param leaves the existing name unchanged so the
    legacy description-only callers (and the description-only edit
    form that pre-dated this handler) keep working.
    """
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method != "POST":
        return _text(405, "POST required")
    app_id = (_get_param(event, "app_id") or "").strip()
    if not app_id:
        return _error_redirect_or_next(event, "/admin/apps",
            "Missing app id.")
    target = db.get_application(user.community_id, app_id)
    if target is None:
        return _redirect("/admin/apps")
    raw_name = _get_param(event, "name")
    if raw_name is not None:
        new_name = raw_name.strip()
        if not new_name:
            return _error_redirect_or_next(event, "/admin/apps",
                "App name cannot be blank.")
        target.name = new_name
    description = (_get_param(event, "description") or "").strip()
    target.description = description
    db.put_application(target)
    log.info("%s %s updated app %s (name=%r, desc=%d chars)",
             user.community_role.upper(), user.user_id, app_id,
             target.name, len(description))
    return _redirect_next(event, "/admin/apps")


# Legacy alias for the original description-only endpoint. Kept so any
# bookmarked or in-flight POST keeps working; new code routes to
# /api/apps/update.
_api_app_update_description = _api_app_update


def _api_app_delete(event: dict, user: User,
                    community: Community | None) -> dict:
    # Deleting an app — and cascading away every template, cohort,
    # slot, assignment, and membership tied to it — is the most
    # destructive single action in the system. CA only.
    if user.community_role != "ca":
        return _text(403,
                     "Only Community Admins can delete apps.")
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method != "POST":
        return _text(405, "POST required")
    app_id = (_get_param(event, "app_id") or "").strip()
    if not app_id:
        return _error_redirect("/admin/apps", "Missing app id.")
    target = db.get_application(user.community_id, app_id)
    if target is None:
        return _redirect("/admin/apps")     # already gone, idempotent
    counts = db.delete_application(user.community_id, app_id)
    log.info("CA %s deleted app %s (name=%r); cascade=%s",
             user.user_id, app_id, target.name, counts)
    return _redirect("/admin/apps")


def _page(body: str, *, narrow: bool = True, title: str = "Community Organizer",
          og_title: str | None = None, og_description: str | None = None,
          og_image: str | None = None) -> str:
    width = "480px" if narrow else "960px"
    spinner = (
        "<div id='loading' style='display:none;position:fixed;top:0;left:0;"
        "width:100%;height:100%;background:rgba(255,255,255,0.7);z-index:9999;"
        "justify-content:center;align-items:center'>"
        "<div style='border:4px solid #ddd;border-top:4px solid #2a7;"
        "border-radius:50%;width:36px;height:36px;"
        "animation:spin 0.8s linear infinite'></div></div>"
        "<style>@keyframes spin{to{transform:rotate(360deg)}}</style>"
        "<script>"
        # Bail on defaultPrevented so an onsubmit/onclick that
        # showed a confirmation modal and returned false doesn't get
        # its cancel-the-action followed by a spinner that never
        # goes away (clicking cancel on the cancel-
        # event confirm dialog left the spinner-of-death visible).
        "document.addEventListener('click',function(e){"
        "if(e.defaultPrevented)return;"
        "var a=e.target.closest('a');"
        "if(a&&a.href&&!a.href.startsWith('javascript'))"
        "document.getElementById('loading').style.display='flex'});"
        "document.addEventListener('submit',function(e){"
        "if(e.defaultPrevented)return;"
        "if(!e.target.classList.contains('no-spinner')){"
        "document.getElementById('loading').style.display='flex'}});"
        # Browser back/forward and bfcache restore: when the user navigates
        # away mid-submit we flip the spinner on, then the browser caches
        # the page in that state. Hitting Back restores the page with the
        # spinner still visible — looks like a hung site.
        # `pageshow` fires on both initial load (persisted=false) and
        # bfcache restore (persisted=true); hide the spinner either way
        # so a refresh never finds it stuck on.
        "window.addEventListener('pageshow',function(){"
        "var el=document.getElementById('loading');"
        "if(el)el.style.display='none'});"
        # Real-User-Monitoring beacon: report perceived page-load timing once,
        # after load, via sendBeacon (fire-and-forget, non-blocking). Wrapped
        # in try/catch + feature guards so it can never affect the page.
        "try{window.addEventListener('load',function(){setTimeout(function(){"
        "if(!navigator.sendBeacon||!performance.getEntriesByType)return;"
        "var n=performance.getEntriesByType('navigation')[0];if(!n)return;"
        "navigator.sendBeacon('/api/rum',JSON.stringify({"
        "p:location.pathname,ttfb:Math.round(n.responseStart),"
        "dcl:Math.round(n.domContentLoadedEventEnd),"
        "load:Math.round(n.loadEventEnd),type:n.type}));"
        "},0)});}catch(e){}"
        "function confirmModal(message,onYes,yesLabel,yesColor,noLabel){"
        "yesLabel=yesLabel||'OK';"
        "yesColor=yesColor||'#c33';"
        "noLabel=noLabel||'Cancel';"
        "var d=document.createElement('div');"
        "d.style.cssText='position:fixed;top:0;left:0;width:100%;height:100%;"
        "background:rgba(0,0,0,0.4);z-index:10000;display:flex;"
        "justify-content:center;align-items:center';"
        "var card=document.createElement('div');"
        "card.style.cssText='background:white;padding:24px 32px;"
        "border-radius:8px;max-width:400px;text-align:center;"
        "font-family:-apple-system,BlinkMacSystemFont,sans-serif';"
        "var p=document.createElement('p');"
        "p.style.cssText='font-size:1.1em;margin-bottom:20px';"
        "p.textContent=message;"
        "card.appendChild(p);"
        "var btnYes=document.createElement('button');"
        "btnYes.textContent=yesLabel;"
        "btnYes.style.cssText='padding:8px 20px;cursor:pointer;color:white;"
        "background:'+yesColor+';border:none;border-radius:4px;"
        "font-size:1em;margin-right:8px';"
        "btnYes.onclick=function(){d.remove();"
        "document.getElementById('loading').style.display='flex';onYes()};"
        "var btnNo=document.createElement('button');"
        "btnNo.textContent=noLabel;"
        "btnNo.style.cssText='padding:8px 20px;cursor:pointer;"
        "background:#eee;border:none;border-radius:4px;font-size:1em';"
        "btnNo.onclick=function(){d.remove()};"
        "card.appendChild(btnYes);card.appendChild(btnNo);"
        "d.appendChild(card);document.body.appendChild(d);}"
        "function confirmSubmit(form,message,yesLabel,yesColor,noLabel){"
        # The document-level submit listener already flipped the
        # spinner on before this onsubmit handler ran. Hide it now so
        # a Cancel doesn't leave the user staring at a spinner
        # forever. confirmModal's Yes handler re-shows it when the
        # user actually proceeds — programmatic form.submit() doesn't
        # re-fire the submit event, so the listener won't re-trigger.
        "document.getElementById('loading').style.display='none';"
        "confirmModal(message,function(){form.submit()},"
        "yesLabel,yesColor,noLabel);"
        "return false;}"
        "function showReleaseModal(url){"
        "var d=document.createElement('div');"
        "d.style.cssText='position:fixed;top:0;left:0;width:100%;height:100%;"
        "background:rgba(0,0,0,0.4);z-index:10000;display:flex;"
        "justify-content:center;align-items:center';"
        "d.innerHTML='<div style=\"background:white;padding:24px 32px;"
        "border-radius:8px;max-width:360px;text-align:center;"
        "font-family:-apple-system,BlinkMacSystemFont,sans-serif\">"
        "<p style=\"font-size:1.1em;margin-bottom:16px\">Withdraw from this slot?</p>"
        "<label style=\"display:block;margin-bottom:20px;font-size:0.9em;"
        "color:#666;cursor:pointer\">"
        "<input type=\"checkbox\" id=\"release-notify\" checked style=\"margin-right:6px\">"
        "Send me a confirmation with calendar update</label>"
        "<button onclick=\"doRelease(\\x27'+url+'\\x27)\" style=\"padding:8px 20px;"
        "cursor:pointer;color:white;background:#c33;border:none;"
        "border-radius:4px;font-size:1em;margin-right:8px\">Withdraw</button>"
        "<button onclick=\"this.closest(\\x27div\\x27).parentElement.remove()\" "
        "style=\"padding:8px 20px;cursor:pointer;background:#eee;border:none;"
        "border-radius:4px;font-size:1em\">Cancel</button></div>';"
        "document.body.appendChild(d)}"
        "function doRelease(url){"
        "var n=document.getElementById('release-notify');"
        "if(n&&n.checked)url+='&notify_me=1';"
        "var d=document.querySelector('[style*=\"z-index: 10000\"]')"
        "||document.querySelector('[style*=\"z-index:10000\"]');"
        "if(d)d.remove();"
        "document.getElementById('loading').style.display='flex';"
        "var f=document.createElement('form');"
        "f.method='POST';f.action=url;"
        "document.body.appendChild(f);"
        "f.submit()}"
        "</script>"
    )
    # Open Graph + Twitter tags so links unfurl with a card (image + title)
    # in iMessage, Slack, etc. og:title rides the page title (often the app /
    # community name, so it's already somewhat per-app); the image is generic
    # for now. og:image must be absolute + public — /og-image.png is served
    # unauthenticated, and DOMAIN_NAME is per-stack so each one points at itself.
    _t = html.escape(og_title or title)
    _og_img = og_image or f"https://{DOMAIN_NAME}/og-image.png"
    _og_desc = html.escape(og_description or (
        "Sign-ups, schedules, and reminders for parish ministries "
        "and small groups."))
    og_tags = (
        "<meta property='og:type' content='website'>"
        "<meta property='og:site_name' content='Community Organizer'>"
        f"<meta property='og:title' content='{_t}'>"
        f"<meta property='og:description' content='{_og_desc}'>"
        f"<meta property='og:image' content='{_og_img}'>"
        "<meta property='og:image:width' content='1200'>"
        "<meta property='og:image:height' content='630'>"
        f"<meta property='og:url' content='https://{DOMAIN_NAME}/'>"
        "<meta name='twitter:card' content='summary_large_image'>"
        f"<meta name='twitter:title' content='{_t}'>"
        f"<meta name='twitter:description' content='{_og_desc}'>"
        f"<meta name='twitter:image' content='{_og_img}'>"
        "<meta name='theme-color' content='#2aa877'>"
    )
    return (
        "<!doctype html><html><head>"
        "<meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        + og_tags
        + f"<title>{html.escape(title)}</title></head>"
        "<body style=\"font-family: -apple-system, BlinkMacSystemFont, "
        f"'Segoe UI', Roboto, sans-serif; max-width: {width}; "
        "margin: 4vh auto; padding: 24px; text-align: center;\">"
        f"{body}{spinner}"
        "</body></html>"
    )


def _fmt_time(hhmm: str) -> str:
    h, m = (int(x) for x in hhmm.split(":"))
    suffix = "AM" if h < 12 else "PM"
    h12 = h if 1 <= h <= 12 else (12 if h == 0 else h - 12)
    return f"{h12}:{m:02d} {suffix}"


def _html(status: int, body: str) -> dict:
    return {"statusCode": status,
            "headers": {"Content-Type": "text/html; charset=utf-8"},
            "body": body}


def _og_image_response() -> dict:
    """Serve the social-unfurl PNG. Binary via base64 (Lambda FURL decodes it
    before CloudFront). Cached a day so crawlers + the CDN don't refetch."""
    if not _OG_IMAGE_B64:
        return _text(404, "not found")
    return {"statusCode": 200,
            "headers": {"Content-Type": "image/png",
                        "Cache-Control": "public, max-age=86400"},
            "body": _OG_IMAGE_B64,
            "isBase64Encoded": True}


def _app_og_image_response(app_id: str) -> dict:
    """Serve a single app's custom social-card art from S3, falling back to the
    generic image when the app has none (or anything goes wrong). Public."""
    if COMMUNITY_ID and OG_ART_BUCKET and app_id:
        app = db.get_application(COMMUNITY_ID, app_id)
        if app and app.og_art_content_type:
            try:
                obj = _s3().get_object(Bucket=OG_ART_BUCKET, Key=app_id)
                data = obj["Body"].read()
                return {"statusCode": 200,
                        "headers": {"Content-Type": app.og_art_content_type,
                                    "Cache-Control": "public, max-age=86400"},
                        "body": base64.b64encode(data).decode(),
                        "isBase64Encoded": True}
            except Exception:
                log.exception("og art fetch failed for app %s", app_id)
    return _og_image_response()


def _maybe_current_user(event: dict) -> User | None:
    """Resolve the signed-in user from cookies WITHOUT redirecting (for public
    pages that adapt to auth state). Returns None when not logged in."""
    cookies = auth.parse_cookies(event)
    token = cookies.get(auth.ID_COOKIE)
    if not token:
        return None
    try:
        claims = auth.verify_id_token(token)
    except Exception:
        return None
    sub = claims.get("sub", "")
    if not sub:
        return None
    return db.get_user_by_cognito_sub(
        sub, community_id=os.environ.get("COMMUNITY_ID") or None)


def _app_landing(event: dict, slug: str) -> dict:
    """Public front door for one app at /home/<slug> (alias: /a/<slug>).

    - signed-in member (or CA/UA): deep-link straight into the app, skipping
      the multi-app launcher.
    - signed-in non-member: the public card + a note (no access leak).
    - logged out / crawler: the public card + a sign-in that returns here-into
      the app, plus the app's own OG tags so the link unfurls per-app."""
    app = db.get_application_by_slug(COMMUNITY_ID, slug) if COMMUNITY_ID else None
    if app is None or not app.active:
        return _html(404, _page(
            "<h1>Page not found</h1><p>This link doesn't match any group. "
            "<a href='/'>Go to the home page</a>.</p>", title="Not found"))
    deep = f"/?app_id={app.app_id}"
    user = _maybe_current_user(event)
    if user is not None and (user.community_role in ("ca", "ua")
                             or db.get_membership(app.app_id, user.user_id)):
        return _redirect(deep)

    name = html.escape(app.name)
    desc = html.escape(app.description) if app.description else ""
    og_img = f"https://{DOMAIN_NAME}/og/{app.app_id}.png"
    if user is not None:
        cta = ("<p style='color:#a80;margin-top:20px'>You're signed in but not "
               f"a member of {name}. <a href='/'>Go to your home page</a>.</p>")
    else:
        nxt = urllib.parse.quote(deep, safe="")
        cta = (f"<p style='margin-top:24px'><a href='/login?next={nxt}' "
               "style='display:inline-block;padding:10px 26px;background:#2a7;"
               "color:#fff;border-radius:6px;text-decoration:none;"
               f"font-size:1.05em'>Sign in to {name}</a></p>")
    body = (
        f"<h1 style='margin-bottom:6px'>{name}</h1>"
        + (f"<p style='color:#555;font-size:1.05em'>{desc}</p>" if desc else "")
        + cta
        + "<p style='margin-top:36px;color:#aaa;font-size:0.85em'>"
        "Powered by Community Organizer</p>"
    )
    return _html(200, _page(body, title=app.name, og_title=app.name,
                            og_description=app.description or None,
                            og_image=og_img))


# ---- AA: the Share page (slug + public description + card art) ------------

def _parse_multipart_file(event: dict, field: str) -> tuple[bytes | None, str]:
    """Pull one uploaded file's raw bytes + declared content-type out of a
    multipart/form-data POST. Returns (None, '') if absent/not multipart."""
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    ct = headers.get("content-type", "")
    if "multipart/form-data" not in ct:
        return None, ""
    raw = event.get("body") or ""
    data = base64.b64decode(raw) if event.get("isBase64Encoded") else raw.encode(
        "utf-8", "replace")
    import email as _email
    msg = _email.message_from_bytes(
        b"Content-Type: " + ct.encode() + b"\r\n\r\n" + data)
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        disp = part.get("Content-Disposition", "") or ""
        if f'name="{field}"' in disp:
            payload = part.get_payload(decode=True)
            return payload, part.get_content_type()
    return None, ""


def _public_page_section(app: Application, slug: str, share_url: str,
                         *, next_path: str = "/admin/settings") -> str:
    """The 'Public page' config block: shareable link + editable slug +
    public description + social card image. Rendered on the App Settings
    tab (consolidated from the former standalone Share page, 2026-06-29).
    The art forms carry ``next`` in the query string because the upload is
    multipart — its body fields aren't visible to _get_param."""
    has_art = bool(app.og_art_content_type)
    art_block = (
        f"<img src='/og/{app.app_id}.png' alt='card preview' "
        "style='max-width:100%;width:480px;border:1px solid #ddd;"
        "border-radius:8px;margin:8px 0'>"
        + (f"<form method='post' class='no-spinner' "
           f"action='/api/sharing/delete-art?app_id={app.app_id}"
           f"&next={next_path}' style='display:inline'>"
           "<button type='submit' style='font-size:0.85em;color:#a80;"
           "background:none;border:none;text-decoration:underline;"
           "cursor:pointer;padding:0'>remove custom image</button></form>"
           if has_art else
           "<p style='color:#888;font-size:0.85em'>Using the generic "
           "Community Organizer image.</p>")
    )
    return (
        "<h2 style='font-size:1.1em;color:#444'>Public page</h2>"
        f"<p style='margin-top:-6px'><a href='{share_url}'>{share_url}</a></p>"
        "<p style='color:#888;font-size:0.85em'>Send this link to members. It "
        "opens a public page for this app and, once they sign in, takes them "
        "straight here — overriding their last-used app.</p>"
        "<form method='post' action='/api/app/slug"
        f"?app_id={app.app_id}' style='text-align:left;margin-top:12px'>"
        f"<input type='hidden' name='app_id' value='{app.app_id}'>"
        f"<input type='hidden' name='version' value='{app.version or 0}'>"
        f"<input type='hidden' name='next' value='{next_path}'>"
        "<label style='display:block;font-size:0.9em;color:#555'>Link name "
        "(the part after /home/)</label>"
        f"<input type='text' name='slug' value='{html.escape(slug)}' "
        "style='padding:6px;width:280px' pattern='[a-zA-Z0-9 -]+'>"
        "<label style='display:block;font-size:0.9em;color:#555;margin-top:12px'>"
        "Public description (shown on the card &amp; the launcher)</label>"
        f"<textarea name='description' rows='2' style='padding:6px;width:100%;"
        f"max-width:480px'>{html.escape(app.description or '')}</textarea>"
        "<p style='margin:10px 0 0'><button type='submit' "
        "style='padding:8px 20px;cursor:pointer'>Save</button></p>"
        "</form>"
        "<h3 style='font-size:1em;color:#444;margin-top:20px'>Card image</h3>"
        + art_block
        + "<form method='post' class='no-spinner' enctype='multipart/form-data' "
        f"action='/api/sharing/upload-art?app_id={app.app_id}&next={next_path}' "
        "style='text-align:left;margin-top:10px'>"
        "<input type='file' name='art' accept='image/png,image/jpeg' required> "
        "<button type='submit' style='padding:6px 16px;cursor:pointer'>"
        "Upload</button>"
        "<p style='color:#888;font-size:0.85em;margin-top:6px'>PNG or JPEG, "
        "ideally 1200&times;630 px. Max 2&nbsp;MB.</p>"
        "</form>"
    )


def _app_sharing_page(event: dict, user: User, community: Community | None,
                      app: Application, membership: Membership | None) -> dict:
    # The standalone Share tab was folded into the App Settings page
    # (2026-06-29). Keep the route as a permanent redirect so old links /
    # bookmarks land on the consolidated screen.
    return _redirect("/admin/settings")


def _api_sharing_save(event: dict, user: User, community: Community | None,
                      app: Application, membership: Membership | None) -> dict:
    if not _is_admin(user, membership) and user.community_role not in ("ca", "ua"):
        return _text(403, "admins only")
    if _http_method(event) != "POST":
        return _text(405, "POST required")
    raw_slug = _get_param(event, "slug") or ""
    new_slug = _slugify(raw_slug)
    taken = {a.public_slug for a in db.list_applications(app.community_id)
             if a.public_slug and a.app_id != app.app_id}
    if new_slug in taken:
        return _error_redirect_or_next(event, "/admin/sharing",
            f"The link name '{new_slug}' is already in use. Pick another.")
    try:
        version = int(_get_param(event, "version") or 0)
    except ValueError:
        version = 0
    app.public_slug = new_slug
    desc = _get_param(event, "description")
    if desc is not None:
        app.description = desc.strip()
    try:
        db.put_application(app, expected_version=version)
    except db.ConcurrencyConflict:
        return _error_redirect_or_next(event, "/admin/sharing",
            "Someone else just changed this app — reloaded with their values.")
    return _redirect("/admin/sharing")


def _api_app_slug_save(event: dict, user: User, community: Community | None,
                       app: Application, membership: Membership | None) -> dict:
    """Save the public_slug (the /home/<slug> link) and, when present, the
    public description. Drives the Public-page section of the App Settings
    tab; redirects back to ``next``. AA-or-CA/UA gated so both roles edit it."""
    if not _is_admin(user, membership) and user.community_role not in ("ca", "ua"):
        return _text(403, "admins only")
    if _http_method(event) != "POST":
        return _text(405, "POST required")
    nxt = _safe_next(_get_param(event, "next") or "/admin/settings")
    new_slug = _slugify(_get_param(event, "slug") or "")
    if not new_slug:
        return _error_redirect_or_next(event, nxt, "Link name can't be blank.")
    taken = {a.public_slug for a in db.list_applications(app.community_id)
             if a.public_slug and a.app_id != app.app_id}
    if new_slug in taken:
        return _error_redirect_or_next(event, nxt,
            f"The link name '{new_slug}' is already in use. Pick another.")
    try:
        version = int(_get_param(event, "version") or 0)
    except ValueError:
        version = 0
    app.public_slug = new_slug
    # Description rides along on the same form when the Public-page section
    # renders it; absent on a slug-only surface, so only touch it if sent.
    desc = _get_param(event, "description")
    if desc is not None:
        app.description = desc.strip()
    try:
        db.put_application(app, expected_version=version)
    except db.ConcurrencyConflict:
        return _error_redirect_or_next(event, nxt,
            "Someone else just changed this app — reloaded with their values.")
    sep = "&" if "?" in nxt else "?"
    return _redirect(f"{nxt}{sep}notice="
                     + urllib.parse.quote("Public page saved."))


def _api_sharing_upload_art(event: dict, user: User, community: Community | None,
                            app: Application, membership: Membership | None) -> dict:
    if not _is_admin(user, membership) and user.community_role not in ("ca", "ua"):
        return _text(403, "admins only")
    if _http_method(event) != "POST":
        return _text(405, "POST required")
    nxt = _safe_next(_get_param(event, "next") or "/admin/settings")
    if not OG_ART_BUCKET:
        return _error_redirect_or_next(event, nxt,
            "Image uploads aren't configured on this server.")
    data, _declared = _parse_multipart_file(event, "art")
    if not data:
        return _error_redirect_or_next(event, nxt,
            "No image received. Choose a PNG or JPEG file.")
    if len(data) > 2_000_000:
        return _error_redirect_or_next(event, nxt,
            "Image is too large (2 MB max).")
    # Trust the bytes, not the declared type (defense against a spoofed
    # Content-Type / disguised payload).
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        ctype = "image/png"
    elif data[:3] == b"\xff\xd8\xff":
        ctype = "image/jpeg"
    else:
        return _error_redirect_or_next(event, nxt,
            "That doesn't look like a PNG or JPEG image.")
    try:
        _s3().put_object(Bucket=OG_ART_BUCKET, Key=app.app_id, Body=data,
                         ContentType=ctype)
    except Exception:
        log.exception("og art upload failed for app %s", app.app_id)
        return _error_redirect_or_next(event, nxt,
            "Upload failed — please try again.")
    app.og_art_content_type = ctype
    db.put_application(app)
    log.info("AA %s set card art for app %s (%s, %d bytes)",
             user.user_id, app.app_id, ctype, len(data))
    return _redirect(nxt)


def _api_sharing_delete_art(event: dict, user: User, community: Community | None,
                            app: Application, membership: Membership | None) -> dict:
    if not _is_admin(user, membership) and user.community_role not in ("ca", "ua"):
        return _text(403, "admins only")
    if _http_method(event) != "POST":
        return _text(405, "POST required")
    nxt = _safe_next(_get_param(event, "next") or "/admin/settings")
    if OG_ART_BUCKET and app.og_art_content_type:
        try:
            _s3().delete_object(Bucket=OG_ART_BUCKET, Key=app.app_id)
        except Exception:
            log.exception("og art delete failed for app %s", app.app_id)
    app.og_art_content_type = None
    db.put_application(app)
    return _redirect(nxt)


def _text(status: int, body: str) -> dict:
    return {"statusCode": status,
            "headers": {"Content-Type": "text/plain; charset=utf-8"},
            "body": body}


def _redirect(location: str) -> dict:
    return {"statusCode": 302, "headers": {"Location": location}, "body": ""}


def _error_redirect(source: str, msg: str) -> dict:
    """Redirect back to ``source`` with the error message tucked into a
    ``?error=`` query param. Landing pages render the param as a styled
    banner via ``_flash_banner_html``. Replaces the old ``_text(400,
    msg)`` pattern that put admins on a bare text/plain page after a
    failed form submit (see #182).

    If ``source`` already has a query string, the error is appended
    with ``&``; existing fragment is preserved. The error is URL-encoded
    so reserved characters in the message survive the round-trip.
    """
    encoded = urllib.parse.quote(msg, safe="")
    base, _, frag = source.partition("#")
    sep = "&" if "?" in base else "?"
    new_url = f"{base}{sep}error={encoded}"
    if frag:
        new_url += f"#{frag}"
    return _redirect(new_url)


def _error_redirect_or_next(event: dict, default_source: str, msg: str) -> dict:
    """Like ``_error_redirect`` but honors any ``?next=`` / form ``next``
    param the caller supplied (validated via ``_safe_next``). When the
    form already declared where to go on success, the failure path
    should land back there too — otherwise the error banner shows up
    on an unrelated page.
    """
    raw_next = _get_param(event, "next")
    source = _safe_next(raw_next) if raw_next else default_source
    return _error_redirect(source, msg)


_FLASH_BANNER_STYLE_ERROR = (
    "margin:12px auto;padding:12px 16px;max-width:640px;"
    "border:1px solid #c33;border-radius:6px;background:#fff5f5;"
    "color:#900;text-align:center;font-size:0.95em")
_FLASH_BANNER_STYLE_INFO = (
    "margin:12px auto;padding:12px 16px;max-width:640px;"
    "border:1px solid #2a7;border-radius:6px;background:#f4fbf6;"
    "color:#264;text-align:center;font-size:0.95em")


def _flash_banner_html(event: dict) -> str:
    """Render the ``?error=`` or ``?notice=`` flash message as a banner
    div, or empty string when neither param is present. Admin landing
    pages prepend this to their body so errors from
    ``_error_redirect`` and notices from ``_notice_redirect`` show up
    consistently across the app.

    The message is HTML-escaped so a redirector cannot inject markup
    via the query param.
    """
    err = _get_param(event, "error")
    if err:
        return (f"<div style='{_FLASH_BANNER_STYLE_ERROR}'>"
                f"{html.escape(err)}</div>")
    notice = _get_param(event, "notice")
    if notice:
        return (f"<div style='{_FLASH_BANNER_STYLE_INFO}'>"
                f"{html.escape(notice)}</div>")
    return ""
