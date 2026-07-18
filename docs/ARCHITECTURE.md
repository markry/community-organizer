# Architecture

Community Organizer is a single-tenant serverless application. A deployment
represents one community and can contain multiple applications inside that
community.

## Core Concepts

- **Community**: the top-level tenant for one deployment.
- **Application**: a scheduling surface within a community, such as volunteer
  coverage for recurring events.
- **Membership**: the relationship between a user and an application, including
  application-level role.
- **Schedule**: a period of slots that can be drafted, published, unpublished,
  and republished.
- **Slot**: a scheduled event requiring one or more assigned members.
- **Assignment**: a member's relationship to a slot.

## Runtime Components

- **Web Lambda**: serves the server-rendered admin/member web UI and handles
  most interactive workflows.
- **Notifier Lambda**: reacts to DynamoDB stream changes and scheduled reminder
  events.
- **Inbound Lambda**: processes inbound email replies and calendar responses.
- **Bounce Lambda**: handles SES bounce and complaint notifications.
- **Sandbox Lambda**: optional infrastructure probe used by the template.

## Storage

The app uses a DynamoDB single-table model with community-scoped users,
application-scoped schedules and assignments, and community-scoped email logs.
Application membership rows are the primary boundary for app-level visibility.

## Authentication

Cognito provides authentication. The app supports email/password and federated
identity providers when configured in the deployment.

## Authorization Model

- Community admins can manage community-level setup.
- User admins can help manage community users where enabled.
- Application admins manage only the applications where they hold an admin
  membership.
- Members see their own assignments and the member-visible surfaces for their
  applications.

Authorization checks should be enforced server-side even when the UI hides an
action.
