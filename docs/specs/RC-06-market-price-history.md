# RC-06: Market price history from distinct eBay listings

**Status:** Approved
**Backlog item:** RC-06 in `docs/BACKLOG.md`

## Summary

Store each newly discovered eBay listing's first observed USD asking price once per tracked item.
Keep every valid listing observation permanently, filter outliers across the accumulated pool, and use the arithmetic mean of the retained prices as `items.market_price_usd` once at least five remain.

## Motivation

`ebay_market_price` currently deduplicates `itemId` only within one search response, calculates a median from that page, and discards the listing IDs and prices afterward (`src/ebay_pricing.py:50-114`).
`refresh_market_prices` overwrites `items.market_price_usd` with that day's estimate (`src/ebay_pricing.py:116-155`).
A listing seen on several days can therefore influence several daily estimates, while the database retains no listing-level evidence for a longer-term average.
RC-09's validation and median absolute deviation (MAD) filter provide a starting point, but RC-06 changes the pool from one response to every distinct captured listing.

The official [eBay Browse discovery guide](https://developer.ebay.com/develop/guides/buy/inventory-discovery-and-refresh-guide) documents `sort=newlyListed`, `limit`, `offset`, and REST item IDs.
The official [Browse API overview](https://developer.ebay.com/api-docs/buy/api-browse.html) distinguishes a legacy listing ID from a REST item ID that can identify one variation of a listing, and describes checking later price revisions with `getItem`.
The saved `tests/fixtures/ebay/search_live.json` has `legacyItemId` and `itemCreationDate` on all 50 rows, and three REST IDs with variation suffixes.
Those fields make listing-level deduplication feasible without a new API call.
The current test fixture `search_ramones.json` omits `legacyItemId`, so a safe fallback is needed.

## Scope

**In scope:**

- Persist each valid new listing's canonical eBay listing ID, first observed USD price, and first observed UTC timestamp per tracked item.
- Keep all raw valid listing observations forever, including prices that the current MAD calculation classifies as outliers.
- Recompute MAD over the complete accumulated pool after each completed search and calculate the arithmetic mean of retained prices.
- Update the current eBay price only when at least five distinct, non-outlier listing observations exist.
- Use one SQLite transaction for new listing inserts and any current-price update.
- Add only `fake` as an exclude keyword for both tracked catalog items, then apply a targeted data update to the existing database after the schema upgrade.
- Add a version 1 to 2 migration and a backup-first upgrade procedure.
- Update the RC-06 backlog summary to describe distinct listing observations and the all-time filtered arithmetic mean.

**Out of scope:**

- Daily estimate rows or a separate computed snapshot table.
  The listing rows and first-seen timestamps are the stored history; a past running mean is not saved and requires replaying rows through the same filter.
- Deleting observations or excluding them from the calculation because they are old, sold, ended, or absent from a later search.
- Replacing a listing's stored first observed price when its eBay asking price changes.
- Fetching sold prices, shipping, taxes, negotiated Best Offers, or other marketplaces and currencies.
- Additional search pages beyond the first 50 results, live availability checks, eBay notifications, or background price revision checks.
- Changing Yahoo deal math, manual catalog prices, the RC-17 matcher, or seeding behavior.
- Adding `replica`, `Rikko`, or any other new include or exclude keyword.

## Design

### Files touched

| File | Change |
| --- | --- |
| `src/schema.sql` | Define schema version 2 and the persistent `ebay_listing_prices` table. |
| `src/catalog.json` | Append `fake` to the exclude keywords for the two existing Rick Owens items only. |
| `src/db.py` | Add migration 2 and the transactional pool writer and calculator. |
| `src/ebay_pricing.py` | Return validated listing observations instead of a page median; submit each completed search to the database writer. |
| `tests/test_ebay_listing_prices.py` | Test canonical IDs, cumulative mean and MAD, deduplication, immutability, atomicity, and missing data. |
| `tests/test_ebay_pricing.py` | Adapt RC-09 estimator and job tests to the cumulative listing pool, first-page search policy, and `fake` exclusion. |
| `tests/test_matching.py` | Update exact real-catalog keyword assertions and test whole-word `fake` rejection. |
| `tests/test_yahoo_scrape.py` | Prove that the shared matcher also blocks `fake` Yahoo listing titles for both database-backed items. |
| `tests/test_db.py` | Update exact seeded-keyword counts and tracked-item tuples for the sample fixture. |
| `tests/fixtures/catalog.json` | Add only the same `fake` exclude to both sample items so database-backed tests exercise it. |
| `tests/test_migration.py` | Check fresh version 2, version 1 to 2 and version 0 to 2 migration, preservation, and rollback. |
| `tests/test_db_cli.py` | Update schema-version expectations and future-version checks. |
| `tests/test_listing_matches.py` | Update the fresh-schema version assertion. |
| `tests/test_seed_catalog.py` | Update any version expectations and assert inactive items keep listing observations. |
| `tests/dbhelpers.py` | Include the new table in full database snapshots while retaining old-version fixture support. |
| `tests/fixtures/ebay/search_ramones.json` | Add canonical listing IDs and buying options to the saved-shape test response if required by the approved policy. |
| `README.md` | Explain the permanent distinct-listing pool, current price rule, and how to inspect listing history. |
| `docs/BACKLOG.md` | Replace RC-06's obsolete per-run median-history summary with distinct listing asks and the all-time filtered mean. |

`tests/conftest.py` already supplies an offline HTTP fake and a throwaway database.
`src/matching.py` and `src/yahoo_scrape.py` need no change.
The existing real catalog has Ramones excludes `drkshdw`, `canvas`, `low`, and `ローカット`, and Pod Shorts exclude `pants`.
The sample catalog omits the Japanese Ramones exclude but has the same two tracked items.
Both catalogs must keep every existing include and exclude in its current order, appending only `fake` to each exclude list.
Do not replace database keywords wholesale because the user may have added DB-only rows.
If another file proves necessary, amend this list before editing it.

### Schema changes and migration

Increase `SCHEMA_VERSION` and the fresh-schema comment from 1 to 2.
Append `MIGRATIONS[2]`; do not modify the shipped migration 1.
Fresh creation and migration 2 must produce the same table and index:

```sql
CREATE TABLE ebay_listing_prices (
    item_id            INTEGER NOT NULL REFERENCES items(id),
    ebay_listing_id    TEXT NOT NULL CHECK (length(ebay_listing_id) > 0),
    first_price_usd    REAL NOT NULL CHECK (first_price_usd > 0),
    first_seen_at      TEXT NOT NULL,
    PRIMARY KEY (item_id, ebay_listing_id)
);
CREATE INDEX idx_ebay_listing_prices_item_seen
    ON ebay_listing_prices (item_id, first_seen_at);
```

The composite primary key counts one eBay listing once for each tracked item whose matching rule accepts it.
A matching listing may belong to more than one tracked item's price pool, but it is never counted twice within one pool.
`first_seen_at` is one UTC ISO 8601 timestamp with an explicit `+00:00` offset, captured when a successful search result is processed.
It is the discovery time, not eBay's `itemCreationDate`, so a listing first encountered today enters today's cumulative pool even if the seller posted it earlier.
The stored price and first-seen timestamp are immutable after insertion.
The user approved this first-ask policy, so a later eBay price revision does not rewrite historical evidence.
Do not add an outlier flag to the table, because an observation can change from outlier to inlier or the reverse as the cumulative pool changes.

The existing `_migrate` applies migration 2 and its version bump in one `BEGIN IMMEDIATE` transaction.
A recognized version 0 database goes through unchanged migration 1 then migration 2; version 1 applies only migration 2.
All existing item, keyword, Yahoo listing, and match rows and stored market prices remain unchanged.
The new table starts empty because past eBay listing identities, prices, and capture times were never persisted.
The migration does not change `item_keywords`; the approved `fake` exclusions reach the existing user database through the separate targeted step below.
`get_connection()` keeps refusing jobs on an older schema until the explicit upgrade.
The foreign key blocks orphaned price observations; catalog deactivation preserves item rows and their observations.

#### Existing database upgrade after merge

Only the user should run these steps against the real database after merging and pulling the code.
Stop scheduled jobs and run from the repository root on the machine containing `src/raincheck.db`.
Do not run `--seed`; it would replace complete keyword sets and might erase DB-only keywords.
The separate targeted `fake` update below adds at most one exact exclude row for each of the two items.

1. Confirm `src/raincheck.db` exists and has `PRAGMA user_version = 1` through a read-only SQLite connection.
2. Make a timestamped SQLite online backup outside the repository.
   The online backup captures a consistent state even when SQLite uses a write-ahead log:

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
4. Run this read-only check before the next eBay run, pasting the backup path printed in step 2 when prompted:

   ```sh
   python3 - <<'PY'
   from contextlib import closing
   from pathlib import Path
   import sqlite3

   backup_path = Path(input("Paste backup path from step 2: ").strip()).expanduser().resolve(strict=True)
   live_path = Path("src/raincheck.db").resolve(strict=True)
   counts_sql = (
       "SELECT (SELECT COUNT(*) FROM brands), (SELECT COUNT(*) FROM items), "
       "(SELECT COUNT(*) FROM item_keywords), (SELECT COUNT(*) FROM listings), "
       "(SELECT COUNT(*) FROM listing_matches)"
   )
   names = ("brands", "items", "item_keywords", "listings", "listing_matches")
   with closing(sqlite3.connect(backup_path.as_uri() + "?mode=ro", uri=True)) as before:
       with closing(sqlite3.connect(live_path.as_uri() + "?mode=ro", uri=True)) as after:
           if before.execute("PRAGMA user_version").fetchone()[0] != 1:
               raise RuntimeError("Backup is not schema version 1")
           if after.execute("PRAGMA user_version").fetchone()[0] != 2:
               raise RuntimeError("Upgraded database is not schema version 2")
           if after.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
               raise RuntimeError("Upgraded database failed integrity_check")
           if after.execute("PRAGMA foreign_key_check").fetchone() is not None:
               raise RuntimeError("Upgraded database has broken foreign keys")
           if after.execute("SELECT COUNT(*) FROM ebay_listing_prices").fetchone()[0] != 0:
               raise RuntimeError("Listing history should be empty before the first pricing run")
           old_counts = before.execute(counts_sql).fetchone()
           new_counts = after.execute(counts_sql).fetchone()
           if old_counts != new_counts:
               raise RuntimeError(f"Existing table counts changed: {old_counts} -> {new_counts}")
           print("Preserved rows:", dict(zip(names, new_counts)))
           print("Schema version 2, integrity, foreign keys, and empty listing history verified")
   PY
   ```

5. Add only the `fake` exclude for the two exact tracked items.
   This step makes another unique backup of the now-version-2 database before any keyword write.
   It compares every keyword row with that backup, refuses conflicting normalized include keywords, and checks that no unrelated keyword changes before committing.
   It is safe to rerun after a successful application because an existing exact `fake` exclude is left alone:

   ```sh
   python3 - <<'PY'
   from contextlib import closing
   from datetime import datetime, timezone
   from pathlib import Path
   import os
   import sqlite3
   import sys
   import unicodedata

   sys.path.insert(0, str(Path("src").resolve()))
   from matching import title_matches

   db_path = Path("src/raincheck.db").resolve(strict=True)
   backup_dir = Path.home() / "RainCheck-backups"
   backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
   stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
   backup_path = backup_dir / f"raincheck-before-rc06-fake-{stamp}.db"
   targets = (
       ("Rick Owens", "rick owens ramones", "Rick Owens Fake Ramones", "ramones"),
       ("Rick Owens", "pod shorts", "Rick Owens Fake Pod Shorts", "pod shorts"),
   )
   find_item = (
       "SELECT items.id FROM items JOIN brands ON brands.id = items.brand_id "
       "WHERE brands.name = ? AND items.name = ?"
   )
   all_keywords = "SELECT item_id, keyword, kind FROM item_keywords"
   normalize = lambda word: unicodedata.normalize("NFKC", word).lower()

   with closing(sqlite3.connect(db_path.as_uri() + "?mode=rw", uri=True,
                                isolation_level=None)) as conn:
       conn.execute("PRAGMA foreign_keys = ON")
       if conn.execute("PRAGMA user_version").fetchone()[0] != 2:
           raise RuntimeError("Expected schema version 2; database left unchanged")
       item_ids = []
       for brand, name, _, _ in targets:
           matches = conn.execute(find_item, (brand, name)).fetchall()
           if len(matches) != 1:
               raise RuntimeError(f"Expected exactly one {brand} / {name} item")
           item_ids.append(matches[0][0])
       if len(set(item_ids)) != 2:
           raise RuntimeError("Target items are not distinct")
       before = set(conn.execute(all_keywords).fetchall())
       if any(
           item_id in item_ids and kind == "include" and normalize(word) == "fake"
           for item_id, word, kind in before
       ):
           raise RuntimeError("A target item includes fake; database left unchanged")

       fd = os.open(backup_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
       os.close(fd)
       with closing(sqlite3.connect(backup_path)) as backup:
           conn.backup(backup)
           if backup.execute("PRAGMA quick_check").fetchone()[0] != "ok":
               raise RuntimeError("Backup integrity check failed; database left unchanged")
           if backup.execute("PRAGMA user_version").fetchone()[0] != 2:
               raise RuntimeError("Backup version differs; database left unchanged")
           if set(backup.execute(all_keywords).fetchall()) != before:
               raise RuntimeError("Backup keywords differ; database left unchanged")

       conn.execute("BEGIN IMMEDIATE")
       try:
           if conn.execute("PRAGMA user_version").fetchone()[0] != 2:
               raise RuntimeError("Schema changed after backup")
           for (brand, name, _, _), item_id in zip(targets, item_ids):
               if conn.execute(find_item, (brand, name)).fetchall() != [(item_id,)]:
                   raise RuntimeError("Target item changed after backup")
           if set(conn.execute(all_keywords).fetchall()) != before:
               raise RuntimeError("Keywords changed after backup")
           for item_id in item_ids:
               conn.execute(
                   "INSERT INTO item_keywords (item_id, keyword, kind) "
                   "VALUES (?, ?, ?) ON CONFLICT(item_id, keyword) DO NOTHING",
                   (item_id, "fake", "exclude"),
               )
           expected = before | {(item_id, "fake", "exclude") for item_id in item_ids}
           after = set(conn.execute(all_keywords).fetchall())
           if after != expected:
               raise RuntimeError("Unexpected keyword change")
           for (_, _, fake_title, include), item_id in zip(targets, item_ids):
               rows = conn.execute(
                   "SELECT keyword, kind FROM item_keywords WHERE item_id = ?",
                   (item_id,),
               ).fetchall()
               excludes = tuple(word for word, kind in rows if kind == "exclude")
               if "fake" not in excludes or title_matches(
                   fake_title, (include,), excludes
               ):
                   raise RuntimeError("fake exclusion did not block the target title")
               if not title_matches(fake_title.replace("Fake", "Fakeout"),
                                    (include,), ("fake",)):
                   raise RuntimeError("fake unexpectedly matched inside a larger word")
           conn.execute("COMMIT")
       except BaseException:
           if conn.in_transaction:
               conn.execute("ROLLBACK")
           raise

   print(f"Backup: {backup_path}")
   for (_, name, _, _), item_id in zip(targets, item_ids):
       prior = sum(1 for row in before if row[0] == item_id)
       final = sum(1 for row in after if row[0] == item_id)
       print(f"{name}: keyword rows {prior} -> {final}; fake exclude verified")
   PY
   ```

The next authorized eBay pricing run begins accumulating listing observations and uses the new excludes.
If migration or the targeted update fails, leave jobs stopped and inspect the error before retrying.
Each transaction rolls back its own failed step; retain both backups.
No agent runs these instructions against the user's real database.

### Interfaces

Replace the page-level median result of `ebay_market_price(query, keywords, exclude_keywords, token)` with a clearly named function such as `ebay_listing_prices(query, keywords, exclude_keywords, token) -> tuple[ListingPrice, ...]`.
`ListingPrice` holds `ebay_listing_id: str` and `price_usd: float`; it may also hold the raw REST ID for diagnostics, but no raw response body is stored.
This deliberately changes the RC-09 estimator interface and requires its tests and the old four-argument signature assertion to be updated.
Reuse RC-09's title matcher, USD-only check, finite positive price validation, and skip-malformed-row behavior.
Return validated raw listing observations before outlier filtering.
Require `FIXED_PRICE` in a row's `buyingOptions`; skip auction-only rows and rows without a usable `buyingOptions` list.
A listing may also allow `BEST_OFFER` and still qualify when `FIXED_PRICE` is present.
The user approved fixed-price-only sampling because an auction bid is not a fixed asking price.
The official [buying-options filter reference](https://developer.ebay.com/api-docs/buy/static/ref-buy-browse-filters.html) distinguishes `FIXED_PRICE` from `AUCTION`.
Do not drop a valid high or low price at the page boundary; outlier status is calculated over the cumulative pool in the database writer.

Determine the canonical listing ID as follows:

1. If `legacyItemId` is a nonempty decimal string, use `legacy:<digits>`.
2. If `legacyItemId` is absent or invalid and `itemId` has the documented `v1|<digits>|<digits>` shape, use the middle listing component as `legacy:<digits>`.
3. If neither applies but `itemId` is a nonempty string, use `rest:<exact itemId>` as an opaque fallback for older or synthetic response shapes.
4. If both a decimal `legacyItemId` and parseable REST `itemId` are present but their listing components disagree, skip that malformed row.
5. If no usable ID exists, skip the row.

Use a set of canonical IDs to keep the first valid appearance within one response.
If a malformed first appearance is followed by a valid copy, the valid copy wins.
The database primary key deduplicates the same listing across runs.
For `rest:` fallback IDs that lack a listing component, variations cannot be proven to share a parent listing; this limitation must be printed only as an aggregate skip or fallback count, without dumping titles.
The official Browse format and recorded live response provide the normal `legacy:` path.

Keep the current one-page `limit=50` search and 30-second timeout.
Request `sort=newlyListed` to improve discovery of different listings on later days.
This does not guarantee complete coverage when more than 50 relevant new listings appear between runs; pagination is out of scope.
Keep `EBAY_US`, the query, and the bearer token.
If the response top level is not an object or `itemSummaries` is present but not a list, raise `ValueError`.
An absent or empty `itemSummaries` produces an empty tuple.
HTTP errors and timeouts propagate as they do today.

Add `record_ebay_listing_prices(item_id, listings, observed_at=None) -> PoolResult` in `src/db.py`.
The result reports `item_found`, `new_count`, `duplicate_count`, `candidate_count`, `outlier_count`, `retained_count`, `mean_usd: float | None`, and whether the current market price was updated.
Validate item ID, UTC timestamp, and every listing ID and finite positive USD price before opening a connection.
Use one `BEGIN IMMEDIATE` transaction per completed item search.
Insert every valid new canonical ID with `ON CONFLICT(item_id, ebay_listing_id) DO NOTHING`, without changing existing prices or timestamps.
Load all stored prices for that item, including earlier outliers.
Apply RC-09's single-pass MAD rule to the complete pool: compute median `m` and `MAD=median(abs(price-m))`; if MAD is zero retain only values equal to `m`, otherwise retain values with `abs(price-m) <= 3.5 * 1.4826 * MAD`.
Compute `statistics.mean(retained)` without rounding when at least five prices remain, and update `items.market_price_usd` for only that item.
If fewer than five remain, leave the existing market and manual prices unchanged.
Commit all new rows and the current-price change together.
Return a no-item result without a write if the item vanished since `get_tracked_items()`, and roll back and close on every error.
A completed empty search creates no listing rows; it may recompute counts but must leave the current price unchanged when no IDs were added.
A completed repeat-only search inserts nothing, does not alter first prices or timestamps, and does not rewrite the current price.
`update_market_price(item_id, price)` remains a price-only compatibility function for direct callers and tests, with documentation saying it is not the eBay job's path.

The eBay job calls the new writer once per completed search, prints new and duplicate counts plus cumulative candidate, outlier, and retained counts, and explains whether the current price was updated or preserved.
An eBay request error, malformed response container, or token failure causes no write for the failing item and retains the current stop-the-run behavior.
Earlier items' committed rows and prices remain.
Manual catalog fallback remains the Yahoo job's behavior when no usable `market_price_usd` exists.

## Acceptance criteria

1. Fresh `init_db()` creates schema version 2 with the empty `ebay_listing_prices` table, composite primary key, item foreign key, and item/time index.
2. A version 1 database with items, keywords, Yahoo listings, listing matches, and stored eBay prices migrates to version 2 with all old rows unchanged and an empty eBay listing table.
3. A recognized version 0 database migrates through versions 1 and 2; running `init_db()` again changes nothing.
4. A forced error inside migration 2 leaves the database at version 1 with no partial eBay listing table or index.
5. Five new, distinct matching fixed-price USD listings at `100, 110, 120, 130, 140` produce five stored listing rows, no outliers, mean `120.0`, and current `market_price_usd=120.0`.
6. Two later new listings priced `150, 160`, while the first five IDs repeat, add exactly two rows and calculate the mean of all seven retained prices once each.
7. Four total distinct usable listings persist four rows but preserve the prior `market_price_usd` and `manual_price_usd`; a later run adding a fifth usable listing updates the current price from all five.
8. Six raw prices `100, 105, 110, 115, 120, 1000` all persist, but MAD removes `1000` from the current arithmetic mean, which is `110.0` from five retained prices.
9. If an extreme value later becomes an inlier as new prices arrive, it can contribute without re-fetching or reinserting its ID because raw prices were retained.
10. When MAD is zero, five prices of `100` and one of `1000` store six rows, retain five `100` prices, and set the mean to `100.0`.
11. Repeating a completed search with no new canonical IDs does not add rows, change first-observed values, or rewrite the current price, even if a repeated listing's ask changed.
12. An empty completed search adds no rows and preserves the current price; an insufficient pool still reports its existing cumulative counts.
13. Two REST variation IDs with the same numeric legacy listing ID contribute one row to the same item's pool; a matching row for a different tracked item may add its own row.
14. A missing `legacyItemId` falls back to the numeric listing component of a valid REST ID, then to an opaque `rest:` ID only if necessary; missing IDs and disagreeing parseable IDs are skipped.
15. Malformed titles, prices, currencies, and IDs are skipped under the documented validation rules; a malformed response container raises `ValueError` without a write.
16. A simulated failure during insertion, outlier calculation, price update, or commit rolls back all rows and price changes for that item and closes the connection.
17. Unknown or deleted item IDs add no rows and change no prices; invalid writer inputs raise `ValueError` before database access.
18. An HTTP 500 or timeout writes nothing for the failing item, stops later searches, and leaves earlier items' committed observations in place.
19. Seeding leaves `market_price_usd` and eBay listing observations untouched, and deactivated items retain their observation rows.
20. The search uses one GET per reached item with `q`, `limit=50`, `sort=newlyListed`, the `EBAY_US` header, and the existing timeout; README describes the first-page coverage limit and all-time averaging rule.
21. The post-merge procedure makes a verified backup before migration and compares all five old table counts to the read-only backup after version 2 is installed.
22. `src/catalog.json` retains the three Ramones includes, its four existing excludes, the three Pod Shorts includes, and its existing `pants` exclude in their original order, appending only `fake` to each exclude list; `tests/fixtures/catalog.json` likewise preserves all its existing terms and appends only `fake` to both exclude lists.
23. After seeding the sample catalog into a throwaway database, both tracked items have exactly one `fake` exclude and all their prior keyword rows; `title_matches` rejects `Rick Owens Fake Ramones` and `Rick Owens Fake Pod Shorts` but does not treat `fake` inside `Fakeout` as a word match.
24. The eBay and Yahoo jobs each reject otherwise matching titles containing whole-word `fake` for either tracked item after loading the seeded database, without writing a price observation or emailing that listing.
25. The targeted post-merge keyword procedure backs up schema version 2 before writing, requires exactly the two named items, and adds at most two exact `fake` exclude rows while preserving every other keyword row, including database-only keywords; rerunning it adds no rows.
26. If either target has an include keyword that normalizes to `fake`, the targeted update refuses to write; if keyword rows change after the backup or a later check fails, its transaction rolls back both possible inserts.
27. An auction-only summary or one lacking a valid `buyingOptions` list is skipped even if its title and USD price are valid; `FIXED_PRICE` with or without `BEST_OFFER` is eligible.
28. The RC-06 section of `docs/BACKLOG.md` describes persistent distinct listing asking prices and their all-time MAD-filtered arithmetic mean, without claiming that the feature stores a median or one history row per run.

## Edge cases

- Empty results: no new rows and no current-price write.
- `None` or missing values: skip invalid summaries; keep the manual fallback if the market price is still null.
- Network failure or bad HTTP status: no write for the failing item; stop the run under existing behavior.
- Malformed input: skip bad rows and reject bad response containers or writer arguments before writing.
- Duplicates: first valid occurrence wins within a response; the database primary key prevents repeats across days and variation IDs with the same canonical listing ID.
- Changed asking price for a known ID: keep the first observed price and timestamp.
- All-time pool: old observations, including ended listings, remain eligible forever if they pass the current MAD filter.
- No new IDs: preserve current price rather than issue an identical update.
- Tied first-seen timestamps: use `ORDER BY first_seen_at, ebay_listing_id` for stable historical inspection.
- Existing keyword sets: preserve all current include and exclude rows, including DB-only terms, and add only an exact `fake` exclude for the two named items.
- Unicode keyword collision: refuse a target item whose include keyword normalizes to `fake`, such as full-width `ｆａｋｅ`.
- Partial data update: fail and roll back both keyword inserts if the second item or stored keyword set changes after the backup.

## Test plan

Use the offline HTTP fakes, `seeded_db`, `db_conn`, and strict warning checks already in `tests/conftest.py`.
Reuse `tests/fixtures/ebay/search_live.json` as the recorded Browse shape, and add `buyingOptions` to the small `search_ramones.json` fixture where the approved fixed-price rule requires it.
Create synthetic responses with two days of overlapping IDs, a later new fifth ID, changed asks, two variations of one legacy listing, malformed identifiers, missing and auction-only buying options, empty results, and a valid high outlier.
Assert the raw stored rows, immutable prices, cumulative MAD counts, arithmetic means, and item-specific current prices.
Inject a fixed UTC `observed_at` so ordering and first-seen behavior are deterministic.
Build a throwaway schema version 1 database and reuse the legacy version 0 fixture for the two migration paths.
Force a writer failure after at least one insert and after an item-price update to prove transaction rollback and connection closure.
Update RC-09 assertions that previously expected a per-page median or `PriceEstimate` return; keep its row-validation and MAD boundary coverage at the new pool-calculation layer.
Assert exact real and sample catalog keyword sets before and after adding only `fake`, including their preexisting Japanese and DB-only keywords.
Exercise `title_matches` with `Fake Ramones`, `Fake Pod Shorts`, and larger Latin words such as `Fakeout`, then check both the eBay job and Yahoo job use the stored exclusions.
Run the targeted update procedure only against throwaway version 2 databases, including already-present `fake`, normalized include conflict, and unexpected keyword change cases; never run it against the real database.
No test touches the network, sends email, or opens the user's real database.

## Shared-file conflicts

RC-06 touches `src/schema.sql` and `src/db.py`, so it must not run in parallel with any other feature touching those files or `src/catalog.json`.
It also changes `src/ebay_pricing.py` and its tests.
RC-09, RC-17, and RC-08 are merged; later pricing changes must start from the merged RC-06 branch or rebase onto it.

## Open questions

None.
The first-price, fixed-price, newest-first, all-time-pool, five-retained minimum, and exact `fake` exclusion choices are resolved below.

## Resolved

- 2026-10-27: The user chose to retain raw listing observations forever and include all distinct retained listing prices forever in the current running arithmetic average.
- 2026-10-27: The user chose to keep the previous market price until at least five non-outlier listing observations have accumulated across days.
- 2026-10-27: The user approved fixed-price-only observations, immutable first observed asking prices per canonical listing ID, and `sort=newlyListed` within the first 50 results.
- 2026-10-27: The user chose to add only `fake` to the exclude keywords of the two currently tracked catalog items, `Rick Owens` / `rick owens ramones` and `Rick Owens` / `pod shorts`.
  The user did not approve adding `replica` or `Rikko`.
  Existing real-database keyword sets must be preserved through a targeted backup-first update instead of a full seed.
- 2026-10-27: The user approved the complete revised RC-06 spec after reviewing listing-level history, the cumulative mean, the five-retained minimum, and the `fake` exclusion.

## Rollback

Before upgrading the user's database, revert the code; the previous code still reads version 1.
After upgrading, the previous code refuses version 2, so reverting code alone is insufficient.
If only the targeted `fake` update must be undone while keeping schema version 2, make another backup first and remove only the verified `fake` exclude rows for the two named item IDs with parameterized `DELETE` statements bounded by `item_id`, `keyword`, and `kind`; do not replace whole keyword sets.
Stop scheduled jobs and restore the verified pre-RC-06 SQLite backup to return to version 1, accepting loss of any post-backup changes.
If post-backup data must be kept, design and test a separate down-migration against a copy; RC-06 does not ship one.
