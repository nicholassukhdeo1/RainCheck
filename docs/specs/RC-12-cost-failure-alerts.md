# RC-12: Cost and failure alerts

**Status:** Approved (2026-10-27)
**Backlog item:** RC-12 in `docs/BACKLOG.md`

## Summary

Add a monthly AWS cost budget and an email notification when the RainCheck Lambda function reports an error.
Define both in the existing `us-east-2` SAM template and document confirmation and verification before relying on either alert.

## Motivation

`template.yaml` defines the RainCheck function and its 30-day log group, but no budget, CloudWatch alarm, or alert destination.
The function raises on a failed run, so Lambda's `Errors` metric can report that failure.
The daily `ScheduleV2` event in `template.yaml` remains disabled, and an error alarm alone cannot detect a run that never starts.
The existing [RC-11 spec](RC-11-aws-daily-run.md) explicitly deferred cost budgets and failure alarms to RC-12.

## Scope

**In scope:**

- A monthly USD `COST` budget created by CloudFormation, with the amount and notification recipient supplied when a deployment change set is prepared.
- A CloudWatch alarm on this function's `AWS/Lambda` `Errors` metric, with an SNS email destination and a policy allowing only this alarm to publish.
- A runbook for reviewing the change set, confirming both email paths, checking alarm behavior, and inspecting cost and notification status.
- Offline semantic tests of the template and runbook.

**Out of scope:**

- Creating AWS resources, sending live mail, publishing a test message, uploading a database, or enabling the production schedule during spec, test, or implementation work.
- A hard spending cap, automatic budget actions, shutdown, or spend-limit configuration in AWS Settings.
- Missing-schedule, Scheduler delivery, throttling-before-invocation, and silent-success detection.
- Log-query alarms, dashboards, custom metrics, tracing, or a dead-letter queue.
- Changes to the Lambda handler, job code, database schema, catalog, digest, or SMTP transport.
- A second Region, cross-Region notifications, or organization-wide budget administration.

## Design

### Files touched

| File | Change |
| --- | --- |
| `template.yaml` | Add explicit budget and alert-recipient parameters, one `AWS::Budgets::Budget`, one standard SNS topic and email subscription, its restricted topic policy, one `AWS::CloudWatch::Alarm`, and useful nonsecret outputs. |
| `README.md` | Add RC-12 setup, cost categories, verification, operational limits, and rollback instructions to the AWS runbook. |
| `tests/test_sam_template.py` | Add offline assertions for the new resources, their wiring, parameter constraints, and runbook. |

`docs/specs/RC-12-cost-failure-alerts.md` is the planning artifact.
The existing `RainCheckFunction`, execution role, S3 resources, SSM configuration, log group, and disabled schedule are reused without changes to their behavior.

### Schema changes and migration

None.
`src/schema.sql`, `src/db.py`, and the existing `raincheck.db` are untouched, so there is no database migration or database upgrade procedure.

### Interfaces

- Add a required `MonthlyBudgetAmountUsd` CloudFormation parameter with no default, a positive decimal constraint, and a description that the value is the user's chosen monthly USD ceiling for notification purposes.
  The amount is an alert reference, not a cap on charges.
- Add a required `AlertEmail` CloudFormation parameter with no default, `NoEcho: true`, and a basic nonblank email-shape constraint.
  Use this one address for the budget's direct `EMAIL` subscriber and the SNS email subscription unless the user resolves otherwise.
  Keep the address out of committed examples, resource names, descriptions, logs, and outputs.
  `NoEcho` masks the parameter in CloudFormation parameter displays, but AWS still stores the address as the budget recipient and SNS subscription endpoint.
- The budget uses `AWS::Budgets::Budget`, `BudgetType: COST`, `TimeUnit: MONTHLY`, `BudgetLimit` in USD, and a user-approved actual-cost percentage threshold.
  A direct email subscriber avoids a second SNS publishing policy for AWS Budgets.
  Do not set a `CostFilters` region or service restriction by default because storage, monitoring, notification, deployment artifacts, and billing charges may not all appear under the Lambda service or selected Region.
  Without filters, this is a budget for charges in the underlying AWS account, not a boundary around RainCheck or an AWS project label.
  The precise budget scope is an open question below.
- The failure path uses a standard SNS topic in `us-east-2`, one email subscription, and an `AWS::CloudWatch::Alarm` in `us-east-2`.
  The alarm uses `Namespace: AWS/Lambda`, `MetricName: Errors`, `Dimensions: [{Name: FunctionName, Value: !Ref RainCheckFunction}]`, `Statistic: Sum`, `Period: 300`, `EvaluationPeriods: 1`, `DatapointsToAlarm: 1`, `Threshold: 0`, `ComparisonOperator: GreaterThanThreshold`, and `TreatMissingData: notBreaching`.
  Its only alarm action is the SNS topic ARN; no OK or insufficient-data action is configured.
  Use a stack-scoped alarm name so the SNS policy can constrain `sns:Publish` to its exact ARN and `aws:SourceAccount`.
  Neither the Lambda execution role nor the Scheduler role needs SNS publishing permission.
- Keep the existing `SelectedRegion` CloudFormation rule and disabled daily schedule.
  These resources are deployed through the same `us-east-2` stack; do not create a second-region stack for Budgets even though billing data is account-wide.
  AWS publishes a [budget CloudFormation quickstart with a US East (Ohio) launch link](https://startups.aws.com/learn/why-every-startup-should-set-up-a-budget-and-how-aws-budgets-makes-it-easy?lang=en-US), which supports this design.
  The [Budgets API endpoint list](https://docs.aws.amazon.com/general/latest/gr/billing.html) describes direct API endpoints, not the Region of a CloudFormation stack.
  As a deployment preflight, check that the current [us-east-2 CloudFormation resource specification](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/cfn-resource-specification.html) includes `AWS::Budgets::Budget`.
  If it does not, stop before executing a change set and amend this design; do not create a second-region stack or cross-Region workaround.
- Describe direct budget email verification and SNS subscription confirmation as separate required steps.
  An unconfirmed recipient does not receive notifications.
  CloudFormation stack success alone does not prove email delivery.
- The runbook must distinguish a real failed invocation from an absent invocation, and must say that AWS billing data and budget alerts are delayed rather than real-time.
  For an authorized delivery check, use CloudWatch `SetAlarmState` to temporarily set this metric alarm to `ALARM` and inspect the alarm history and email; this does not invoke RainCheck.

AWS documents [CloudFormation budget resources](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-budgets-budget.html), [budget threshold properties](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-properties-budgets-budget-notification.html), and [direct budget email verification](https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-email-recipients.html).
Lambda's [Errors metric](https://docs.aws.amazon.com/lambda/latest/dg/monitoring-metrics-types.html) is a count of failed function invocations, including exceptions and timeouts, and AWS recommends the `Sum` statistic.
CloudWatch documents [missing-data treatment](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/alarms-and-missing-data.html) and [scoped SNS publish policies for alarm actions](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Notify_Users_Alarm_Changes.html).
SNS [requires email confirmation](https://docs.aws.amazon.com/sns/latest/dg/sns-email-notifications.html), and AWS says [Budgets uses refreshed billing data](https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-best-practices.html), so neither path is a real-time or hard-limit guarantee.
CloudWatch documents [`SetAlarmState` as a temporary alarm-action test](https://docs.aws.amazon.com/AmazonCloudWatch/latest/APIReference/API_SetAlarmState.html); a metric alarm normally returns to its measured state quickly.

## Acceptance criteria

Each criterion is checked offline until the user separately authorizes an AWS deployment.

1. The template has exactly one `AWS::Budgets::Budget` with a monthly `COST` limit in USD obtained from the required, validated `MonthlyBudgetAmountUsd` parameter; no committed amount or recipient is used as a default.
   The user supplies those parameter values when an AWS deployment is separately authorized, not as a condition of approving local implementation.
2. The budget has at least one `ACTUAL`, `GREATER_THAN`, `PERCENTAGE` notification at the user-approved percentage, with exactly one direct `EMAIL` subscriber sourced from `AlertEmail`.
   The template contains no automatic budget action.
3. The budget scope matches the user's resolved choice below; for the recommended account-wide scope, it has no Region, service, or tag cost filter and the runbook clearly says unrelated charges in the underlying AWS account can contribute.
4. The `AlertEmail` parameter has `NoEcho: true`, no default, and an email-shape constraint; the template defines exactly one new standard SNS topic and one email subscription of that address to the topic in the same stack.
   It does not place an email address in an output or a resource name.
5. The topic policy permits `sns:Publish` from `cloudwatch.amazonaws.com` only for this stack's exact alarm ARN and `AWS::AccountId`; it does not grant public or wildcard-principal publishing, and the Lambda execution role gains no SNS permission.
6. The one new `AWS::CloudWatch::Alarm` targets `Errors` with the exact `AWS/Lambda` namespace and `FunctionName` dimension of `RainCheckFunction`, sums over five minutes, alarms on one or more errors in one period, and treats missing data as `notBreaching`.
7. The alarm's `AlarmActions` contains only the topic ARN and its action is enabled; no OK or insufficient-data action sends mail.
8. The `SelectedRegion` rule still rejects other Regions, and `DailySchedule.State` remains `DISABLED` after this change.
   The runbook includes a pre-deployment check of `AWS::Budgets::Budget` in the current `us-east-2` CloudFormation resource specification, with a stop condition if the resource is absent.
9. The runbook requires explicit budget amount and alert email input, review of the CloudFormation change set, and approval before any resource creation.
   It gives a way to check the resulting budget, alarm, SNS topic policy, and subscription without showing credentials or the alert email in shared logs.
10. The runbook requires the recipient to verify the direct AWS Budgets email and separately confirm the SNS subscription, then check each status before depending on alerts.
11. After separately authorized deployment, a verification procedure uses CloudWatch `SetAlarmState` to move the alarm temporarily to `ALARM`, confirms one alarm email reaches the recipient, checks the alarm history, and confirms metric evaluation returns it to its measured state without invoking the production job or changing the production database.
    The budget path is checked by recipient verification and budget notification configuration, without artificially creating charges.
12. The runbook lists Lambda, S3 database versions and deployment artifacts, SSM, EventBridge Scheduler, CloudWatch alarm and log storage, SNS, and Budgets as cost categories, tells the user to check current AWS pricing and existing budget count, and explains that notifications do not stop charges.
13. The runbook states that disabled schedules, undelivered Scheduler events, and missing invocations do not trigger this `Errors` alarm; budget alerts follow delayed billing data.
14. The existing offline test suite and new semantic tests pass without AWS calls, network access, SMTP, or the real database.

## Edge cases

- Empty results: A successful run with no deals produces no Lambda error and leaves the alarm OK or without breaching data; the budget still follows billed costs.
- `None` or missing values: Missing, blank, malformed, or nonpositive budget amount and missing or blank alert email fail template parameter validation before a change set can be executed.
- Network failure or bad HTTP status: A failed job that reaches the handler and raises increments `Errors`; an AWS delivery failure before invocation may not.
  Email delivery failure or an unconfirmed address can leave an alarm in `ALARM` without a received message, so the runbook checks SNS subscription and delivery separately.
- Malformed input: Invalid CloudFormation parameter values are rejected; unexpected event payloads do not alter alarm configuration.
- Duplicates: Repeated failures while an alarm stays in `ALARM` do not imply one email per failure because CloudWatch alarm actions occur on state changes.
  If the alarm returns to OK and fails again, a new ALARM transition can notify again.
- Disabled schedule: With no invocation and no metric sample, `notBreaching` does not signal a missed run.
- Billing delay: A budget can alert after spend passes the configured amount because AWS billing data is refreshed periodically.

## Test plan

- Parse `template.yaml` with the existing CloudFormation-aware YAML loader in `tests/test_sam_template.py` and inspect resource types, intrinsics, exact metric dimensions, policy principals and conditions, Region rule, and disabled schedule.
- Assert required parameters have no defaults and reject representative invalid amount and email inputs by inspecting constraints.
- Inspect `README.md` for distinct budget verification and SNS confirmation procedures, a documented `SetAlarmState` test, explicit cost categories, delay and non-cap language, and rollback.
- No saved HTML, API JSON, or database seed fixture is needed because RC-12 changes infrastructure definitions and documentation only.
- If SAM CLI and `cfn-lint` are available locally, validate the template without deploying or contacting AWS.
- The AWS CloudFormation resource specification for `us-east-2` is the authoritative pre-deployment availability check; the official AWS quickstart for Ohio supports this approach, while the Budgets direct API endpoint list alone does not determine CloudFormation stack support.
- Live alarm delivery and budget-recipient status are post-deployment manual checks requiring separate user authorization; offline tests do not claim those checks passed.

## Shared-file conflicts

This change does not touch `src/schema.sql`, `src/db.py`, or `src/catalog.json`.
It does touch `template.yaml`, `README.md`, and `tests/test_sam_template.py`, so it must be coordinated with any concurrent RC-11 infrastructure or runbook edit.
The RC-11 infrastructure changes are already merged; preserve their function, storage, and disabled-schedule settings.

## Open questions

The `MonthlyBudgetAmountUsd` and `AlertEmail` parameters have no defaults.
The user chooses their values when preparing an authorized AWS change set; approving this spec and local implementation does not require disclosing either value now.
The budget amount should reflect the user's expected total account spend and comfort with overruns after checking current pricing and any project spend limit in AWS Settings.
The recipient should be an address the user monitors and can verify; the application's SMTP recipient is not assumed to be the operational contact.

1. **Budget scope:** Should the monthly budget cover all charges in the underlying AWS account, or only RainCheck-tagged or selected-service charges?
   Recommendation: all account charges, with no cost filter, because it includes S3, Lambda, CloudWatch, SNS, Scheduler, deployment artifacts, and charges that do not carry a RainCheck tag.
   This can include unrelated workloads in the same account; filtering improves attribution but may miss costs.
2. **Budget notification threshold:** At what percentage of the monthly amount should the first actual-cost email fire?
   Recommendation: one `ACTUAL` alert at 80% of the selected amount.
   Earlier notification gives time to react, while a 100% threshold is quieter but may arrive after the amount is already exceeded.
   A forecasted alert can be added later if the user wants an earlier estimate; estimates can be noisy for a small workload.

## Resolved (2026-10-27)

The user approved the recommended account-wide monthly budget with no Region, service, or tag filter.
The user approved one actual-cost email alert at 80% of the selected monthly amount.
The dollar amount and alert recipient remain required deployment inputs to be supplied when AWS deployment is separately authorized.

## Rollback

Before an authorized stack update, review a change set that removes only the RC-12 budget, alarm, topic policy, subscription, and topic, while preserving RC-11's function, bucket, logs, and disabled schedule.
Execute that update only with the user's approval.
Confirm the budget and alarm are gone and that SNS no longer has an active subscription; deleting the alert resources ends their notifications and does not reverse charges already incurred.
If the failure path itself is noisy, first review and apply a change set that disables the alarm action while keeping the budget intact, then investigate the metric and subscription.
