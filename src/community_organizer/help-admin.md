# {{app_name}} — App Admin Guide

As an App Admin, you can manage the schedule, members, and cohorts for {{app_name}}. You also have all the capabilities of a regular member (viewing schedules, signing up for slots, managing your own non-admin notifications).

## Your Admin Home Page

After signing in, you'll see these sections in order:

1. **Your upcoming {{events}}** — your personal assignments with **Trade** and **Withdraw** buttons. This is essentially the same view that your members see. Below the assignment list you'll find quick links to the full schedule page and to your personal **notification settings** (reminder preferences for your member-role reminders, not your administrator role).
2. **Schedules** — create, edit, activate, and delete monthly schedules. Includes a link to view all active schedules on a single page.
3. **Member management** — recent members + links to manage members and cohorts.
4. **Email** — log of the last 5 system emails, with quick links to compose and send a new email and to view all activity.
5. **Schedule template** — quick list of your recurring {{events}}, with an edit link to the full template page.
6. **Settings and reminders** — a compact read-only summary of the application's current settings (app name, event / volunteer terminology, default reminders, trade default, group cohort emails), with an edit link to the full settings page. Community Admins also see the community name and timezone in this summary.

A navigation bar at the bottom of every admin page provides quick access to: Home, Schedules, My Schedule, Members, Cohorts, Send Email, Emails, Templates, and Settings. The current page is shown in bold.

### Application name vs Community name

Most user-facing labels — page headers, email subjects, signatures, From display name — use the **application** name (*e.g.*, "{{app_name}}"). The **community** name (*e.g.*, "{{community_name}}") is shared infrastructure that surfaces only on the login pages and the Community Admin settings section. Both names can be edited on the Settings page; the community name and application name are Community-Admin-only.

---

## Managing the Schedule Template

Creating a template is typically the starting point for your use of this system. However, you probably will only use it once or occasionally. So it is located near the bottom of the admin home page rather than the top. Managing Schedules and Members will be your primary on-going tasks.

Click **edit** next to **Schedule template** on the home page (or open **Templates** from the navigation bar) to manage the template that forms the basis for the recurring {{events}} each month's schedule.

Think of the template as the pattern that you want for your monthly schedule. Creating a new schedule instantiates the pattern into a concrete monthly schedule. After that the two are not linked. So changes to the template will only impact new schedules that you publish. If you want to change an existing schedule, you can do so manually in the schedule page, or delete it and start again from the current template.

### Adding {{Events}} to the Template

Scroll to **Add new {{event}}** and fill in:

| Field | Description | Default |
|---|---|---|
| Name | Display name. Leave blank to auto-generate based on pre-defined application parameters (*e.g.*, "{{Volunteer}} for Sun 8:00 AM"). | Auto-generated |
| Day | Day of the week. | Sunday |
| Start | {{Event}} start time (24-hour). | 08:00 |
| Duration | Length in minutes. | 60 |
| Arrive early | Minutes before start that {{volunteers}} should arrive. | 10 |
| Required | Desired number of {{volunteers}}. Below this triggers an amber warning. | 2 |
| Minimum | Absolute minimum. Below this triggers a red/urgent warning. | 1 |
| Maximum | Hard cap on signups. Prevents over-staffing. | 5 |

**Color coding that will show later on the schedule page:**
- **Green** — at or above Required.
- **Amber** — below Required but at or above Minimum.
- **Red** — below Minimum (urgent).

### Editing and Deleting {{Events}}

- Click **edit** next to any {{event}} to modify it inline. Clear the name field to regenerate the automatic name.
- Click **delete** to remove it (with confirmation).

**Important:** Changes to the template only affect schedules created afterwards. There is no link between changes to a template and any existing schedule. Existing schedules can only be modified from their edit page (cancel {{events}}, add one-offs, change assignments).

---

## Settings

The **Settings** page (accessed from the nav bar or from the **edit** link in the **Settings and reminders** section on the home page) gathers everything that controls the behavior of your community and application — things that change rarely after initial setup. The home page shows a compact read-only summary of the most important settings so you can see at a glance how the system is configured.

### Application Settings

Controls how the application refers to its events and volunteers across the UI and emails:
- **Event name (singular)** — *e.g.*, "event", "Mass", "shift", "practice".
- **Event name (plural)** — leave blank to auto-derive (handles common English suffixes like Mass → Masses, party → parties), or set explicitly for irregular cases or other languages.
- **Volunteer terminology (singular)** — *e.g.*, "volunteer", "usher", "scorekeeper".
- **Volunteer terminology (plural)** — leave blank to auto-derive, or set explicitly for irregular plurals.
- **Arrival label** — the phrase used in calendar invites and emails, *e.g.*, "please arrive by".

### Default Reminder Settings

Set the default reminder times for newly added members. Existing members keep their own settings. Members can also change their own reminders at any time via their **Your notification settings** page.

### Trade Default Behavior

Configure the default trade mode for your application:
- **Release slot immediately** (recommended) — when a member requests a trade, their slot opens up right away.
- **Keep slot while looking** — the member holds their slot until someone accepts.

Members can override this default when creating each trade request.

### Automated Cohort Emails

When the system automatically sends the same email to multiple cohort members (*e.g.*, a slot opening), you can choose whether everyone is on the same email (visible to each other, supporting reply-all) or whether each person gets their own separate email. App Admins are included on shared emails for reply-all visibility.

This setting only affects automated cohort notifications. The admin **Send Email** page always uses multi-recipient mode for cohort and individual sends, regardless of this setting.

### Community Admin settings (Community Admin only)

If you are a Community Admin, the top of the Settings page shows a yellow **Community Admin settings** section where you can adjust:
- **Community name** — surfaces only on the shared login pages and in this Community Admin section. Members see the application name everywhere else.
- **Default timezone** — IANA timezone string (*e.g.*, America/New_York) used by all applications unless overridden.
- **Application name** — how the application is identified in headers, email subjects and signatures, and the "From" display name (*e.g.*, "Jane Smith of {{app_name}}").

App Admins do not see this section. (In a future release, Community Admins will also create and manage multiple applications from this area.)

---

## Creating and Managing Schedules

### Creating a New Month

On the admin home page under **Schedules**:
- Click the **Create [month] schedule** link to create the next uncreated month.
- Or expand **Create a different month...** for a specific month.

The system materializes one {{event}} per template entry per matching day in the month. For example, 4 templates with weekend {{events}} x 4-5 weekends = 16-20 {{events}}. You'll be taken to the schedule edit page immediately.

### Viewing All Active Schedules

Click **View all active schedules** on the admin home page or **Schedules** in the navigation bar to see all active months on a single page with full assignment management controls. Each month's header links to its individual edit page. Archived (history) months appear under **Past schedules** at the bottom.

### Understanding the Schedule Table

The admin home shows each month with:

| Column | Meaning |
|---|---|
| Month | Click to open the schedule edit page. |
| State | Draft (grey), Active (green), or History (an active schedule you've archived). |
| Covered/Total | {{Events}} with at least one member / total {{events}}. |
| Filled/Total | Individual slots filled / total slots needed. |
| Action | Make active, Return to draft, Archive, or Delete. |

### Copying from a Previous Month

When viewing a new schedule with no assignments, click **Copy assignments from [previous month]** at the top of the page to pre-populate.

When using a prior month, the system matches by ordinal position: 1st Saturday of the source to 1st Saturday of the target, 2nd Sunday to 2nd Sunday, *etc.* If the months have different numbers of any particular day of the week, extra dates are left empty, or extra source dates are ignored.

You can use the copy feature even if you've updated items in the schedule, but you are warned that all existing data will be lost, with an Ok/Cancel option.

### Editing a Schedule

On the schedule edit page you can:

- **Assign members** — use the dropdown in the **Add** column to select a person. The dropdown auto-submits (no Go button needed).
- **Bulk-assign** — next to the dropdown, click **all** to assign all members, or click a cohort name to assign that cohort's members. Assignments stop at the slot's maximum capacity.
- **Remove assignments** — click the **x** next to a name.
- **Cancel an {{event}}** — click **cancel** to grey out an {{event}} (with confirmation dialog). Cancelled {{events}} won't generate reminders or show on the member schedule. Click **restore** to bring it back.
- **Add a one-off {{event}}** — expand **Add one-off {{event}} to this schedule...** for {{events}} not in the template (*e.g.*, Christmas Eve service; Extra practice before tournament). All the same fields as the template are available.

### Making a schedule active

Click the green **Make active** button at the bottom of the edit page. This makes the schedule visible to members — they can view it and sign up for open slots — and creates reminder notifications based on each member's settings.

**Making a schedule active does not send any email.** Sending the schedule to members is a separate step on the **Send Email** page (tick *Include a copy of the schedule*). So the natural flow is: make the schedule active, let sign-ups settle, then send it out when it's ready.

### Returning to draft

Click **Return to draft** (with confirmation) to take an active schedule back to draft. This will:
- Hide the schedule from members' view immediately.
- Cancel any reminder emails not yet sent for that month. Reminders that already went out are not recalled.
- Preserve all assignments, signups, and trades — they're untouched and reappear when you make it active again.
- Send a notification email to your fellow App Admins.

Members are not directly notified, and the calendar invites already on their personal calendars are not cancelled. If you want members to know, send them a quick note from the **Send Email** page.

### Archiving (moving to history)

When a month is finished — usually once you've moved on to the next one — click **Archive (move to history)** on an active schedule. Unlike *Return to draft*, this is **non-destructive**: the schedule stays exactly as it is (reminders still fire, calendar invites keep working). It simply drops out of the default screens and the Send-Email audience so old months stop cluttering the current view. Archived months appear under **Past schedules** on the schedules page, where you can **Reactivate** one at any time.

Archiving is the recommended way to "age out" a month you no longer need front-and-center — it never sends June's schedule by accident, but keeps it as a valid record.

### Re-activating

Making a schedule active again (after returning it to draft) rebuilds reminder notifications for each upcoming assignment; reminders whose send time has already passed are skipped. As with the first activation, this does **not** send email — use the **Send Email** page to notify members.

### Deleting

On the admin home page, click **Delete** next to a draft schedule (with confirmation). This removes the schedule, all its {{events}}, and all assignments. Active schedules must be returned to draft first.

---

## Managing Members

Click **manage members** on the admin home page.

### Adding a Member

Scroll to **Add new member** and fill in:
- **Name** (required) — their display name.
- **Email** (required) — must be unique in the community.
- **Phone** (optional) — for future SMS features.
- **Notes** (optional) — admin-only reference (*e.g.*, "prefers morning services", "new member as of June").

New members are automatically added as members of {{app_name}} with the community's default reminder settings.

### How Members Sign In

When you add a member, the system automatically provisions their login identity. No "send invite" step is required. Tell the new member to:

1. Visit the site.
2. Click **New user or forgot your password?** on the sign-in page.
3. Enter their email address. The system sends a verification code.
4. Enter the code and choose their own password.

This same flow also works any time a member forgets their password.

Members with a Google account matching their registered email can alternatively click **Sign in with Google** and skip the password setup entirely.

Please warn your members that although the sending email address has all the proper authentication settings, automated emails from an unknown sender may still end up in their "Junk" or "Other" email folders. Please ask them to check for an email from your system and then choose the "this is not junk" option to ensure future delivery.

### Editing a Member

Click **edit** in the Actions column to modify:
- Name, email, phone, notes.
- Reminder settings (the admin can adjust on behalf of the member, but the member can always edit their own settings).

If two admins try to edit the same member at the same time, only the first save wins. The second admin will see a red banner: *"Your edit was not saved. Another admin changed [Name] while you were editing. The current values are shown below — please review and try your edit again."* The page reloads showing the current values so you can decide whether to redo your changes. The same conflict protection applies to the Application identity / settings form on the templates page, and to publishing a schedule (a duplicate publish click never sends duplicate broadcast emails).

### Roles

- **Member** — can view published schedules, sign up for open slots, and release their own slots.
- **App Admin** — all member capabilities plus managing schedules, members, and cohorts.

Click **make admin** or **demote** in the Role/Cohorts column to change a member's role.

### Resetting Member Access

If a member's account is compromised, behaving suspiciously, or they simply need to start over with a fresh password:

- Click **reset access** in the Actions column.
- The system will:
  - Rotate the member's password to a fresh random value (their old password no longer works).
  - Sign the member out of all devices (within roughly an hour, as their current session token expires).
- The member can recover access by visiting the site and clicking **New user or forgot your password?** to set a new password.

You will not see this action for your own account, or for members who do not yet have a login identity.

### Removing vs Deleting

- **remove from app** — removes the member from {{app_name}} only. They remain in the community and can be added to other applications later. Their login still works for other apps.
- **delete from community** (Community Admin only) — permanently removes the member from everything, including their login identity. Cannot be undone. *(This action will move to a future community-wide member management screen, since it is not specific to one application.)*

### Handling Bounced Emails

If a member's email bounces (permanent delivery failure), their name shows **(bouncing / clear)** next to it. While flagged:
- The system stops sending them emails (reminders, notifications, *etc.*).
- Other functionality is unaffected.

To resolve: contact the member for their correct email, update it via **edit**, and click **clear** to remove the bounce flag.

---

## Managing Cohorts

Click **manage cohorts** on the admin home page or **Cohorts** in the navigation bar.

### What Cohorts Do

Cohorts are groups of {{volunteers}} who typically cover {{events}} that occur at the same time on a recurring basis. Membership in a cohort powers the automatic replacement notification system:

When someone releases a slot on a published schedule **and coverage drops below desired:**
1. Cohort members **not assigned** to that {{event}} receive: "A slot opened up -- sign up at [link]."
2. Cohort members **already assigned** to that {{event}} receive: "Coverage update -- [name] released a slot you're also assigned to."
3. App Admins receive: "[name] released their slot -- coverage is now X/Y."

When coverage remains at the desired level after a release, **no cohort or admin notifications are sent** -- only the releaser receives their confirmation notice.

### How Cohorts Are Created

A cohort is automatically created for each {{event}} that you save in your template:
- "Sat 5:30 PM" -- for {{volunteers}} who usually cover Saturday vigil.
- "Sun 8:00 AM" -- for Sunday morning regulars.
- *etc.*

Adding an {{event}} to a template is the only way to create a cohort.

Once a cohort is created its lifetime is independent of {{events}} in the template. For example, deleting an {{event}} from the template does not delete the associated cohort, because it may already be in use in a schedule. Cohorts continue to exist and be useful so long as there is some schedule in the present or future that refers to them. Once they are no longer in use, the system will automatically delete them. You cannot manually delete a cohort (although your Community Admin can on your request).

Cohorts are sorted by day and time on the management page.

### Managing Cohort Members

Each cohort shows its current members as names in rounded boxes. To manage:
- **Add a member** -- use the **+add** dropdown to select from members not yet in this cohort. Auto-submits on selection.
- **Remove a member** -- click **x** next to their name.

Members can belong to multiple cohorts (*e.g.*, someone willing to cover both 10 AM and 12 PM {{events}} on a given weekend day).

### Best Practices

- **Add each member to the cohort matching the time for which they usually volunteer.** This ensures they get notified when coverage is needed at "their" time.
- **Add flexible {{volunteers}} to multiple cohorts.** If someone is happy to cover any (say) Sunday {{event}}, add them to all Sunday cohorts.
- **You can see each member's cohort memberships** on the member management page under the Role/Cohorts column. Go to the cohorts page to edit those.

---

## Sending Email to Members

Click **Send Email** in the navigation bar, or **compose and send email** on the admin home page.

Choose your recipients:
- **All members** — sends to everyone in {{app_name}}. Each member gets a personalized email with their own greeting ("Hi [Name]"). This mode never exposes member email addresses to each other.
- **Select recipients** — pick any combination of cohorts, individual members, and free-text email addresses. Everyone on the resulting list receives a single shared email (visible to each other, supporting reply-all). App Admins are automatically included on the recipient list so they're part of any follow-up discussion. The greeting becomes "Hi all".

When **Select recipients** mode is active, you can also use:
- **+ never logged in (N)** — a quick-pick button next to the picker. It adds every member who has not yet signed in to the recipient list, with a live count of how many will be added. Useful for sending a welcome / get-started email shortly after creating member accounts. You can still remove anyone you don't want, or add more.
- **+ haven't responded (N)** — for date-poll (book-club) apps with an open poll, this button also appears. It adds members who haven't answered the current poll. Because members answer via a login-free link (which doesn't count as signing in), use this — not "never logged in" — to reach non-responders. Both buttons are available. Household-aware: if one member of a household answers with an attendance headcount that exactly matches their household's size, the rest of that household are treated as already covered and left out. If the headcount doesn't match the household size (e.g. answered for 2 in a household of 3), we can't tell who was covered, so the household's non-responders are still included.
- **Additional email addresses** — a textarea for arbitrary semicolon-separated addresses (*e.g.*, a community administrator or a non-member observer).

Enter a subject line and message body, then click **Send email** (with confirmation). The From address shows your name (*e.g.*, "John Doe of Rapids Swim Club").

Members can reply to the email and their reply will be forwarded to all App Admins.

---

## Sending a Schedule Summary Email

On any schedule's edit page, at the bottom you'll find **Email schedule table to:** with an email field and **Send** button.

This generates a formatted HTML table of the full month's schedule -- matching a simple scheduling email format (DATE | TIME | {{volunteers}} columns, one name per row) -- and sends it to the specified address.

Note: this is different from the **Publish** action. When you publish a schedule, each member receives a personalized email listing only their own assignments. The schedule summary email sends the complete table -- all {{events}}, all members -- to a single email address you specify.

Members can also send the schedule to themselves by clicking **Send schedule to me by email** on their schedule page or home page.

Useful for:
- Running parallel with an existing email-based scheduling process during transition.
- Sending the schedule to someone who doesn't use the system (*e.g.*, the head of the community).
- Forwarding to a broader distribution list.
- Creating a printable version (forward to yourself and print).

---

## Email Activity Log

Click **Emails** in the navigation bar, or **view all activity** in the **Email** section on the admin home page.

This shows a complete log of all emails sent by the system. You can filter by:
- **Direction** -- outbound (sent by system) or inbound (bounces).
- **Kind** -- publish_broadcast, reminder, change_notification, bounce, *etc.*
- **Outcome** -- accepted (delivered to mail server), bounced, rejected, error.

**Common uses:**
- Verify a publish broadcast went out successfully.
- Check if a specific member received their reminder.
- Investigate delivery failures (look for "error" or "bounced" outcomes).
- Confirm cohort notifications fired after a release.

---

## Automated Emails Sent to App Admins

As an App Admin, you automatically receive email notifications in these cases:

| # | Email | When |
|---|---|---|
| 1 | **Member signed up** | A member signs up for a slot on a published schedule. |
| 2 | **Coverage drop** | A member withdraws or is removed and coverage falls below the desired level. Shows current coverage status. |
| 3 | **Calendar decline withdrawal** | A member declines a calendar invitation, triggering automatic withdrawal. Same coverage notification as #2 if coverage drops. |
| 4 | **Trade requested** | A member requests a trade — shows which slot they want to give up and which they'd prefer. |
| 5 | **Trade completed** | A trade is completed — shows who traded what with whom. |
| 6 | **Member reply forwarded** | A member replies to any scheduling email. The original sender, subject, and message are included so you can respond directly. |
| 7 | **Schedule unpublished** | Another App Admin unpublishes a published schedule. The email names the admin who took the action and the affected month. |

You are excluded from notifications for actions you initiated yourself (*e.g.*, if you remove someone, you won't get the App Admin coverage drop email for that action).

---

## Notification Emails Reference (All Users)

The system sends these emails automatically:

| # | Email | When | To | App Admins notified? |
|---|---|---|---|---|
| 1 | Schedule published (with assignments) | Admin publishes schedule. | Each member with assignments. Includes calendar invitations. | No |
| 2 | Schedule published (no assignments) | Admin publishes schedule. | Members without assignments. | No |
| 3 | You've signed up / You're assigned | Someone assigned to published slot. | The assigned member. Includes calendar invitation. | Yes (always) |
| 4 | Withdrawal | Member withdraws. | The member. Includes calendar cancellation. | Only if below desired coverage |
| 5 | Admin removal | Admin removes someone. | The removed person. Includes calendar cancellation. | Only if below desired coverage |
| 6 | App Admin coverage drop | Release or removal drops below desired. | Each App Admin. | -- |
| 7 | Cohort opening | Same trigger as #6. | Cohort members not in {{event}}. | -- |
| 8 | Cohort coverage update | Same trigger as #6. | Cohort members in {{event}}. | -- |
| 9 | Reminder | Per member's notification settings. | The assigned person. | No |
| 10 | Schedule summary | Admin clicks Send. | Specified email address. | No |
| 11 | Trade completed | Trade accepted. | Both parties. Includes calendar invitation for new slot. | Yes |
| 12 | Withdrawn via calendar | Member declines calendar invitation. | The member (confirmation). | Only if below desired coverage |
| 13 | Tentative response nudge | Member responds "tentative" to calendar invitation. | The member (with new invitation). | No |
| 14 | Reply forwarded | Member replies to any scheduling email. | All App Admins. | -- |

Admin-initiated emails (assignments, removals, publish, send email) show the admin's name in the From field. Members can reply to any email — replies are forwarded to all App Admins.
