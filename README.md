# Community Organizer

Community Organizer is a serverless volunteer scheduling and community event
coordination app for churches, clubs, nonprofits, and other groups that need
recurring coverage.

The project is in beta. The current codebase includes the coverage-scheduling
application type: admins create schedules, publish assignments, send calendar
invitations, receive member responses, and notify members about changes.

## Features

- Community and application model: one community can host multiple scheduling
  applications with separate admins, members, terminology, and schedules.
- Coverage scheduling: admins define reusable slot templates, create schedule
  periods, assign members, and publish schedules.
- Member self-service: members can view assignments, accept or decline slots,
  release assignments, and update reminder preferences.
- Calendar integration: published assignments include iCalendar attachments.
- Email workflow: outbound assignment notices, admin broadcasts, inbound
  calendar replies, and bounce/complaint handling.
- Optional SMS reminders through a pluggable provider.
- AWS SAM deployment using Lambda, DynamoDB, Cognito, SES, EventBridge, S3,
  SNS, CloudFront, and Route 53.
- Python CLI for administrative setup and scripted operations.

## Stack

- Python 3.13 on AWS Lambda
- DynamoDB single-table storage
- Cognito for authentication
- SES for outbound and inbound email
- EventBridge for scheduled reminders
- CloudFront and Lambda Function URLs for the web surface
- SAM/CloudFormation for infrastructure

## Quick Start

```bash
git clone https://github.com/markry/community-organizer.git
cd community-organizer
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
pytest
```

To deploy, review [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md), then run:

```bash
sam build
sam deploy --guided
```

The SAM templates intentionally use placeholder domains and certificate
parameters. Supply your own domain, hosted zone, certificate ARN, and provider
settings during deployment.

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Deployment](docs/DEPLOYMENT.md)
- [Privacy](docs/PRIVACY.md)
- [Security](docs/SECURITY.md)
- [Dependencies](docs/DEPENDENCIES.md)
- [Changelog](docs/CHANGELOG.md)

## Development

Run the local test suite:

```bash
pytest
```

The repository also includes an optional `community-organizer` CLI entry point:

```bash
community-organizer --help
```

## Contact

Questions, bug reports, and security disclosures: **co@amdg.io** (reaches the maintainers). For security issues please email rather than opening a public issue — see [Security](docs/SECURITY.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

Apache-2.0. See [LICENSE](LICENSE).
