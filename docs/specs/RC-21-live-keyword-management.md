# RC-21: Simple live keyword management

**Status:** Implemented
**Backlog item:** RC-21 in `docs/BACKLOG.md`

## Summary

Add a small command to list, add, or remove one production keyword without retyping an item's other keywords.
The command saves a verified backup, asks the existing Lambda to make the edit, and checks the exact uploaded database version.

## Motivation

`src/setup.py:34` and `src/setup.py:43` accept repeated keyword flags that replace whole sets, and `src/db.py:787` implements that replacement.
The production database is instead the versioned S3 object used by `src/lambda_handler.py:182`, so a local `setup.py` edit does not affect daily runs.
The current manual production procedure requires copying a database, changing it, uploading it, and checking the result.

## Scope

**In scope:**

- A dedicated production command with `list`, `add`, `remove`, and `recover` operations for one include or exclude keyword at a time.
- A narrow management request handled by the existing Lambda, using its existing single reserved execution slot.
- Local backup and operation record outside the repository before each real edit, plus verification of the uploaded S3 version.
- Account, Region, function, database, schema, and concurrency preflight checks.
- Clear busy, stale, failed, and uncertain outcomes with recovery guidance.
- Documentation of credentials and use from the repository root.

**Out of scope:**

- A graphical interface, chat parser, or general remote item editor.
- Changes to title matching, seed catalog, prices, listings, or digest content.
- New AWS resources, extra database objects, schema migrations, or a separate configuration store.
- Automatic retries after an uncertain result or automatic rollback of a later database version.
- Pausing or changing the EventBridge schedule.

## Design

The normal command is `python3 src/live_keywords.py add 1 --include "sneakers"`.
`remove` has the same shape, and `--exclude` selects the exclude set.
`list` shows both sets for the specified item or all items.
The command uses the named `raincheck` AWS profile and fixed `us-east-2` Region.
It checks STS account `510258803420`, reads the `raincheck` stack outputs, and confirms that the function's configured bucket and key agree with those outputs, that production dry run is off, that function timeout is no more than 840 seconds, that reserved concurrency is exactly one, and that its environment declares `RAINCHECK_MANAGEMENT_PROTOCOL=keyword-v1`.
If the capability marker is absent or different, it refuses a mutating Invoke before the older deployed handler can mistake the event for a daily run.
It does not read `.env` or default to `src/raincheck.db`.

For an edit, the command downloads and syncs the current S3 version to a timestamped backup outside the repository and checks its version ID, SQLite integrity, foreign keys, and schema version.
It verifies that it can fetch that exact version by ID and list object versions before proceeding, so later recovery has the required permissions.
If the requested keyword is already in the desired state, the command reports no change, preserves `config_source`, and makes no Lambda invocation or upload.
Otherwise it atomically writes and syncs a small operation record beside the backup with a UUID, item, term, action, original version ID and ETag, and `prepared` state.
Immediately before Invoke it atomically records `dispatched_at` and `dispatching` status, so a crash cannot leave a possible send labelled safely unsent.
It then invokes the existing Lambda synchronously with exactly `{"raincheck_management":"keyword-v1","operation_id":<UUID string>,"item_id":<positive integer>,"action":"add"|"remove","kind":"include"|"exclude","keyword":<nonblank string>,"expected_version_id":<nonblank string>}`.
If a dictionary has the `raincheck_management` key, the Lambda requires this exact shape and its own capability marker, and rejects unknown markers, extra keys, and wrong types before any AWS call.
Events without a dictionary management marker follow the existing daily-job path, including arbitrary Scheduler event fields and legacy non-dictionary invocation payloads.

The Lambda downloads the current object and refuses the edit unless its VersionId matches the expected original version.
It applies the term change to its private SQLite copy through a new transactional `db.change_keyword` helper, using the existing keyword normalization and validation.
It skips eBay, Yahoo, SSM credential retrieval, and email in management mode.
It checks the resulting SQLite snapshot, uploads it using the original ETag as `IfMatch` with the operation ID under the `raincheck-operation-id` key in that S3 version's metadata, and returns `{"status":"changed","operation_id":...,"original_version_id":...,"new_version_id":...,"sha256":...,"before":{"include":[...],"exclude":[...]},"after":{"include":[...],"exclude":[...]}}`.
An unchanged request returns the same operation and original version IDs with `status: "unchanged"`, identical before and after sets, and no new version ID or upload.
A validly shaped request that is rejected before upload returns `{"status":"rejected","reason":"invalid"|"stale"|"conflict","operation_id":...}`.
Malformed event shapes raise `RunnerError` before any AWS call, because they may lack a valid operation ID.
The CLI fetches that exact new version, compares its bytes to the intended snapshot checksum returned by Lambda, repeats the SQLite checks, and confirms only the requested term and the item's `config_source` changed while all other tables and item fields match the backup.
The allowed row delta is derived directly from the backup rows, independently of `db.change_keyword`, so a mutation bug cannot also corrupt verification's expected result.
It marks the operation record `verified` only after these checks.
The uploaded version may no longer be current if a later daily job ran; verification uses its version ID rather than assuming it is still the head object.

This path leaves the schedule untouched.
The one execution slot serializes the edit with running daily or manual invocations; a busy function returns a throttle and the CLI exits with a short retry message rather than waiting for the up-to-14-minute job.
A queued scheduled invocation, if delivered within its configured event age, runs later and reads the then-current object; an expired or undelivered event is not assumed to have run.
Lambda's ETag condition also rejects a changed object, but identical-content writes can retain the same ETag, so all supported edits must use this command and the Lambda slot.
The version check catches earlier equal-content replacements; an out-of-band writer racing after that check remains a limitation of S3's ETag condition and is called out in the runbook.
CloudWatch metrics or stale logs are never used as proof that the function is idle.

The CLI handles `StatusCode`, `FunctionError`, and the response body separately because a synchronous Lambda invoke can return HTTP 200 for a function error.
It uses explicit connect and read timeouts on every AWS client, disables automatic SDK retries of both the mutating Invoke request and the management-mode S3 upload, and never waits indefinitely for a running job.
An upload whose response is lost must stay uncertain rather than being retried into a misleading conditional-write rejection.
It atomically syncs the operation record again when it receives a Lambda response, before post-upload verification.
On a lost response, upload verification failure, or interrupted process, the command leaves the backup and operation record in place, reports `uncertain`, and gives a `recover <record-path>` command.
Recovery paginates the exact key's S3 versions and checks their metadata for the recorded operation ID.
It reports `applied` only after verifying that matching version's database and row delta; this means the edit occurred, even if the current keyword state has since changed again, which it reports separately.
It reports `not applied` only with a recorded pre-invocation failure or an explicit Lambda or service response proving no upload.
A crash with a prepared record or a lost Invoke response remains `uncertain` without a matching receipt, even if the original version is still current, because absence of evidence cannot prove that an invocation did not run or cannot still finish.
Recovery never writes to AWS or silently retries the operation; it may atomically mark the local record resolved only after proving `applied` or `not applied`.
S3 retains noncurrent database versions for 30 days under the existing template, so an unresolved record must be investigated before that retention expires.
An operator may start a fresh edit only after resolving an uncertain record.

### Files touched

| File | Change |
| --- | --- |
| `src/live_keywords.py` | New AWS CLI, backup, invocation, verification, and read-only recovery. |
| `src/lambda_handler.py` | Strict management-event branch using the existing database snapshot and conditional upload path. |
| `src/db.py` | Transactional single-term keyword change with existing validation. |
| `template.yaml` | Declare `RAINCHECK_MANAGEMENT_PROTOCOL=keyword-v1` on the existing Lambda. |
| `tests/test_rc21_live_keywords.py` | Offline CLI, database, Lambda, and recovery tests with fake AWS clients. |
| `tests/test_sam_template.py` | Assert the exact production capability marker. |
| `README.md` | User commands, backup location, busy/uncertain recovery, and deployment prerequisite. |

The Lambda package's existing source allowlist already includes `src/lambda_handler.py` and `src/db.py`, so the local CLI is not packaged into Lambda.
No change to `src/schema.sql`, `src/catalog.json`, or requirements is planned.
The new CLI requires deployment of the reviewed Lambda code and capability marker before its first production use.

### Schema changes and migration

None.
The existing schema version 4 database and its listing and pricing history remain intact.

### Interfaces

`db.change_keyword(item_id: int, action: str, kind: str, keyword: str) -> bool` returns `True` for a real change and `False` when the requested state already exists.
It raises `ValueError` for an unknown item, invalid action or kind, blank term, include/exclude overlap, or removal of the last include.
It performs the read, validation, update, and ownership change in one SQLite transaction and closes its connection on every path.

`src/live_keywords.py` exits 0 for a verified edit or no-op, 2 for command syntax, and 1 for preflight, busy, stale, verification, or uncertain failures.
Its user-facing output identifies the item, exact changed term, backup path, original and new version IDs, and final status without printing secrets or database contents.
`recover` makes only AWS reads and may update its local operation record after proof; it names the next manual action when it cannot establish the outcome.

## Acceptance criteria

1. `list` shows the production item's include and exclude keywords in stable order without invoking Lambda, changing S3, or changing the schedule.
2. Adding or removing one term changes only that term and sets that item's `config_source` to `cli`; all other item fields, other items, listings, matches, and eBay history are byte-for-byte equivalent at the SQLite row level.
3. A duplicate add or removal of an absent term reports no change, preserves `config_source`, and makes no Lambda invocation or S3 upload.
4. Keyword normalization matches existing `db.py` rules; a blank term, overlap between sets, unknown item, or removal of the last include fails without an upload.
5. The command refuses the wrong AWS account, Region, missing or mismatched stack outputs, unexpected function database settings, absent or wrong capability marker, or reserved concurrency other than one before any mutating Invoke.
6. Before invoking Lambda for a real edit, the command has a verified local backup outside the repository and a durable operation record containing the original version ID and ETag, with `dispatched_at` recorded before dispatch.
7. A valid management request never runs pricing, scraping, SSM credential loading, or email; an event without `raincheck_management` follows the existing daily path, and a malformed or unknown management marker fails before any AWS call.
8. A running Lambda invocation causes the synchronous management Invoke to fail busy without changing the database; the CLI gives retry guidance and leaves the schedule state untouched.
9. If S3's current VersionId differs from the backup version, or `IfMatch` returns 412 or 409, the Lambda makes no successful upload and reports a stale/conflict result without blind retry.
10. A successful edit returns a new VersionId; the CLI retrieves that exact version, verifies checksum, schema, integrity, foreign keys, and the expected row-level delta, then marks the record verified.
11. A function error with HTTP 200, service failure, timeout, lost response, failed exact-version read, or failed post-upload check never prints success; the record remains available for read-only recovery.
12. `recover` finds an operation-tagged version through paginated S3 version listing and verifies its expected row delta before reporting `applied`, separately showing the latest keyword state; it reports `not applied` only with a recorded no-upload response, and otherwise remains `uncertain` without a receipt; it never invokes Lambda or uploads.
13. The schedule configuration and enabled or disabled state are identical before and after every command.
14. All tests use temporary SQLite files and fake AWS responses, with no network, SMTP, real database, or production AWS access.

## Edge cases

- Empty results: `list` prints a clear no-items result; an unknown item ID fails before edit.
- `None` or missing values: missing object VersionId, ETag, stack output, function setting, or malformed Lambda response fails closed.
- Network failure or bad HTTP status: preflight fails without a write; an Invoke or verification failure after dispatch is marked uncertain until `recover` inspects S3.
- Malformed input: a dictionary management event with extra fields, wrong types, or unknown actions cannot reach the management write path.
  Non-dictionary events retain the existing daily-job behavior.
- Duplicates: normalized duplicate adds and absent removals are no-ops; a repeated request after an uncertain outcome is inspected through `recover`, not automatically resent.

## Test plan

Use a seeded schema version 4 temporary database with two items and representative listing, match, and eBay history rows.
Use fake STS, CloudFormation, Lambda configuration and concurrency, Lambda Invoke, and versioned S3 clients; no saved HTML or external API JSON is needed.
Inject an absent capability marker, a running-job throttle, a stale version, equal-ETag but changed VersionId, S3 412 and 409, HTTP 200 with `FunctionError`, timeout after upload, crash after dispatch, corrupted backup, corrupted uploaded version, paginated versions, and a later valid job version.
Assert exact database rows, backup contents, operation-record state, no-op call counts, unchanged schedule calls, closed database connections, and no email or job calls.
Run the full offline test suite and the normal packaging checks before any reviewed deployment.

## Shared-file conflicts

This touches `src/db.py`, so do not run it in parallel with another feature editing that file.
It does not touch `src/schema.sql` or `src/catalog.json`.

## Open questions

None required for the proposed scope.
The substantive design choice is to leave the daily schedule untouched and serialize edits through the existing Lambda execution slot.
This avoids schedule-update races and preserves its original settings; a running job makes an edit fail fast and the user retries later.
If the user wants a command that waits for a running job or always pauses the schedule, that is a larger operator workflow and should be a separate item.

### Resolved

- **2026-11-05:** The user approved this written spec, including individual keyword commands, verified backups, edits through the existing Lambda execution slot, an unchanged daily schedule, and explicit recovery for uncertain results.
  Tests and implementation may proceed; production deployment remains a separate reviewed step after the pull request.

### Amendments

- **2026-11-05:** Named the operation receipt's S3 metadata key `raincheck-operation-id` so independent tests and implementation use the same wire convention.
  This clarifies the approved receipt design without changing its behavior or scope.
- **2026-11-05:** Clarified that management-mode S3 uploads also disable SDK retries, preserving the approved uncertain-outcome behavior if an upload succeeds but its response is lost.
  The existing daily-run retry configuration is unchanged.
- **2026-11-05:** Clarified that legacy non-dictionary invocation payloads retain the daily-job behavior already covered by RC-11 tests.
  Strict validation applies to dictionaries with a management marker, preserving the approved compatibility rule.
- **2026-11-05:** Review requires independent verification of the allowed row delta, rejection of a mismatched configured profile Region before applying the fixed client Region, and private local backup permissions.
  The command ensures its backup directory is `0700` and creates backups and journals with `0600` permissions, including when the directory already exists.

### Verification

- **2026-11-05:** All 881 offline tests passed without warnings, and the fresh reviewer approved the fixes.
  End-to-end verification exercised the actual CLI and Lambda handler with fake AWS boundaries and a fresh database containing two items, one listing, one match, and two eBay price observations.
  Add, remove, no-op, busy, stale-version, protocol refusal, and lost-response recovery checks passed while preserving history.
  Source-only package staging passed; SAM is not installed locally, and no deployment package was published or deployed.
  The existing production Lambda must receive the reviewed code and capability marker before live edits are enabled.

## Rollback

Keep the local verified backup and original S3 VersionId from the operation record.
If the new keyword is wrong, use the same command to reverse that single term, creating another backup and verified version without discarding later listing history.
If the uploaded database itself is corrupt, stop further jobs and follow the existing RC-11 version-restore runbook, comparing newer versions first so a restore does not erase listings or prices added after the backup.
Code rollback is a reviewed Lambda deployment of the prior package; do not roll back the database merely to undo code.

AWS behavior checked against the official [Lambda Invoke API](https://docs.aws.amazon.com/lambda/latest/api/API_Invoke.html), [synchronous invocation guide](https://docs.aws.amazon.com/lambda/latest/dg/invocation-sync.html), [reserved concurrency guide](https://docs.aws.amazon.com/lambda/latest/dg/lambda-concurrency.html), [S3 conditional writes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html), [S3 versioned reads](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetObject.html), and [version listing](https://docs.aws.amazon.com/AmazonS3/latest/API/API_ListObjectVersions.html).
