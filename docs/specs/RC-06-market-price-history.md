# RC-06: Market price history

**Status:** Draft
**Backlog item:** RC-06 in `docs/BACKLOG.md`

## Summary

Store a timestamped observation for each tracked item whose eBay search and estimate complete, including an insufficient sample.
Keep `items.market_price_usd` as the latest usable benchmark.

## Motivation

`refresh_market_prices` prints sample counts and discards them after each run (`src/ebay_pricing.py:116-149`).
`update_market_price` overwrites the only saved eBay value (`src/db.py:700-725`), so the price's movement and sample support cannot be inspected.
RC-09 already returns the candidate, outlier, and retained counts and preserves an older price when fewer than five listings remain.

The official [eBay Browse API overview](https://developer.ebay.com/develop/api/buy) confirms that the job searches item summaries with an application access token.
The official [Browse API reference](https://developer.ebay.com/api-docs/buy/api-browse.html) describes keyword search over those summaries.
This feature changes local persistence only.

## Scope

**In scope:**

- Append one per-item history row for every completed eBay estimate, whether or not it produces a price.
- Store the nullable estimate, RC-09 sample counts, and a UTC timestamp.
- Make a successful estimate's history insert and current-price update atomic.
- Migrate version 1 databases to schema version 2 without altering existing rows.
- Document a backup-first upgrade and a read-only way to inspect history.

**Out of scope:**

- Backfilling old prices without known sample sizes or timestamps.
- Changing the estimator, five-listing minimum, first-50-result limit, eBay request, or Yahoo deal math.
- Clearing older prices after an insufficient result.
- Recording token failures, HTTP failures, malformed response containers, or items not reached after a failed item.
- Run IDs, charts, alerts, retention, manual-price history, and catalog changes.

## Design

### Files touched

| File | Change |
| --- | --- |
| `src/schema.sql` | Define schema version 2, history table, and item/time index. |
| `src/db.py` | Add migration 2 and an atomic observation writer. |
| `src/ebay_pricing.py` | Record each completed `PriceEstimate`. |
| `tests/test_market_price_history.py` | Add writer, atomicity, validation, and per-run cases. |
| `tests/test_migration.py` | Check fresh and migrated version 2 shape, preservation, idempotency, and rollback. |
| `tests/test_db_cli.py` | Update version expectations while preserving outdated/newer refusal cases. |
| `tests/test_listing_matches.py` | Update its fresh-schema version assertion. |
| `tests/test_seed_catalog.py` | Update any version assertions without changing seeding. |
| `tests/test_ebay_pricing.py` | Assert history for completed samples and adapt its price-writer interception. |
| `tests/dbhelpers.py` | Include the new table in full database snapshots and comparisons. |
| `README.md` | Explain history and a read-only query for an item's observations. |

Existing `tests/conftest.py` fixtures, `src/catalog.json`, and recorded eBay JSON should need no change.
If implementation reveals another necessary file, amend this list before editing that file.

### Schema changes and migration

Increase `SCHEMA_VERSION` and the `schema.sql` comment from 1 to 2.
Append `MIGRATIONS[2]`; never alter the shipped migration 1.
Fresh creation and migration 2 must produce the same table and index:

```sql
CREATE TABLE market_price_history (
    id               INTEGER PRIMARY KEY,
    item_id          INTEGER NOT NULL REFERENCES items(id),
    price_usd        REAL CHECK (price_usd IS NULL OR price_usd > 0),
    candidate_count  INTEGER NOT NULL CHECK (candidate_count >= 0),
    outlier_count    INTEGER NOT NULL CHECK (outlier_count >= 0),
    retained_count   INTEGER NOT NULL CHECK (retained_count >= 0),
    observed_at      TEXT NOT NULL,
    CHECK (candidate_count = outlier_count + retained_count),
    CHECK ((price_usd IS NULL AND retained_count < 5) OR (price_usd IS NOT NULL AND retained_count >= 5))
);
CREATE INDEX idx_market_price_history_item_time
    ON market_price_history (item_id, observed_at);
```

`price_usd=NULL` means the completed search produced no usable estimate, not that the current price was cleared.
The counts are after RC-09's title, currency, price, and duplicate validation, with outliers identified by its existing rule.
Capture one UTC ISO 8601 timestamp with explicit `+00:00` after estimating and before the database transaction.
Do not add a uniqueness constraint on item, time, or price, because repeated runs are separate observations.
Read newest rows with `ORDER BY observed_at DESC, id DESC` for a stable tie break.
The foreign key prevents orphaned rows; catalog deactivation retains both item and history.

The existing `_migrate` runs migration 2 and its version bump in one `BEGIN IMMEDIATE` transaction.
Version 0 databases migrate through unchanged step 1 and new step 2.
Version 1 databases apply only step 2.
All preexisting prices and rows remain unchanged, and history starts empty because older sample details are unknown.
Jobs continue to refuse outdated databases until the explicit upgrade.

#### Existing database upgrade after merge

Only the user should run these steps against the real database after merging and pulling the code.
Stop scheduled jobs and run from the repository root.
Do not use `--seed`, since schema upgrade does not require catalog synchronization.

1. Confirm `src/raincheck.db` exists and has `PRAGMA user_version = 1` through a read-only connection.
2. Make a verified, timestamped SQLite online backup outside the repository.
   The backup API includes the consistent database state even when SQLite uses a write-ahead log:

   ```sh
   python3 - <<'PY'
   from contextlib import closing
   from datetime import datetime, timezone
   from pathlib import Path
   import os
   import sqlite3

   source = Path("src/raincheck.db").resolve(strict=True)
   backup_dir = Path.home() / "RainCheck-backups"
   backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
   stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
   target = backup_dir / f"raincheck-before-rc06-{stamp}.db"
   with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as live:
       if live.execute("PRAGMA user_version").fetchone()[0] != 1:
           raise RuntimeError("Expected schema version 1; database left unchanged")
       fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
       os.close(fd)
       with closing(sqlite3.connect(target)) as backup:
           live.backup(backup)
           if backup.execute("PRAGMA quick_check").fetchone()[0] != "ok":
               raise RuntimeError("Backup integrity check failed; database left unchanged")
           if backup.execute("PRAGMA user_version").fetchone()[0] != 1:
               raise RuntimeError("Backup version differs; database left unchanged")
   print(target)
   PY
   ```

3. Keep the printed backup path and run `RAINCHECK_DB_PATH="$(pwd)/src/raincheck.db" python3 src/db.py`.
   This applies migration 2 without seeding or starting either job.
4. Run this read-only check before the next eBay run, pasting the backup path printed in step 2 when prompted.
   It confirms the versions, integrity, foreign keys, empty history, and unchanged row counts for all five preexisting tables:

   ```sh
   python3 - <<'PY'
   from contextlib import closing
   from pathlib import Path
   import sqlite3

   backup_path = Path(input("Paste backup path from step 2: ").strip()).expanduser().resolve(strict=True)
   live_path = Path("src/raincheck.db").resolve(strict=True)
   old_uri = backup_path.as_uri() + "?mode=ro"
   new_uri = live_path.as_uri() + "?mode=ro"
   counts_sql = (
       "SELECT (SELECT COUNT(*) FROM brands), (SELECT COUNT(*) FROM items), "
       "(SELECT COUNT(*) FROM item_keywords), (SELECT COUNT(*) FROM listings), "
       "(SELECT COUNT(*) FROM listing_matches)"
   )
   names = ("brands", "items", "item_keywords", "listings", "listing_matches")

   with closing(sqlite3.connect(old_uri, uri=True)) as before:
       with closing(sqlite3.connect(new_uri, uri=True)) as after:
           if before.execute("PRAGMA user_version").fetchone()[0] != 1:
               raise RuntimeError("Backup is not schema version 1")
           if after.execute("PRAGMA user_version").fetchone()[0] != 2:
               raise RuntimeError("Upgraded database is not schema version 2")
           if after.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
               raise RuntimeError("Upgraded database failed integrity_check")
           if after.execute("PRAGMA foreign_key_check").fetchone() is not None:
               raise RuntimeError("Upgraded database has broken foreign keys")
           if after.execute("SELECT COUNT(*) FROM market_price_history").fetchone()[0] != 0:
               raise RuntimeError("History should be empty before the first pricing run")
           old_counts = before.execute(counts_sql).fetchone()
           new_counts = after.execute(counts_sql).fetchone()
           if old_counts != new_counts:
               raise RuntimeError(f"Existing table counts changed: {old_counts} -> {new_counts}")
           print("Preserved rows:", dict(zip(names, new_counts)))
           print("Schema version 2, integrity, foreign keys, and empty history verified")
   PY
   ```

   The next authorized eBay pricing run creates the first observations.

If migration fails, leave jobs stopped and inspect the error before retrying.
The migration transaction rolls back a failed step; retain the backup.
No agent runs these instructions against the user's real database.

### Interfaces

Add `record_market_price_observation(item_id, price_usd, candidate_count, outlier_count, retained_count, observed_at=None) -> bool` in `src/db.py`.
The optional `observed_at` is an aware UTC `datetime`, defaulting to the current UTC time.
Validate before opening a connection: `item_id` is an integer but not a boolean; counts are nonnegative integers but not booleans; and `candidate_count == outlier_count + retained_count`.
`price_usd` is either `None` or a finite, nonboolean number greater than zero; a price is present exactly when at least five listings are retained.
Invalid arguments, including a naive or non-UTC timestamp and a null price with five or more retained listings, raise `ValueError` with no write.

The writer starts `BEGIN IMMEDIATE`.
For an existing item and a non-null price, update only its `market_price_usd`, insert one history row, then commit.
For an existing item and a null price, leave the item unchanged, insert one row, then commit.
Return `True` on commit.
If the item was deleted after `get_tracked_items()`, return `False` and add no row.
On any SQL or commit failure, roll back both effects, propagate the database error, and close the connection.
Never write `manual_price_usd` or another item field.

Retain `update_market_price(item_id, price)` as a price-only compatibility operation, documenting that it does not represent a completed eBay sample.
The eBay job becomes the production caller of the observation writer and calls it once for every returned `PriceEstimate`, including `price_usd=None`.
Preserve the existing insufficient-sample output and stop-the-run behavior for search exceptions.
Earlier items' committed observations remain if a later item fails.

## Acceptance criteria

1. Fresh initialization creates schema version 2, the empty history table, its foreign key and index, and the existing tables unchanged.
2. A version 1 database with existing prices, catalog rows, listings, and matches migrates to version 2 with all old rows unchanged and empty history.
3. A recognized version 0 database migrates through steps 1 and 2; rerunning `init_db()` at version 2 changes nothing.
4. A forced failure within migration 2 leaves version 1, old rows intact, and no partial history table or index.
5. `PriceEstimate(120.0, 5, 0, 5)` creates one row for the correct item with those values and a UTC timestamp, and sets its current market price to `120.0`.
6. A six-candidate, one-outlier, five-retained estimate stores `6, 1, 5`, not the API's overall `total`.
7. `PriceEstimate(None, 4, 0, 4)` creates a null-price row with those counts and preserves both stored market and manual prices.
8. `PriceEstimate(None, 0, 0, 0)` creates a zero-count row and preserves the current price.
9. Two completed runs append two rows even with identical price and timestamp; the most recent usable estimate is the current price.
10. A multi-item run adds exactly one row per completed item, linked to each item's own ID, and an insufficient first item does not block a successful second item.
11. A failed history insert rolls back the matching current-price update; a failed current-price update leaves no history row; both error paths close the connection.
12. A deleted or unknown item ID returns `False` with no price change and no history row.
13. Invalid price, count, or timestamp arguments raise `ValueError` before database access; direct invalid SQL inserts fail the table constraints.
14. HTTP 500, timeout, and malformed response-container failures produce no row or price change for the failing item and stop later items; completed earlier items remain committed.
15. Existing `update_market_price(item_id, price)` callers retain their return and price-only behavior; seeding preserves eBay prices and deactivated items retain history.
16. `README.md` explains null observations, preserved current price, and a read-only history query ordered by `observed_at DESC, id DESC`.

## Edge cases

- Empty results: write a completed zero-count, null-price observation.
- `None` or missing values: RC-09 discards malformed summaries; an estimator result with `price_usd=None` is valid.
- Network failure or bad HTTP status: write no row for the failed item; retain current timeout and run-stopping behavior.
- Malformed input: reject malformed API containers under the existing estimator contract; reject inconsistent writer arguments before opening the database.
- Duplicates: RC-09 deduplicates eBay item IDs before counting; repeated completed pricing runs append separate rows.
- Unknown item: return `False` without orphan history.
- Deactivated item: retain history; future jobs read only active items.
- Tied timestamps: allow both rows and order by timestamp then ID.

## Test plan

Use `tests/conftest.py`'s throwaway `seeded_db`, `db_conn`, fake HTTP, and network-blocking fixtures.
Reuse recorded `tests/fixtures/ebay/search_ramones.json` and `search_empty.json`, plus RC-09's synthetic valid and thin responses.
Seed two active items and an existing market price to prove item association and null-observation preservation.
Build a version 1 database in a temporary path from the pre-change schema or a dedicated fixture; reuse the version 0 fixture for the two-step migration case.
Inject fixed UTC datetimes for deterministic timestamps and tie ordering.
Use a temporary constraint trigger or monkeypatch on SQLite execution to force an insert failure after a current-price update, and check the rollback and connection closing under strict warnings.
Adapt version expectations in migration, CLI, and other affected tests; keep old `update_market_price` cases as compatibility checks.
No tests open the user's database, access the network, or send email.

## Shared-file conflicts

RC-06 touches `src/schema.sql` and `src/db.py`, so the project rule prevents it from running in parallel with another feature touching either shared file or `src/catalog.json`.
It also touches `src/ebay_pricing.py`, so pricing-job changes require sequencing or a rebase.
RC-09 and RC-17 are merged.
The separately merged RC-08 changes Yahoo currency conversion and does not overlap RC-06 production files.

## Open questions

1. **Should a completed search with too few listings create a history row?**
   Recommendation: yes, with `price_usd=NULL` and all counts, as specified.
   This exposes gaps without changing the last usable benchmark.
   Only recording successful prices makes a simpler chart but hides thin searches.
2. **Should failed requests and malformed response containers create rows?**
   Recommendation: no for RC-06; treat absence as incomplete and preserve the current stop-the-run behavior.
   A separate failure ledger would improve auditability but needs statuses and error details, better handled with RC-10.
3. **Should existing market prices be backfilled?**
   Recommendation: no, because their timestamps and sample sizes are unknown.
   History therefore begins on the first post-upgrade eBay run even if a current price already exists.

## Rollback

Before upgrading the user's database, revert the code; version 1 remains compatible with the prior code.
After upgrading, old code refuses version 2, so reverting code alone is insufficient.
Stop scheduled jobs and restore the verified pre-RC-06 SQLite backup to return to version 1, accepting loss of any post-backup data.
If post-backup data must be retained, develop and test a separate down-migration against a copy; RC-06 does not ship one.
