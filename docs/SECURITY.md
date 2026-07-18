# Security

## Reporting Vulnerabilities

Please do not report security vulnerabilities in public issues. Instead,
email the maintainers privately at **co@amdg.io** and we will respond as
quickly as we can.

## Security Model

Community Organizer relies on:

- Cognito for user authentication.
- Server-side authorization checks for community and application roles.
- DynamoDB condition expressions for critical state transitions.
- SES/SNS validation for inbound email and delivery feedback.
- Escaping and sanitization for generated HTML, email, and iCalendar content.

## Deployment Secrets

Do not commit secrets or live deployment-specific values. Keep provider tokens,
API keys, certificate private keys, and production configuration in AWS-managed
secret stores or deployment-specific configuration outside the repository.

## Recommended Deployment Controls

- Use least-privilege IAM roles for Lambda functions.
- Use separate AWS accounts or stacks for testing and production when possible.
- Enable CloudWatch logging and alerting for failed delivery, auth errors, and
  unexpected Lambda failures.
- Review SES, Cognito, and SMS-provider settings before inviting real users.
- Keep dependencies updated and run the test suite before deploying.
