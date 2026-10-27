# RC-14: Item management command line tool

**Status:** Draft
**Backlog item:** RC-14 in `docs/BACKLOG.md`

## Summary

Replace the interactive `src/setup.py` catalog writer with commands that add, list, update, remove, and explicitly adopt catalog ownership for tracked items in the SQLite database.
Protect those changes from later catalog seeding while preserving listing matches and eBay price history.
An item is a tracked product, such as Rick Owens Pod Shorts; an individual Yahoo auction for that product is a separate listing.

## Motivation

`src/setup.py:20-48` assigns Rick Owens, size 42, and Ramones keywords to every new item, writes `+` into eBay queries, and saves relative to the current directory.
The jobs read the database, while this script writes only `catalog.json`.
The current seed operation replaces matching item settings and deactivates every active item absent from the catalog (`src/db.py:504-590`), so a database-only command would otherwise lose its changes on the next seed.
Item identity is `(brand_id, name)`, and related tables refer to `items.id` (`src/schema.sql:20-70`).

## Scope

**In scope:**

- Replace `src/setup.py` with an `argparse` command line interface that runs from any working directory and honors `RAINCHECK_DB_PATH`.
- Add database functions for listing all items, adding an item, replacing selected item fields, deactivating an item, and explicitly adopting one item from the catalog.
- Reuse the catalog's keyword and query validation rules for command input, including lowercase, trimming, duplicate removal, and rejection of include/exclude overlap.
- Keep `market_price_usd` and all history tables intact during every item operation.
- Make catalog seeding coexist with CLI managed items, subject to Open question 1.
- Document the commands, seed interaction, and backup-first upgrade steps.

**Out of scope:**

- Managing brands or Yahoo brand IDs (RC-13).
- Renaming items or moving them between brands, which changes the identity used by seeding.
- Permanently deleting items, listing matches, listings, or eBay price observations.
- Setting eBay-derived `market_price_usd` manually, changing price algorithms, or adding size matching.
- Editing the price of an individual Yahoo listing through the item management CLI.
- A graphical interface, import/export format, or network calls.

## Design

### Item and price model

One `items` row defines a tracked product and its matching rules, so multiple individual Yahoo auctions can match the same item through `listing_matches`.
Each Yahoo auction has its own `listings` row, URL, and yen price.
For example, an item named `Pod Shorts` can match two separate Yahoo auctions, each with a different `price_yen`.
`python3 src/setup.py update ID --manual-price 250` changes the fallback USD reference price for that one tracked item, not either auction's yen price.
The eBay pricing job manages that item's `market_price_usd`; when that eBay price is usable, deal comparisons use it before the manual fallback.
The CLI does not edit either a Yahoo listing's price or the eBay-derived market price.

### Files touched

| File | Change |
| --- | --- |
| `src/setup.py` | Replace the import-time prompt loop with `main(argv=None)` and the commands below. |
| `src/db.py` | Add item management helpers and shared single-item validation; update seeding to respect item ownership; add migration 4 if Open question 1 accepts the recommendation. |
| `src/schema.sql` | Add item ownership for fresh databases if Open question 1 accepts the recommendation. |
| `tests/test_setup_cli.py` (new) | Exercise parsing, output, validation, item operations, and exit status through `setup.main`. |
| `tests/test_db.py` | Exercise transactional database helpers and history preservation. |
| `tests/test_seed_catalog.py` | Exercise ownership boundaries and repeated seeds. |
| `tests/test_migration.py`, `tests/test_db_cli.py`, `tests/dbhelpers.py` | Exercise schema 4 migration and update assertions pinned to schema 3. |
| `tests/fixtures/db/schema_v3.sql` (new) | Preserve the pre-RC-14 schema as a migration fixture if schema 4 is approved. |
| `README.md` | Replace the obsolete `setup.py` instructions and explain catalog ownership and database backup. |

`src/catalog.json` remains unchanged and is still seed input.
The jobs in `src/ebay_pricing.py` and `src/yahoo_scrape.py` are unchanged because `get_tracked_items()` still returns the same active-item shape.

### Schema changes and migration

Recommended design, pending Open question 1: schema version 4 adds `items.config_source TEXT NOT NULL DEFAULT 'unclaimed' CHECK (config_source IN ('unclaimed', 'catalog', 'cli'))`.
Migration 4 uses `ALTER TABLE items ADD COLUMN` inside the existing versioned transaction, so every existing item becomes unclaimed and keeps its ID, active state, prices, keywords, and related rows.
Existing databases do not record item provenance, so the migration must not assume that any old row was created from `catalog.json`.
CLI add or update sets `config_source = 'cli'`; CLI remove sets `active = 0` and `config_source = 'cli'`.
The seed operation inserts new rows with `config_source = 'catalog'` and may update or deactivate only catalog managed items.
If a catalog entry has the same brand and name as an unclaimed or CLI managed item, seeding skips that entry and reports it as skipped, leaving that item unchanged.
An unclaimed item absent from the catalog is also left active and unchanged.
The existing `SeedResult` gains a `skipped` tuple with a default empty value so existing calls and checks remain usable.
For a fresh database, the column default makes direct SQL inserts unclaimed; `seed_from_catalog` explicitly marks its own inserted rows catalog managed.
The explicit `adopt ID` command validates that the item's exact brand and name exist in the catalog, then atomically copies that catalog entry's query, manual price, and keyword sets to the item and changes its source to `catalog`.
It preserves the item ID, active state, eBay price, listing matches, and eBay observations.
This is an intentional one-item reconciliation; a later seed may update or deactivate that item according to the catalog.
Only the new migration changes version 3 databases; migration statements 1 through 3 remain unchanged.

For the user's existing database, stop scheduled jobs, use SQLite's `.backup` to create a timestamped copy outside the repository, and run `PRAGMA integrity_check` on the backup before upgrading.
Then run `python3 src/db.py` with `RAINCHECK_DB_PATH` pointing at that database, without `--seed`.
Confirm `PRAGMA user_version = 4`, `PRAGMA integrity_check = ok`, an empty `PRAGMA foreign_key_check`, unchanged row counts for `items`, `item_keywords`, `listings`, `listing_matches`, and `ebay_listing_prices`, and `config_source = 'unclaimed'` for all preexisting items.
Review `python3 src/setup.py list --all` before using `adopt` on any legacy item.
Keep the backup until the CLI and both jobs work normally.
The user performs this procedure after merging; this planning stage never opens the real database.

### Interfaces

The commands require an already initialized, current-version database; they never call `init_db()` or seed automatically.
Run `python3 src/db.py --seed` once for a new database, or `python3 src/db.py` to upgrade an existing one.

| Command | Inputs and effect |
| --- | --- |
| `python3 src/setup.py list [--all]` | Show active items by ascending ID with brand, name, source, search query, manual price, include and exclude keywords; `--all` also shows inactive items and marks them inactive. |
| `python3 src/setup.py add --name NAME --keyword WORD [--keyword WORD ...] [--brand BRAND] [--exclude-keyword WORD ...] [--search-query TEXT] [--manual-price USD]` | Add an active item under an existing brand, or reactivate the same inactive `(brand, name)` row and replace its configuration. |
| `python3 src/setup.py update ID [--keyword WORD ...] [--exclude-keyword WORD ...] [--clear-exclude-keywords] [--search-query TEXT | --clear-search-query] [--manual-price USD | --clear-manual-price]` | Replace only fields explicitly named; repeated keyword flags replace that whole set; at least one change flag is required. |
| `python3 src/setup.py remove ID` | Set the item inactive; keep its row, keyword rows, prices, matches, and eBay observations. |
| `python3 src/setup.py adopt ID` | Read and validate `catalog.json`, require an exact identity match, and apply that item's catalog configuration while marking it catalog managed. |

`add` uses no placeholder keywords, stores `search_query` as plain text, and leaves the manual price null when omitted.
The item name is stripped but retains its case; omitted `--brand` means Rick Owens for the current single-brand database.
An explicit `--brand` must name an existing database brand, and the CLI never invents a Yahoo brand ID.
This works with RC-13 if it lands first: RC-13 may add brands through its own onboarding path, and RC-14 then manages items under those existing brands.
`update` and `remove` identify rows by displayed integer ID, including inactive rows for update; updating an inactive row does not reactivate it.
Adding an already active `(brand, name)` returns an error and changes nothing.
`adopt` accepts active or inactive unclaimed or CLI managed rows; it retains their active state and fails if the catalog has no exact matching entry.
List returns exit 0 and an explicit empty message when there are no matching rows.
Missing database, old schema, unknown ID, invalid values, and SQLite failures return exit 1 with one `Error:` line on stderr and no partial item changes.
Argument syntax errors return exit 2; `--help` returns exit 0 without opening a database.
All writes use parameterized SQL in one `BEGIN IMMEDIATE` transaction, roll back on failure, and close the connection on every path.
Only `adopt` reads `catalog.json`; no CLI command writes it.

Suggested helpers in `src/db.py`: `list_items(include_inactive=False)`, `add_item(...)`, `update_item(item_id, ...)`, `deactivate_item(item_id)`, and `adopt_catalog_item(item_id, catalog_path=None)`.
Use an internal sentinel for omitted update fields so omission differs from explicit clearing.
Centralize keyword, query, and manual-price normalization with catalog validation rather than maintaining incompatible rules.

## Acceptance criteria

1. Importing `setup` performs no prompt, file write, or database access; `setup.main(['--help'])` exits 0 without creating a database.
2. `list` shows active rows in ascending ID order, including source, actual per-item keywords, exclusions, manual price, and query; `list --all` includes inactive rows.
3. Adding `pod shorts` with `--keyword ' Pod Short '` and `--keyword 'pod short'` stores one `pod short` include, no Ramones keywords, and a plain-text query exactly as validated.
   Omitting `--brand` selects Rick Owens; naming an unknown brand fails without creating a brand or item.
4. Adding an item without a manual price or query stores SQL `NULL` in both fields and the jobs can still read the item.
5. Adding an item with the same brand and name as an inactive row reuses its ID, activates it, replaces its item configuration, and preserves its eBay price and historical rows.
6. Adding a duplicate active item exits 1 and leaves every table unchanged.
7. Updating an item changes only the requested fields; omitted flags preserve their previous values, including the include and exclude sets.
   Updating Pod Shorts with `--manual-price 250` changes only that item's `manual_price_usd`; two matched Yahoo listings retain their distinct `price_yen` values, and the item's `market_price_usd` remains unchanged.
8. Repeated `--keyword` or `--exclude-keyword` flags replace their respective full set after normalization; clear flags store `NULL` or an empty exclude set as named.
9. Removing an active item makes it disappear from `get_tracked_items()` and normal `list`, while `list --all` shows it inactive and all related rows and prices remain unchanged.
10. Updating or removing an unknown ID exits 1 and leaves every table unchanged; a second remove is reported as already inactive without further writes.
11. Blank names or keywords, zero or nonfinite manual prices, `+` or `*` in a query, queries over 100 characters, and a keyword in both sets fail before any write.
12. A failed SQL statement during add, update, remove, adopt, or seed rolls back the entire operation and closes its connection.
13. A subsequent `db.seed_from_catalog()` neither changes nor deactivates CLI managed or unclaimed rows, even when the catalog contains the same item identity or omits it.
14. A version 3 fixture containing a database-only item and keywords absent from `catalog.json` migrates to unclaimed ownership; a subsequent seed preserves its active state, ID, query, manual and eBay prices, keywords, matches, and eBay observations.
15. A version 3 fixture containing a row with the same identity as a catalog entry but different database-only keywords migrates to unclaimed ownership; a subsequent seed preserves those keywords and prices rather than copying the catalog values.
16. `adopt ID` on a matching unclaimed row copies the validated catalog configuration and marks it catalog managed while preserving its ID, active state, eBay price, and history; the next seed can update it normally.
17. `adopt ID` with no matching catalog identity, an invalid catalog, or an unknown ID exits 1 without changing any row.
18. Seeding still updates and deactivates catalog managed rows as before, and never overwrites an eBay-derived market price.
19. Fresh databases and databases migrated from version 3 have the same schema 4 shape; migration preserves every existing row and passes integrity and foreign key checks.
20. `README.md` shows a complete add, list, update, remove, and adopt sequence from the repository root and explains which edits a later seed can change.

## Edge cases

- Empty results: `list` prints an empty message and exits 0; `list --all` works even when the `items` table has no rows; `adopt` rejects an identity absent from the catalog.
- `None` or missing values: Omitted query and manual price become `NULL` on add; omitted update options leave stored values alone; clear flags set `NULL`.
- Network failure or bad HTTP status: No network calls occur in this feature.
- Malformed input: Reject invalid numbers, queries, keywords, IDs, and mutually exclusive flags without changing the database.
- Duplicates: Duplicate keywords collapse in first-seen order; duplicate active item names fail; inactive items reactivate under the same ID.
- Concurrent writes: A busy or locked SQLite database reports an error without a partial item change.

## Test plan

Use only `tmp_path` databases through the existing `RAINCHECK_DB_PATH` fixture, with the fixture catalog from `tests/fixtures/catalog.json`.
Seed rows that have listing matches and eBay observations to prove add/reactivate, update, remove, adopt, and seed preserve history.
Use a version 3 throwaway database with both a catalog-named item whose database keywords differ from the catalog and a database-only item omitted from the catalog; migrate, seed, and compare all preexisting item fields and history.
Inject a failing migration statement to prove rollback.
Capture stdout/stderr and exit codes for `setup.main(argv)`; call it from an unrelated working directory.
No saved HTML or API JSON fixtures are needed because this feature makes no network request.

## Shared-file conflicts

This feature changes `src/db.py`, and the recommended ownership design also changes `src/schema.sql`.
It must not implement in parallel with RC-13 or any other feature touching `src/db.py` or `src/schema.sql`.
The backlog currently lists only `src/setup.py` and `src/db.py`; update its `Touches` line when the ownership decision is approved.
If RC-13 lands first, start RC-14 from its merged schema and assign the next schema version rather than reusing version 4.
RC-13's upgrade procedure must not require `--seed` as a way to discover new brands because current seeding can overwrite or deactivate database-only items.

## Open questions

**Resolved (2026-10-27):** The user confirmed that editing the tracked item's manual fallback price is sufficient; the eBay-derived estimate continues to take priority when available.
This resolves the price-scope clarification only; the spec remains Draft pending the ownership and reactivation decisions below.

1. **How should CLI edits survive a later `--seed`?**
   Recommend schema 4 `config_source` ownership as described above: all existing rows become unclaimed, so a later seed cannot silently overwrite or deactivate unknown-provenance data.
   The user can explicitly adopt selected rows after checking their catalog entries, and fresh seed rows retain the current catalog update behavior.
   This requires a migration, one extra field, and a one-time reconciliation for older catalog rows that should keep following `catalog.json`.
   A simpler schema-free alternative is to make `--seed` bootstrap-only and refuse it when a database already contains items; this avoids a migration but removes the existing reseed workflow and would change current tests and documentation.
   Synchronizing CLI changes into `catalog.json` is another alternative, but gives the CLI two write targets and conflicts with the backlog's database-only direction.
2. **Should an inactive item be reactivated by `add`?**
   Recommend yes, using its original ID so listing matches and eBay history stay attached.
   Refusing it would force a separate reactivation command or a destructive workaround.

## Rollback

Before the production upgrade, retain the SQLite `.backup` outside the repository.
If migration 4 or the new CLI fails, stop jobs, restore that backup to the original database path, and check its integrity before running the previous code version.
CLI remove is reversible through `add` with the same name, although its previous configuration must be supplied again.
