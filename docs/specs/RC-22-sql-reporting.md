# RC-22: SQL reporting and data-quality commands

**Status:** Approved
**Backlog item:** RC-22 in `docs/BACKLOG.md`

## Summary

Add a read-only command line tool, `python3 src/reports.py`, with three commands: `report` (deal statistics per brand), `trends` (recent eBay price observations per item with running changes and a rolling average), and `doctor` (data-quality checks).
Schema version 5 starts recording the deal-time pricing of every recorded deal on `listing_matches`, so `report` can show real discounts.
The user writes every report, trend, and doctor query by hand as SQL practice; this spec defines what each query must return, not how to write it.

## Motivation

RainCheck has collected listings, matches, and eBay price observations since RC-04 and RC-06, but the only way to look at them is ad hoc `sqlite3` sessions.
The user is learning SQL and wants real questions about real data to practice JOIN, GROUP BY, HAVING, LEFT JOIN with IS NULL, ROW_NUMBER, LAG, and AVG() OVER with a frame.

Three facts about today's data shape this design.

1. **Discounts are not stored anywhere.**
   `find_item_deals` computes the market price, the JPY-USD conversion, and the percent below market in memory (`src/yahoo_scrape.py:296-347`).
   `record_listing_matches` stores only `listings` metadata and `(listing_id, item_id, matched_at)` (`src/db.py:957-1023`).
   For an `auction_buy_now` deal, `listings.price_yen` holds the current bid (`src/db.py:995-1006`), while the discount was computed from the buy-now price (`src/yahoo_scrape.py:319-335`), so a discount cannot be reconstructed from `listings` either.
   The rate comes from Frankfurter or the 0.0062 fallback (`src/yahoo_scrape.py:120-129`), and which one was used is only logged.
2. **Auction watches are stored exactly like deals.**
   RC-16 records every displayed digest row, including provisional auction watches whose percentage is only a potential discount at the current bid (`docs/specs/RC-16-auction-aware-deals.md`, decisions at lines 264 and 340; `src/yahoo_scrape.py:587`).
   Nothing in `listings` or `listing_matches` says which rows were watches.
   A "deal" report over today's tables would silently count ¥1 opening bids as 99.9% discounts.
3. **`ebay_listing_prices` is not a price history of `items.market_price_usd`.**
   Each row is the first-seen asking price of one newly discovered eBay listing (`docs/specs/RC-06-market-price-history.md:28,103`).
   The market price itself is a MAD-filtered mean recomputed over the whole pool and overwritten in place (`src/db.py:1130-1159`); past market prices are not stored.
   Every row inserted in one pricing run shares one `first_seen_at` value (`src/db.py:1076-1085,1121-1127`), so time alone does not order rows within a run.

Timestamp text facts that the queries rely on, verified in `src/db.py`:

- Every writer stores `datetime.isoformat()` of a UTC datetime, which always ends in `+00:00` (`src/db.py:906,927,989,1051,1077`).
- The caller-supplied `observed_at` path in `record_ebay_listing_prices` rejects any offset other than zero and re-serializes with `isoformat()`, so it also stores `+00:00` (`src/db.py:1078-1085`).
- `isoformat()` omits the fractional part when microseconds are zero, so both `2026-10-02T00:00:00+00:00` and `2026-10-02T00:00:00.250000+00:00` occur.
  Plain text comparison still orders these correctly, because `+` sorts before `.` in ASCII.
- Text ordering would break for a row written by hand in another form, such as SQLite's `2026-10-02 00:00:00` or a `Z` suffix; `doctor` detects such rows.

## Scope

**In scope:**

- Schema version 5: six nullable deal-time columns on `listing_matches`, written when a deal is recorded, plus at most one index chosen by the EXPLAIN QUERY PLAN process below.
- A versioned, all-or-nothing migration 5 and a backup-first upgrade procedure for the production database.
- Recording the deal kind, compared price, effective market price, exchange rate, fallback flag, and discount for every newly recorded listing-item pair.
- A read-only database connection helper that refuses an outdated schema like the other entry points.
- `src/reports.py` with `report`, `trends`, and `doctor`, aligned text output, and `--json` output.
- Query files under `src/sql/`, one statement each, written by the user; the implementer provides only headers.
- A deterministic benchmark database builder and an evidence document for the index decision.
- README documentation for the commands, the query files, and the version 5 upgrade.

**Out of scope:**

- Any SQL text for report, trend, or doctor queries in this spec, in the implementer's diff, or in review suggestions; reviewers point to the checklist item instead of rewriting a query.
- Backfilling deal-time columns or deal kinds for rows recorded before version 5 (see Open question 2).
- Storing a history of `items.market_price_usd` or replaying the MAD filter over past pools.
- Changing deal selection, the digest, the eBay pricing algorithm, or matching.
- Downloading the production database from S3 inside `reports.py`; reports read a local file.
- Adding `reports.py` or `src/sql/` to the Lambda artifact (`Makefile` allowlist stays unchanged).
- Charts, CSV export, scheduling reports, or emailing them.
- Changing `db.record_listing_match` (the single-pair RC-04 API, unused by the jobs); it keeps writing `NULL` deal-time columns, which the constraints allow.
- Validating calendar correctness of timestamps (for example month 13); `doctor` checks text shape only.

## Design

### Who writes what

This feature changes the normal workflow in one place, because the user writes the queries.

1. The test-writer writes every test, including query tests with hand-computed expected rows that load the real files in `src/sql/`.
2. The implementer builds schema version 5, deal-time recording, the read-only connection, the `reports.py` harness, the benchmark builder, and one header-only file per query.
   Its done condition is that every test passes except the query tests, which fail with the harness's "no query yet" error.
3. The user writes each query file until its query tests pass, committing on the feature branch.
   Queries can be tried interactively with `sqlite3` and `.parameter set :name value`, because the files use named parameters.
4. The orchestrator reviews the user's SQL against the "Review checklist for the user's SQL" below, explains each finding, and does not rewrite queries.
5. The user and orchestrator run the EXPLAIN QUERY PLAN process; the implementer then adds the chosen index to migration 5 and `src/schema.sql`, or records that none was justified.
6. The reviewer and verifier then run as usual on the complete branch.

Migration 5 has not shipped until the branch merges, so amending it in step 5 does not violate the "never edit a shipped migration" rule in `src/db.py:30`.

### Files touched

| File | Change |
| --- | --- |
| `src/schema.sql` | Schema version 5: six deal-time columns on `listing_matches`; the chosen index, if any. |
| `src/db.py` | `SCHEMA_VERSION = 5`, `MIGRATIONS[5]`, `MIN_POOL_PRICES = 5` (replacing the literal 5 in `record_ebay_listing_prices`), `get_read_only_connection()`, schema checks shared with `get_connection()`, and deal-time recording in `record_listing_matches`. |
| `src/yahoo_scrape.py` | `YenRate` result from `resolve_yen_to_usd_rate`, and forwarding the rate and fallback flag to recording. |
| `src/reports.py` (new) | Command line harness: argument parsing, read-only connection, query loading and execution, column contract checks, text and JSON output, exit codes. |
| `src/sql/report.sql`, `src/sql/trends.sql` (new) | Header comment from the implementer; query from the user. |
| `src/sql/doctor/<check_id>.sql` (new, seven files) | Header comment from the implementer; query from the user. |
| `tests/test_rc22_schema_v5.py` (new) | Migration, constraints, fresh-versus-migrated shape, rollback. |
| `tests/test_rc22_recording.py` (new) | Deal-time recording, validation, rate fallback flag, end-to-end job recording. |
| `tests/test_rc22_reports_harness.py` (new) | CLI, read-only guarantees, schema refusal, formatting, JSON, exit codes, with the query runner stubbed. |
| `tests/test_rc22_queries.py` (new) | The user's queries against hand-built fixture databases with hand-computed expected rows. |
| `tests/rc22_fixtures.py` (new) | Builders for fixture databases F0, F1, and F2 described in the test plan. |
| `tests/tools/build_bench_db.py` (new) | Deterministic benchmark database builder for the index process; not collected by pytest. |
| `tests/fixtures/db/schema_v4.sql` (new) | The pre-RC-22 schema as a migration fixture. |
| `tests/test_listing_matches.py`, `tests/test_rc16_auctions.py` | Pass the new required rate arguments to `record_listing_matches`. |
| `tests/test_yahoo_scrape.py`, `tests/test_lambda_handler.py` | Expect or stub a `YenRate` instead of a float. |
| `tests/test_migration.py`, `tests/test_db_cli.py`, other version-pinned tests | Update assertions pinned to schema version 4. |
| `docs/specs/RC-22-evidence/query-plans.md` (new) | EXPLAIN QUERY PLAN evidence and the index decision. |
| `README.md` | Reports usage, query file workflow, and the version 5 upgrade procedure. |

`src/catalog.json`, `src/digest.py`, `src/lambda_handler.py`, `src/ebay_pricing.py`, `template.yaml`, and `Makefile` are unchanged.
`src/lambda_handler.py` already compares against `db.SCHEMA_VERSION` (`src/lambda_handler.py:91`), so it follows version 5 automatically.

### Schema changes and migration

**Where the deal-time data lives.**
The columns go on `listing_matches`, because a discount belongs to one listing-item pair: it is computed against that item's effective market price (`src/yahoo_scrape.py:296,335`).
One listing matched to two items has two different discounts, so `listings` is the wrong table.
A separate `deal_events` table was considered and rejected: `listing_matches` already has exactly one row per recorded pair, written in the same transaction, and a second table would add a join and a way for the two to disagree.

**New columns**, added in this order at the end of `listing_matches` in both `src/schema.sql` and `MIGRATIONS[5]`, so fresh and migrated databases have identical `PRAGMA table_info`:

| Column | Type | Constraint | Meaning |
| --- | --- | --- | --- |
| `deal_kind` | `TEXT` | `NULL` or one of `fixed_price`, `auction_buy_now`, `auction_watch` | `Deal.kind` at recording time. |
| `deal_price_yen` | `INTEGER` | `NULL` or greater than 0 | `Deal.price_yen`: the fixed price, the buy-now price, or a watch's current bid; the yen amount the discount was computed from. |
| `deal_market_price_usd` | `REAL` | `NULL` or greater than 0 | `Deal.market_price_usd`: the item's effective price (eBay first, manual fallback) used for the comparison. |
| `deal_yen_to_usd` | `REAL` | `NULL` or greater than 0 | The JPY-to-USD rate used for the whole run. |
| `deal_rate_is_fallback` | `INTEGER` | `NULL`, 0, or 1 | 1 when the run used the documented 0.0062 fallback because Frankfurter failed. |
| `deal_discount_pct` | `REAL` | `NULL`, or greater than 0 and less than 100 | `Deal.discount_pct` exactly as computed (unrounded); for `auction_watch` it is the potential discount at the current bid. |

The `deal_` prefix keeps these names distinct from `items.market_price_usd` in joins.
The last column, `deal_discount_pct`, also carries an all-or-nothing check: either all six columns are `NULL` or all six are non-`NULL`.
SQLite allows a column `CHECK` to reference other columns, and placing it on the last added column means every referenced column already exists when the `ALTER TABLE` runs (verified on SQLite 3.49.1).
The discount is stored, not derived in queries, because it is the exact value the email showed; its inputs are stored too so `doctor` can audit it.

**Migration 5** is a tuple of six `ALTER TABLE listing_matches ADD COLUMN` statements, followed by the index statement only if the index process selects one.
It runs inside the existing `_migrate` transaction (`src/db.py:305-350`), so any failure leaves the file at version 4, and the version bump commits with the statements.
Every existing row gets `NULL` in all six columns; no existing value changes.
Migrations 1 through 4 are not edited.
The header comment in `src/schema.sql` and `SCHEMA_VERSION` change to 5 together.

**Index.**
No index is pre-selected.
The process is defined in "Index decision process" below.
If an index is chosen, it is created in both `src/schema.sql` and `MIGRATIONS[5]` with the same name, so the existing fresh-versus-migrated shape comparison (`tests/test_migration.py:34-39`) covers it.

**Production upgrade (after merge, orchestrator with the user, modeled on RC-03a Phase C and RC-16).**
The authoritative database is the versioned S3 object used by the daily Lambda (RC-11, `src/lambda_handler.py`).
If the orchestrator finds that the authoritative copy is still the local `src/raincheck.db`, apply steps U1 to U3 and U6 to that file with a SQLite online backup as in RC-16, and skip U4 and U5.
Subagents never touch the production database.
Each step is run one at a time, every result is shown to the user, and the procedure stops at the first unexpected result.

- **U0.** Confirm RC-21 is merged and no RC-21 keyword operation record is in a `prepared` or `dispatching` state.
  Choose a time well away from the 09:00 `Asia/Tokyo` daily run.
- **U1.** Download the current S3 object version into a new directory under `~/RainCheck-backups` with mode 0700, recording its `VersionId` and `ETag`.
  Require `PRAGMA user_version = 4`, `PRAGMA integrity_check = ok`, an empty `PRAGMA foreign_key_check`, and record row counts for `brands`, `items`, `item_keywords`, `listings`, `listing_matches`, and `ebay_listing_prices`.
  This file is the backup and is never modified.
- **U2.** Copy the backup to a separate working file, then run `RAINCHECK_DB_PATH=<working file> .venv/bin/python src/db.py` from the merged `main`, without `--seed`.
  It prints `Migrated database at ... from schema version 4 to 5.`
- **U3.** On the working file, require `PRAGMA user_version = 5`, `integrity_check = ok`, an empty `foreign_key_check`, unchanged row counts, every preexisting column value equal to the backup's, and all six deal-time columns `NULL` on every existing `listing_matches` row.
  Then run `reports.py doctor`, `reports.py report --min-deals 0`, and `reports.py trends --latest 3` against the working file as a read-only smoke test, and show the output to the user.
- **U4.** Build and deploy the version 5 release through the README's reviewed change-set procedure.
  From this point until U5 completes, a daily run fails at the schema check without changing S3, which is safe and raises the existing error alarm.
- **U5.** Upload the working file with SSE-S3 and `If-Match` set to the `ETag` from U1, record the new `VersionId`, download that exact version, and repeat the U3 integrity, foreign key, and version checks on the downloaded bytes.
  A `412 Precondition Failed` means something wrote the object after U1; stop and restart from U1.
  AWS documents that `If-Match` compares the supplied `ETag` with the current object and fails with 412 on mismatch ([S3 conditional writes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html)).
- **U6.** After the next daily run that records deals, download a copy and confirm that every `listing_matches` row with `matched_at` after the upload has all six deal-time columns non-`NULL`.
  Keep the U1 backup at least until then.

### Interfaces

**`db.get_read_only_connection() -> sqlite3.Connection`**

- Uses `get_db_path()`, so `RAINCHECK_DB_PATH` (including from `.env` via `load_dotenv`) works as for the jobs.
- A missing file raises the same `SchemaVersionError` message as `get_connection()` and creates nothing.
- Opens `Path(path).resolve().as_uri() + "?mode=ro"` with `uri=True`, so special characters in the path are percent-encoded, as in `src/yahoo_scrape.py:415`.
- Then sets `PRAGMA query_only = ON` as a second guard, and `row_factory = sqlite3.Row`.
- Applies the same not-a-database, empty-database, older-version, and newer-version checks as `get_connection()`, sharing one private helper so the messages cannot drift.
- Closes the connection on every error path.

**`db.record_listing_matches(deals, *, sources_by_url=None, yen_to_usd_rate, rate_is_fallback) -> int`**

- `yen_to_usd_rate` and `rate_is_fallback` are required keyword arguments, so a caller that forgets them fails with `TypeError` instead of silently writing `NULL`.
- Before opening a connection, it validates: the rate is a finite number greater than 0 and not a `bool`; `rate_is_fallback` is exactly a `bool`; and for every deal, `kind` is one of the three kinds, `price_yen` is a positive `int` and not a `bool`, `market_price_usd` is finite and greater than 0, and `discount_pct` is finite, greater than 0, and less than 100.
  Any failure raises `ValueError` and writes nothing.
- A newly inserted `listing_matches` row gets all six deal-time values from its deal and the run's rate.
- An existing `(listing_id, item_id)` pair keeps its original values (`ON CONFLICT DO NOTHING`, unchanged): first recording wins, like `ebay_listing_prices`.
- Return value and every other behavior are unchanged.

**`yahoo_scrape.YenRate`** is a frozen dataclass with `rate: float` and `is_fallback: bool`.
`resolve_yen_to_usd_rate(today_utc) -> YenRate` returns `is_fallback=False` with the provider's rate, or logs the existing warning and returns `YenRate(0.0062, True)`.
`main` passes `rate.rate` wherever a float is used today and passes both fields to `record_listing_matches`.
The module-level `yahoo_scrape.record_listing_matches` wrapper forwards the new keyword arguments.
Dry runs record deal-time values, because dry runs already record deals (RC-15 decision 8); previews still record nothing.

**`python3 src/reports.py <command> [options]`**

A separate module, not subcommands of `src/setup.py`, because `setup.py` manages item configuration and writes, while every report command is read-only; RC-21 is also editing management tooling now.

| Command | Options |
| --- | --- |
| `report` | `--min-deals N` (integer, 0 or more, default 1); `--since YYYY-MM-DD` (optional UTC calendar date); `--json` |
| `trends` | `--latest N` (integer 1 to 1000, default 10); `--window W` (integer 1 to 1000, default 5); `--item ID` (optional positive integer); `--include-inactive`; `--json` |
| `doctor` | `--json` |

- Argument errors, including out-of-range integers and a `--since` that is not exactly a valid `YYYY-MM-DD`, exit 2 through `argparse` before any database access.
- `--help` exits 0 without opening a database.
- Every command opens the database only through `db.get_read_only_connection()`.
- A missing database, wrong schema version, non-database file, missing or header-only query file, more than one statement in a file, a column contract mismatch, or any `sqlite3.Error` prints one `Error: ...` line on stderr and exits 1.
- The header-only message is `Error: src/sql/<name>.sql has no query yet.` (with the path relative to the repository).
- A successful `report` or `trends` exits 0, including when there are no rows.
- `doctor` exit codes are in its section below.

**Query files.**
Each file in `src/sql/` holds exactly one `SELECT` statement (a leading `WITH` is allowed), uses only the named parameters listed for it, and must return exactly the listed columns in the listed order.
The harness reads files as UTF-8 at run time from `reports.SQL_DIR`, which tests can repoint.
It detects a header-only file with `sqlite3.complete_statement` on the text after comments are stripped, and treats it as "no query yet".
It compares `cursor.description` names with the contract and reports both lists when they differ.
It passes parameters as a dict, so a query cannot be built by string formatting, and every value is bound.
The implementer's header comment in each file copies the question, parameters, columns, ordering, and NULL rules from this spec, ending with `-- Write your query below.`

**Text output.**
Columns are separated by two spaces, and each column is as wide as its longest cell or header.
Header cells are the exact column names, followed by a row of `-` characters as wide as each column.
Text columns are left-aligned; numeric columns are right-aligned.
Display width counts East Asian Wide and Fullwidth characters as 2, using `unicodedata.east_asian_width`, so Japanese item names line up.
Runs of whitespace inside text cells collapse to one space, as `_display_title` does in `src/digest.py:88-90`.
SQL `NULL` prints as `-`.
Percent columns print with exactly one decimal, USD columns with exactly two, and `change_usd` with an explicit sign for nonzero values (`+30.00`, `-40.00`, `0.00`).

**JSON output.**
`--json` prints one object, indented 2 spaces, with `ensure_ascii=False`.
`report` and `trends` print `{"command": ..., "parameters": {...}, "rows": [...]}`, where each row is an object keyed by column name in column order and `NULL` is `null`.
Numbers are the values the query returned (already rounded by the query).
`parameters` echoes the effective values, including defaults, with `null` for omitted optional ones.

#### `report`: deals per brand

**Question.**
For each brand, how many deals did RainCheck record, how good were they, and when was the latest one?

**Definitions.**

- A *recorded pair* is a `listing_matches` row: one listing shown for one item in a digest that was sent or printed by a dry run.
  It is not proof of a successful delivery for dry runs; the report header line says "recorded".
- A *deal* is a recorded pair whose `deal_kind` is `fixed_price`, `auction_buy_now`, or `NULL`.
  `NULL` covers pairs recorded before version 5; some of those may have been auction watches, which cannot be known (Open question 2).
- A *watch* is a recorded pair whose `deal_kind` is `auction_watch`.
  Watches are counted separately and never contribute to discount statistics or the last deal date, because their percentage is only potential.
- A *priced deal* is a deal whose `deal_discount_pct` is not `NULL`.
- Pairs are counted, not distinct listings: one listing recorded for two items counts as two deals, each with its own discount.
- A pair belongs to the brand of its **item** (`items.brand_id`), because the discount is item-relative.
  `listings.brand_id` is not used; `doctor` reports pairs where the two disagree.
- Inactive items' pairs count: deactivating an item does not erase its history.
- `--since D` keeps only pairs whose `matched_at` is on or after 00:00:00 UTC on date `D`.
  This filter applies to individual pairs before grouping.
- `--min-deals N` keeps only brands whose `deals` value is at least `N`.
  This filter applies to groups after counting.
  With `--min-deals 0`, every row in `brands` appears, including brands with no items or no pairs.

**Parameters:** `:min_deals` (integer), `:since` (text `YYYY-MM-DD`, or `NULL` when omitted).

**Columns, in order:**

| Column | Type | Rule |
| --- | --- | --- |
| `brand` | TEXT | `brands.name`. |
| `deals` | INTEGER | Count of deals for the brand; 0 when none. |
| `watches` | INTEGER | Count of watches for the brand; 0 when none. |
| `priced_deals` | INTEGER | Count of priced deals; 0 when none. |
| `avg_discount_pct` | REAL | Mean of `deal_discount_pct` over priced deals, rounded to 1 decimal place after averaging; `NULL` when `priced_deals` is 0. |
| `best_discount_pct` | REAL | Largest `deal_discount_pct` over priced deals, rounded to 1 decimal place; `NULL` when `priced_deals` is 0. |
| `last_deal_date` | TEXT | UTC calendar date `YYYY-MM-DD` of the latest `matched_at` among the brand's deals (watches excluded); `NULL` when `deals` is 0. |

**Ordering:** `deals` descending, then `brand` ascending (brand names are unique, so this is total).

**Text output extras.**
The table is preceded by `Recorded deals per brand` plus ` since D` when `--since` is given.
When there are no rows, it prints `No brands with at least N deal(s).` with `deal` singular for N = 1 and exits 0.
When the sum of `deals - priced_deals` over shown rows is greater than 0, a final line says `Note: K deal(s) were recorded before discounts were stored and are excluded from avg_discount_pct and best_discount_pct.`

**SQL concepts practiced:** JOIN, LEFT JOIN from `brands` so zero-deal brands survive, GROUP BY, conditional aggregation, HAVING for `--min-deals`, WHERE (or the join condition) for `--since`, AVG and MAX with NULLs, ROUND, date extraction from ISO-8601 text.

#### `trends`: recent eBay asking prices per item

**Question.**
For each item, what were the last N eBay asking prices RainCheck discovered, how did each compare with the one discovered just before it, and what was the rolling average?

**Honest definition.**
These are first-seen asking prices of newly discovered eBay listings, in the order RainCheck first saw them, with no outlier filtering.
They are not past values of `items.market_price_usd`, which is a filtered mean that is not stored historically.
`change_usd` compares two different listings, not one listing's price change.

**Observation order.**
An item's observations are ordered by `first_seen_at` ascending, then `ebay_listing_id` ascending.
This matches the ordering the pricing code already uses (`src/db.py:1130-1133`) and RC-06's stated tie-break.
Rows that share a `first_seen_at` came from one pricing run, and their relative order is the deterministic but arbitrary listing ID order.
"Most recent" is the reverse of exactly this order.

**Window semantics.**
The change and rolling average are computed over the item's **complete** observation history, and only then are the latest N kept, so the oldest displayed row still compares with its true predecessor.
The rolling average covers this row and up to W-1 rows immediately before it in observation order: a window of W rows, counted as rows, not by time or by peer groups.
Early rows with fewer predecessors average over what exists, and `window_rows` shows how many.

**Item selection.**
By default only active items appear.
`--include-inactive` adds inactive items.
`--item ID` restricts output to that one item whether it is active or not; an unknown ID or an item with no observations produces no rows.
Items with no observations produce no rows.

**Parameters:** `:latest` (integer), `:window` (integer, the W above), `:item_id` (integer or `NULL`), `:include_inactive` (0 or 1).

**Columns, in order:**

| Column | Type | Rule |
| --- | --- | --- |
| `brand` | TEXT | Brand name of the item. |
| `item_id` | INTEGER | `items.id`. |
| `item` | TEXT | `items.name`. |
| `recency_rank` | INTEGER | 1 for the item's most recent observation, 2 for the one before, and so on. |
| `first_seen_at` | TEXT | As stored. |
| `ebay_listing_id` | TEXT | As stored. |
| `price_usd` | REAL | `first_price_usd` rounded to 2 decimals. |
| `change_usd` | REAL | This price minus the previous observation's price, computed from unrounded prices and then rounded to 2 decimals; `NULL` for the item's first observation ever. |
| `rolling_avg_usd` | REAL | Mean of the unrounded prices in the window, rounded to 2 decimals. |
| `window_rows` | INTEGER | Number of observations in that window: the smaller of W and the row's position in the item's history. |

Only rows with `recency_rank` at most N are returned.

**Ordering:** `brand` ascending, `item` ascending, `item_id` ascending, then `recency_rank` descending, so each item's rows read oldest to newest with the newest last.

**Text output extras.**
When there are no rows, it prints `No eBay price observations.` and exits 0.
After the table, it prints: `Prices are first-seen eBay asking prices of newly discovered listings, not past market prices. Rows sharing first_seen_at came from one pricing run and are ordered by ebay_listing_id.`
That is two sentences on one terminal line, which is fine because it is program output, not Markdown.

**SQL concepts practiced:** JOIN, ROW_NUMBER with PARTITION BY, LAG, AVG() OVER with a ROWS frame whose size is a bound parameter, COUNT() OVER with the same frame, a CTE or subquery so filtering happens after the window functions, ROUND, nullable parameters.

Bound parameters are accepted in a frame offset (verified on SQLite 3.49.1); a negative offset raises `frame starting offset must be a non-negative integer`, which the CLI's range check prevents.

#### `doctor`: data-quality checks

`doctor` runs seven checks in this fixed order, each from its own file `src/sql/doctor/<check_id>.sql`.
Integrity checks come first, configuration checks last.
Each check returns zero rows when healthy.

| Order | `check_id` | Severity | Finds | Parameters | Columns | Ordering |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `dangling_reference` | error | Rows whose foreign key points at a missing parent: `listing_matches` to `listings` and to `items`; `item_keywords` to `items`; `ebay_listing_prices` to `items`; `listings` to `brands`; `items` to `brands`. One finding per missing reference, so a match missing both parents gives two findings. | none | `child_table` TEXT, `child_key` TEXT, `parent_table` TEXT, `missing_id` INTEGER | `child_table`, `parent_table`, `missing_id`, `child_key`, all ascending |
| 2 | `listing_brand_mismatch` | error | Recorded pairs whose listing's `brand_id` differs from the item's `brand_id`, which would make brand reporting ambiguous. Pairs with a missing parent are left to check 1. | none | `listing_id` INTEGER, `item_id` INTEGER, `listing_brand` TEXT, `item_brand` TEXT | `listing_id`, `item_id` |
| 3 | `noncanonical_timestamp` | error | Values in `listings.first_seen_at`, `listings.last_seen_at`, `listing_matches.matched_at`, and `ebay_listing_prices.first_seen_at` that are not canonical UTC text. Canonical means exactly 25 characters shaped `YYYY-MM-DDTHH:MM:SS+00:00`, or exactly 32 characters shaped `YYYY-MM-DDTHH:MM:SS.ffffff+00:00`, with ASCII digits in every digit position. | none | `table_name` TEXT, `column_name` TEXT, `row_key` TEXT, `value` TEXT | `table_name`, `column_name`, `row_key` |
| 4 | `deal_discount_mismatch` | error | Pairs with deal-time values whose stored `deal_discount_pct` differs from 100 times (market price minus yen price times rate) divided by market price by more than the tolerance. | `:tolerance_pct` = 0.001 | `listing_id` INTEGER, `item_id` INTEGER, `stored_pct` REAL, `computed_pct` REAL (both unrounded) | `listing_id`, `item_id` |
| 5 | `no_include_keywords` | error | Active items with no `include` keyword, including items with no keyword rows at all or only excludes. `title_matches` returns `False` for an empty include set (`src/matching.py:56`), so such an item silently never matches on Yahoo or eBay. | none | `item_id` INTEGER, `brand` TEXT, `item` TEXT | `item_id` |
| 6 | `no_usable_price` | warning | Active items where neither `market_price_usd` nor `manual_price_usd` is a usable price (non-`NULL` and greater than 0); the Yahoo job skips them (`src/yahoo_scrape.py:297-298`). `market_price_usd` has no `CHECK`, so 0 or negative legacy values are possible and count as unusable. | none | `item_id` INTEGER, `brand` TEXT, `item` TEXT, `market_price_usd` REAL, `manual_price_usd` REAL (as stored) | `item_id` |
| 7 | `thin_ebay_pool` | warning | Active items with fewer than `MIN_POOL_PRICES` stored eBay observations, including items with none. Fewer than five raw observations guarantees no eBay price can be set (`src/db.py:1151`); five or more does not guarantee one, because outliers are removed first. | `:min_observations` = `db.MIN_POOL_PRICES` | `item_id` INTEGER, `brand` TEXT, `item` TEXT, `observations` INTEGER | `item_id` |

`child_key` formats are exact: `listing_id=<n> item_id=<n>` for `listing_matches`, `item_id=<n> keyword=<keyword>` for `item_keywords`, `item_id=<n> ebay_listing_id=<id>` for `ebay_listing_prices`, and `id=<n>` for `listings` and `items`.
`row_key` uses the same formats for check 3.
Rows written before version 5 have `NULL` deal-time columns and are never findings in check 4.

**Why check 1 is still worth having when foreign keys exist.**
SQLite enforces foreign keys only on connections that run `PRAGMA foreign_keys = ON`, and the setting is off by default.
RainCheck's own connections turn it on (`src/db.py:269,363`), but a `sqlite3` shell session, a GUI browser, an ad hoc script, or a restored partial dump does not, and can leave orphans.
`ebay_listing_prices` also has no `ON DELETE CASCADE`, so a deletion made with enforcement off leaves orphaned observations.
SQLite's built-in `PRAGMA foreign_key_check` finds the same rows; check 1 is the user's hand-written equivalent, and a test asserts the two agree on fixture F2.

**Output.**
For each check, a status line, then the table when there are findings:

```text
[error] no_include_keywords: 2 findings - Active items with no include keyword can never match a listing.
item_id  brand       item
-------  ----------  -----------
      3  Undercover  Scab Jacket
      6  Undercover  Hoodie

[ok] dangling_reference
```

A check with findings prints `[error]` or `[warning]`, its `check_id`, the count with `finding` or `findings`, and a fixed one-sentence description defined in `reports.py`.
A healthy check prints `[ok] <check_id>`.
A blank line separates checks.
The last line is `doctor: E error findings, W warning findings, K of 7 checks ok.`
`--json` prints `{"command": "doctor", "checks": [{"id", "severity", "description", "count", "rows"}...], "error_findings": E, "warning_findings": W}` with checks in the fixed order.

**Exit codes (recommended; Open question 1):** 0 when no check has error-severity findings (warnings allowed); 3 when at least one error-severity finding exists; 1 when `doctor` could not run; 2 for argument errors.
A failing check query stops `doctor` with exit 1 rather than reporting a partial result.

**SQL concepts practiced:** LEFT JOIN with IS NULL (checks 1, 5, 7), UNION ALL (checks 1, 3), GROUP BY with HAVING (checks 5, 7), conditional counting, text shape matching with `length` and `GLOB`, string concatenation, REAL arithmetic and `abs`.

### Index decision process

The goal is one index justified by evidence, not by intuition.

1. **Benchmark database.**
   `tests/tools/build_bench_db.py <path>` builds a version 5 database at a path that must not already exist, with a fixed random seed so every run is identical.
   It contains 5 brands; 200 items, 180 active; 3 include keywords and 1 exclude keyword per item; 500,000 `ebay_listing_prices` rows (2,500 per item, in batches of 50 sharing one canonical `first_seen_at`); 50,000 `listings`; and 55,000 `listing_matches` rows, where 10% of listings match two items, 80% of pairs have deal-time values, and 10% of those are watches.
   It does not run `ANALYZE`, because production never does, and the plan must reflect production statistics.
   It refuses to write to `src/raincheck.db` or any existing file.
2. **Before.**
   For each of the user's final queries, with default parameters, the user runs EXPLAIN QUERY PLAN on the benchmark database and records the plan text.
   They also record the median wall time of 5 runs measured with `time.perf_counter` from Python, plus the SQLite version (`sqlite3.sqlite_version`).
3. **Find a target.**
   A target is a plan step that touches one of the two large tables (`ebay_listing_prices`, `listing_matches`) and shows one of these:
   - `SCAN <table>` without `USING INDEX` or `USING COVERING INDEX`, where only some rows are needed.
   - `USE TEMP B-TREE FOR ORDER BY`, `FOR GROUP BY`, `FOR LAST TERM OF ORDER BY`, or `FOR DISTINCT` over many rows.
   Window function sorting also shows up as a temporary b-tree.
   [SQLite's documentation](https://www.sqlite.org/eqp.html) defines `SCAN` as reading every row, `SEARCH` as visiting a subset, and the temporary b-tree as a sort that an index can often remove.
4. **Candidate.**
   The user proposes one index aimed at the slowest target and explains which plan line it should change.
   They record the index's column list and whether an existing index becomes a strict prefix of it (and therefore redundant).
5. **After.**
   On a copy of the benchmark database with the candidate added, rerun every query's plan and timing.
   Also time one 50-row `record_ebay_listing_prices` insert batch and one `record_listing_matches` batch of 10 deals, and record the file size before and after.
6. **Accept the index only if all of these hold:**
   - The target query's plan changes as predicted: a `SCAN` becomes `SEARCH` or a covering-index scan, or a temporary b-tree line disappears.
   - Its median time improves by at least 25%.
   - No other report query and neither job write batch gets more than 10% slower.
   If the candidate makes an existing index a strict prefix, the evidence states it, and the user decides whether migration 5 also drops the old one; the default is to keep it.
7. **Record** everything in `docs/specs/RC-22-evidence/query-plans.md`.
   If no candidate meets the rule after at most three attempts, the document says so, migration 5 contains only the six columns, and the user may later propose a separate item.

Tests never assert plan text, because SQLite says the format can change between releases ([EXPLAIN QUERY PLAN](https://www.sqlite.org/eqp.html)).
On SQLite 3.49.1 today, an ordering by item, time, and listing ID over `ebay_listing_prices` plans as `SCAN ebay_listing_prices USING INDEX idx_ebay_listing_prices_item_seen` plus `USE TEMP B-TREE FOR LAST TERM OF ORDER BY`.
That is shown only to illustrate what a target looks like, not as a chosen answer.

### Review checklist for the user's SQL

The orchestrator checks each query against this list, explains any finding in terms of the user's own query, and suggests what to reconsider, never the replacement text.

**Joins and counting**

1. **COUNT(\*) versus COUNT(column) after a LEFT JOIN.**
   COUNT(\*) counts the all-NULL row a LEFT JOIN produces for an unmatched parent, so a brand or item with nothing shows 1 instead of 0.
   Check `report --min-deals 0` (Kapital must show 0) and `thin_ebay_pool` (items with no observations must show 0).
2. **Filtering a LEFT JOIN in WHERE.**
   A condition on the right-hand table in WHERE (for example the `--since` cutoff) removes the unmatched rows and turns the LEFT JOIN into an inner join, dropping zero-count brands.
   Such a condition belongs in the join condition, or in a filtered subquery joined afterwards.
3. **Row multiplication from joins.**
   Joining a second one-to-many table (`item_keywords`, `ebay_listing_prices`, or `listing_matches` twice) multiplies rows, inflating counts and sums and skewing averages.
   Pairs are intentionally counted per item: one listing matched to two items is two deals, and fixture F1 tests exactly that.
   Using a distinct count of listing IDs would be a different, wrong definition.
4. **Joining tables a query does not need.**
   `report` does not need `listings`; an unneeded inner join can drop rows and adds work.
5. **Brand attribution.**
   Deals belong to the item's brand, not `listings.brand_id`.
6. **Ambiguous columns.**
   Every column in a multi-table query is qualified with its table or alias.
   `items.market_price_usd` and `listing_matches.deal_market_price_usd` are different values.

**Grouping and aggregates**

7. **WHERE versus HAVING.**
   `--since` filters rows (before grouping); `--min-deals` filters groups (after counting).
   Swapping them either fails or changes the meaning.
8. **Every selected non-aggregate column is in GROUP BY.**
   SQLite accepts a bare column that is not grouped and silently picks a value from an arbitrary row, where most databases would reject the query.
9. **NULL in AVG, MIN, MAX, and SUM.**
   These ignore NULLs, which is correct for discounts recorded before version 5, but only if watches are excluded explicitly.
   They return NULL, not 0, for an empty set; SUM over no rows is NULL while COUNT is 0.
10. **Conditional aggregation.**
    A conditional count must count only matching rows; counting a conditional expression that yields 0 instead of NULL for non-matching rows counts every row.
11. **Comparisons with NULL.**
    A NULL `deal_kind` is neither equal nor unequal to `auction_watch`, so "not a watch" must explicitly keep NULL kinds.
12. **Integer division.**
    In SQLite, dividing two integers truncates (7 / 2 is 3).
    Any ratio built from counts or integer columns, such as `deal_price_yen`, needs a REAL operand before dividing.
13. **Round last.**
    Round after averaging and after subtracting.
    Rounding inputs first changes results; rounding before LAG subtraction makes `change_usd` drift.
14. **Rounding of binary floats.**
    SQLite's ROUND works on the binary value, so 0.15 rounds to 0.1; expected values in tests avoid exact halves for this reason.

**Window functions**

15. **Deterministic ORDER BY inside OVER.**
    `first_seen_at` alone ties within a pricing run, and SQLite then numbers tied rows in arbitrary order.
    Every window ordering must end with `ebay_listing_id`, which is unique per item.
16. **The same order, reversed, for recency.**
    ROW_NUMBER for "most recent" must use exactly the reverse of the LAG and rolling-average order, both columns reversed, or tied rows disagree about which is newest.
17. **PARTITION BY item.**
    Without partitioning, LAG compares across items and ranks are global.
18. **Default frame is RANGE, not ROWS.**
    With an ORDER BY and no frame clause, SQLite uses RANGE from the start of the partition through the current row **and its peers**, which is a cumulative average that also includes later rows of the same pricing run.
    The rolling average needs an explicit ROWS frame ([SQLite window functions](https://www.sqlite.org/windowfunctions.html)).
19. **Off-by-one in the frame size.**
    A window of W rows is the current row plus W-1 preceding rows, not W preceding rows.
20. **Filter after windowing.**
    Window functions see only rows that survive WHERE.
    Filtering to the latest N, or to active items, in the same query level as LAG changes the predecessor of the oldest shown row.
    The latest-N filter belongs in an outer query over a CTE or subquery.
    The item filter is applied before or after the window computation; it gives the same result either way because each item is its own partition.
21. **LAG on the first row.**
    LAG returns NULL for an item's first observation, which is the intended `change_usd`; it must not be replaced with 0.

**Time and text**

22. **ISO-8601 text comparison.**
    Comparing timestamps as text is correct only because every canonical value has the same layout and the same `+00:00` offset.
    A date parameter such as `2026-10-20` compares lower than every timestamp on that day, so "on or after" works as greater-than-or-equal.
    Wrapping the column in a date function on every row also works but cannot use an index.
23. **Fractional seconds.**
    Whole-second and fractional values coexist; text order handles them, but slicing a fixed number of characters for anything other than the date can mis-handle one of the two forms.
24. **Date extraction.**
    `last_deal_date` is the date part of the maximum timestamp; taking the maximum of dates and of timestamps gives the same day here, but the maximum must still be taken over deals only.

**Determinism and safety**

25. **Final ORDER BY matches the contract exactly**, including tie-breaks, because tests compare row order.
26. **Named parameters only.**
    No values are inlined; optional parameters are handled with NULL tests on the parameter, not by changing the SQL text.
27. **One statement per file, read-only.**
    The harness enforces this, but the query must not rely on temporary tables or PRAGMAs.
28. **Output column names** match the contract exactly; aliases are required for computed columns.

## Acceptance criteria

**Schema and migration**

1. A fresh `init_db()` creates `PRAGMA user_version = 5`, and `listing_matches` ends with exactly the six deal-time columns, in the order and with the types in the schema table.
2. A version 4 database built from `tests/fixtures/db/schema_v4.sql`, with rows in every table, migrates to version 5 and matches a fresh database in `table_info`, index names, and foreign keys for every table.
3. After migration, every preexisting row in every table is unchanged, every preexisting `listing_matches` row has `NULL` in all six new columns, `integrity_check` is `ok`, and `foreign_key_check` is empty.
4. In both fresh and migrated databases, inserting a `listing_matches` row with only some deal-time columns set fails with a `CHECK` error, and all-`NULL` and all-set rows succeed.
5. In both fresh and migrated databases, a `deal_kind` outside the three kinds, a `deal_price_yen` of 0, a `deal_market_price_usd` or `deal_yen_to_usd` of 0, a `deal_rate_is_fallback` of 2, and a `deal_discount_pct` of 0 or 100 each fail with a `CHECK` error.
6. A migration 5 made to fail on its last statement leaves the file at version 4 with byte-identical table contents.
7. `get_connection()` and `get_read_only_connection()` refuse a version 4 database with the existing "Back up the database, then run python3 src/db.py" message, and a version 6 database with the "newer RainCheck" message.

**Recording**

8. `record_listing_matches` with one deal of each kind, `yen_to_usd_rate=0.005`, and `rate_is_fallback=False` stores each deal's kind, `price_yen`, `market_price_usd`, and unrounded `discount_pct`, plus 0.005 and 0, on its new pair.
9. For an `auction_buy_now` deal whose current bid differs from its buy-now price, `listings.price_yen` stores the current bid and `deal_price_yen` stores the buy-now price.
10. Re-recording an existing pair with different deal values leaves its original deal-time values and `matched_at` unchanged.
11. Calling `record_listing_matches` without `yen_to_usd_rate` or `rate_is_fallback` raises `TypeError`, and invalid rates (0, negative, NaN, infinity, `True`), a non-`bool` fallback flag, and each invalid per-deal value listed in Interfaces raise `ValueError`; in all cases no row is written and no connection is opened.
12. `resolve_yen_to_usd_rate` returns `YenRate(rate, False)` for a valid provider response and `YenRate(0.0062, True)` for HTTP 500, a network error, or invalid rate data, logging the existing warning once.
13. An end-to-end `yahoo_scrape.main()` run with stubbed Yahoo, a stubbed provider failure, and a successful send records `deal_rate_is_fallback = 1` and `deal_yen_to_usd = 0.0062` on every new pair; with a stubbed provider rate of 0.007 it records 0 and 0.007.
14. A failed send records nothing, and a preview records nothing, as before.

**Read-only harness**

15. `reports.main(["--help"])` and every subcommand's `--help` exit 0 without creating or opening a database.
16. Every command against a missing database path exits 1 with one `Error:` line and does not create the file.
17. Every command against a version 4 database, an empty file, and a non-SQLite file exits 1 with the matching `SchemaVersionError` message and leaves the file byte-identical.
18. With `reports.SQL_DIR` pointed at a temporary directory whose `report.sql` contains a data-modifying statement, `report` exits 1 and the database file's SHA-256 is unchanged.
19. A header-only query file produces `Error: src/sql/<name>.sql has no query yet.` and exit 1.
    A file with two statements, a query returning wrong column names, and a query using an unknown parameter name each exit 1 with one `Error:` line.
20. Argument errors exit 2: `--min-deals -1`, `--since 2026-13-01`, `--since 2026-1-5`, `--latest 0`, `--latest 1001`, `--window 0`, `--item 0`, and `--item abc`.
21. With the query runner stubbed to return fixed rows, text output matches the format rules exactly: two-space separators, dash rule, alignment, `-` for `NULL`, fixed decimals, signed `change_usd`, whitespace collapse, and double width for a Japanese item name.
22. With the same stub, `--json` output parses to the documented object, preserves column order in each row, uses `null` for `NULL`, keeps Japanese text unescaped, and echoes effective parameters including defaults.
23. Every command opens the database only through `db.get_read_only_connection()`, verified by making `db.get_connection` raise during the command.

**`report` (user's query, fixture F1 unless stated)**

24. With defaults, rows are exactly `Rick Owens, 6, 1, 5, 30.7, 48.5, 2026-11-01` and `Undercover, 1, 0, 1, 38.0, 38.0, 2026-10-31`, in that order.
25. `--min-deals 0` adds `Kapital, 0, 0, 0, NULL, NULL, NULL` as the third row; `--min-deals 2` returns only the Rick Owens row.
26. `--since 2026-10-20` returns `Rick Owens, 4, 1, 4, 33.6, 48.5, 2026-11-01` and `Undercover, 1, 0, 1, 38.0, 38.0, 2026-10-31`, and the pair whose `matched_at` is exactly `2026-10-20T00:00:00+00:00` is included.
27. `--since 2026-10-20 --min-deals 0` still includes `Kapital, 0, 0, 0, NULL, NULL, NULL`.
28. On fixture F0 (empty version 5 database), `report` prints `No brands with at least 1 deal.` and exits 0, and `--min-deals 0` returns no rows.
29. On a single-row fixture (one brand, one item, one priced fixed-price deal of 12.3%), the row is `deals 1, watches 0, priced_deals 1, avg 12.3, best 12.3` with that deal's date.
30. A brand with only a pre-version-5 deal shows `priced_deals 0` and `NULL` average and best, and text output prints the "recorded before discounts were stored" note with K = 1.
31. A brand whose only pair is a watch appears only with `--min-deals 0`, as `deals 0, watches 1, priced_deals 0`, `NULL` statistics, and `NULL` `last_deal_date`.

**`trends` (user's query, fixture F1)**

32. `--window 3 --latest 5` returns item 2 (`Pod Shorts`) first, then item 1 (`Ramones`) rows in this order, as `(listing, price, change, rolling, rows, rank)`: `legacy:a, 110.00, NULL, 110.00, 1, 5`; `rest:b, 100.00, -10.00, 105.00, 2, 4`; `legacy:c, 130.00, 30.00, 113.33, 3, 3`; `legacy:d, 90.00, -40.00, 106.67, 3, 2`; `legacy:e, 120.00, 30.00, 113.33, 3, 1`.
    This holds even though `rest:b` was inserted before `legacy:a` and they share `first_seen_at`.
33. `--window 3 --latest 2` returns for item 1 only `legacy:d` (change -40.00, rolling 106.67, rows 3) then `legacy:e`, proving the window used rows that are not displayed.
34. Defaults (`--latest 10 --window 5`) return item 1 rolling averages 110.00, 105.00, 113.33, 107.50, 110.00 with `window_rows` 1 through 5.
35. Item 2's single observation returns `change_usd NULL`, `rolling_avg_usd 250.00`, `window_rows 1`, `recency_rank 1`.
36. Inactive item 4 appears only with `--include-inactive` or `--item 4`; `--item 5` (active, no observations) and `--item 999` return no rows and exit 0.
37. On fixture F0, `trends` prints `No eBay price observations.` and exits 0.
38. `--window 1` returns `rolling_avg_usd` equal to `price_usd` and `window_rows` 1 on every row.

**`doctor` (user's queries)**

39. On fixture F0, all seven checks print `[ok]`, the summary is `doctor: 0 error findings, 0 warning findings, 7 of 7 checks ok.`, and the exit code is 0.
40. On fixture F1, `no_include_keywords` returns items 3 and 6, `no_usable_price` returns items 3 and 6 (item 6 has `market_price_usd` 0.0 and no manual price), `thin_ebay_pool` returns items 2 (1), 3 (0), 5 (0), and 6 (0), the other four checks return nothing, and the exit code is 3.
41. On fixture F2, `dangling_reference` returns exactly the eight findings listed for F2 in the test plan, in contract order, and the multiset of `(child_table, parent_table)` equals the one derived from `PRAGMA foreign_key_check`.
42. On fixture F2, `listing_brand_mismatch`, `noncanonical_timestamp`, and `deal_discount_mismatch` return exactly the rows listed for F2 in the test plan.
43. A database with only warning findings exits 0; with any error finding, it exits 3.
44. `doctor --json` lists all seven checks in the fixed order with correct counts and totals.

**Index, documentation, and authorship**

45. `docs/specs/RC-22-evidence/query-plans.md` records the SQLite version, benchmark parameters, before and after plans and timings for every query, the write-batch timings, file sizes, and the decision with the rule from step 6.
46. If an index is chosen, it exists with the same name in fresh and migrated version 5 databases; if not, migration 5 has exactly six statements.
47. Running `tests/tools/build_bench_db.py` twice to two new paths produces databases with identical table contents; it refuses an existing path.
48. README documents all three commands with an example of each, the query file workflow including `.parameter set`, running reports against a downloaded production copy, and the version 5 upgrade summary.
49. The implementer's commits add no SQL statement to any file in `src/sql/` (headers only), and `src/reports.py` contains no SQL other than what the harness needs to run a file; the reviewer checks this in the diff.

## Edge cases

- Empty results: an empty database gives the "no rows" messages and exit 0 for `report` and `trends` and a clean `doctor`; brands without items appear only with `--min-deals 0`; items without observations never appear in `trends`.
- `None` or missing values: pre-version-5 pairs have all-`NULL` deal-time columns and count as deals without discounts; NULL `deal_kind` counts as a deal; `--since` and `--item` omitted bind as `NULL`; `market_price_usd` of 0 or below counts as unusable in `doctor`.
- Network failure or bad HTTP status: reports make no network calls; a Frankfurter failure during the Yahoo job is recorded as `deal_rate_is_fallback = 1` with rate 0.0062.
- Malformed input: invalid CLI values exit 2 before opening the database; a non-SQLite or wrong-version file exits 1 unchanged; a hot rollback journal that a read-only connection cannot recover surfaces as an `Error:` line with exit 1, and the user runs any normal RainCheck entry point to recover it.
- Malformed data: hand-edited timestamps break text ordering and are reported by `noncanonical_timestamp`; dangling rows are reported by `dangling_reference`; `report` and `trends` output on a database with integrity findings is unspecified, so run `doctor` first.
- Duplicates: one listing matched to two items gives two deals with separate discounts; re-recording a pair keeps the first values; pricing-run timestamp ties are broken by `ebay_listing_id`.
- Concurrency: a report run while a job writes may wait up to the default 5-second busy timeout and then fails with exit 1; it never blocks a writer for longer than one read.
- Path handling: a relative `RAINCHECK_DB_PATH` resolves against the current directory, and paths with spaces or `#` work because the URI is built with `Path.as_uri()`.

## Test plan

All tests use `tmp_path` databases through the existing `RAINCHECK_DB_PATH` fixture and never touch the network or `src/raincheck.db`.
Fixture databases are built by `tests/rc22_fixtures.py` with plain `sqlite3`, version 5 schema from `src/schema.sql`, and literal values, so expected results are computed by hand, not by running the code under test.

**F0:** a fresh, empty version 5 database.

**F1 (foreign keys on):**

- Brands: 1 `Rick Owens`, 2 `Undercover`, 3 `Kapital` (no items).
- Items:
  - 1 `Ramones`: brand 1, market 400.0, active, include `ramones`.
  - 2 `Pod Shorts`: brand 1, market `NULL`, manual 300.0, active, include `pod short`.
  - 3 `Scab Jacket`: brand 2, no prices, active, exclude `fake` only.
  - 4 `Geobasket`: brand 1, market 500.0, inactive, include `geobasket`.
  - 5 `Bones Tee`: brand 2, manual 100.0, active, include `bones`.
  - 6 `Hoodie`: brand 2, market 0.0, manual `NULL`, active, no keywords.
- Pairs, as (listing, item, kind, yen, market, rate, fallback, discount, `matched_at`); every listing's `brand_id` equals its item's brand, and every listing's `first_seen_at` and `last_seen_at` equal its earliest pair's `matched_at`:
  - (L1, 1): all deal columns `NULL`, `2026-09-06T00:13:01.331953+00:00`.
  - (L2, 1): `fixed_price`, 59200, 400.0, 0.005, 0, 26.0, `2026-10-20T00:00:00+00:00`.
  - (L3, 1): `auction_buy_now`, 41200, 400.0, 0.005, 0, 48.5, `2026-10-20T00:00:00.500000+00:00`; its `listings.price_yen` is a lower current bid of 20000.
  - (L4, 2): `fixed_price`, 48000, 300.0, 0.005, 0, 20.0, `2026-11-01T08:30:00.000001+00:00`.
  - (L4, 1): `fixed_price`, 48000, 400.0, 0.005, 0, 40.0, same `matched_at`; one listing, two items.
  - (L5, 1): `auction_watch`, 2000, 400.0, 0.005, 0, 97.5, `2026-11-05T09:00:00+00:00`; the latest pair, but a watch.
  - (L7, 4): `fixed_price`, 81000, 500.0, 0.005, 0, 19.0, `2026-10-15T12:00:00+00:00`; an inactive item.
  - (L6, 5): `fixed_price`, 10000, 100.0, 0.0062, 1, 38.0, `2026-10-31T23:59:59.999999+00:00`.
- eBay observations:
  - Item 1, inserted in this order: `rest:b` 100.0 and `legacy:a` 110.0 at `2026-10-01T00:00:00+00:00`; `legacy:c` 130.0 at `2026-10-02T00:00:00.250000+00:00`; `legacy:e` 120.0 and `legacy:d` 90.0 at `2026-10-03T00:00:00+00:00`.
  - Item 2: `legacy:z` 250.0 at `2026-10-05T00:00:00+00:00`.
  - Item 4: `legacy:g` 400.0 at `2026-10-06T00:00:00+00:00`.
- Hand-computed results are in criteria 24 to 27 and 32 to 35, and 40.
  Average 30.7 is (26.0 + 48.5 + 20.0 + 40.0 + 19.0) / 5; 33.6 is 134.5 / 4 = 33.625 rounded; no expected value is an exact rounding half.

**F2 (built with `PRAGMA foreign_keys = OFF`, which is the only way to create these rows and is why the check exists):**

- A valid base: brand 1 `Rick Owens`, brand 2 `Undercover`; item 1 (brand 1) and item 2 (brand 2), both active with include keywords, a manual price, and five eBay observations each; listing 10 (brand 1).
- Every timestamp not listed below as noncanonical is canonical.
- Dangling rows (eight findings):
  - Match (99, 1): missing listing 99.
  - Match (10, 999): missing item 999.
  - Match (98, 998): missing listing 98 and item 998, so two findings.
  - `item_keywords` (997, `ghost`): missing item 997.
  - `ebay_listing_prices` (996, `legacy:x`): missing item 996.
  - Listing 11 with brand 95: missing brand 95.
  - Item 3 with brand 94: missing brand 94.
  Item 3 is inactive with an include keyword and a manual price, so it adds no configuration findings.
- Brand mismatch: listing 12 has brand 2 and is matched to item 1 (brand 1), giving one row `12, 1, Undercover, Rick Owens`.
- Noncanonical timestamps:
  - Listing 12's `first_seen_at` is `2026-10-01 12:00:00`; its `last_seen_at` is canonical.
  - Pair (12, 1) has `matched_at` `2026-10-01T12:00:00Z`.
  - One of item 2's five eBay observations, `legacy:short`, has `first_seen_at` `2026-10-01T12:00:00.25+00:00` (fractional part too short).
  These give exactly three rows.
- Discount mismatch: pair (10, 1) has `fixed_price`, 60000, 400.0, 0.005, 0, stored 50.0 while the computed value is 25.0, giving one row `10, 1, 50.0, 25.0`.
  Listing 13 (brand 1) matched to item 1 has `fixed_price`, 60000, 400.0, 0.005, 0, stored 25.0005, which is within tolerance and is not reported.
- The single-row and watch-only fixtures for criteria 29 to 31 are small builders in the same module.

**Harness tests** stub `reports.run_query` to return canned columns and rows, so formatting, JSON, exit codes, and contract checks are tested independently of the user's SQL.
Read-only tests compare SHA-256 of the database file before and after, and use a temporary `SQL_DIR` holding deliberately bad files: a write statement, two statements, wrong column names, and an unknown parameter.

**Migration tests** add `tests/fixtures/db/schema_v4.sql` (today's `src/schema.sql` content stamped at version 4) and follow the existing patterns in `tests/test_migration.py`, including injected failure for rollback.

**Recording tests** extend the RC-16 style end-to-end job test with a stubbed `send_email`, stubbed Yahoo page, and a stubbed Frankfurter response through `FakeHTTP` (`tests/conftest.py`).

**Existing tests** that call `record_listing_matches` (`tests/test_listing_matches.py:167-200`, `tests/test_rc16_auctions.py:602-679`) pass the new arguments; tests asserting `resolve_yen_to_usd_rate(...) == 0.0062` (`tests/test_yahoo_scrape.py:374-398,547,558,1802,1814`) compare `.rate` and `.is_fallback`; and Lambda tests that monkeypatch it with a float (`tests/test_lambda_handler.py:460,582,614,635,732`) return `YenRate(0.0062, True)` instead.

No saved HTML or recorded API JSON is needed beyond the existing Yahoo fixtures.

## Shared-file conflicts

Yes: this touches `src/schema.sql` and `src/db.py`, so it must not run in parallel with any other feature touching either file.
RC-21 is in progress on `codex/rc-21-live-keyword-management` and changes `src/db.py`, `src/lambda_handler.py`, and `README.md`; RC-22 implementation must start only after RC-21 merges, from the merged `main`.
If RC-21 or any other feature takes schema version 5 first, RC-22 takes the next free version, and every "5" in this spec moves with it.
RC-22 also changes `src/yahoo_scrape.py`, which most deal and digest features touch, and `README.md`.
`src/catalog.json` is not touched.
This spec is safe to approve while RC-21 is in progress, because planning changes no shared file.

## Open questions

1. **`doctor` exit codes.**
   Recommendation: 0 when there are no error findings (warnings allowed), 3 for error findings, 1 when it cannot run, and 2 for usage errors.
   Tradeoff: a distinct code lets a script tell "found problems" from "could not check", but 3 is less familiar than 1.
   The alternative of using 1 for findings matches `diff` but collides with every other RainCheck error.
2. **Legacy pairs' kind.**
   Recommendation: no backfill; pre-version-5 pairs stay `NULL` and count as deals without discounts, as the user decided for discounts.
   Tradeoff: some RC-16-era auction watches will be counted as deals.
   A partial backfill is possible: an auction with no buy-now price (`is_fixed_price = 0`, `buy_now_price_yen` `NULL`) can only have been a watch, and a fixed-price listing only a fixed-price deal.
   But it would be an inferred data change inside a migration, and buy-now auctions remain ambiguous.
3. **Where the user's SQL lives.**
   Recommendation: `.sql` files under `src/sql/`, so they can be run in the `sqlite3` shell, syntax-highlighted, and reviewed as SQL.
   Tradeoff: one more directory, and the harness must read files.
   The alternative is string constants in `reports.py`, which mixes the user's SQL with the implementer's Python.
4. **Planner-added doctor checks.**
   The user requested checks 1, 5, 6, and 7; checks 2, 3, and 4 are planner additions that protect the assumptions the reports rely on (brand attribution, text ordering, and stored discounts).
   Recommendation: keep all three.
   Tradeoff: three more queries to write; any can be dropped before approval, with its criteria removed.
5. **One branch or two.**
   The item is large (size L).
   Recommendation: one spec and one branch, built in the phases under "Who writes what", with the index decided before review.
   Tradeoff: a long-lived branch while the user writes SQL.
   Splitting into "RC-22a deal-time recording" and "RC-22b reports" would ship recording sooner, so more deals have discounts by the time the report exists, but an index chosen in RC-22b would need a second migration (version 6) and a second production upgrade.
6. **`--since` on `report`.**
   Recommendation: keep it, because it is the clearest WHERE-versus-HAVING exercise.
   Tradeoff: one more parameter and two more criteria.
7. **Upgrade timing control.**
   Recommendation: rely on timing away from 09:00 JST plus the `If-Match` upload in U5, without disabling the EventBridge schedule.
   Tradeoff: a daily run that lands between U4 and U5 fails visibly (safely, without changing S3) and must be rerun; disabling the schedule avoids that but adds two stack changes.

### Resolved decisions

- 2026-11-10: the user approved this spec as written, accepting the recommended option for every open question above (doctor exit codes 0/3/1/2, no backfill of old deal kinds, queries in `.sql` files, the three added doctor checks, one phased branch, `--since` on `report`, and upgrading away from the 09:00 JST run without disabling the schedule).

- **2026-11-10, discounts:** the user decided to record discounts from now on, through a schema version 5 migration with the same all-or-nothing rules as migrations 1 to 4 and a backup-first production upgrade.
  Existing rows get `NULL`; `report` counts them as deals, excludes them from average and best, and shows `priced_deals` explicitly.
- **2026-11-10, query style and tests:** report queries are read-only and parameterized; tests come first, with hand-built fixture databases and hand-computed expected results, including an empty database and single-row cases.
- **2026-11-10, authorship:** the user writes every report, trend, and doctor query; this spec and the agents provide definitions and review, not query text.

## Rollback

- **Before the production upgrade:** revert the merge commit; no database has changed.
- **After U2 or U3 but before U5:** discard the working file; production is untouched.
  If U4 already deployed version 5, redeploy the previous release.
- **After U5:** stop and redeploy the previous release, then restore the U1 `VersionId` as the current object, after verifying its integrity and version 4, as described in the README's S3 restore guidance.
  Keep the version 5 object for investigation.
  Deals recorded since the upgrade are lost from the database and may be emailed once more.
- **Local-only database:** restore the U1 backup file over the database path with the previous release checked out.
- Running version 4 code against a version 5 database is refused by the schema check, so a partial rollback fails loudly rather than corrupting data.
