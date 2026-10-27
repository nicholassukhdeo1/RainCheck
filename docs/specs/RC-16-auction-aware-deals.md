# RC-16: Auction-aware deals

**Status:** Draft
**Backlog item:** RC-16 in `docs/BACKLOG.md`

## Summary

Classify Yahoo listings as fixed price, auction with a buy-now price, or bid-only auction.
Compare only an immediately purchasable price with the eBay market price, and store the listing's auction metadata with any emailed deal.

## Motivation

`src/yahoo_scrape.py:138-159` currently parses Yahoo's `price` field but discards `isFixedPrice`, `buyNowPrice`, and `endTime`.
`src/yahoo_scrape.py:253-265` compares that field with the market price for every listing.
For a running auction, `price` is only the current bid or starting price, so an auction beginning at ¥1 can appear to be an exceptional deal even though nobody can buy it for ¥1.
`src/schema.sql:40-51` has no columns for sale format, buy-now price, or scheduled end time.
`src/db.py:619-650` stores the emailed deal's price without these facts.

Yahoo's [product page guide](https://support.yahoo-net.jp/PccAuctions/s/article/H000005375) distinguishes the current price from the seller's buy-now price and says that the current price can be the opening price when nobody has bid.
Yahoo's [buy-now explanation](https://support.yahoo-net.jp/SccAuctions/s/article/H000008828) says a buyer can win immediately at the buy-now price while bidding may continue below it.
Yahoo's [fixed-price guide](https://support.yahoo-net.jp/SccAuctions/s/article/H000008822) says a listing with equal opening and buy-now prices can be purchased immediately.
Yahoo's [automatic-extension explanation](https://support.yahoo-net.jp/SccAuctions/s/article/H000008832) says a bid that raises the price near the end can move the scheduled end time repeatedly.
These pages describe Yahoo's buyer-visible behavior, but Yahoo does not document the embedded `__NEXT_DATA__` field contract used by this scraper.
The mapping of `price`, `isFixedPrice`, `buyNowPrice`, and `endTime` below is therefore based on saved fixtures and must fail conservatively when the fields become inconsistent.

The saved `tests/fixtures/yahoo/brand_page_live.html` contains all four fields on 100 rows: 68 report `isFixedPrice: true`, 82 have a positive `buyNowPrice`, 18 are bid-only, and all have an `endTime`.
`tests/fixtures/yahoo/brand_page_minimal.html` includes a live auction with a current price of ¥50,000 and a buy-now price of ¥90,000, which demonstrates the current false-discount risk.

## Scope

**In scope:**

- Parse and validate Yahoo's `isFixedPrice`, `buyNowPrice`, and `endTime` alongside the existing current `price`.
- Use the fixed price or buy-now price as the comparison and displayed deal price; never use a bid-only auction's current price as an immediately purchasable deal.
- Preserve the existing `listings.price_yen` meaning as Yahoo's observed current price, and store the sale format, buy-now price, and scheduled end time for newly emailed listings.
- Add schema version 3 and a version 2 to 3 migration that preserves all existing rows.
- Keep the existing send-then-record and one-digest-per-run behavior, including no new row for an unsent or excluded listing.
- Update the saved-fixture and isolated-database tests that assumed current bids were actionable.

**Out of scope:**

- Alerting on bid-only auctions, even when their scheduled end is close.
- Predicting a final auction price, bidding automatically, fetching individual auction pages, or querying completed listings.
- Populating `ended_at` or `final_price_yen`, or treating a scheduled end as proof that an auction ended.
- Tracking changes in an auction's current price, buy-now price, or end time after its first emailed record.
- Recording every scanned Yahoo listing or changing the existing URL-based alert deduplication rule.
- Including shipping, taxes, customs, coupons, or fees in the price comparison.
- Changing the eBay market-price pool, currency conversion, keyword matching, or digest layout.

## Design

### Files touched

| File | Change |
| --- | --- |
| `src/schema.sql` | Define schema version 3 and nullable auction metadata columns on `listings`. |
| `src/db.py` | Add migration 3 and accept validated metadata when recording an emailed listing and match. |
| `src/yahoo_scrape.py` | Parse and classify sale format, calculate deals from actionable prices, and pass original listing metadata to storage. |
| `tests/test_yahoo_scrape.py` | Cover parsing, eligibility, displayed prices, item ordering, and send/record behavior. |
| `tests/test_db.py` | Cover migration, constraints, atomic writes, and preservation of legacy rows. |
| `tests/fixtures/yahoo/brand_page_minimal.html` | Retain as the recorded mixed-format fixture; no editing expected unless a documented test correction is necessary. |
| `tests/fixtures/yahoo/brand_page_live.html` | Retain as the recorded 100-row fixture for field-shape assertions; do not regenerate with credentials. |
| `tests/fixtures/yahoo/brand_page_malformed.html` | Retain for malformed-field and duplicate tests; supplement with synthetic `__NEXT_DATA__` snippets where needed. |
| `docs/specs/RC-16-auction-aware-deals.md` | Record approved decisions and the backup-first upgrade procedure. |

If RC-10 merges before RC-16 implementation, use its post-send batch-recording interface rather than reintroducing per-deal writes.
This spec does not ask for a separate digest-format change; the existing `Deal.price_yen` and `Deal.price_usd` fields carry the actionable price.

### Schema changes and migration

Raise `SCHEMA_VERSION` and the `src/schema.sql` header from 2 to 3.
Add three nullable columns to `listings` in both the fresh schema and `MIGRATIONS[3]`:

| Column | Meaning |
| --- | --- |
| `is_fixed_price INTEGER CHECK (is_fixed_price IN (0, 1))` | `1` for a validated fixed-price listing, `0` for a validated auction, `NULL` when older data do not establish the format. |
| `buy_now_price_yen INTEGER CHECK (buy_now_price_yen IS NULL OR buy_now_price_yen > 0)` | Positive immediate-purchase price, or `NULL` when absent or unknown. |
| `scheduled_end_at TEXT` | Yahoo's timezone-aware `endTime` normalized to UTC ISO 8601, or `NULL` when unavailable or invalid. |

`listings.price_yen` remains Yahoo's observed current price, not the buy-now amount.
The migration only adds nullable columns, so old listing IDs, URLs, prices, timestamps, and match rows remain unchanged, with `NULL` auction metadata.
Do not infer old format or backfill from the title, URL, or current price.
Migration 3 runs inside the existing transaction and increments `PRAGMA user_version` only after all three additions succeed.
The existing `init_db` path can migrate a recognized version 1 database through versions 2 and 3, but the user should complete RC-06's separate data-upgrade instructions before deploying RC-16.

**Backup-first upgrade for the user after merge:**

1. Stop scheduled RainCheck jobs and identify the database path that `RAINCHECK_DB_PATH` selects; if unset, the project default is `src/raincheck.db`.
2. Complete the RC-06 post-merge procedure in `docs/specs/RC-06-market-price-history.md` and confirm `PRAGMA user_version` is 2 before starting RC-16.
3. Make a timestamped, byte-for-byte copy of that database into `~/RainCheck-backups`, and verify the copy exists and has a nonzero size.
4. With the same database path explicitly set in `RAINCHECK_DB_PATH`, run `python3 src/db.py` once; do not pass `--seed` because this schema upgrade does not change catalog rows.
5. Check `PRAGMA user_version = 3`, `PRAGMA quick_check = ok`, the three new columns in `PRAGMA table_info(listings)`, and unchanged counts in `listings` and `listing_matches` before restarting jobs.

The user performs this on their machine; agents use temporary databases only.
If any check fails, stop jobs, restore the verified backup, and run the previous code until the failure is investigated.

### Interfaces

Extend `YahooListing` with `is_fixed_price: bool | None`, `buy_now_price_yen: int | None`, and `scheduled_end_at: datetime | None`.
`price_yen` remains the parsed current price.
`_parse_record` accepts only literal booleans for `isFixedPrice`, positive integer yen amounts for `buyNowPrice`, and ISO 8601 `endTime` strings with a UTC offset.
It normalizes valid end times to UTC.
Missing or non-boolean `isFixedPrice` becomes `None`, and missing or `null` `buyNowPrice` becomes `None`.
An invalid non-null `buyNowPrice` rejects the record because treating it as absent could falsely make a fixed-price listing actionable at its current price.
Missing or invalid `endTime` becomes `None` without invalidating an otherwise valid record.
The parser still rejects invalid IDs, titles, current prices, and Unicode text as before.

Define a pure actionable-price rule for `find_item_deals`:

1. If `is_fixed_price is True`, compare `price_yen` only when a positive `buy_now_price_yen` is absent or equals `price_yen`.
   A positive but different buy-now value is inconsistent, so the listing is not eligible.
2. Otherwise, if `is_fixed_price is False` and `buy_now_price_yen` is a valid positive integer, compare that buy-now price, not the current bid.
3. Otherwise, the price is not known to be immediately purchasable, so the listing is not eligible.

Treat `isFleamarketItem` only as the existing URL selector, not as a substitute for a missing or contradictory `isFixedPrice` value.
The saved live fixture contains flea-market rows with explicit format flags.
This conservative rule prevents an undocumented field combination from creating an apparent deal.
`Deal.price_yen`, `Deal.price_usd`, and `Deal.discount_pct` use the actionable price, so the digest and discount agree.
The stored `listings.price_yen` uses the original `YahooListing.price_yen`.

Extend `record_listing_match` with explicit keyword-only auction metadata arguments after the existing six positional arguments, preserving existing callers.
The Yahoo job finds each displayed deal's source `YahooListing` by URL and passes its current price and parsed metadata to the record operation; the record operation must not derive the source current price from `Deal.price_yen`.
For the job's new call path, pass the original current price as the existing fourth argument and pass metadata through the new keyword-only arguments.
On a URL conflict, preserve the preexisting listing metadata and insert only a missing item association, as today.
The listing and match still commit or roll back together and every database connection closes.
If RC-10 has replaced this operation with a batch operation, extend that operation instead and retain its all-or-nothing post-send transaction.

### Existing records and transition

Previously emailed bid-only auctions remain stored with unknown format and continue to suppress duplicate alerts for the same URL.
No migration invents new buy-now values or resends old alerts.
An auction that was previously below market only at its current bid but was never emailed remains unstored and may later qualify if Yahoo adds a valid buy-now price.
Because this job reads only the newest page, it cannot promise to notice every later edit.

## Acceptance criteria

1. A fresh database has `PRAGMA user_version = 3`, all three new `listings` columns, the stated checks, and the prior tables and indexes.
2. Migrating a version 2 database with listing and match rows produces version 3; the old rows and all their previous values remain unchanged, while all three new fields are `NULL`.
3. Running `init_db()` again on version 3 leaves schema and rows unchanged.
4. A deliberately failing migration step rolls back the added columns and version bump, and closes the connection.
5. The saved live fixture parses literal fixed-price flags, positive buy-now amounts, and timezone-aware end times into UTC without a network call.
6. A fixed-price record with `price = buyNowPrice = 50000` is eligible at ¥50,000 when below market, and the emailed and stored price fields reflect the separate actionable and observed-price meanings.
7. A fixed-price record with no buy-now field is eligible at its validated current price.
8. An auction with `price = 1000` and `buyNowPrice = 90000` uses ¥90,000 for conversion, sorting, discount, and digest text or HTML; it is not a deal if ¥90,000 is at or above market even when ¥1,000 is below market.
9. An auction with no buy-now price is not emailed or stored, even when its current bid is ¥1 and its scheduled end is near.
10. A listing with a missing or non-boolean `isFixedPrice`, invalid non-null buy-now value, or fixed-price/buy-now mismatch is not emailed as an immediate deal; the invalid non-null buy-now record is skipped by the parser.
11. Missing or malformed `endTime` leaves `scheduled_end_at` as `None` without making an otherwise valid immediate-purchase listing ineligible.
12. A valid `endTime` with an explicit `+09:00` offset is stored as the equivalent UTC instant; naive timestamps are not accepted as known end times.
13. `isFleamarketItem` continues to select the existing URL prefix and does not bypass the format and buy-now rules.
14. Two listings with different actionable prices are sorted by actual discount; equal discounts retain source-page order and existing per-item digest limits apply.
15. A page mixing fixed-price, buy-now, and bid-only matching listings sends at most one digest containing only the eligible below-market listings and records exactly those displayed deals after a successful send.
16. A failed email send records no new listing, metadata, or match row, and an eligible deal can be offered on the next run.
17. A known URL is not re-emailed; its existing price and auction metadata are not overwritten, while `last_seen_at` still follows RC-05's observation rule.
18. A listing that initially lacks an actionable price creates no row, then can be emailed and recorded on a later run when the same URL acquires a valid buy-now price.
19. A duplicate Yahoo record follows the parser's first-record-wins rule, and a duplicate stored URL creates no second listing row.
20. An invalid page, network error, or bad HTTP status produces no new digest or listing write; existing RC-10 retry behavior remains in force if merged first.
21. Invalid metadata never bypasses database constraints; an SQL failure while recording an emailed deal rolls back its listing and match together and closes the connection.
22. Existing schema-version rejection, keyword matching, market-price fallback, and exchange-rate behavior remain unchanged.

## Edge cases

- Empty results: A valid empty page produces no digest and no new listing; no auction metadata is written.
- `None` or missing values: Missing buy-now or format means an auction is not an immediate deal; a fixed-price listing may use its positive current price when buy-now is absent.
- Network failure or bad HTTP status: Fetch failure exits before parsing or recording; RC-10's bounded retries apply once it is merged.
- Malformed input: Invalid IDs, titles, and current prices follow the existing parser's skip rule; invalid auction metadata cannot create a false actionable deal.
- Duplicates: First Yahoo record per auction ID wins; database URL uniqueness and match keys remain authoritative.
- End-time changes: `scheduled_end_at` is a snapshot, not proof of completion; a seller can extend or end early.
- Exchange conversion: Only the chosen actionable yen price is converted with the run's one validated yen-to-USD rate.
- Existing rows: Legacy metadata stays `NULL` and URL deduplication stays in effect.

## Test plan

- Use `tests/conftest.py` temporary SQLite databases, network blocking, fake HTTP, and fake send functions; never load `.env` or use the real database.
- Use the recorded `brand_page_live.html` fixture to assert field types and UTC conversion, and `brand_page_minimal.html` for fixed-price, buy-now, and bid-only cases.
- Create small synthetic `__NEXT_DATA__` pages for missing and malformed metadata, contradictory flags, duplicate IDs, empty results, and mixed-item digest ordering.
- In `tests/test_yahoo_scrape.py`, test pure parsing and deal selection, then run `main()` with a temporary database and fake Yahoo, Frankfurter, and SMTP responses to check send-before-record behavior and stored fields.
- Revise RC-15 and RC-07 tests that currently expect a discount from a current bid; keep their other assertions intact.
- In `tests/test_db.py`, start from a version 2 database with listing and match rows, check version 3 migration preservation, fresh-schema parity, idempotence, rollback on injected SQL failure, constraints, URL conflicts, and connection closure.
- Add a two-run case where an initially bid-only listing gains a valid buy-now price and becomes a deal.
- Do not capture live fixtures automatically or run a live Yahoo request in tests.

## Shared-file conflicts

This feature touches both `src/schema.sql` and `src/db.py`, so do not implement it in parallel with another feature that touches either file, including RC-10 if it has not merged, RC-11, RC-13, RC-14, or RC-18.
It also touches `src/yahoo_scrape.py`, which RC-10 changes; rebase on the merged RC-10 code before testing or implementing RC-16.
`src/catalog.json` is not touched.

## Open questions

1. Should RainCheck suppress bid-only auctions entirely in RC-16, even when they are near their scheduled end?
   **Recommendation:** Yes.
   The current bid is not a price the reader can lock in, daily newest-page scans can miss the near-end window, and Yahoo can extend the closing time.
   The tradeoff is that potentially cheap auctions with no buy-now price will not be emailed.
   A future feature could offer a separately labeled bid-watch alert with its own timing and deduplication policy.
2. Should fixed-price and buy-now listings be compared using the price that can be paid immediately, while retaining Yahoo's current price separately in `listings.price_yen`?
   **Recommendation:** Yes.
   This makes the advertised discount honest and preserves the original observation for future auction analysis.
   The tradeoff is a slightly wider record interface and more test updates.

## Rollback

Stop the jobs and restore the pre-upgrade database backup before running the previous version of the code; version 2 code intentionally refuses a version 3 database.
If the new alert policy is undesirable but the schema is healthy, deploy a follow-up code change that changes eligibility without deleting or downgrading data.
New columns can remain in version 3 and old listing rows retain their original values.
