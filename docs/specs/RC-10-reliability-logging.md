# RC-10: Reliability and logging

**Status:** Draft
**Backlog item:** RC-10 in `docs/BACKLOG.md`

## Summary

Retry transient HTTP failures within a small, explicit time budget, report job outcomes through Python logging, and continue independent item work when one item fails.
Keep one Yahoo digest per run and preserve the database rule that a deal is recorded only after a successful send.

## Motivation

The backlog's claim that no network call has a timeout is stale.
The eBay token and Browse calls already pass 30 seconds (`src/ebay_pricing.py:33-35,71-76`), Yahoo passes 30 seconds (`src/yahoo_scrape.py:114-121`), Frankfurter passes 10 seconds (`src/yahoo_scrape.py:60-63`), and SMTP passes 30 seconds (`src/sendemail.py:60`).
The human-only fixture capture requests also pass 30 seconds (`scripts/capture_fixtures.py:108-128`).
None of these HTTP calls retries 429 or 5xx responses.
An eBay search error currently stops later items (`src/ebay_pricing.py:134-140`), and the jobs still use `print` for operational status (`src/ebay_pricing.py:120-166`, `src/yahoo_scrape.py:300-345`, `src/sendemail.py:65-68`).
The Yahoo job sends one digest, then records each shown deal in a separate transaction (`src/yahoo_scrape.py:329-342`), so a later insert failure leaves only part of the digest marked seen.

The [eBay REST guide](https://developer.ebay.com/develop/guides/sell/using-ebay-restful-apis) documents 429 for request limits and 500/502 for server failures.
The [eBay Browse API page](https://developer.ebay.com/develop/api/buy) documents the application token required for Browse search.
[Frankfurter's Python documentation](https://frankfurter.dev/python/) confirms the flat single-rate JSON response and the `/v2/rate/{base}/{quote}` path already used by the rate fetcher.
[RFC 9110](https://www.rfc-editor.org/rfc/rfc9110.html#section-10.2.3) defines `Retry-After` as delay seconds or an HTTP date.
The [Requests documentation](https://requests.readthedocs.io/en/latest/user/advanced/#timeouts) explains that a request timeout limits socket inactivity, rather than the complete elapsed job time.
The [Python SMTP documentation](https://docs.python.org/3/library/smtplib.html#smtplib.SMTP.sendmail) describes `sendmail` errors and its return value, but SMTP cannot atomically commit a SQLite transaction with message delivery.

## Scope

**In scope:**

- Preserve and test explicit timeouts on every production HTTP request and SMTP connection.
- Add one shared, testable HTTP retry policy for the eBay token POST, eBay Browse GET, Yahoo page GET, and Frankfurter rate GET.
- Retry only HTTP 429 and 500 through 599, with bounded backoff and bounded `Retry-After` handling.
- Continue eBay work after an isolated item search, parse, or item transaction failure under the recommended failure policy below.
- Continue Yahoo deal evaluation for other items after an isolated item calculation failure while preserving the one-digest rule.
- Make shown-deal recording one SQLite transaction after a successful Yahoo send.
- Replace operational `print` calls in the two jobs and SMTP sender with module loggers, while keeping dry-run email output and intentional database CLI messages.
- Give both job entry points deterministic success and failure exit codes and a concise final summary.
- Update user documentation and affected tests.
- Align `AGENTS.md` and `CLAUDE.md` with the current merged RC-06 state and catalog-as-seed behavior, without changing project workflow rules.

**Out of scope:**

- Changing the price estimator, listing match rules, Yahoo JSON parser, exchange-rate fallback value, digest format, or SMTP provider.
- Retrying SMTP delivery, including a timeout after `sendmail` starts, because delivery may already have happened.
- Concurrent runs, distributed locks, or guaranteed exactly-once email delivery across SMTP and SQLite.
- A schema migration, persistent retry queue, dead-letter table, or cloud-specific logging format.
- Changing `src/setup.py`, `src/digest.py`, `src/matching.py`, or the human-only `scripts/capture_fixtures.py`.
- Retrying malformed JSON, malformed Yahoo HTML, HTTP 400/401/403/404, catalog validation failures, or SQLite schema errors.
- Raising the current one-page eBay search limit or Yahoo page size.

## Design

### Files touched

| File | Change |
| --- | --- |
| `src/http_retry.py` (new) | Shared HTTP request wrapper with bounded status retries, `Retry-After` parsing, and injected sleep/clock hooks for offline tests. |
| `src/ebay_pricing.py` | Use the retry wrapper and module logger; isolate item failures; return a run summary; set CLI exit status. |
| `src/yahoo_scrape.py` | Use the retry wrapper for Yahoo and Frankfurter; isolate per-item deal calculation; call the batch recorder; log run outcomes and set CLI exit status. |
| `src/sendemail.py` | Log real SMTP success/failure without message bodies or credentials; preserve the exact dry-run content on stdout and the `bool` return contract. |
| `src/db.py` | Add an atomic batch recorder for displayed deals; remove the operational print from the direct `update_market_price` compatibility setter. |
| `tests/test_http_retry.py` (new) | Retry attempts, wait bounds, `Retry-After`, non-retryable errors, and timeout propagation with fake responses and injected sleep. |
| `tests/test_ebay_pricing.py` | Per-item failure isolation, persisted price invariants, log records, and CLI exit status. |
| `tests/test_yahoo_scrape.py` | Shared-page retry, per-item calculation isolation, one digest, atomic post-send writes, logs, and CLI exit status. |
| `tests/test_sendemail.py` | SMTP logs and unchanged dry-run output using fake SMTP. |
| `tests/test_db.py` | Batch recorder atomicity, deduplication, rollback, and connection closure on failure. |
| `tests/test_listing_matches.py` | Preserve the single-listing recorder contract and verify equivalent batch association behavior. |
| `tests/conftest.py` | Extend fake HTTP responses with headers and ordered response sequences for retries. |
| `README.md` | Explain retries, partial-run exit status, logs, and post-send duplicate-delivery limit. |
| `docs/BACKLOG.md` | Correct RC-10's stale timeout claim and list the concrete touched modules. |
| `AGENTS.md` | Correct the current schema version and merged-feature list to include RC-06. |
| `CLAUDE.md` | Clarify that `src/catalog.json` is seed input and jobs read tracked-item configuration from the database; align the current schema and merged-feature facts with `AGENTS.md`. |

Existing tests that assert exact operational `print` output in `tests/test_ebay_pricing.py`, `tests/test_yahoo_scrape.py`, `tests/test_sendemail.py`, and `tests/test_db.py` must be updated to assert logging or preserved intentional output.
The fixture capture script's comment claiming the token POST lacks a timeout is stale, but editing that human-only script is outside this pull request.
If the user wants that comment corrected here, amend this file list before implementation.

### Schema changes and migration

None.
`src/schema.sql`, `SCHEMA_VERSION = 2`, and `MIGRATIONS` remain unchanged.
An existing schema version 2 `raincheck.db` needs no migration or seed step.
Agents must use `RAINCHECK_DB_PATH` with a throwaway database and must not open the user's real database.

### Interfaces

`http_retry.request_with_retry(method, url, *, timeout, sleep=time.sleep, now=...)` accepts the existing Requests keyword arguments and returns the final `requests.Response`.
It does not parse a body or call `raise_for_status` for the caller.
The wrapper makes at most three total attempts for a response with status 429 or 500 through 599.
For no usable `Retry-After`, waits are 0.5 seconds before attempt 2 and 1.0 second before attempt 3.
For a valid `Retry-After`, use the larger of that wait and the normal backoff, but only when it is at most five seconds.
If the server asks for more than five seconds, do not retry that response; return it so the caller raises its HTTP error.
An invalid, negative, non-integer, or past-date header is ignored and normal backoff applies.
The cumulative planned sleep for one request is at most ten seconds.
Each individual attempt keeps its existing timeout: 30 seconds for eBay and Yahoo, 10 seconds for Frankfurter.
Requests transport exceptions propagate without retry, including connection and read timeouts, to avoid multiplying the worst-case elapsed time.
The token POST is eligible only for response-status retries; do not retry a transport exception after an unknown server outcome.
The wrapper must not log request headers, token bodies, raw URLs with query strings, seller titles, response bodies, or credentials.

`refresh_market_prices() -> RunResult` processes active items in database order after one token request.
`RunResult` reports succeeded item IDs, failed item IDs, and a boolean `had_failure`.
A token failure, missing credential, or schema mismatch is a run-level failure before item searches.
For an item search HTTP error, transport error, malformed response container, or item-specific transactional writer error, log the item ID and a short exception class/status, leave that item's existing price and history as before the item attempt, and continue according to Open question 1.
An item with an empty or insufficient valid result is a successful processed item with its current price preserved.
The CLI logs a final count of succeeded and failed items and exits 1 if any item or run-level step failed; otherwise it exits 0.

`record_listing_matches(deals: Sequence[Deal]) -> int` stores all displayed listing rows and their item associations in one `BEGIN IMMEDIATE` transaction and returns the number of newly inserted listing rows.
For this interface, `Deal` means the narrow immutable record supplied by `digest.Digest.deals`: `brand_id`, `url`, `title`, `price_yen`, `image_url`, and `item_id` are the fields read by the writer.
`src/db.py` must not import `src/yahoo_scrape.py`; it may import the pure `digest.Deal` type or accept this structural field contract, so the existing dependency direction remains acyclic.
It must preserve the current `record_listing_match` behavior for an existing URL and an additional item association.
If any insert fails, roll back every listing and match from that batch, close the connection, and propagate the error.
The Yahoo job calls it only after a `True` send result, exactly once per digest.
The existing single-deal `record_listing_match` remains available for callers.

The Yahoo main flow fetches and parses the shared page before any new-deal recording or send.
Page fetch, page parse, `update_last_seen_at`, known-URL lookup, and digest send are run-level steps.
If the page fetch or parse fails after retries, no email or listing write occurs.
After the shared page and rate are ready, process items in database order, catching only item-local `ValueError` or `TypeError` from deal calculation; a failing item contributes no section and does not claim any listing URL.
The first successful item that matches a listing retains the existing claim priority, including deals hidden by its top-five limit.
Build and send at most one digest from successful item sections.
If any item failed, report partial failure with exit 1 even when the other sections were emailed successfully.
If the send returns `False`, exit 1 and record no new deals.
If the post-send batch transaction fails, exit 1 and leave the entire digest unrecorded.

Use `logging.getLogger(__name__)` in modules.
At a CLI entry point, configure the root logger once at INFO level with a stderr handler only if it has no handlers; if the host already installed handlers, leave its configuration and propagation unchanged.
Repeated `main()` calls must not add handlers or duplicate emitted records.
INFO covers job start, counts, price updates, no-deal outcome, successful send, and final summary.
WARNING covers retries, exchange-rate fallback, skipped malformed records, and one item failure while work can continue.
ERROR covers terminal job failures.
Log only service name, item ID or configured item name, attempt number, status code, count, and exception class; do not log raw response text, eBay authorization headers, email addresses, email body, full seller titles, or URLs.
Keep `send_email()` dry-run's exact subject, text, and HTML block on stdout because that is its intentional product output.
Keep `db.py` CLI initialization and seed messages, argument help, and actionable schema errors as CLI output; they are not part of the unattended jobs' operational logs.

#### Delivery limitation and proposed amendment

RC-15's approved design explicitly chose at-least-once email delivery when an SMTP send succeeds but a later database write fails.
The atomic batch removes partial recording, but SQLite cannot prove whether an SMTP server accepted a message if the process crashes or the socket times out near delivery.
RC-10 therefore must not promise zero duplicate emails across runs under that failure.
Proposed RC-15 amendment, dated 2026-10-27: replace separate per-deal post-send inserts with one batch transaction while retaining the approved at-least-once delivery policy and one digest per run.
The user must approve this amendment as part of approving RC-10.
Proposed RC-06 amendment, dated 2026-10-27: replace criterion 18's stop-later-items outcome after a failed search with the RC-10 item-isolation policy selected in Open question 1, while retaining its no-write rule for the failed item and all earlier committed observations.

## Acceptance criteria

1. A successful first HTTP response produces exactly one request and zero sleeps for each of the eBay token, eBay Browse, Yahoo, and Frankfurter calls, with their existing 30, 30, 30, and 10 second timeouts.
2. For each HTTP call, two 503 responses followed by 200 produce three total attempts and waits of 0.5 and 1.0 seconds, then the original parser receives only the successful response.
3. HTTP 429 and each 500 through 599 status are retryable; HTTP 400, 401, 403, and 404 produce one attempt and no sleep, and the caller's `raise_for_status` raises.
4. A retryable response with `Retry-After: 2` sleeps 2 seconds before the next attempt; a valid future HTTP-date sleeps the corresponding nonnegative number of seconds, with a frozen clock in the test.
5. A retryable response with `Retry-After: 6` makes one attempt, does not sleep, and surfaces the original HTTP error; invalid or past-date headers use the normal bounded backoff.
6. Three consecutive retryable error responses make exactly three attempts and two sleeps, then surface the final status as an HTTP error without parsing its body or writing item data.
7. A `requests.Timeout` or `requests.ConnectionError` on any HTTP attempt propagates without an automatic second attempt; eBay item work is handled according to the item failure policy and Yahoo shared-page failure sends no digest.
8. A token request failure or absent token credential makes no Browse calls or price writes, logs one terminal failure without credential values, and makes the eBay CLI exit 1.
9. When the first eBay item has a completed valid search and the second item's final search status is 500, the first item's listing observations and price remain committed, the second item's observations and price remain unchanged, and the final CLI status is 1.
10. Under the recommended Open question 1 policy, a third eBay item after that failed second item is searched and its valid observations are committed; one warning names the failed item ID and HTTP status, and the final summary reports two succeeded and one failed.
11. A malformed eBay response container or failed `record_ebay_listing_prices` transaction is logged as a failure for that item, changes no rows for that item, and does not alter earlier successful items.
12. An eBay item with empty `itemSummaries` or fewer than five retained observations is counted as succeeded, preserves its current price, and does not trigger a nonzero exit by itself.
13. A 429 whose `Retry-After` exceeds the five-second cap stops that service's remaining calls under the recommended Open question 2 policy and exits 1, preserving previously committed item results.
14. A Yahoo page that returns 503, then a valid 200 page, is parsed once after the retry and produces at most one digest send and one batch record call.
15. A Yahoo page whose final response is 503, whose transport fails, or whose `__NEXT_DATA__` structure is malformed sends no email, adds no listing or match rows, does not refresh `last_seen_at`, and makes the CLI exit 1.
16. A Frankfurter 500 followed by a valid 200 uses the provider rate with no fallback warning; three 500s or a transport failure logs one fallback warning and uses the existing constant once for all items.
17. A malformed or empty Yahoo listing array keeps the parser's existing distinction between valid no-results and layout drift; the retry wrapper does not re-fetch after a 200 page with malformed HTML.
18. With three tracked items and one item-local deal calculation error, the two valid items contribute sections in their original order to one digest, the failed item contributes none, and the run exits 1 after the send and successful batch record.
19. A failed item does not claim a listing URL, so a later healthy item can include that listing; a healthy earlier item's claim still blocks duplicates in later sections even when the first item's top-five limit hid it.
20. When the digest has two shown deals and a fake SMTP send returns `False`, the job calls send exactly once, inserts no new listing or match rows, logs the failure, and exits 1.
21. When the SMTP send succeeds and the second batch insert is forced to fail, the batch rolls back both deals and matches, closes the connection, logs a terminal post-send error, and exits 1.
22. A subsequent successful run after criterion 21 may offer those same deals again under the approved at-least-once policy; it sends at most one digest and records both deals together.
23. Duplicate URLs within a displayed batch do not create duplicate listing rows or duplicate `(listing_id, item_id)` matches; an existing listing can gain a different item's match while its original metadata stays unchanged.
24. In dry run, no SMTP object is constructed, no email credentials are required, and the existing full subject, text, and HTML block remains on stdout; a successful dry run still records displayed deals in the throwaway database exactly as the current approved behavior specifies.
25. Operational status in the eBay and Yahoo jobs and real SMTP sender is emitted through `logging` with levels and fields above; logs contain no bearer token, client secret, SMTP password, email address, response body, raw seller title, or full URL.
26. A fully successful job, including no active items or no new Yahoo deals, exits 0; any terminal network, parser, schema, SMTP, batch-record, or isolated item failure exits 1, with no unhandled traceback at the CLI boundary.
27. Calling either job's `main()` twice in one process does not add duplicate logging handlers or duplicate each emitted record.
    With a preinstalled root handler, the job adds no handler, leaves its level intact, and emits through that handler once, so a future Lambda host can own logging configuration.
28. `db.py --seed` and `db.py` retain their deliberate human-facing CLI output and exit behavior; `update_market_price` emits no direct operational print.
29. `AGENTS.md` and `CLAUDE.md` both state that the current schema version is 2 and RC-06 is merged, and both describe `src/catalog.json` as seed input while the jobs read tracked items from SQLite; their feature workflow rules remain unchanged.

## Edge cases

- Empty results: an empty eBay search or valid empty Yahoo page is a successful no-work outcome; a malformed empty Yahoo placeholder is an error.
- `None` or missing values: existing eBay row validation and manual price fallback remain; a missing SMTP credential fails the job without printing its value.
- Network failure or bad HTTP status: only 429 and 5xx responses retry; request transport errors do not retry; exhausted attempts do not write incomplete search data.
- Malformed input: bad eBay response containers and Yahoo HTML do not retry; invalid `Retry-After` uses normal backoff.
- Duplicates: the eBay listing primary key remains the pricing deduplicator, and the Yahoo batch recorder deduplicates URL and match keys within one transaction.
- SMTP uncertainty: after delivery but before database commit, the next run may resend the digest; this is an explicit at-least-once behavior.
- Multiple runs: concurrent schedulers can still each choose and send the same unrecorded deal.

## Test plan

Use `tests/conftest.py`'s offline network block, temporary database, and fake SMTP.
Extend its fake HTTP response only with header access and ordered responses; do not use live requests or real credentials.
Reuse `tests/fixtures/ebay/token.json`, `search_empty.json`, `search_ramones.json`, and the saved Yahoo minimal and malformed HTML.
Build small synthetic responses for 429, 500, 503, `Retry-After` seconds and HTTP dates, malformed eBay containers, and one item-specific calculation exception.
Inject sleep and a frozen UTC clock into the retry helper so tests run without real waiting.
Use seeded temporary schema version 2 data for price and listing history assertions and failure injection during the second batch insert.
Assert the sent digest count and content, all relevant database rows, `caplog` records, stdout dry-run block, and CLI `SystemExit` codes.
Run only the normal offline suite during implementation and verification; never run fixture capture or the jobs against the user's real database.

## Shared-file conflicts

RC-10 touches `src/db.py` but does not touch `src/schema.sql` or `src/catalog.json`.
Do not implement it in parallel with another feature that edits `src/db.py`, `src/schema.sql`, or `src/catalog.json`.
The current main branch has RC-06's schema version 2 and permanent eBay listing pool; `AGENTS.md` still describes version 1 and an unmerged RC-06, while `CLAUDE.md` still describes `src/catalog.json` as the tracked-item list without the seed-only distinction.
The documentation alignment in this feature must use current code and the approved RC-06 spec as the factual source.

## Open questions

1. **Which eBay item failures may continue to later items?**
   Recommendation: after final 5xx, transport error, malformed result, or item transaction failure, log the item, preserve its data, continue later items, and exit 1 at the end; keep token, schema, and missing-credential failures run-level.
   This fulfills RC-10's per-item isolation but supersedes RC-06 criterion 18 and its approved `failed search stops later items` rule.
   The alternative preserves RC-06's stop rule and leaves the main RC-10 reliability goal only partly met.
2. **How should a long eBay 429 affect later item searches?**
   Recommendation: when a `Retry-After` exceeds the five-second wait cap or the final retry is still 429, stop later eBay searches and exit 1 so the job does not keep hitting a limited API.
   Continuing maximizes per-item progress but may spend more of the eBay request quota and ignore an explicit server delay.
3. **Should RC-10 retain RC-15's at-least-once email policy?**
   Recommendation: yes; send once per run, record the whole digest atomically after a successful send, and accept possible resend after a crash or post-send database failure.
   Pre-recording would avoid some duplicate emails but could permanently lose deals when SMTP fails; guaranteed exactly-once delivery needs a larger design with an external idempotent delivery service or a user-visible recovery process.

## Rollback

Revert the RC-10 code and documentation commit if its retry or failure policy causes trouble.
There is no schema migration to reverse, and existing schema version 2 data remains readable.
If a partially failed eBay run has already committed some item prices, a code rollback does not undo those legitimate committed observations; inspect the affected item pool on a throwaway copy before any manual repair.
If an email was accepted but the batch record failed, expect its deals to be eligible again on the next run under the approved delivery policy.
