# Deployment

Community Organizer is deployed with AWS SAM.

## Prerequisites

- AWS account with permissions to deploy the resources in `template.yaml`
- AWS SAM CLI
- Python 3.13-compatible build environment
- A domain hosted in Route 53 if using the CloudFront/Route 53 resources
- ACM certificate in `us-east-1` for CloudFront aliases
- SES identities and receipt-rule setup if using email features

## Configure Parameters

The templates use placeholder values. Supply deployment-specific values during
`sam deploy --guided` or through a parameter file:

- `DomainName`
- `HostedZoneId`
- `CommunityId`
- `CookieDomain`
- `AcmCertArn`
- `TableName`
- `SsmParamPrefix`
- `ResourcePrefix`
- provider selections such as `EmailProvider`, `SmsProvider`, and
  `InboundProvider`

Do not commit real account-specific values, credentials, live domains, or
personal data to the repository.

Existing deployments should override `TableName`, `SsmParamPrefix`, and
`ResourcePrefix` with their current physical names. Do not rename a live
DynamoDB table through CloudFormation; the template retains the table on
deletion or replacement, but the correct operational path is to keep the
existing table name stable. Likewise, `ResourcePrefix` sets the prefix for the
Lambda function names and the SNS topic — because `FunctionName` and
`TopicName` are replacement properties, an existing stack must keep this stable
(e.g. your existing prefix) or CloudFormation will replace those resources.

## Deploy

```bash
sam build
sam deploy --guided
```

After deployment, follow the stack outputs and AWS console guidance for any
out-of-band service setup, such as Cognito custom domains or SES receipt-rule
activation.

## SES Bounce And Complaint Notifications

If SES is enabled, configure SES feedback notifications to the SNS topic
created by the stack. Example shape:

```bash
aws ses set-identity-notification-topic \
  --identity example.org \
  --notification-type Bounce \
  --sns-topic <topic-arn>

aws ses set-identity-notification-topic \
  --identity example.org \
  --notification-type Complaint \
  --sns-topic <topic-arn>
```

Use your own identity and topic ARN.

## Inbound Email

SES receipt rule sets must be activated explicitly. If using inbound email,
activate the receipt rule set created by your stack:

```bash
aws ses set-active-receipt-rule-set --rule-set-name <rule-set-name>
```

Only one SES receipt rule set can be active per AWS account and region.
