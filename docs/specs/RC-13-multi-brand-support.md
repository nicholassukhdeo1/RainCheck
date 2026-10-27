# RC-13: Multi-brand support

**Status:** Approved
**Backlog item:** RC-13 in `docs/BACKLOG.md`

## Summary

Let catalog entries declare their Yahoo brand ID, store that ID with each brand, and fetch one Yahoo results page for each brand with active items.
Combine eligible deals from all brands into the existing one digest per run.

## Motivation

`src/schema.sql` already has `brands.yahoo_brand_id TEXT NOT NULL UNIQUE`, but `src/db.py:62` and `src/db.py:530-531` assign the Rick Owens ID `103538` to every new brand, so a second brand fails seeding.
`src/yahoo_scrape.py:25` and `src/yahoo_scrape.py:399` use one hardcoded Rick Owens page regardless of the active items.
The existing global listing URL uniqueness and `listing_matches` table already support multiple items and brands.

Yahoo's [search change notice](https://auctions.yahoo.co.jp/topic/notice/function/post_3188/) confirms that search results support manufacturer and brand filtering, and Yahoo's [fashion category page](https://auctions.yahoo.co.jp/category/list/23000/) exposes a brand filter.
Yahoo does not publish a stable contract for the `brand_id` query parameter or the embedded `__NEXT_DATA__` shape used here.
The exact URL shape comes from RainCheck's existing Rick Owens URL and saved Yahoo fixture; any other brand ID must be checked in Yahoo's current user interface before adding it to the catalog.

## Scope

**In scope:**

- Add a required `yahoo_brand_id` string to each catalog entry, with the same value on all entries for one brand and a different value for every distinct brand.
- Seed new brands with their declared Yahoo ID and retain the existing transactional item upsert and deactivation behavior.
- Read the brands that have active items from the database and build one category URL per brand using the stored ID.
- Fetch and parse each active brand once, then calculate deals only against that brand's listings.
- Send at most one digest for all brands and record only displayed deals after the send succeeds.
- Distinguish equal item names from different brands in a multi-brand digest.
- Keep a single-brand run's existing URLs, item headings, and ordering.
- Document a staged, reviewable procedure for adding a brand without losing configuration that exists only in the database.

**Out of scope:**

- A brand management command or automatic lookup of Yahoo IDs by brand name.
- Pagination beyond the current first 100 results per brand, concurrent requests, or a new Yahoo API.
- Adding a second real brand or making live requests as part of this feature.
- Changing eBay searches, keyword matching, currency conversion, auction classification, or digest layout.
- Recording every scanned listing or changing URL-based alert deduplication.
- Retrying a failed brand independently or sending a partial digest.
- Changing the current seed command's rule that replaces an item's keyword set and deactivates items absent from the catalog.

## Design

### Files touched

| File | Change |
| --- | --- |
| `src/db.py` | Validate catalog Yahoo IDs, seed brand mappings, and return active brands with IDs in deterministic order. |
| `src/yahoo_scrape.py` | Build a brand-specific URL, fetch and parse once per active brand, isolate item matching by brand, and combine sections into one digest. |
| `src/catalog.json` | Add `"yahoo_brand_id": "103538"` to both current Rick Owens entries without changing other values. |
| `tests/fixtures/catalog.json` | Add the same ID to each Rick Owens fixture entry. |
| `tests/test_seed_catalog.py` | Cover multi-brand validation, seeding, reseeding, and atomic rollback. |
| `tests/test_db.py` | Cover the active-brand reader and existing schema version. |
| `tests/test_yahoo_scrape.py` | Cover one fetch per brand, separated matching, a combined digest, and failure before send. |
| `docs/specs/RC-13-multi-brand-support.md` | Record the approved choices and operational procedure. |

`tests/test_seed_catalog.py` already compares `CatalogEntry` values directly, so its expected records must include the new field.
Keep `RICK_OWENS_URL` as a compatibility constant for existing single-brand tests.
No change to `src/schema.sql` or `src/digest.py` is needed under this design.

### Schema changes and migration

None.
Schema version remains 3 because `brands.yahoo_brand_id` already exists, is non-null, and is unique.
Do not add `MIGRATIONS[4]` or alter shipped migrations.
Existing brand, item, listing, match, and eBay observation rows remain in place.

After merge, the user's existing version 3 database requires no write and no seed.
The stored Rick Owens brand already has Yahoo ID `103538`; adding that value to `catalog.json` changes seed input only.
For the existing one-brand deployment, stop scheduled jobs, make and verify an SQLite online backup using the backup-first procedure in `docs/specs/RC-16-auction-aware-deals.md`, and check the original and backup through read-only connections for `PRAGMA user_version = 3`, `PRAGMA integrity_check = ok`, and empty `PRAGMA foreign_key_check`.
Read-only queries should confirm Rick Owens still has Yahoo ID `103538`, the two tracked items remain active, and the existing keyword and listing rows remain present.
Restart the jobs only after these checks; do not run `db.py --seed` as part of the RC-13 upgrade.
Seeding the current catalog would replace keyword sets and deactivate active database-only items, even when no new brand is being added.
If the real database is still version 2, complete RC-16's separate backup-first version 2 to 3 upgrade first; RC-13 introduces no version 3 to 4 migration.

To intentionally add a new brand later, stop scheduled jobs and first verify its numeric Yahoo ID against Yahoo's current user interface.
Make a fresh verified SQLite online backup outside the repository, including any write-ahead log state, and save it as the immutable pre-change reference.
Copy that backup to a separate writable preview database and point `RAINCHECK_DB_PATH` only at the preview while running the proposed `db.py --seed` with the edited catalog.
Compare every pre-existing row by primary key in `brands`, `items`, `listings`, and `ebay_listing_prices`, every pre-existing `item_keywords` row by `(item_id, keyword)`, and every `listing_matches` row by `(listing_id, item_id)` between the immutable backup and preview.
Require exact equality for those rows, including existing item `active`, manual and market prices, search queries, keyword kinds, listing timestamps, and match history; reject the proposal if any old row is missing or changed.
Review the preview's additional brand, item, and keyword rows and require that they match the intended catalog additions exactly.
If the comparison fails because an active item or keyword exists only in the database, add its full current settings to the catalog, repeat the preview from a fresh backup copy, and review the new diff.
Only after an exact preservation comparison and review should the operator seed the real database with that same catalog while jobs remain stopped, then repeat the row comparison and integrity checks against the immutable backup.
Do not use the real database or its backup in automated tests or agent verification.

### Interfaces

`validate_catalog(entries) -> list[CatalogEntry]` remains pure.
Extend `CatalogEntry` with `yahoo_brand_id: str`.
Require `yahoo_brand_id` on every entry as a non-blank string of ASCII decimal digits; trim surrounding whitespace and store the normalized string without changing leading zeros.
Reject booleans, integers, signs, embedded whitespace, and non-ASCII digits.
Reject two entries with the same brand name but different Yahoo IDs, and two distinct brand names with the same Yahoo ID, in one aggregated `CatalogError` before opening the database.

`seed_from_catalog(catalog_path=None) -> SeedResult` keeps its existing return shape and one transaction.
Insert each new brand with the validated ID.
For an existing brand with a different stored ID, reject the seed with `CatalogError` and roll back all catalog changes, pending the user decision below.
Preserve item IDs, eBay-owned prices, listing history, and existing item deactivation semantics.
The existing seed command still replaces keyword sets and deactivates items omitted from the catalog; the new-brand procedure above must catch these effects before a real seed.

Add `get_active_brands() -> list[TrackedBrand]`, where `TrackedBrand` is an immutable record of database brand ID, name, and Yahoo ID.
Return only brands with at least one active item, ordered by the smallest active item ID, and close the connection on success or failure.
No catalog read occurs in either job.

Add `build_brand_url(yahoo_brand_id: str) -> str`, a pure function using the current category path and fixed query values `auccat=23000`, `s1=new`, `o1=d`, and `n=100` plus the brand ID.
Validate the ID before URL construction; only ASCII decimal digits are accepted, so an ID cannot inject another query field or redirect the host.
For `103538`, the result is exactly the current `RICK_OWENS_URL` string.

`yahoo_scrape.main()` reads active items and active brands before network work, fetches and parses one page per brand in active-item order, and stores parsed listings by brand ID.
It completes every brand fetch and parse before refreshing `last_seen_at`, calculating any deal, sending email, or recording a new listing.
A request timeout, HTTP error, or `YahooPageError` for any brand fails the run with no email and no database writes.
If a URL appears on more than one brand page, remove its later occurrences before updating `last_seen_at`, matching items, or building the source mapping; the first fetched brand owns it for this run.
Once all pages are valid, call the exchange-rate resolver at most once when at least one listing exists, and perform deal calculation in the existing active-item order using only that item's brand page.
Keep the current one-digest send and post-send batch record behavior.
In a run with more than one active brand, use `brand / item name` as each section heading; with one active brand, keep the existing item heading.

## Acceptance criteria

1. A fresh database remains at schema version 3 and has the existing `brands.yahoo_brand_id` uniqueness constraint; no new migration is added.
2. The real and fixture catalogs include `"yahoo_brand_id": "103538"` on both Rick Owens entries and preserve their other fields and order.
3. `validate_catalog` normalizes `" 103538 "` to `"103538"` and rejects a missing ID, `None`, blank string, integer, boolean, non-ASCII digits, and a value containing `&` with a `CatalogError` identifying the entry and field.
4. A catalog with one brand name assigned two IDs, or two brand names assigned one ID, raises `CatalogError` before any database connection is opened.
5. Seeding two brands with distinct IDs creates two brand rows and their respective active items; reseeding the same catalog does not duplicate brands, items, or keywords and preserves item IDs and existing market prices.
6. Seeding a new brand followed by an invalid existing-brand ID change in the same catalog leaves all tables byte-for-byte equivalent at the SQL row level to their pre-seed state.
7. Removing every item of one brand from a subsequent valid catalog deactivates its items but retains its brand and listing history; `get_active_brands()` omits that brand.
8. `get_active_brands()` returns one entry per brand with active items, ordered by first active item ID, and returns an empty list when no items are active.
9. `build_brand_url("103538")` equals the old Rick Owens URL exactly; `build_brand_url("123456")` changes only the `brand_id` value, and rejects an ID containing URL syntax.
10. In a two-brand run, fake HTTP sees exactly one Yahoo GET for each active brand with a 30-second timeout, and at most one exchange-rate GET; there is one digest containing the eligible sections from both brands in active-item order.
11. A title matching a tracked item in brand A but appearing only on brand B's page is not evaluated as a brand A deal and creates no brand A listing match.
12. The digest headings disambiguate identical item names as `brand / item name` when two brands are active; a one-brand run keeps its previous heading and URL.
13. If either brand page is empty but valid, the other brand's deals can still produce one digest; if all pages are valid and empty, no email is sent and no exchange-rate request occurs.
14. If the second brand returns HTTP 500, times out, or serves malformed `__NEXT_DATA__`, the run exits nonzero, sends no email, and leaves listings, matches, and `last_seen_at` unchanged even when the first brand had eligible listings.
15. If the email send fails after all pages parse, no new listings or matches are recorded and both brands' displayed deals remain eligible on the next run.
16. If duplicate Yahoo listing URLs appear across brand pages, the first active brand page owns that URL for this run, the URL appears at most once in the digest, and the stored listing retains the owning brand ID.
17. Upgrading an existing version 3 database with only Rick Owens requires no call to `seed_from_catalog`, leaves `PRAGMA user_version` at 3, and preserves every existing brand, item, keyword, listing, match, and eBay observation row.
18. In a staged new-brand seed on a temporary copy containing a database-only include or exclude keyword, or an active item absent from the catalog, the required row comparison detects the deletion or deactivation and the live database remains unchanged.
19. When the catalog includes the full existing database configuration plus one new brand, a staged seed on a temporary copy preserves every pre-existing row and adds only the requested brand, item, and keyword rows; the second seed of that same catalog is idempotent.

## Edge cases

- Empty results: An active brand may have a valid page with zero listings; no-active-item and all-empty runs send nothing.
- `None` or missing values: Missing catalog Yahoo IDs are invalid; existing item prices may still be `None` and retain the current skip behavior.
- Network failure or bad HTTP status: Any brand fetch failure fails the whole run before email or database writes, with the existing 30-second per-request timeout and retry helper.
- Malformed input: A malformed brand page raises `YahooPageError`; invalid catalog IDs cannot reach URL construction; existing record-level parser behavior remains unchanged.
- Duplicates: A duplicate brand mapping in the catalog fails validation; repeated items keep the existing duplicate-entry error; duplicate listing URLs across pages use first-page ownership and one URL-based alert.

## Test plan

Use temporary SQLite databases through `RAINCHECK_DB_PATH` and the existing offline `fake_http`, `sent_emails`, and saved Yahoo page fixtures.
Create a small second-brand catalog fixture inside each test with a test-only numeric Yahoo ID and item with a distinct keyword.
Reuse `tests/fixtures/yahoo/brand_page_minimal.html` for Rick Owens and construct a second-brand `__NEXT_DATA__` page with one fixed-price listing and one cross-brand keyword decoy.
Use the existing empty, malformed, and duplicate-record Yahoo fixtures where their shape applies.
Assert exact GET URLs and timeouts, one digest, section order, recorded `brand_id`, and unchanged SQL rows after failures.
Seed a temporary database with a database-only keyword and active item, copy it, and show that a proposed new-brand seed on the copy changes the old rows and therefore fails the required preservation comparison.
Then include those exact settings in the staged catalog and show that the old rows remain identical while only intended rows are added.
For the migration check, use the existing schema-version-3 temporary database fixture; no version-4 fixture is needed.
Visual verification should render a two-brand digest at 375px and desktop width, as required by the project workflow, without modifying the digest renderer unless a concrete layout issue appears.

## Shared-file conflicts

This feature touches `src/db.py` and `src/catalog.json`, so it must not run in parallel with RC-11, RC-14, or any other feature changing `src/db.py`, `src/schema.sql`, or `src/catalog.json`.
It also touches `src/yahoo_scrape.py`, so it must not run concurrently with a Yahoo scraper change.
RC-16 must be merged first; this spec assumes its schema version 3 and auction-aware pipeline.

## Open questions

None.

## Resolved

- 2026-10-27: The user approved the recommended RC-13 spec.
  A stored Yahoo brand ID cannot be changed through catalog seeding; the ID is repeated and validated on each flat catalog entry; one failed brand page stops the whole digest; and a built-in seed preview or preservation gate is deferred to RC-14.

## Rollback

If the new code fails in production, stop scheduled jobs, restore the verified backup to the same database path, and return to the previous code revision before restarting jobs.
Because there is no schema migration, returning to the previous code with the database left at version 3 is also structurally possible, but the previous code cannot seed a catalog containing a second brand.
Keep any new catalog changes out of the old code's seed path during rollback.
