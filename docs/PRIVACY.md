# Privacy

Community Organizer stores personal data needed to coordinate schedules, such
as names, email addresses, optional phone numbers, application memberships,
assignments, and delivery state.

## Data Boundaries

The app is designed around application-level visibility:

- Application admins should see only the members and schedules for their own
  applications.
- Members should see their own assignments and member-visible application
  content.
- Community-level administrators may have broader user-management visibility.

These boundaries must be enforced server-side. UI filtering alone is not a
privacy control.

## Email And SMS

Email and SMS providers may process message content and recipient information.
Deployers are responsible for configuring providers, retention, and legal
notices appropriate for their communities.

## Public Repository Guidance

Do not commit production exports, live member rosters, email logs, SMS logs,
provider credentials, private deployment identifiers, or screenshots containing
personal data.
