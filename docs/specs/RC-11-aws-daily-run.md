# RC-11: Daily run on AWS Lambda

**Status:** Draft
**Backlog item:** RC-11 in `docs/BACKLOG.md`

## Summary

Run eBay pricing and then the Yahoo deal job once per day in one AWS Lambda function in `us-east-2`.
Keep the current-schema SQLite database in a private, versioned S3 bucket, load credentials from SSM Parameter Store, and define the resources in AWS SAM.
The Yahoo reachability check from AWS CloudShell is a prerequisite, not an implementation test that has already passed.

## Motivation

Today pricing runs only when `src/ebay_pricing.py:202` is started manually, and deals run only when `src/yahoo_scrape.py:309` is started manually.
Both jobs obtain their configuration from SQLite through `src/db.py:150`, while the current default database path points into `src/`.
`src/sendemail.py:44` and `src/ebay_pricing.py:46` expect credentials in process environment variables.
An unattended run needs a persistent database, a scheduled entry point, and explicit handling of partial failure.

## Scope

**In scope:**

- **RC-11a, feasibility gate:** From AWS CloudShell in `us-east-2`, fetch the current Yahoo results URL and verify that the page contains parseable `__NEXT_DATA__` listings at the path in `src/yahoo_scrape.py:28`.
  Record only the HTTP status, whether the expected path exists, and the count of records.
  If AWS egress is blocked or Yahoo serves a different layout, stop before RC-11b or RC-11c is built.
- **RC-11b, runner and local tests:** Add a Lambda handler that downloads the single SQLite object into a fresh invocation-specific path under `/tmp`, verifies it, reads the needed SecureString parameters, runs eBay pricing followed by Yahoo deals, and conditionally uploads a complete SQLite snapshot back to the same S3 key.
  All test cases use fake AWS clients, fake HTTP and SMTP, and a throwaway database.
- **RC-11c, infrastructure and deployment:** Define the Lambda function, private versioned S3 bucket, least-privilege execution role, standard SecureString parameter names, log retention, and daily EventBridge Scheduler trigger in a SAM template.
  Document a backup-first initial database upload, a dry-run deployment check, enabling the schedule, and rollback.
  These are separate reviewable pull requests in dependency order, with the feasibility gate before any implementation.

**Out of scope:**

- A database schema or catalog change, moving SQLite to another database engine, or modifying the price and matching algorithms.
- Creating AWS resources, uploading the user's database, moving live secrets, or enabling the schedule during spec approval.
- New email transport or exactly-once digest delivery.
- Cost budgets and failure notification alarms, which belong to RC-12.
- Multi-brand Yahoo searches, which belong to RC-13.

## Design

### Files touched

| File | Change |
| --- | --- |
| `src/lambda_handler.py` (new) | Orchestrate S3 download, validation, SSM reads, eBay then Yahoo, S3 upload, and final failure signal. |
| `src/ebay_pricing.py` | Only if a small callable entry-point adjustment is needed to expose the existing `RunResult` without `SystemExit`. |
| `src/yahoo_scrape.py` | Only if a small callable entry-point adjustment is needed to expose the existing result without swallowing failure. |
| `src/db.py` | Reuse `RAINCHECK_DB_PATH`, schema validation, and transaction handling; no schema change. |
| `src/schema.sql` | Read for schema validation; no edit. |
| `src/sendemail.py` | Reuse the existing SMTP and dry-run behavior; no planned edit. |
| `src/http_retry.py` | Reuse existing bounded HTTP retries; no planned edit. |
| `requirements.txt` | Pin `boto3` and its compatible SDK dependencies in the deployment bundle. |
| `template.yaml` (new) | AWS SAM definition in `us-east-2` for the function, storage, schedule, roles, and logs. |
| `tests/test_lambda_handler.py` (new) | Offline orchestration, state, error, and credential tests. |
| `tests/test_sam_template.py` (new) | Offline template assertions and SAM validation where the CLI is available. |
| `README.md` | Human CloudShell gate, initial upload, parameter setup, deploy, dry-run, schedule enablement, and rollback instructions. |

The existing source files listed as reused are explicitly in this design because the handler must preserve their behavior.
If implementing the handler requires a change beyond the stated callable adjustments, amend this spec before changing that file.

### Schema changes and migration

None.
At drafting, `src/db.py:25` sets `SCHEMA_VERSION = 2`, but RC-16 may merge before deployment.
The S3 object must match the `db.SCHEMA_VERSION` in the deployed code, rather than a version number copied into the handler or SAM template.
The runner must not call `db.init_db()` or `db.seed_from_catalog()` on the production object, and it must not create an empty database if the object is missing.
For first deployment, the user makes a local backup of the existing database, runs any then-current migration through the documented backup-first procedure for that feature, and confirms integrity and `PRAGMA user_version = db.SCHEMA_VERSION` against the exact release to deploy.
Only then does the user upload the verified database to the private bucket and check the downloaded object before enabling the schedule.
No agent reads or copies the user's real `src/raincheck.db` as part of this feature.
Use S3 versioning for recovery, with a documented noncurrent-version lifecycle retention choice.

### Interfaces

- `src/lambda_handler.py` exports `handler(event, context)`.
  It accepts the Scheduler event or an empty test event; it does not accept arbitrary bucket or parameter names from event input.
  Names come from nonsecret Lambda configuration and must refer to resources in `us-east-2`.
- At invocation start, create a unique temporary directory, download the configured S3 key, close the response stream, and set `RAINCHECK_DB_PATH` to the downloaded file before any database access.
  Do not reuse warm `/tmp` state from a previous invocation.
- Validate the local file with SQLite `PRAGMA integrity_check` and the existing schema-version check before contacting eBay, Yahoo, or SMTP.
  A missing, corrupt, wrong-version, or oversized database raises an invocation error without network calls or replacement of the S3 object.
- Fetch only the named SSM SecureString parameters with `WithDecryption=True` and map them to `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET`, `SENDER_EMAIL`, `SENDER_PASSWORD`, and optional `RECEIVER_EMAIL` before running the jobs.
  Fail before either job if a required value is missing or blank.
  Never log values, full parameter responses, SMTP messages, or credential-bearing exception text.
- Call eBay pricing once, then Yahoo deals once.
  The Yahoo job still runs when the eBay job reports item failures, using the last stored prices and manual fallbacks already supported by `db.py`.
  If either job fails, the handler still attempts to persist any valid local database changes and then raises a sanitized error so Lambda records a failed invocation.
- Before upload, require `PRAGMA integrity_check = ok`, `PRAGMA user_version = db.SCHEMA_VERSION`, no active SQLite transaction, and a finalized database file with no unmerged WAL state.
  Upload the file using S3 `PutObject` with `IfMatch` equal to the ETag returned when the object was downloaded.
  A changed ETag or upload error is a failed invocation; never overwrite a newer S3 object.
  A successful upload is required even after a zero-deal run because eBay prices and listing last-seen dates can change.
- Preserve RC-10 and RC-15's existing send-then-record semantics.
  If SMTP succeeds but S3 upload later fails, a later run may resend the digest.
  The handler logs a clear warning about this specific state and raises rather than claiming success.
  The spec does not promise exactly-once email.
- Configure function reserved concurrency to one, but retain the S3 conditional write because sequential or manually invoked runs can still see stale state.
  Set Lambda asynchronous retry attempts to zero for function errors, and Scheduler target-delivery retry attempts to a small bounded value chosen in the template.
  An alarm and dead-letter destination are deferred to RC-12.
- Use a Python runtime supported by AWS SAM, a zip deployment with pinned dependencies including `boto3`, no VPC attachment, default 512 MB `/tmp` unless measured database size requires more, and a timeout below Lambda's 15-minute limit.
  Without a VPC, the function can reach public eBay, Yahoo, Frankfurter, and SMTP endpoints without a NAT Gateway.
  The handler should not rely on the runtime's incidental `boto3` version.
- Use a daily Scheduler expression with a named time zone and flexible window disabled.
  Keep the schedule disabled until the CloudShell gate, parameter setup, backup-first upload, and a dry-run invocation have passed.

Current AWS documentation confirms Lambda `/tmp` is ephemeral and defaults to 512 MB ([Lambda ephemeral storage](https://docs.aws.amazon.com/lambda/latest/dg/configuration-ephemeral-storage.html)); SAM supports Scheduler through `ScheduleV2` ([SAM Scheduler guide](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/using-eventbridge-scheduler.html)); Scheduler invokes Lambda asynchronously ([Lambda Scheduler guide](https://docs.aws.amazon.com/lambda/latest/dg/with-eventbridge-scheduler.html)); and S3 `PutObject` supports an `If-Match` conditional write ([S3 conditional writes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html)).
AWS recommends bundling `boto3` with its dependencies for a stable Python Lambda package ([Lambda Python packaging](https://docs.aws.amazon.com/lambda/latest/dg/python-package.html)).

## Acceptance criteria

1. The CloudShell gate records the selected Region, Yahoo HTTP status, presence of the expected `__NEXT_DATA__` listing path, and number of listing records without printing titles, credentials, or page HTML.
2. A CloudShell 403, 429, timeout, missing expected path, or zero valid records is a no-go; RC-11b and RC-11c do not proceed until the cause is resolved or the user approves a revised architecture.
3. An empty or malformed handler event cannot redirect storage, parameters, or Region; only configured names are used.
4. Missing S3 object, wrong schema version, failed `PRAGMA integrity_check`, or size beyond configured `/tmp` capacity stops before eBay, Yahoo, or SMTP and leaves the S3 object unchanged.
5. The handler sets a unique `RAINCHECK_DB_PATH` per invocation and removes its temporary directory after completion, including failures.
6. The S3 download body and all SQLite connections opened by the handler are closed on success and failure.
7. Required SSM parameters are fetched with decryption and a blank or missing required value prevents both jobs and an S3 upload.
8. Secret values never appear in logs or returned error messages, including when SSM or SMTP raises.
9. In a successful fake-service run, eBay pricing is called before Yahoo deals, both are called once, and the final S3 object contains both jobs' committed changes.
10. When eBay reports one failed item, the Yahoo job still runs once, the valid database changes upload, and the handler raises a sanitized invocation error.
11. When eBay token acquisition fails, the Yahoo job still runs once using the existing saved prices, the database uploads if valid, and the handler raises a sanitized invocation error.
12. When Yahoo fails before sending, any eBay database changes upload and the invocation fails.
13. When SMTP fails, no listing matches are recorded; the database upload preserves any eBay changes and the invocation fails.
14. When SMTP succeeds and listing recording succeeds but S3 upload fails, the invocation fails with a warning that the digest may repeat; no success is reported.
15. S3 `PutObject` includes the downloaded ETag as `IfMatch`; a precondition failure leaves the newer S3 object untouched and fails the invocation.
16. Before each upload the SQLite integrity and schema checks pass, and there is no unmerged WAL sidecar.
17. The SAM template defines only `us-east-2` resources; deployment documentation rejects another selected Region.
18. The S3 bucket blocks public access, disables ACLs, encrypts objects at rest, enables versioning, and has a documented noncurrent-version lifecycle policy.
19. The Lambda execution role can read and conditionally write only the designated database object, call SSM `GetParameter` only for the named parameters, and write only its logs; it cannot list the whole bucket or read unrelated parameters.
20. The schedule is disabled by default, daily at the approved local time zone, and has an explicit bounded delivery retry policy; Lambda function-error retries are zero.
21. Reserved concurrency is one, there is no public Lambda URL or API endpoint, and the function is not placed in a VPC.
22. The deployment package contains the handler, application modules, and pinned third-party dependencies, including `boto3`; it contains no `.env`, database, test fixture, or secret.
23. The documented preflight checks the CloudShell result, selected Region, parameter presence without exposing values, database backup and integrity, and S3 object version before the schedule is enabled.
24. A dry-run invocation against a throwaway database sends no email and does not modify the production S3 object.
25. The full existing test suite and new offline handler/template tests pass with no warnings and no real AWS, network, SMTP, or production database access.

## Edge cases

- Empty results: An eBay or Yahoo search with valid empty results follows the current job behavior; the valid database snapshot still uploads.
- `None` or missing values: Missing bucket/key configuration, SSM parameter names, required secret values, or S3 ETag fails closed before the jobs.
- Network failure or bad HTTP status: Existing RC-10 bounded retries govern eBay, Yahoo, and Frankfurter.
  S3 or SSM failures are surfaced as invocation failures; the handler does not make an unbounded retry loop.
- Malformed input: A corrupt or wrong-version SQLite object is never replaced, and unexpected Scheduler payload fields are ignored.
- Duplicates: Existing eBay listing and Yahoo match constraints remain authoritative.
  Repeated invocation with the same S3 snapshot can resend mail only in the known send-before-upload failure window.
- Concurrency: Reserved concurrency limits simultaneous executions; conditional S3 writes reject stale sequential snapshots.
- Warm reuse: Every invocation downloads from S3 again and uses an isolated `/tmp` path.
- Partial failure: Persist healthy database changes when possible, but report the overall invocation as failed.
- Oversized state: Reject a database that cannot safely fit alongside the temporary write space; increasing `/tmp` is an explicit template change.

## Test plan

- Saved Yahoo fixture from the existing test suite for parser-shape validation, plus a separate human CloudShell probe of the live page before any build.
- Existing seed database fixture at the then-current `db.SCHEMA_VERSION` (version 2 at drafting); temporary copies for success, corrupt bytes, wrong version, missing object, and oversized-object cases.
- Fake S3 client returning an in-memory object and ETag, recording conditional `PutObject` calls and simulating 412 and transport errors.
- Fake SSM client returning named SecureString values or missing/blank errors; assert decryption flags and no secret leakage.
- Fake eBay, Frankfurter, Yahoo, and SMTP behavior for success, partial eBay failure, token failure, malformed Yahoo page, failed send, and post-send upload failure.
- Static SAM assertions for Region guard, private versioned bucket, exact IAM resource scopes, disabled ScheduleV2, retry settings, log retention, and package exclusions.
- Verify the real handler locally with fake AWS clients and a throwaway database in dry-run mode; do not run against the user's real database.
- After approved deployment, perform one explicitly authorized AWS dry-run against a separate throwaway S3 object before enabling the production schedule.

## Shared-file conflicts

No schema or catalog edits are planned.
`src/db.py` is reused but not edited; if implementation reveals a needed DB change, pause for a spec amendment and do not run it alongside RC-13 or RC-14.
RC-11c changes `template.yaml`, so RC-12's alarm and budget work must wait until RC-11c is merged.

## Open questions

1. **CloudShell go/no-go result:** No CloudShell session is available to this planner, so the required live probe has not run.
   Recommendation: the user runs the following from AWS CloudShell with `us-east-2` selected, then shares only the four summary lines.
   The command reads public Yahoo HTML into CloudShell memory, parses only counts, and prints no listing data.

   ```sh
   echo "Selected Region: ${AWS_REGION:-${AWS_DEFAULT_REGION:-unknown}}"
   python3 - <<'PY'
   import json, urllib.error, urllib.request
   from html.parser import HTMLParser

   URL = 'https://auctions.yahoo.co.jp/category/list/23000?auccat=23000&s1=new&o1=d&brand_id=103538&n=100'
   PATH = ('props', 'pageProps', 'initialState', 'search', 'items', 'listing', 'items')
   class NextData(HTMLParser):
       def __init__(self):
           super().__init__(); self.in_script = False; self.parts = []
       def handle_starttag(self, tag, attrs):
           self.in_script = tag == 'script' and dict(attrs).get('id') == '__NEXT_DATA__'
       def handle_endtag(self, tag):
           if tag == 'script': self.in_script = False
       def handle_data(self, data):
           if self.in_script: self.parts.append(data)
   status = None
   path_ok = False
   valid = 0
   failure = None
   try:
       with urllib.request.urlopen(URL, timeout=30) as response:
           status = response.status
           body = response.read()
       parser = NextData(); parser.feed(body.decode('utf-8', errors='replace'))
       node = json.loads(''.join(parser.parts))
       for key in PATH: node = node[key]
       path_ok = isinstance(node, list)
       valid = sum(
           isinstance(item, dict)
           and isinstance(item.get('auctionId'), str)
           and item['auctionId'].isascii()
           and item['auctionId'].isalnum()
           and isinstance(item.get('title'), str)
           and bool(item['title'].strip())
           and isinstance(item.get('price'), int)
           and not isinstance(item['price'], bool)
           and item['price'] > 0
           for item in node
       ) if path_ok else 0
   except urllib.error.HTTPError as exc:
       status = exc.code
       failure = type(exc).__name__
   except Exception as exc:
       failure = type(exc).__name__
   print('Yahoo HTTP status:', status if status is not None else 'unavailable')
   print('Expected listing path:', path_ok)
   print('Valid listing record count:', valid)
   if failure: print('Probe failed:', failure)
   PY
   ```

   CloudShell has documented public-internet egress ([CloudShell limits](https://docs.aws.amazon.com/cloudshell/latest/userguide/limits.html)), but Yahoo may still block its addresses.
   A pass from CloudShell is necessary for this plan, yet it does not guarantee Lambda's egress or HTML shape forever.
2. **Daily time:** Recommendation: 09:00 `Asia/Tokyo`, with flexible window off so the Japanese auction page is checked in its local morning.
   Tradeoff: that is 17:00 or 16:00 on the US Pacific coast depending on daylight saving time, and the current digest date uses Lambda's UTC local date.
   The user should choose the time and zone before RC-11c.
3. **Partial eBay failure:** Recommendation: still run Yahoo using the last saved or manual prices, persist any healthy state, and fail the invocation for monitoring.
   Tradeoff: the Yahoo deal threshold may be based on an older price, but skipping Yahoo entirely would lose a day of deal detection.
4. **Database backup retention:** Recommendation: keep S3 versioning and expire noncurrent database versions after 30 days, with the user's separate local backup retained.
   Tradeoff: longer retention improves recovery from unnoticed bad writes but increases storage cost.
5. **Secrets key choice:** Recommendation: standard-tier SSM SecureString using the AWS-managed `aws/ssm` key for this small personal project, with the Lambda role allowed to call `ssm:GetParameter` only on the named parameter ARNs.
   This avoids the monthly customer-managed-key charge and key-policy setup; standard Parameter Store has no additional parameter charge, though KMS API requests can still be billed ([Parameter Store overview](https://docs.aws.amazon.com/systems-manager/latest/userguide/systems-manager-parameter-store.html), [AWS KMS pricing](https://aws.amazon.com/kms/pricing/)).
   Tradeoff: AWS says the `aws/ssm` key's decrypt permission is available to all IAM principals in the project, so it cannot provide parameter-specific key-level isolation.
   If other workloads or team members later need different decrypt rights, switch the sensitive parameters to a customer-managed key and scope its decrypt policy before sharing access ([SSM decryption guidance](https://docs.aws.amazon.com/systems-manager/latest/userguide/ps-restrict-decryption.html)).
6. **Deployment authorization:** The spec can be approved and code can be tested locally without granting AWS deployment permission.
   Before creating resources or uploading the production database, the user should review the SAM change set and the documented cost categories: Lambda requests and duration, S3 current and noncurrent storage plus requests, SSM parameters and API calls, any customer-managed KMS key, Scheduler invocations, and CloudWatch logs.
   RC-12 adds budget and failure alerts after this feature.

## Rollback

Disable the Scheduler schedule first so no new invocation starts.
If the latest S3 object is bad, restore a known-good prior S3 version after verifying its SQLite integrity and schema version, and keep the current version for investigation.
Redeploy the prior Lambda code and SAM stack definition if needed.
Do not remove the database bucket, parameter values, or local backup until a verified replacement is operating.
If the deployment is abandoned, remove only the RC-11 resources after exporting the last good database version and obtaining the user's choice about retaining the data and any billable resources.
