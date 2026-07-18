"""Community Organizer CLI.

Community-scoped: --community-id (or COMMUNITY_ID env var).
App-scoped: --app-id (or APP_ID env var; auto-detected if only one app).
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict

import click

from . import __version__
from .core import db, publishing, schedule_email, scheduling
from .core.models import (
    Application,
    Assignment,
    Community,
    Membership,
    Schedule,
    SlotTemplate,
    User,
)
from .providers.email import get_email_provider

_DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_DAY_LABEL = {v: k.capitalize() for k, v in _DAYS.items()}


def _parse_day(value: str) -> int:
    v = value.strip().lower()
    if v in _DAYS:
        return _DAYS[v]
    try:
        n = int(v)
    except ValueError:
        raise click.BadParameter("day must be mon/tue/.../sun or 0-6")
    if 0 <= n <= 6:
        return n
    raise click.BadParameter("day-of-week must be 0..6")


def _parse_hhmm(value: str) -> str:
    parts = value.split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise click.BadParameter("start-time must be HH:MM (24-hour)")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h < 24 and 0 <= m < 60):
        raise click.BadParameter("start-time out of range")
    return f"{h:02d}:{m:02d}"


def _resolve_community_id(ctx_val: str | None) -> str:
    cid = ctx_val or os.environ.get("COMMUNITY_ID")
    if not cid:
        raise click.UsageError("--community-id (or COMMUNITY_ID env var) required")
    return cid


def _resolve_app_id(ctx_val: str | None, community_id: str) -> str:
    app_id = ctx_val or os.environ.get("APP_ID")
    if app_id:
        return app_id
    apps = list(db.list_applications(community_id))
    if len(apps) == 1:
        # Print to stderr so the admin sees WHICH app got auto-selected
        # (security fix D18). When --app-id is omitted and there's
        # exactly one, the silent default used to make it easy to
        # operate on the wrong app after a second app was added.
        click.echo(
            f"auto-selected app: {apps[0].app_id} ({apps[0].name!r})",
            err=True,
        )
        return apps[0].app_id
    if not apps:
        raise click.UsageError("no applications exist; create one with 'apps init'")
    raise click.UsageError(
        f"--app-id required (found {len(apps)} apps); "
        "set APP_ID env var or pass --app-id"
    )


@click.group()
@click.version_option(__version__, prog_name="community-organizer")
@click.option("--table-name", envvar="TABLE_NAME", default="community-organizer", show_default=True)
@click.option("--community-id", envvar="COMMUNITY_ID", default=None)
@click.option("--app-id", envvar="APP_ID", default=None)
@click.pass_context
def main(ctx: click.Context, table_name: str, community_id: str | None,
         app_id: str | None) -> None:
    """Community Organizer -- volunteer scheduling for communities."""
    os.environ["TABLE_NAME"] = table_name
    ctx.obj = {"community_id": community_id, "app_id": app_id}


# ---- community ------------------------------------------------------------

@main.group()
def community() -> None:
    """Community-level commands."""


@community.command("init")
@click.option("--name", required=True)
@click.option("--timezone", "tz", default="America/New_York", show_default=True)
@click.option("--admin-emails", default="",
              help="Comma-separated admin email allowlist.")
@click.pass_context
def community_init(ctx: click.Context, name: str, tz: str,
                   admin_emails: str) -> None:
    """Create or replace the Community record."""
    cid = _resolve_community_id(ctx.obj["community_id"])
    allowlist = [e.strip() for e in admin_emails.split(",") if e.strip()]
    comm = Community(community_id=cid, name=name, default_timezone=tz,
                     admin_email_allowlist=allowlist)
    db.put_community(comm)
    click.echo(json.dumps(asdict(comm), indent=2))


@community.command("show")
@click.pass_context
def community_show(ctx: click.Context) -> None:
    """Show the Community record."""
    cid = _resolve_community_id(ctx.obj["community_id"])
    comm = db.get_community(cid)
    if not comm:
        raise click.ClickException(f"Community {cid!r} not found")
    click.echo(json.dumps(asdict(comm), indent=2))


# ---- apps ------------------------------------------------------------------

@main.group()
def apps() -> None:
    """Application commands."""


@apps.command("init")
@click.option("--name", required=True, help='Display name, e.g. "Usher Schedule".')
@click.option(
    "--app-type",
    type=click.Choice(["coverage", "recurring_commitments",
                       "standing_event", "flexible_event"]),
    required=True,
    help="Application type — pick explicitly, no implicit default. "
         "'coverage' is Ushers-style rotational; "
         "'recurring_commitments' is the same-person-same-slot model.",
)
@click.option("--terminology", default="volunteer", show_default=True)
@click.option("--timezone", "tz", default=None,
              help="Override community timezone for this app.")
@click.option(
    "--period-type",
    type=click.Choice(["monthly", "weekly"]),
    default=None,
    help="Period a Schedule covers. Defaults: monthly for coverage, "
         "weekly for recurring_commitments. Pass explicitly to override.",
)
@click.pass_context
def apps_init(ctx: click.Context, name: str, app_type: str,
              terminology: str, tz: str | None,
              period_type: str | None) -> None:
    """Create a new Application in the community."""
    cid = _resolve_community_id(ctx.obj["community_id"])
    if period_type is None:
        period_type = "weekly" if app_type == "recurring_commitments" else "monthly"
    app = Application(community_id=cid, name=name, app_type=app_type,
                      terminology=terminology, default_timezone=tz,
                      period_type=period_type)
    db.put_application(app)
    click.echo(json.dumps(asdict(app), indent=2))


@apps.command("list")
@click.pass_context
def apps_list(ctx: click.Context) -> None:
    """List Applications in the community."""
    cid = _resolve_community_id(ctx.obj["community_id"])
    rows = [{"app_id": a.app_id, "name": a.name, "app_type": a.app_type,
             "terminology": a.terminology, "active": a.active}
            for a in db.list_applications(cid)]
    click.echo(json.dumps(rows, indent=2))


@apps.command("show")
@click.pass_context
def apps_show(ctx: click.Context) -> None:
    """Show the current Application."""
    cid = _resolve_community_id(ctx.obj["community_id"])
    aid = _resolve_app_id(ctx.obj["app_id"], cid)
    app = db.get_application(cid, aid)
    if not app:
        raise click.ClickException(f"Application {aid!r} not found")
    click.echo(json.dumps(asdict(app), indent=2))


@apps.command("bootstrap-adoration")
@click.option("--name", default="Eucharistic Adoration", show_default=True,
              help="Display name for the new Application.")
@click.option("--terminology", default="adorer", show_default=True)
@click.option("--timezone", "tz", default=None,
              help="Override community timezone for this app.")
@click.option("--first-slot", default="12:45", show_default=True,
              help="Local start time of the Wed opener slot (HH:MM, "
                   "24-hour). Default 12:45 reflects a parish that "
                   "starts adoration after the noon Mass.")
@click.option("--opener-duration", default=45, show_default=True, type=int,
              help="Duration in minutes for the opener slot. The "
                   "remaining slots are all 60 minutes.")
@click.option("--hourly-start-hour", default=13, show_default=True, type=int,
              help="Hour-of-day (24h) when the first regular hourly "
                   "slot starts on Wed. Defaults to 13 = 1 PM so that "
                   "the 12:45 opener + the 1 PM hourly tile together.")
@click.option("--last-thu-hour", default=7, show_default=True, type=int,
              help="Hour-of-day (24h) of the last Thu slot's START. "
                   "Default 7 means slots run through Thu 7-8 AM (the "
                   "last one ends at 8 AM, when the 8 AM Mass starts).")
@click.pass_context
def apps_bootstrap_adoration(ctx: click.Context, name: str,
                             terminology: str, tz: str | None,
                             first_slot: str, opener_duration: int,
                             hourly_start_hour: int,
                             last_thu_hour: int) -> None:
    """Bootstrap a weekly Eucharistic adoration Application.

    Creates a recurring_commitments / weekly app, then seeds slot
    templates from the Wed opener through the Thu morning Mass
    cutoff — one per hour, 1 volunteer needed, auto_reminders off
    (members already have the weekly recurrence on their calendars
    via the per-assignment .ics).

    Defaults match a Wed 12:45 PM opener + Wed 1 PM through Thu 7 AM
    hourly slots (20 templates total). Run it again with --name
    "..." to seed a second adoration cohort under a different name.
    """
    cid = _resolve_community_id(ctx.obj["community_id"])
    _parse_hhmm(first_slot)
    if not (1 <= opener_duration <= 120):
        raise click.BadParameter("opener-duration must be 1..120")
    if not (0 <= hourly_start_hour <= 23):
        raise click.BadParameter("hourly-start-hour must be 0..23")
    if not (0 <= last_thu_hour <= 23):
        raise click.BadParameter("last-thu-hour must be 0..23")

    app = Application(community_id=cid, name=name,
                      app_type="recurring_commitments",
                      period_type="weekly",
                      terminology=terminology,
                      default_timezone=tz)
    db.put_application(app)
    click.echo(f"created app {app.app_id} ({name})")

    # Generate (day_of_week, start_time, duration) triples for every
    # template, in chronological order: opener → Wed hourly → Thu
    # hourly. Stored auto_reminders=False per recurring-commitments
    # convention (weekly templates rarely want noisy reminders).
    plan: list[tuple[int, str, int, str]] = []
    plan.append((2, first_slot, opener_duration,
                 _hour_label(2, first_slot)))
    # Wed hourly slots: hourly_start_hour through 23.
    for h in range(hourly_start_hour, 24):
        t = f"{h:02d}:00"
        plan.append((2, t, 60, _hour_label(2, t)))
    # Thu hourly slots: 00 through last_thu_hour.
    for h in range(0, last_thu_hour + 1):
        t = f"{h:02d}:00"
        plan.append((3, t, 60, _hour_label(3, t)))

    for dow, st, dur, tpl_name in plan:
        # max_volunteers=None means "no cap" — anyone can sign up for
        # an adoration slot beyond the 1 required. Reflects the
        # parish reality that more people praying is always better.
        tpl = SlotTemplate(community_id=cid, app_id=app.app_id,
                           name=tpl_name, day_of_week=dow,
                           start_time=st, duration_minutes=dur,
                           required_volunteers=1, min_volunteers=1,
                           max_volunteers=None, auto_reminders=False)
        db.put_template(tpl)
    click.echo(f"seeded {len(plan)} templates")
    click.echo(json.dumps(asdict(app), indent=2))


def _hour_label(day_of_week: int, hhmm: str) -> str:
    """Friendly template name like "Wed 12:45 PM" / "Thu 1 AM"."""
    day = _DAY_LABEL.get(day_of_week, str(day_of_week))
    h, m = (int(x) for x in hhmm.split(":"))
    suffix = "PM" if h >= 12 else "AM"
    h12 = 12 if h % 12 == 0 else h % 12
    body = f"{h12}:{m:02d}" if m else f"{h12}"
    return f"{day} {body} {suffix}"


# ---- users -----------------------------------------------------------------

@main.group()
def users() -> None:
    """User commands (community-scoped)."""


@users.command("add")
@click.option("--email", required=True)
@click.option("--name", required=True)
@click.option("--community-role", type=click.Choice(["ca", "ua", "member"]),
              default="member", show_default=True)
@click.option("--phone", default=None)
@click.option("--cognito-sub", default=None)
@click.option("--notes", default=None)
@click.pass_context
def users_add(ctx: click.Context, email: str, name: str, community_role: str,
              phone: str | None, cognito_sub: str | None,
              notes: str | None) -> None:
    """Add a new User to the community."""
    cid = _resolve_community_id(ctx.obj["community_id"])
    user = User(community_id=cid, email=email, name=name,
                community_role=community_role, phone=phone,
                cognito_sub=cognito_sub, notes=notes)
    db.put_user(user)
    out = asdict(user)
    if isinstance(out.get("quiet_hours"), tuple):
        out["quiet_hours"] = list(out["quiet_hours"])
    click.echo(json.dumps(out, indent=2))


@users.command("list")
@click.pass_context
def users_list(ctx: click.Context) -> None:
    """List Users in the community."""
    cid = _resolve_community_id(ctx.obj["community_id"])
    rows = [
        {"user_id": u.user_id, "email": u.email, "name": u.name,
         "community_role": u.community_role, "cognito_sub": u.cognito_sub,
         "phone": u.phone}
        for u in db.list_users(cid)
    ]
    click.echo(json.dumps(rows, indent=2))


@users.command("link-cognito")
@click.option("--user-id", required=True)
@click.option("--cognito-sub", required=True)
@click.pass_context
def users_link(ctx: click.Context, user_id: str, cognito_sub: str) -> None:
    """Link an existing User row to a Cognito sub."""
    cid = _resolve_community_id(ctx.obj["community_id"])
    user = db.get_user(cid, user_id)
    if not user:
        raise click.ClickException(f"User {user_id!r} not found")
    user.cognito_sub = cognito_sub
    db.put_user(user)
    click.echo(f"linked user_id={user_id} cognito_sub={cognito_sub}")


# ---- members ---------------------------------------------------------------

@main.group()
def members() -> None:
    """Membership commands (app-scoped)."""


@members.command("add")
@click.option("--user-id", required=True)
@click.option("--app-role", type=click.Choice(["aa", "member"]),
              default="member", show_default=True)
@click.pass_context
def members_add(ctx: click.Context, user_id: str, app_role: str) -> None:
    """Add a User as a member of the current Application."""
    cid = _resolve_community_id(ctx.obj["community_id"])
    aid = _resolve_app_id(ctx.obj["app_id"], cid)
    user = db.get_user(cid, user_id)
    if not user:
        raise click.ClickException(f"User {user_id!r} not found")
    mem = Membership(community_id=cid, app_id=aid, user_id=user_id,
                     app_role=app_role)
    db.put_membership(mem)
    click.echo(json.dumps(asdict(mem), indent=2))


@members.command("list")
@click.pass_context
def members_list(ctx: click.Context) -> None:
    """List members of the current Application."""
    cid = _resolve_community_id(ctx.obj["community_id"])
    aid = _resolve_app_id(ctx.obj["app_id"], cid)
    users_by_id = {u.user_id: u for u in db.list_users(cid)}
    rows = [
        {"user_id": m.user_id, "app_role": m.app_role,
         "name": users_by_id[m.user_id].name if m.user_id in users_by_id else "?",
         "email": users_by_id[m.user_id].email if m.user_id in users_by_id else "?"}
        for m in db.list_memberships_for_app(aid)
    ]
    click.echo(json.dumps(rows, indent=2))


# ---- templates -------------------------------------------------------------

@main.group()
def templates() -> None:
    """SlotTemplate commands (app-scoped)."""


@templates.command("add")
@click.option("--name", required=True)
@click.option("--day", required=True, help="mon/tue/.../sun or 0-6")
@click.option("--start", required=True, help="HH:MM (24-hour)")
@click.option("--duration", type=int, required=True, help="Duration in minutes.")
@click.option("--arrival-offset", type=int, default=10, show_default=True)
@click.option("--required", "required_volunteers", type=int, default=2, show_default=True)
@click.option("--min", "min_volunteers", type=int, default=1, show_default=True)
@click.option("--recurrence", type=click.Choice([
    "weekly", "biweekly_even", "biweekly_odd",
    "monthly_first_sat", "monthly_last_sun", "rrule"
]), default="weekly", show_default=True)
@click.option("--rrule", default=None)
@click.option("--tags", default="")
@click.pass_context
def templates_add(ctx: click.Context, name: str, day: str, start: str,
                  duration: int, arrival_offset: int, required_volunteers: int,
                  min_volunteers: int, recurrence: str, rrule: str | None,
                  tags: str) -> None:
    """Create a new SlotTemplate."""
    cid = _resolve_community_id(ctx.obj["community_id"])
    aid = _resolve_app_id(ctx.obj["app_id"], cid)
    tpl = SlotTemplate(
        community_id=cid, app_id=aid, name=name,
        day_of_week=_parse_day(day),
        start_time=_parse_hhmm(start),
        duration_minutes=duration,
        arrival_offset_minutes=arrival_offset,
        required_volunteers=required_volunteers,
        min_volunteers=min_volunteers,
        recurrence=recurrence,
        rrule=rrule,
        tags=[t.strip() for t in tags.split(",") if t.strip()],
    )
    db.put_template(tpl)
    click.echo(json.dumps(asdict(tpl), indent=2))


@templates.command("list")
@click.pass_context
def templates_list(ctx: click.Context) -> None:
    """List SlotTemplates in the app."""
    cid = _resolve_community_id(ctx.obj["community_id"])
    aid = _resolve_app_id(ctx.obj["app_id"], cid)
    rows = sorted(db.list_templates(aid),
                  key=lambda t: (t.day_of_week, t.start_time))
    out = [
        {"template_id": t.template_id, "name": t.name,
         "day": _DAY_LABEL[t.day_of_week], "start": t.start_time,
         "duration_min": t.duration_minutes,
         "arrival_offset_min": t.arrival_offset_minutes,
         "required": t.required_volunteers, "min": t.min_volunteers,
         "recurrence": t.recurrence, "tags": t.tags}
        for t in rows
    ]
    click.echo(json.dumps(out, indent=2))


@templates.command("show")
@click.argument("template_id")
@click.pass_context
def templates_show(ctx: click.Context, template_id: str) -> None:
    """Show a single SlotTemplate."""
    cid = _resolve_community_id(ctx.obj["community_id"])
    aid = _resolve_app_id(ctx.obj["app_id"], cid)
    tpl = db.get_template(aid, template_id)
    if not tpl:
        raise click.ClickException(f"template {template_id!r} not found")
    click.echo(json.dumps(asdict(tpl), indent=2))


@templates.command("delete")
@click.argument("template_id")
@click.confirmation_option(prompt="Delete this template?")
@click.pass_context
def templates_delete(ctx: click.Context, template_id: str) -> None:
    """Delete a SlotTemplate."""
    cid = _resolve_community_id(ctx.obj["community_id"])
    aid = _resolve_app_id(ctx.obj["app_id"], cid)
    db.delete_template(aid, template_id)
    click.echo(f"deleted template_id={template_id}")


# ---- schedules -------------------------------------------------------------

@main.group()
def schedules() -> None:
    """Schedule (monthly draft) commands (app-scoped)."""


@schedules.command("create")
@click.argument("yyyy_mm")
@click.option("--replace", is_flag=True)
@click.pass_context
def schedules_create(ctx: click.Context, yyyy_mm: str, replace: bool) -> None:
    """Create a draft Schedule + materialize Slots from templates."""
    cid = _resolve_community_id(ctx.obj["community_id"])
    aid = _resolve_app_id(ctx.obj["app_id"], cid)
    comm = db.get_community(cid)
    app = db.get_application(cid, aid)
    if comm is None:
        raise click.ClickException(f"Community {cid!r} not found")
    if app is None:
        raise click.ClickException(f"Application {aid!r} not found")

    tz = app.default_timezone or comm.default_timezone
    existing_slots = list(db.list_slots(aid, yyyy_mm))
    if existing_slots and not replace:
        raise click.ClickException(
            f"{len(existing_slots)} slots already exist for {yyyy_mm}; "
            "pass --replace to rebuild."
        )
    templates = list(db.list_templates(aid))
    if not templates:
        raise click.ClickException("no templates exist; add at least one first")
    slots = scheduling.materialize(cid, aid, yyyy_mm, tz, templates,
                                   period_type=app.period_type)
    if replace and existing_slots:
        deleted = db.delete_slots(aid, yyyy_mm)
        click.echo(f"deleted {deleted} existing slots")
    db.put_slots(slots)
    sch = db.get_schedule(aid, yyyy_mm) or Schedule(
        community_id=cid, app_id=aid, yyyy_mm=yyyy_mm)
    sch.state = "draft"
    db.put_schedule(sch)
    click.echo(f"materialized {len(slots)} slots for {yyyy_mm}; schedule state=draft")


@schedules.command("list")
@click.pass_context
def schedules_list(ctx: click.Context) -> None:
    """List Schedules in the app."""
    cid = _resolve_community_id(ctx.obj["community_id"])
    aid = _resolve_app_id(ctx.obj["app_id"], cid)
    rows = []
    for sch in db.list_schedules(aid):
        rows.append({
            "yyyy_mm": sch.yyyy_mm, "state": sch.state,
            "slot_count": sum(1 for _ in db.list_slots(aid, sch.yyyy_mm)),
            "created_at": sch.created_at,
        })
    rows.sort(key=lambda r: r["yyyy_mm"])
    click.echo(json.dumps(rows, indent=2))


@schedules.command("publish")
@click.argument("yyyy_mm")
@click.option("--dry-run", is_flag=True)
@click.option("--from-addr", default=None)
@click.option("--yes", is_flag=True)
@click.pass_context
def schedules_publish(ctx: click.Context, yyyy_mm: str,
                      dry_run: bool, from_addr: str | None, yes: bool) -> None:
    """Publish a schedule (state-only transition; no broadcast).

    History (#215): pre-2026-06-06 this command also broadcast the
    schedule + per-slot calendar invites in one shot. The implementation was refactored
    so 'publish' means only 'open for member edits'. To broadcast,
    follow up with the ``broadcast`` command below or use
    /admin/send-email in the web UI.
    """
    cid = _resolve_community_id(ctx.obj["community_id"])
    aid = _resolve_app_id(ctx.obj["app_id"], cid)
    comm = db.get_community(cid)
    app = db.get_application(cid, aid)
    if comm is None or app is None:
        raise click.ClickException("Community or Application not found")

    if dry_run:
        # Even though publish itself is state-only now, surface the
        # planned broadcast/invite counts so admins can sanity-check
        # before the separate broadcast step.
        plans = publishing.plan_publish(comm, app, yyyy_mm)
        invites = publishing.plan_invites(comm, app, yyyy_mm)
        rows = [{"to": p.user.email, "name": p.user.name,
                 "assignments": len(p.slots), "subject": p.subject}
                for p in plans]
        click.echo(json.dumps({"would_send_on_broadcast": len(plans),
                               "would_send_invites_on_broadcast":
                                   len(invites),
                               "to": rows}, indent=2))
        return

    if not yes:
        click.confirm(
            f"Publish {yyyy_mm}? This opens the schedule for member "
            f"self-signup. No emails go out — use 'broadcast' "
            f"separately when ready.",
            abort=True)

    summary = publishing.publish_schedule(comm, app, yyyy_mm)
    click.echo(json.dumps(summary, indent=2))


@schedules.command("unpublish")
@click.argument("yyyy_mm")
@click.confirmation_option(prompt="Flip this schedule back to draft?")
@click.pass_context
def schedules_unpublish(ctx: click.Context, yyyy_mm: str) -> None:
    """Flip a published schedule back to draft."""
    cid = _resolve_community_id(ctx.obj["community_id"])
    aid = _resolve_app_id(ctx.obj["app_id"], cid)
    app = db.get_application(cid, aid)
    if app is None:
        raise click.ClickException(f"Application {aid!r} not found")
    sch = publishing.unpublish_schedule(app, yyyy_mm)
    click.echo(f"schedule {yyyy_mm} is now {sch.state}")


@schedules.command("force-reset")
@click.argument("yyyy_mm")
@click.confirmation_option(
    prompt="Force a schedule stuck in 'publishing' state back to draft? "
           "Only do this if the publish handler crashed.")
@click.pass_context
def schedules_force_reset(ctx: click.Context, yyyy_mm: str) -> None:
    """Recovery: force a schedule stuck in 'publishing' back to draft.

    Use ONLY when a publish handler has crashed and left the schedule
    locked. Running this against a publish that is still executing
    will cause that publish's commit step to fail; some emails may
    have already gone out and the resulting state will be inconsistent.
    """
    cid = _resolve_community_id(ctx.obj["community_id"])
    aid = _resolve_app_id(ctx.obj["app_id"], cid)
    app = db.get_application(cid, aid)
    if app is None:
        raise click.ClickException(f"Application {aid!r} not found")
    sch = db.get_schedule(aid, yyyy_mm)
    if sch is None:
        raise click.ClickException(f"schedule {yyyy_mm} not found")
    if sch.state != "publishing":
        raise click.ClickException(
            f"schedule {yyyy_mm} is in state {sch.state!r}, not 'publishing'; "
            f"force-reset only applies to stuck publishes")
    try:
        db.transition_schedule_state(
            aid, yyyy_mm,
            from_state="publishing", to_state="draft",
            clear_published_at=True,
        )
    except db.ConcurrencyConflict as e:
        raise click.ClickException(
            f"schedule {yyyy_mm} is no longer in 'publishing' state — "
            f"another action moved it") from e
    click.echo(f"schedule {yyyy_mm} reset to draft (was: publishing)")


@schedules.command("show")
@click.argument("yyyy_mm")
@click.pass_context
def schedules_show(ctx: click.Context, yyyy_mm: str) -> None:
    """Print all Slots for a month."""
    cid = _resolve_community_id(ctx.obj["community_id"])
    aid = _resolve_app_id(ctx.obj["app_id"], cid)
    slots = sorted(db.list_slots(aid, yyyy_mm),
                   key=lambda s: (s.local_date, s.start_time))
    out = [
        {"local_date": s.local_date, "day": _DAY_LABEL[s.day_of_week],
         "start": s.start_time, "name": s.name,
         "concrete_utc": s.concrete_date, "required": s.required_volunteers,
         "slot_id": s.slot_id}
        for s in slots
    ]
    click.echo(json.dumps(out, indent=2))


@schedules.command("email-summary")
@click.argument("yyyy_mm")
@click.option("--to", "to_addr", required=True, help="Email address to send the summary to.")
@click.option("--from-addr", default=None)
@click.pass_context
def schedules_email_summary(ctx: click.Context, yyyy_mm: str, to_addr: str,
                            from_addr: str | None) -> None:
    """Generate and email a full schedule table for a month."""
    cid = _resolve_community_id(ctx.obj["community_id"])
    aid = _resolve_app_id(ctx.obj["app_id"], cid)
    comm = db.get_community(cid)
    app = db.get_application(cid, aid)
    if not comm or not app:
        raise click.ClickException("Community or Application not found")
    subject, body_text, body_html = schedule_email.generate_schedule_email(
        comm, app, yyyy_mm)
    from_addr = from_addr or f"organizer@{os.environ.get('DOMAIN_NAME', 'community.example.org')}"
    provider = get_email_provider()
    result = provider.send(
        community_id=cid,
        from_addr=from_addr,
        to_addr=to_addr,
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        kind="other",
        related_yyyy_mm=yyyy_mm,
    )
    click.echo(f"Sent to {to_addr}: outcome={result.outcome}")


# ---- slots -----------------------------------------------------------------

@main.group()
def slots() -> None:
    """Slot + Assignment commands (app-scoped)."""


@slots.command("list")
@click.argument("yyyy_mm")
@click.pass_context
def slots_list(ctx: click.Context, yyyy_mm: str) -> None:
    """List slots in a month with their assignments."""
    cid = _resolve_community_id(ctx.obj["community_id"])
    aid = _resolve_app_id(ctx.obj["app_id"], cid)
    users_by_id = {u.user_id: u for u in db.list_users(cid)}
    slot_rows = sorted(db.list_slots(aid, yyyy_mm),
                       key=lambda s: (s.local_date, s.start_time))
    asgns_by_slot: dict[str, list[Assignment]] = {}
    for a in db.list_assignments_for_month(aid, yyyy_mm):
        asgns_by_slot.setdefault(a.slot_id, []).append(a)
    out = []
    for s in slot_rows:
        assigned = asgns_by_slot.get(s.slot_id, [])
        out.append({
            "slot_id": s.slot_id, "local_date": s.local_date,
            "day": _DAY_LABEL[s.day_of_week], "start": s.start_time,
            "name": s.name, "required": s.required_volunteers,
            "assigned_count": len(assigned),
            "assigned": [
                {"user_id": a.user_id,
                 "name": users_by_id[a.user_id].name if a.user_id in users_by_id else "?"}
                for a in assigned
            ],
        })
    click.echo(json.dumps(out, indent=2))


@slots.command("show")
@click.option("--month", "yyyy_mm", required=True)
@click.option("--slot-id", required=True)
@click.pass_context
def slots_show(ctx: click.Context, yyyy_mm: str, slot_id: str) -> None:
    """Show one slot + its assignments."""
    cid = _resolve_community_id(ctx.obj["community_id"])
    aid = _resolve_app_id(ctx.obj["app_id"], cid)
    slot = db.find_slot_in_month(aid, yyyy_mm, slot_id)
    if not slot:
        raise click.ClickException(f"slot {slot_id!r} not found in {yyyy_mm}")
    users_by_id = {u.user_id: u for u in db.list_users(cid)}
    asgns = list(db.list_assignments_for_slot(aid, yyyy_mm, slot_id))
    out = asdict(slot)
    out["assignments"] = [
        {"user_id": a.user_id,
         "name": users_by_id[a.user_id].name if a.user_id in users_by_id else "?",
         "email": users_by_id[a.user_id].email if a.user_id in users_by_id else None,
         "assigned_at": a.created_at}
        for a in asgns
    ]
    click.echo(json.dumps(out, indent=2))


@slots.command("assign")
@click.option("--month", "yyyy_mm", required=True)
@click.option("--slot-id", required=True)
@click.option("--user-id", required=True)
@click.pass_context
def slots_assign(ctx: click.Context, yyyy_mm: str, slot_id: str, user_id: str) -> None:
    """Assign a user to a slot."""
    cid = _resolve_community_id(ctx.obj["community_id"])
    aid = _resolve_app_id(ctx.obj["app_id"], cid)
    slot = db.find_slot_in_month(aid, yyyy_mm, slot_id)
    if not slot:
        raise click.ClickException(f"slot {slot_id!r} not found in {yyyy_mm}")
    user = db.get_user(cid, user_id)
    if not user:
        raise click.ClickException(f"user {user_id!r} not found")
    asg = Assignment(community_id=cid, app_id=aid, yyyy_mm=yyyy_mm,
                     slot_id=slot_id, user_id=user_id,
                     local_date=slot.local_date)
    db.put_assignment(asg)
    click.echo(json.dumps(asdict(asg), indent=2))


@slots.command("unassign")
@click.option("--month", "yyyy_mm", required=True)
@click.option("--slot-id", required=True)
@click.option("--user-id", required=True)
@click.pass_context
def slots_unassign(ctx: click.Context, yyyy_mm: str, slot_id: str, user_id: str) -> None:
    """Remove an assignment."""
    cid = _resolve_community_id(ctx.obj["community_id"])
    aid = _resolve_app_id(ctx.obj["app_id"], cid)
    db.delete_assignment(aid, yyyy_mm, slot_id, user_id)
    click.echo(f"unassigned slot_id={slot_id} user_id={user_id}")


if __name__ == "__main__":
    main()
