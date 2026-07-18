"""Generate a full-month schedule summary as an HTML email.

Produces a table matching the parish's existing usher schedule email
format: DATE | TIME | <ROLE_PLURAL> columns, one assignee per row,
grouped by weekend (one DATE/TIME cell with ``rowspan`` over all
volunteers covering that slot).

The third column's header pluralizes the app's ``terminology`` —
"USHERS" for a parish app, "SCOREKEEPERS" for a sports league, etc.
Uses the explicit ``terminology_plural`` if set, otherwise derives via
``_pluralize``.

Two entry points:

    - ``generate_schedule_email``       — full (subject, text, html)
      triple suitable for the admin "Email schedule table to:" feature.
    - ``generate_schedule_table_html``  — just the centered ``<table>``,
      extracted from the full HTML for embedding in the publish
      broadcast (so every recipient sees the whole month, not just
      their own assignments).

Tested by:
    tests/core/test_schedule_email.py
"""
from __future__ import annotations

import datetime as dt
import html
import os
from collections import defaultdict

from . import db
from .models import Application, Assignment, Community, Slot, User


def _h(s: str) -> str:
    """HTML-escape a user-controlled string for safe interpolation.

    All member display names, slot names, app names, and role/role_plural
    flow into the table HTML via this helper. Without it, an admin (or
    self-registering member) could plant ``<img src=x onerror=…>`` in
    a name field and execute JS in every recipient's HTML email client.
    """
    return html.escape(s or "", quote=True)


def _pluralize(word: str) -> str:
    """Naive English plural; handles -s/-x/-z/-ch/-sh and consonant-y.

    Used to derive the column header from ``app.terminology`` when no
    explicit override (``app.terminology_plural``) is set:

        - "usher"   -> "ushers"   (regular)
        - "Mass"    -> "Masses"   (sibilant ending)
        - "party"   -> "parties"  (consonant-y)
        - "key"     -> "keys"     (vowel-y)
        - "child"   -> "childs"   (WRONG — irregulars need explicit override)

    Case is preserved on the first letter. See
    ``tests/core/test_pluralize.py`` for the full case matrix.
    """
    if not word:
        return word
    w = word.lower()
    if w.endswith(("s", "x", "z")) or w.endswith(("ch", "sh")):
        return word + "es"
    if w.endswith("y") and len(w) > 1 and w[-2] not in "aeiou":
        return word[:-1] + "ies"
    return word + "s"

DOMAIN_NAME = os.environ.get("DOMAIN_NAME", "community.example.org")


def _community_host(community) -> str:
    """Hostname for embedded URLs (mirrors notifier._community_host).
    Threads Community.public_url through so multi-community shared
    notifiers route each user back to the right stack; falls back to
    env DOMAIN_NAME for single-community deployments."""
    if community is not None and getattr(community, "public_url", None):
        return community.public_url
    return DOMAIN_NAME

_DAY_LABEL = {0: "Monday", 1: "Tuesday", 2: "Wednesday", 3: "Thursday",
              4: "Friday", 5: "Saturday", 6: "Sunday"}
_MONTH_LABEL = {1: "JANUARY", 2: "FEBRUARY", 3: "MARCH", 4: "APRIL",
                5: "MAY", 6: "JUNE", 7: "JULY", 8: "AUGUST",
                9: "SEPTEMBER", 10: "OCTOBER", 11: "NOVEMBER", 12: "DECEMBER"}
_MONTH_TITLE = {1: "January", 2: "February", 3: "March", 4: "April",
                5: "May", 6: "June", 7: "July", 8: "August",
                9: "September", 10: "October", 11: "November", 12: "December"}


def _fmt_time(hhmm: str) -> str:
    """24h ``"HH:MM"`` -> 12h ``"H:MM AM/PM"``.

    Duplicated from ``publishing._fmt_time`` for module independence
    (this module is also imported by the inbound Lambda, which doesn't
    pull in publishing). The behavior must stay in sync — any change
    here should be mirrored in publishing.
    """
    h, m = (int(x) for x in hhmm.split(":"))
    # Range guard mirrored from publishing._fmt_time (security fix D20).
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"_fmt_time: out-of-range {hhmm!r}")
    suffix = "AM" if h < 12 else "PM"
    h12 = h if 1 <= h <= 12 else (12 if h == 0 else h - 12)
    return f"{h12}:{m:02d} {suffix}"


def _fmt_date_long(iso_date: str) -> str:
    """ISO ``"YYYY-MM-DD"`` -> ``"DayAbbr, D MonthName"``  (day first).

    Day-first format mimics the parish's pre-existing usher email so
    the new tool's output reads identically. Differs from
    ``publishing._fmt_date`` which uses month-first ("Sun, May 31"
    instead of "Sunday, 31 May").
    """
    y, mo, d = (int(x) for x in iso_date.split("-"))
    date = dt.date(y, mo, d)
    return f"{_DAY_LABEL[date.weekday()]}, {d} {_MONTH_TITLE[mo]}"


def generate_schedule_table_html(
    community: Community,
    app: Application,
    yyyy_mm: str,
    template_ids: set[str] | None = None,
) -> str:
    """Build just the centered HTML ``<table>`` of the month's schedule.

    Used by ``publishing._build_email`` to embed the full schedule into
    every recipient's broadcast HTML body so each member sees the whole
    month, not just their own slots.

    ``template_ids`` (optional): restrict the table to slots generated from
    those templates — used to render a single cohort's "slice" of the
    schedule (a cohort is linked to one template). ``None`` = whole month.

    Implementation note: extracts the table substring from the full
    ``generate_schedule_email`` output, rather than refactoring the
    table builder out. Single source of truth for layout, at the cost
    of an extra (cheap) HTML scan per render. Always returns a
    non-empty ``<table>`` (even an empty schedule has month + column
    headers).
    """
    subj, _, html = generate_schedule_email(
        community, app, yyyy_mm, template_ids=template_ids)
    start = html.find("<table")
    end = html.find("</table>") + len("</table>")
    return html[start:end] if start != -1 else ""


def generate_schedule_email(
    community: Community,
    app: Application,
    yyyy_mm: str,
    template_ids: set[str] | None = None,
) -> tuple[str, str, str]:
    """Build (subject, body_text, body_html) for the month's schedule.

    The HTML body has a centered table:

        +--------+---------+----------------+
        | DATE   |  TIME   |  USHERS        |
        +========+=========+================+
        | Sun,   | 8:00 AM | Alice          |
        |  31    |         | Bob            |
        |  May   +---------+----------------+
        |        | 10:30   | Carol          |
        +--------+---------+----------------+
        | (spacer row between dates)        |
        +--------+---------+----------------+
        | Sun,   | 8:00 AM | David          |
        |  ...                              |

    The DATE and TIME cells use ``rowspan`` over their assignees so the
    layout matches the parish's pre-existing email format. Slots with
    no assignees show "(open)". Cancelled slots are omitted entirely.

    Subject format::

        <app.name> <Role> Schedule -- <Month> <Year>
        # e.g. "Test Ushers Usher Schedule -- May 2026"

    The third column header is pluralized via ``app.terminology_plural``
    (or ``_pluralize`` if blank) — so a parish gets "USHERS", a sports
    league gets "SCOREKEEPERS", etc.

    Returns ``(subject, body_text, body_html)``. The plain-text body is
    a bullet-list version, suitable for clients that strip HTML.
    """
    y, m = (int(x) for x in yyyy_mm.split("-"))
    month_name = _MONTH_TITLE[m]
    month_upper = _MONTH_LABEL[m]
    org_name = app.name
    event_noun = app.event_noun or "event"

    slots = sorted(db.list_slots(app.app_id, yyyy_mm),
                   key=lambda s: (s.local_date, s.start_time))
    if template_ids is not None:
        # Cohort-slice render: keep only slots from the given template(s).
        slots = [s for s in slots if s.template_id in template_ids]
    users_by_id = {u.user_id: u for u in db.list_users(community.community_id)}
    asgns_by_slot: dict[str, list[Assignment]] = defaultdict(list)
    for a in db.list_assignments_for_month(app.app_id, yyyy_mm):
        asgns_by_slot[a.slot_id].append(a)

    role = (app.terminology or "volunteer").capitalize()
    # Pluralized form for table column header — prefer explicit override
    role_plural = app.terminology_plural or _pluralize(app.terminology or "volunteer")
    subject = f"{org_name} {role} Schedule -- {month_name} {y}"

    # Build the table rows
    html_rows = []
    text_lines = []
    current_date = ""

    for slot in slots:
        if slot.cancelled:
            continue
        asgns = asgns_by_slot.get(slot.slot_id, [])
        names = [users_by_id.get(a.user_id, _stub(a.user_id)).name for a in asgns]
        if not names:
            names = ["(open)"]

        is_new_date = (slot.local_date != current_date)
        is_new_time = is_new_date  # first time on a new date

        date_label = _fmt_date_long(slot.local_date) if is_new_date else ""
        time_label = _fmt_time(slot.start_time)

        # (No blank spacer row between dates — it rendered as an empty
        # bordered/tinted row that read as a formatting error. Bold dates +
        # the rowspanned date/time cells already delineate each day.)
        current_date = slot.local_date

        # Text version
        if is_new_date:
            text_lines.append("")
            text_lines.append(f"{date_label}")
        text_lines.append(f"  {time_label}: {', '.join(names)}")

        # HTML version - first usher gets the date and time cells
        for i, name in enumerate(names):
            date_cell = ""
            time_cell = ""
            if i == 0:
                rowspan = len(names)
                if is_new_date:
                    date_cell = (
                        f'<td style="{_td_style()}" rowspan="{rowspan}">'
                        f'<b>{_h(date_label)}</b></td>'
                    )
                else:
                    date_cell = (
                        f'<td style="{_td_style()}" rowspan="{rowspan}">'
                        f'</td>'
                    )
                time_cell = (
                    f'<td style="{_td_style(bg=True)}" rowspan="{rowspan}">'
                    f'{_h(time_label)}</td>'
                )
            # Member display name — the most exploitable injection sink
            # because anyone added to the community can set this.
            name_cell = f'<td style="{_td_style()}">{_h(name)}</td>'
            html_rows.append(f'<tr>{date_cell}{time_cell}{name_cell}</tr>')

    # Compose the full HTML email. role and role_plural come from
    # admin-settable app.terminology — escape every interpolation.
    host = _community_host(community)
    intro = (
        f"<p>Here is the {_h(role.lower())} schedule for {_h(month_name)} {y}. "
        f"If you have a scheduling conflict, you can release your slot at "
        f'<a href="https://{_h(host)}/your-schedule">'
        f"https://{_h(host)}/your-schedule</a>. "
        f"Others covering your event and your cohort will be notified, "
        f"encouraging someone to sign up. You can also sign up for an "
        f"alternative slot at the same time.</p>"
    )

    header_row = (
        f'<tr>'
        f'<td colspan="3" style="{_header_style()}">'
        f'<b>{_h(month_upper)} {y}</b></td></tr>'
        f'<tr>'
        f'<td style="{_col_header_style()}"><b>DATE</b></td>'
        f'<td style="{_col_header_style(bg=True)}"><b>TIME</b></td>'
        f'<td style="{_col_header_style()}"><b>{_h(role_plural.upper())}</b></td>'
        f'</tr>'
    )

    table_html = (
        f'<table align="center" cellspacing="0" cellpadding="0" '
        f'style="border-collapse:collapse;border-spacing:0;'
        f'margin:0 auto;'
        f'font-family:Arial,sans-serif;font-size:14px">'
        f'{header_row}'
        + "\n".join(html_rows)
        + '</table>'
    )

    body_html = (
        f'<div style="font-family:Arial,sans-serif;font-size:14px">'
        f'{intro}'
        f'{table_html}'
        f'<p style="margin-top:16px">-- {_h(org_name)}</p>'
        f'</div>'
    )

    # Plain text version
    body_text = (
        f"Here is the {role.lower()} schedule for {month_name} {y}.\n"
        f"If you have a scheduling conflict, visit "
        f"https://{host}/your-schedule to release your slot. "
        f"Others covering your event and your cohort will be notified.\n"
        + "\n".join(text_lines)
        + f"\n\n-- {org_name}\n"
    )

    return subject, body_text, body_html


def _stub(user_id: str) -> User:
    """Fallback User for assignments whose user_id isn't in our user table.

    Should never happen in practice, but defensive: if the join misses
    we render "(open)" in the cell rather than crashing. This sits
    alongside the explicit ``if not names: names = ["(open)"]`` for
    truly-unassigned slots.
    """
    return User(community_id="?", email="?", name="(open)")


def _header_style() -> str:
    return (
        "border:2px solid rgb(0,0,0);"
        "background-color:rgb(179,179,179);"
        "padding:4px 8px;text-align:center;"
        "font-size:16px"
    )


def _col_header_style(*, bg: bool = False) -> str:
    bg_color = "background-color:rgb(241,248,246);" if bg else ""
    return (
        "border:2px solid rgb(0,0,0);"
        f"{bg_color}"
        "padding:4px 8px;vertical-align:top;"
        "font-weight:bold"
    )


def _td_style(*, bg: bool = False) -> str:
    bg_color = "background-color:rgb(241,248,246);" if bg else ""
    return (
        "border:1px solid rgb(154,154,154);"
        f"{bg_color}"
        "padding:4px 8px;vertical-align:top"
    )
