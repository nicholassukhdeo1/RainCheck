# RC-11: Daily run on AWS Lambda

**Status:** Approved
**Backlog item:** RC-11 in `docs/BACKLOG.md`

## Summary

Run eBay pricing and then the Yahoo deal job once per day in one AWS Lambda function in `us-east-2`.
Keep the current-schema SQLite database in a private, versioned S3 bucket, load credentials from SSM Parameter Store, and define the resources in AWS SAM.
The Yahoo reachability check from AWS CloudShell is a prerequisite.
The RC-11a check passed for the current one-brand configuration on 2026-10-27.
Use on-demand Lambda Event Functions for the once-daily Python batch job, with S3 General Purpose as its persistent object store.

## Motivation

Today pricing runs only when `src/ebay_pricing.py:202` is started manually, and deals run only when `src/yahoo_scrape.py:394` is started manually.
Both jobs obtain their configuration from SQLite through `src/db.py:158`, while the current default database path points into `src/`.
`src/sendemail.py:26` and `src/ebay_pricing.py:43` expect credentials in process environment variables.
An unattended run needs a persistent database, a scheduled entry point, and explicit handling of partial failure.

## Scope

**In scope:**

- **RC-11a, feasibility gate:** From AWS CloudShell in `us-east-2`, fetch each active Yahoo brand results URL built by `src/yahoo_scrape.py:44` and verify that each page contains parseable `__NEXT_DATA__` listings at `LISTING_PATH` in `src/yahoo_scrape.py:29`.
  Record only the selected Region, brand ID, HTTP status, whether the expected path exists, and the count of valid records per brand.
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
- Changes to the merged RC-13 multi-brand search and RC-14 item-management behavior.

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
| `src/digest.py` | Reuse the existing one-digest format and send-then-record semantics; no planned edit. |
| `src/matching.py` | Reuse the shared title matcher; no planned edit. |
| `requirements.txt` | Pin `boto3` and its compatible SDK dependencies in the deployment bundle. |
| `template.yaml` (new) | AWS SAM definition in `us-east-2` for the function, storage, schedule, roles, and logs. |
| `tests/test_lambda_handler.py` (new) | Offline orchestration, state, error, and credential tests. |
| `tests/test_sam_template.py` (new) | Offline template assertions and SAM validation where the CLI is available. |
| `README.md` | Human CloudShell gate, initial upload, parameter setup, deploy, dry-run, schedule enablement, and rollback instructions. |

The existing source files listed as reused are explicitly in this design because the handler must preserve their behavior.
If implementing the handler requires a change beyond the stated callable adjustments, amend this spec before changing that file.

### Schema changes and migration

None.
The merged RC-14 code sets `SCHEMA_VERSION = 4`.
The S3 object must match the `db.SCHEMA_VERSION` in the deployed code, rather than a version number copied into the handler or SAM template.
The runner must not call `db.init_db()` or `db.seed_from_catalog()` on the production object, and it must not create an empty database if the object is missing.
For first deployment, the user stops all local RainCheck jobs, makes a local SQLite `.backup` of the existing database, runs any then-current migration through the documented backup-first procedure for that feature, and confirms integrity, `PRAGMA foreign_key_check`, and `PRAGMA user_version = db.SCHEMA_VERSION` against the exact release to deploy.
Only then does the user upload the verified database to the private bucket and check the downloaded object before enabling the schedule.
No agent reads or copies the user's real `src/raincheck.db` as part of this feature.
Use S3 versioning for recovery and expire noncurrent database versions after 30 days.

### Interfaces

- `src/lambda_handler.py` exports `handler(event, context)`.
  It accepts the Scheduler event or an empty test event; it does not accept arbitrary bucket or parameter names from event input.
  Names come from nonsecret Lambda configuration and must refer to resources in `us-east-2`.
- At invocation start, create a unique temporary directory, download the configured S3 key, close the response stream, and set `RAINCHECK_DB_PATH` to the downloaded file before any database access.
  Save the returned ETag and version ID for diagnostics, without logging the object body.
  Do not reuse warm `/tmp` state from a previous invocation.
- Validate the local file with SQLite `PRAGMA integrity_check` and the existing schema-version check before contacting eBay, Yahoo, or SMTP.
  A missing, corrupt, wrong-version, or oversized database raises an invocation error without network calls or replacement of the S3 object.
- Fetch only the named SSM SecureString parameters with `WithDecryption=True` and map them to `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET`, `SENDER_EMAIL`, `SENDER_PASSWORD`, and optional `RECEIVER_EMAIL` before running the jobs.
  Fail before either job if a required value is missing or blank.
  Never log values, full parameter responses, SMTP messages, or credential-bearing exception text.
- Call `ebay_pricing.refresh_market_prices()` once, then `yahoo_scrape.main()` once.
  The handler catches an eBay token exception or a `RunResult` with failed item IDs, and still runs Yahoo using last stored prices and manual fallbacks already supported by `db.py`.
  Treat Yahoo's nonzero return or `SystemExit(1)` as a failed job, while preserving the original exit behavior of both CLI entry points.
  Yahoo already fetches every active brand before writing any Yahoo observation, then sends one combined digest.
  If either job fails, the handler still attempts to persist any valid local database changes and then raises a sanitized error so Lambda records a failed invocation.
- Before upload, close all job database connections and make a consistent, standalone SQLite snapshot with `Connection.backup()` into a separate temporary file.
  Require `PRAGMA integrity_check = ok`, empty `PRAGMA foreign_key_check`, and `PRAGMA user_version = db.SCHEMA_VERSION` on that snapshot, then upload its bytes rather than copying the live main file.
  This includes committed changes even if the working database uses WAL, and never uploads a `-wal` or `-shm` sidecar.
  Upload the snapshot using S3 `PutObject` with `IfMatch` equal to the ETag returned when the object was downloaded.
  A `412 PreconditionFailed`, `409 ConditionalRequestConflict`, or other upload error is a failed invocation; do not re-fetch an ETag and blindly replay a write after email has been sent.
  ETag comparison protects against changed content, but equal-content replacement can have the same ETag, so use reserved concurrency one and document that manual writers must stop the schedule first.
  A successful upload is required even after a zero-deal run because eBay prices and listing last-seen dates can change.
- Preserve RC-10 and RC-15's existing send-then-record semantics.
  If SMTP succeeds but S3 upload later fails, a later run may resend the digest.
  The handler logs a clear warning about this specific state and raises rather than claiming success.
  The spec does not promise exactly-once email.
- Configure function reserved concurrency to one, but retain the S3 conditional write because manual writers and stale sequential invocations can still conflict.
  Set Lambda asynchronous function-error retry attempts to zero, and set both the Scheduler target-delivery retry count and maximum event age explicitly.
  Lambda can still retry throttling or internal errors before handler execution, and asynchronous delivery can duplicate events, so idempotency cannot be assumed.
  An alarm and dead-letter destination are deferred to RC-12.
- Use a Python runtime supported by AWS SAM, a zip deployment with pinned dependencies including `boto3`, no VPC attachment, default 512 MB `/tmp` unless measured database size requires more, and a timeout below Lambda's 15-minute limit.
  Without a VPC, the function has public internet access without a NAT Gateway; actual Yahoo and SMTP reachability still need live verification.
  The handler should not rely on the runtime's incidental `boto3` version.
- Run the daily Scheduler at 09:00 `Asia/Tokyo`, with flexible window disabled.
  Keep the schedule disabled until the CloudShell gate, parameter setup, backup-first upload, and a dry-run invocation have passed.

Current AWS documentation confirms Lambda `/tmp` is ephemeral and configurable from 512 MB ([Lambda ephemeral storage](https://docs.aws.amazon.com/lambda/latest/dg/configuration-ephemeral-storage.html)); SAM supports disabled, time-zone-aware `ScheduleV2` schedules ([SAM ScheduleV2](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/sam-property-function-schedulev2.html)); Lambda may retry asynchronous deliveries even when application retries are disabled ([Lambda asynchronous retries](https://docs.aws.amazon.com/lambda/latest/dg/invocation-async-error-handling.html)); and S3 `PutObject` supports an `If-Match` conditional write with 412 and 409 failure modes ([S3 PutObject](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutObject.html)).
AWS recommends bundling `boto3` with its dependencies for a stable Python Lambda package ([Lambda Python packaging](https://docs.aws.amazon.com/lambda/latest/dg/python-package.html)).

## Acceptance criteria

1. The CloudShell gate records the selected Region and, for each active Yahoo brand ID, the HTTP status, presence of the expected `__NEXT_DATA__` listing path, and number of valid records without printing titles, credentials, or page HTML.
2. Any intended brand's CloudShell 403, 429, timeout, missing expected path, or zero valid records is a no-go; RC-11b and RC-11c do not proceed until the cause is resolved or the user approves a revised architecture.
3. An empty or malformed handler event cannot redirect storage, parameters, or Region; only configured names are used.
4. Missing S3 object, a schema version different from `db.SCHEMA_VERSION`, failed `PRAGMA integrity_check`, failed `PRAGMA foreign_key_check`, or size beyond configured `/tmp` capacity stops before eBay, Yahoo, or SMTP and leaves the S3 object unchanged.
5. The handler sets a unique `RAINCHECK_DB_PATH` per invocation and removes its temporary directory after completion, including failures.
6. The S3 download body and all SQLite connections opened by the handler are closed on success and failure.
7. Required SSM parameters are fetched with decryption and a blank or missing required value prevents both jobs and an S3 upload.
8. Secret values never appear in logs or returned error messages, including when SSM or SMTP raises.
9. In a successful fake-service run, eBay pricing is called before Yahoo deals, both are called once, and the final S3 object contains both jobs' committed changes.
   A two-brand fixture causes one Yahoo fetch per active brand and one combined digest, using the merged RC-13 behavior.
10. When eBay reports one failed item, the Yahoo job still runs once, the valid database changes upload, and the handler raises a sanitized invocation error.
11. When eBay token acquisition fails, the Yahoo job still runs once using the existing saved prices, the database uploads if valid, and the handler raises a sanitized invocation error.
12. When Yahoo fails before sending, any eBay database changes upload and the invocation fails.
13. When SMTP fails, no listing matches are recorded; the database upload preserves any eBay changes and the invocation fails.
14. When SMTP succeeds and listing recording succeeds but S3 upload fails, the invocation fails with a warning that the digest may repeat; no success is reported.
15. S3 `PutObject` includes the downloaded ETag as `IfMatch`; a 412 or 409 leaves the current S3 object untouched, fails the invocation, and does not retry by substituting a newly fetched ETag.
16. Before each upload, a separate `Connection.backup()` snapshot passes SQLite integrity, foreign-key, and schema checks, and no WAL sidecar is uploaded.
   A fixture in WAL mode with committed changes present only in the WAL verifies that the uploaded standalone snapshot includes those changes.
17. The SAM template defines only `us-east-2` resources; deployment documentation rejects another selected Region.
18. The S3 bucket blocks public access, disables ACLs, encrypts objects at rest, enables versioning, and expires noncurrent database versions after 30 days.
19. The Lambda execution role can read and conditionally write only the designated database object, call SSM `GetParameter` only for the named parameters, and write only its logs; it cannot list the whole bucket or read unrelated parameters.
20. The schedule is disabled by default, runs daily at 09:00 `Asia/Tokyo` with flexible window off, and has explicit bounded delivery retry attempts and maximum event age; Lambda function-error retries are zero.
21. Reserved concurrency is one, there is no public Lambda URL or API endpoint, and the function is not placed in a VPC.
22. The deployment package contains the handler, application modules, and pinned third-party dependencies, including `boto3`; it contains no `.env`, database, test fixture, or secret.
23. The documented preflight checks the CloudShell result for every intended brand, selected Region, parameter presence without exposing values, database backup and integrity, and S3 object version before the schedule is enabled.
24. A dry-run invocation uses separately configured throwaway storage and `RAINCHECK_DRY_RUN=1`, sends no email, and cannot modify the production S3 object.
25. The full existing test suite and new offline handler/template tests pass with no warnings and no real AWS, network, SMTP, or production database access.

## Edge cases

- Empty results: An eBay or Yahoo search with valid empty results follows the current job behavior; the valid database snapshot still uploads.
- `None` or missing values: Missing bucket/key configuration, SSM parameter names, required secret values, or S3 ETag fails closed before the jobs.
- Network failure or bad HTTP status: Existing RC-10 bounded retries govern eBay, Yahoo, and Frankfurter.
  S3 or SSM failures are surfaced as invocation failures; the handler does not make an unbounded retry loop.
- Malformed input: A corrupt or wrong-version SQLite object is never replaced, and unexpected Scheduler payload fields are ignored.
- Duplicates: Existing eBay listing and Yahoo match constraints remain authoritative.
  Repeated invocation with the same S3 snapshot can resend mail only in the known send-before-upload failure window.
- Concurrency: Reserved concurrency limits simultaneous executions; conditional S3 writes reject changed-content snapshots but cannot distinguish two versions with equal ETags.
- S3 conflict: A 409 or 412 fails closed; the handler never retries a completed send against a freshly fetched ETag.
- Warm reuse: Every invocation downloads from S3 again and uses an isolated `/tmp` path.
- Partial failure: Persist healthy database changes when possible, but report the overall invocation as failed.
- Oversized state: Reject a database that cannot safely fit alongside both the working file and its backup snapshot; increasing `/tmp` is an explicit template change.

## Test plan

- Saved Yahoo fixture from the existing test suite for parser-shape validation, plus a separate human CloudShell probe of the live page before any build.
- Existing seed database fixture at `db.SCHEMA_VERSION` (version 4 after RC-14); temporary copies for success, corrupt bytes, wrong version, foreign-key failure, missing object, WAL mode, and oversized-object cases.
- Fake S3 client returning an in-memory object, ETag, and version ID, recording conditional `PutObject` calls and simulating 412, 409, and transport errors.
- Fake SSM client returning named SecureString values or missing/blank errors; assert decryption flags and no secret leakage.
- Fake eBay, Frankfurter, per-brand Yahoo pages, and SMTP behavior for success, partial eBay failure, token failure, malformed or failed second brand, failed send, and post-send upload failure.
- Static SAM assertions for Region guard, private versioned bucket, exact IAM resource scopes, disabled ScheduleV2, retry settings, log retention, and package exclusions.
- Verify the real handler locally with fake AWS clients and a throwaway database in dry-run mode; do not run against the user's real database.
- After approved deployment, perform one explicitly authorized AWS dry-run with separate stack configuration and a throwaway S3 object before enabling the production schedule.

## Shared-file conflicts

No schema or catalog edits are planned.
`src/db.py` is reused but not edited; if implementation reveals a needed DB change, pause for a spec amendment and do not run it alongside another schema or database feature.
RC-11c changes `template.yaml`, so RC-12's alarm and budget work must wait until RC-11c is merged.

## Resolved

On 2026-10-27, the user approved the recommended RC-11 policy:

- Run daily at 09:00 `Asia/Tokyo`, with the Scheduler flexible window disabled.
  This is 17:00 or 16:00 on the US Pacific coast depending on daylight saving time; the current digest date uses Lambda's UTC local date.
- If eBay pricing partially fails or token acquisition fails, run Yahoo using saved prices and manual fallbacks, persist valid changes, and report the overall invocation as failed.
  The deal threshold can therefore use an older saved price.
- Keep S3 versioning and expire noncurrent database versions after 30 days, while retaining the user's separate local backup.
  Longer retention would improve recovery from an unnoticed bad write at additional storage cost.
- Use standard-tier SSM SecureString with the AWS-managed `aws/ssm` key and scope the Lambda role to `ssm:GetParameter` on the named parameter ARNs.
  The AWS-managed key does not provide parameter-specific key-level decrypt isolation; use a customer-managed key if different workloads later require different decrypt rights.

Approval authorizes the spec and local implementation only after the CloudShell feasibility gate passes.
It does not authorize AWS resource creation, a production database upload, or enabling the schedule.

### RC-11a CloudShell feasibility observation, 2026-10-27

The orchestrator observed the probe in the user's existing AWS CloudShell browser tab with `us-east-2` selected.
The public Yahoo results URL for Rick Owens brand ID `103538` returned HTTP 200.
The expected `__NEXT_DATA__` listing path was present as a list, and 100 records passed the approved validity check.
No listing titles, credentials, or page HTML were recorded.
The user confirmed that Rick Owens is the only active Yahoo brand in the real database.
The current `src/catalog.json` also names only Rick Owens, although it is seed input rather than proof of database state.
RC-11a is **PASSED for the current configuration**.
Any additional Yahoo brand must pass the same CloudShell gate before that brand is included in an AWS run.

## CloudShell probe for future brands

Before an additional brand is included in an AWS run, run the following from AWS CloudShell with `us-east-2` selected.
Set `BRAND_IDS` to the new comma-separated brand IDs; the default shown probes only Rick Owens.
Share only the selected Region and one status/path/count line per brand.
The command reads public Yahoo HTML into CloudShell memory, parses only counts, and prints no listing data.

   ```sh
   BRAND_IDS=103538 python3 - <<'PY'
   import json, os, urllib.error, urllib.request
   from html.parser import HTMLParser

   URL = 'https://auctions.yahoo.co.jp/category/list/23000?auccat=23000&s1=new&o1=d&brand_id={}&n=100'
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
   ids = [part.strip() for part in os.environ['BRAND_IDS'].split(',')]
   if not ids or any(not part or not part.isascii() or not part.isdecimal() for part in ids):
       raise SystemExit('BRAND_IDS must contain comma-separated ASCII digits')
   print('Selected Region:', os.getenv('AWS_REGION') or os.getenv('AWS_DEFAULT_REGION') or 'unknown')
   for brand_id in dict.fromkeys(ids):
       status = None
       path_ok = False
       valid = 0
       failure = None
       try:
           with urllib.request.urlopen(URL.format(brand_id), timeout=30) as response:
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
       print(f'Brand {brand_id}: HTTP {status if status is not None else "unavailable"}; path {path_ok}; valid {valid}; failure {failure or "none"}')
   PY
   ```

   CloudShell has documented public-internet egress ([CloudShell limits](https://docs.aws.amazon.com/cloudshell/latest/userguide/limits.html)), but Yahoo may still block its addresses.
   A pass from CloudShell is necessary for this plan, yet it does not guarantee Lambda's egress or HTML shape forever.
## Open questions

1. **Deployment authorization and cost:** The spec is approved, but AWS deployment requires separate user authorization after review of the SAM change set.
   Before creating resources or uploading the production database, the user should review the SAM change set and cost categories: [Lambda requests, duration, and extra `/tmp` allocation](https://aws.amazon.com/lambda/pricing/), [S3 current and noncurrent full-object versions, requests, and data transfer](https://aws.amazon.com/s3/pricing/), [Parameter Store tier and higher-throughput requests](https://aws.amazon.com/systems-manager/pricing/), any [customer-managed KMS key and API use](https://aws.amazon.com/kms/pricing/), [Scheduler invocations](https://aws.amazon.com/eventbridge/pricing/), and [CloudWatch log ingestion and storage](https://aws.amazon.com/cloudwatch/pricing/).
   At one run per day, estimate roughly 30 S3 reads and 30 full database versions per month, plus retries and manual runs; [S3 Versioning bills each full version](https://docs.aws.amazon.com/AmazonS3/latest/userguide/Versioning.html).
   Use S3 Standard for the frequently read current object, SSE-S3, blocked public access, bucket-owner-enforced ACLs, HTTPS-only bucket access, and a narrowly scoped Lambda role.
   Use CloudWatch logs and metrics for health; consider S3 CloudTrail data events for object-level audit, with their separate charge, when the deployment is reviewed ([S3 monitoring](https://docs.aws.amazon.com/AmazonS3/latest/userguide/monitoring-overview.html)).
   RC-12 adds budget and failure alerts after this feature.

## Rollback

Disable the Scheduler schedule first so no new invocation starts.
If the latest S3 object is bad, restore a known-good prior S3 version after verifying its SQLite integrity and schema version, and keep the current version for investigation.
Redeploy the prior Lambda code and SAM stack definition if needed.
Do not remove the database bucket, parameter values, or local backup until a verified replacement is operating.
If the deployment is abandoned, remove only the RC-11 resources after exporting the last good database version and obtaining the user's choice about retaining the data and any billable resources.
