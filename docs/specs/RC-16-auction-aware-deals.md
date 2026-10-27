# RC-16: Auction-aware deals

**Status:** Approved
**Backlog item:** RC-16 in `docs/BACKLOG.md`

## Summary

Classify Yahoo listings as fixed price, auction with a buy-now price, or bid-only auction.
Email immediately purchasable deals and separately labeled auction watches, while storing the listing's auction metadata with every emailed opportunity.

## Motivation

`src/yahoo_scrape.py:138-159` currently parses Yahoo's `price` field but discards `isFixedPrice`, `buyNowPrice`, and `endTime`.
`src/yahoo_scrape.py:253-265` compares that field with the market price for every listing.
For a running auction, `price` is only the current bid or starting price, so an auction beginning at ¥1 can appear to be an exceptional deal even though nobody can buy it for ¥1.
`src/schema.sql:40-51` has no columns for sale format, buy-now price, or scheduled end time.
`src/db.py:690-721` stores the emailed digest's displayed price in one batch without these facts.

Yahoo's [product page guide](https://support.yahoo-net.jp/PccAuctions/s/article/H000005375) distinguishes the current price from the seller's buy-now price and says that the current price can be the opening price when nobody has bid.
Yahoo's [buy-now explanation](https://support.yahoo-net.jp/SccAuctions/s/article/H000008828) says a buyer can win immediately at the buy-now price while bidding may continue below it.
Yahoo's [fixed-price guide](https://support.yahoo-net.jp/SccAuctions/s/article/H000008822) says a listing with equal opening and buy-now prices can be purchased immediately.
Yahoo's [automatic-extension explanation](https://support.yahoo-net.jp/SccAuctions/s/article/H000008832) says a bid that raises the price near the end can move the scheduled end time repeatedly.
Yahoo's [bidding explanation](https://support.yahoo-net.jp/PccAuctions/s/article/H000005267) says the minimum next bid can exceed the displayed current price when somebody has already bid, and automatic bidding can raise the price further.
These pages describe Yahoo's buyer-visible behavior, but Yahoo does not document the embedded `__NEXT_DATA__` field contract used by this scraper.
The mapping of `price`, `isFixedPrice`, `buyNowPrice`, and `endTime` below is therefore based on saved fixtures and must fail conservatively when the fields become inconsistent.

The saved `tests/fixtures/yahoo/brand_page_live.html` contains all four fields on 100 rows: 68 report `isFixedPrice: true`, 82 have a positive `buyNowPrice`, 18 are bid-only, and all have an `endTime`.
`tests/fixtures/yahoo/brand_page_minimal.html` includes a live auction with a current price of ¥50,000 and a buy-now price of ¥90,000, which demonstrates the current false-discount risk.

## Scope

**In scope:**

- Parse and validate Yahoo's `isFixedPrice`, `buyNowPrice`, and `endTime` alongside the existing current `price`.
- Compare fixed and buy-now prices as immediately purchasable deals, and include running auctions whose current price is below market as clearly provisional auction watches.
- Include auction watches on discovery in the one digest, with a separate per-item limit and no percentage-off claim based on a current bid.
- Show the scheduled end when available, labeled as subject to change, so the reader can decide when to revisit the Yahoo page.
- Preserve the existing `listings.price_yen` meaning as Yahoo's observed current price, and store the sale format, buy-now price, and scheduled end time for newly emailed listings.
- Add schema version 3 and a version 2 to 3 migration that preserves all existing rows.
- Keep the existing send-then-record and one-digest-per-run behavior, including no new row for an unsent or excluded listing.
- Update the saved-fixture and isolated-database tests that assumed current bids were actionable.

**Out of scope:**

- Near-end rechecks or reminder emails for a previously emailed auction.
- Predicting a final auction price, bidding automatically, fetching individual auction pages, or querying completed listings.
- Populating `ended_at` or `final_price_yen`, or treating a scheduled end as proof that an auction ended.
- Tracking changes in an auction's current price, buy-now price, or end time after its first emailed record.
- Recording every scanned Yahoo listing or changing the existing URL-based alert deduplication rule.
- Including shipping, taxes, customs, coupons, or fees in the price comparison.
- Changing the eBay market-price pool, currency conversion, or keyword matching.
- Predicting a winning bid or presenting a provisional current bid as a final purchase price or guaranteed saving.

## Design

### Files touched

| File | Change |
| --- | --- |
| `src/schema.sql` | Define schema version 3 and nullable auction metadata columns on `listings`. |
| `src/db.py` | Add migration 3 and accept validated metadata when recording an emailed listing and match. |
| `src/yahoo_scrape.py` | Parse and classify sale format, calculate immediate deals and provisional auction watches, and pass original listing metadata to storage. |
| `src/digest.py` | Render separate buy-now and auction-watch labels, provisional prices, and scheduled end times without a false discount claim. |
| `tests/test_yahoo_scrape.py` | Cover parsing, eligibility, displayed prices, item ordering, and send/record behavior. |
| `tests/test_digest.py` | Cover mixed digest labels, provisional pricing, end-time text, neutral subject, and escaping at phone and desktop widths. |
| `tests/test_db.py` | Cover migration, constraints, atomic writes, and preservation of legacy rows. |
| `tests/test_listing_matches.py` | Cover the existing batch recorder with source metadata, missing mapping entries, duplicates, and rollback. |
| `docs/specs/RC-16-auction-aware-deals.md` | Record approved decisions and the backup-first upgrade procedure. |

RC-10 already sends one digest and calls `yahoo_scrape.record_listing_matches(digest.deals)`, which forwards to `db.record_listing_matches(deals)`.
Extend this existing batch path while keeping its one `BEGIN IMMEDIATE` transaction and its returned count of newly inserted listing rows.
Keep the digest's existing `Deal.price_yen` and `Deal.price_usd` fields as the price shown to the reader, with an explicit kind that distinguishes an immediate-purchase price from a provisional auction price.

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

Extend `YahooListing` with trailing defaulted `is_fixed_price: bool | None = None`, `buy_now_price_yen: int | None = None`, and `scheduled_end_at: datetime | None = None` fields so existing direct constructors remain valid.
`price_yen` remains the parsed current price.
`_parse_record` accepts only literal booleans for `isFixedPrice`, positive integer yen amounts for `buyNowPrice`, and ISO 8601 `endTime` strings with a UTC offset.
It normalizes valid end times to UTC.
Missing or non-boolean `isFixedPrice` becomes `None`, and missing or `null` `buyNowPrice` becomes `None`.
An invalid non-null `buyNowPrice` rejects the record because treating it as absent could falsely make a fixed-price listing actionable at its current price.
Missing or invalid `endTime` becomes `None` without invalidating an otherwise valid record.
The parser still rejects invalid IDs, titles, current prices, and Unicode text as before.

Extend `digest.Deal` with trailing defaulted `kind: Literal["fixed_price", "auction_buy_now", "auction_watch"] = "fixed_price"`, `current_price_yen: int | None = None`, `potential_discount_pct: float | None = None`, and `scheduled_end_at: datetime | None = None` fields so existing direct constructors keep working.
For an immediate deal, `Deal.price_yen` is the fixed or buy-now price and `discount_pct` is the real immediate-purchase comparison.
For an `auction_buy_now` row whose current auction price is lower, populate `current_price_yen` and `potential_discount_pct` so the same row also describes the bidding opportunity.
For an auction watch, `Deal.price_yen` is the provisional current auction price and `discount_pct` is only the **potential** percentage difference from market at that moment.
The `kind` controls the label; no auction watch may use the ordinary `X% off` wording.

Define a pure classification rule for `find_item_deals`, keeping its current name for existing callers while returning both kinds of opportunity:

1. If `is_fixed_price is True`, use `price_yen` as one immediate deal only when a positive `buy_now_price_yen` is absent or equals `price_yen`.
   A positive but different buy-now value is inconsistent, so the listing is not eligible.
2. If `is_fixed_price is False` and a valid buy-now price is below market, use that price for one immediate deal; also show its provisional current auction price and potential percentage in the row when it is lower, but do not create a second watch row for the same URL.
3. Otherwise, if `is_fixed_price is False` and the current price is below market, create one auction watch, whether the buy-now price is absent or at or above market.
4. A listing with no validated format, or with both immediate and current auction prices at or above market, is not included.

Treat `isFleamarketItem` only as the existing URL selector, not as a substitute for a missing or contradictory `isFixedPrice` value.
The saved live fixture contains flea-market rows with explicit format flags.
This conservative rule prevents an undocumented field combination from creating an apparent deal.
`Deal.price_yen`, `Deal.price_usd`, and `Deal.discount_pct` use the selected price for that row; the label explains whether the percentage is confirmed against buy-now or only potential at the current bid.
The stored `listings.price_yen` uses the original `YahooListing.price_yen`.

Sort immediate deals by real discount descending, with source-page order for ties.
Sort auction watches by earliest known scheduled end, with unknown ends last and source-page order for ties; do not rank them by the potential percentage, which would make ¥1 opening prices dominate.
For each tracked item, show up to five immediate deals and five auction watches independently.
When a section includes watches, count any further matches in neutral `more listings not shown` text; keep the existing overflow wording for buy-now-only sections.
Keep the existing first-item claim rule for a URL that matches multiple tracked items.
The job sends at most one digest containing both groups.

In HTML and plain text, label every watch row `Auction watch - current price (provisional)` and label immediate rows `Buy now` when watches appear in that digest.
An immediate row uses the existing `X% off` wording for its buy-now comparison; a watch row uses `Potential discount at current price: X% (not guaranteed; bids may rise)`.
When a buy-now auction also has a lower current price, show that current price and its own potential percentage separately as provisional, with the same `not guaranteed; bids may rise` warning.
For an auction row, show `Scheduled end YYYY-MM-DD HH:MM JST (may change)` when `endTime` is valid, or `Scheduled end unavailable` otherwise.
When any watch is displayed, the subject is the neutral `RainCheck: N listings to review - YYYY-MM-DD`, where `N` counts displayed rows of both kinds.
For a digest containing no watches, retain the existing `RainCheck: N new deals - YYYY-MM-DD` subject.
A digest containing only fixed-price deals keeps its existing row markup and plain-text format; an `auction_buy_now` row adds the provisional bid detail described above.
The existing HTML escaping and safe-link rules remain in force.
The digest remains readable at 375px phone and desktop widths without hiding the price column.

Extend both forwarding and database functions to `record_listing_matches(deals, *, sources_by_url=None) -> int`.
`sources_by_url` maps each displayed deal URL to its parsed `YahooListing` from the same page; the parser's first-record-wins rule makes that mapping unambiguous.
The Yahoo job builds the mapping from the parsed page and confirms every displayed deal has a source before attempting SMTP delivery, then passes it to the recorder after a successful send.
When a mapping is supplied, the database recorder validates that every displayed deal URL is present and equals the corresponding source's `url`, and writes `source.price_yen` as the observed current price plus the source's parsed auction metadata.
It also rejects a nonpositive or boolean source current price, a sale flag outside `True`, `False`, or `None`, a nonpositive or boolean non-null buy-now price, and a non-UTC scheduled end.
An absent, mismatched, or invalid source raises `ValueError` before opening a database connection; it never falls back to the displayed buy-now price.
Duplicate deals for the same URL use the one parsed source; existing `(listing_id, item_id)` deduplication remains unchanged.
When `sources_by_url` is omitted, preserve RC-10's existing direct-caller behavior: write `deal.price_yen` with `NULL` auction metadata.
The new defaulted fields on `digest.Deal` remain email-facing data and do not give the pure digest builder database or network access.
The legacy single-deal `record_listing_match` remains available with its current six-argument interface and writes `NULL` for the new columns.
On a URL conflict, preserve the preexisting listing metadata and insert only a missing item association, as today.
The full displayed batch still commits or rolls back together and every database connection closes.

### Existing records and transition

Previously emailed bid-only auctions remain stored with unknown format and continue to suppress duplicate alerts for the same URL.
No migration invents new buy-now values or resends old alerts.
An auction not yet emailed can qualify as a watch when its current price is below market, even without buy-now.
After a watch is emailed and recorded, the same URL is not emailed again when its bid or buy-now price changes; this preserves the existing first-alert deduplication policy.
Because this job reads only the newest page, it cannot promise to notice every later edit.

## Acceptance criteria

1. A fresh database has `PRAGMA user_version = 3`, all three new `listings` columns, the stated checks, and the prior tables and indexes.
2. Migrating a version 2 database with listing and match rows produces version 3; the old rows and all their previous values remain unchanged, while all three new fields are `NULL`.
3. Running `init_db()` again on version 3 leaves schema and rows unchanged.
4. A deliberately failing migration step rolls back the added columns and version bump, and closes the connection.
5. The saved live fixture parses literal fixed-price flags, positive buy-now amounts, and timezone-aware end times into UTC without a network call.
6. A fixed-price record with `price = buyNowPrice = 50000` below market is an immediate deal at ¥50,000; the database stores its observed current price and metadata, and a mixed digest labels it `Buy now`.
7. A fixed-price record with no buy-now field is an immediate deal at its validated current price when below market.
8. An auction with `price = 1000` and `buyNowPrice = 90000` below market produces one immediate deal at ¥90,000, shows ¥1,000 and its potential percentage separately as provisional in the same row, and calculates the real buy-now discount from ¥90,000.
9. When that ¥90,000 buy-now price is at or above market but the ¥1,000 current price is below market, the auction produces one watch row, not a buy-now deal, with a clearly labeled potential percentage at ¥1,000.
10. A bid-only auction with a current price below market produces one watch row when first discovered, even if its scheduled end is several days away; a current price at or above market produces no row.
11. A ¥1 bid-only auction never displays an ordinary `X% off` or final-price claim.
    With a synthetic ¥100 current price, an $800 market price, and a 0.0062 USD/JPY rate, it displays `Auction watch`, `current price (provisional)`, and `Potential discount at current price: 99.9% (not guaranteed; bids may rise)`.
12. A listing with a missing or non-boolean `isFixedPrice`, invalid non-null buy-now value, or fixed-price/buy-now mismatch is not emailed; the invalid non-null buy-now record is skipped by the parser.
13. Missing or malformed `endTime` leaves `scheduled_end_at` as `None`, puts a watch after watches with known scheduled ends, and displays `Scheduled end unavailable` without excluding an otherwise eligible row.
14. A valid `endTime` with an explicit `+09:00` offset is stored as the equivalent UTC instant and shown in JST with a `may change` label; naive timestamps are not accepted as known end times.
15. `isFleamarketItem` continues to select the existing URL prefix and does not bypass the format and price rules.
16. Immediate deals sort by real discount descending; auction watches sort by earliest known scheduled end, with source-page order for ties, so a larger potential percentage does not outrank an earlier ending auction.
17. Each item's digest section shows up to five immediate deals and five auction watches independently; a sixth of either kind increments neutral `more listings not shown` text in a mixed section without displacing the other kind.
18. A page mixing fixed-price, buy-now, and bid-only matching listings sends one digest with the neutral `N listings to review` subject, correct row labels and prices in HTML and plain text, and exactly the displayed rows recorded in one post-send transaction.
19. A digest containing only auction watches still sends one email and records those watches; a page with no qualifying opportunities sends no email or new listing rows.
20. HTML and plain-text auction rows escape scraped titles and URLs under existing rules; the HTML price column stays visible at 375px and desktop widths with a long title and a scheduled end.
21. A failed email send records no new listing, metadata, or match row, and an eligible opportunity can be offered on the next run.
22. A known URL is not re-emailed when its current bid or buy-now price changes; its existing price and auction metadata are not overwritten, while `last_seen_at` still follows RC-05's observation rule.
23. A listing whose current price is initially at or above market creates no row, then can be emailed and recorded on a later run if the same URL becomes a qualifying watch or buy-now deal.
24. A duplicate Yahoo record follows the parser's first-record-wins rule, duplicate displayed opportunities for one URL use that source observation, and a duplicate stored URL creates no second listing row.
25. An invalid page, network error, or bad HTTP status produces no new digest or listing write; RC-10's bounded retry behavior remains in force.
26. Invalid metadata never bypasses database constraints; an SQL failure on the second row in a displayed batch rolls back all new listings and matches from that batch and closes the connection.
27. Existing schema-version rejection, keyword matching, market-price fallback, and exchange-rate behavior remain unchanged.
28. A missing, mismatched, or invalid `sources_by_url` entry raises `ValueError` before database access; the Yahoo job detects a missing or mismatched source before sending email.
29. Direct callers that omit `sources_by_url` retain RC-10's insertion and deduplication behavior with `NULL` metadata, and legacy `record_listing_match` calls still work unchanged.
30. A digest containing no watches retains RC-15's existing subject; a digest containing only fixed-price deals also retains its row markup, plain-text format, overflow wording, and extreme-fixture HTML size limit.

## Edge cases

- Empty results: A valid empty page produces no digest and no new listing; no auction metadata is written.
- `None` or missing values: Missing buy-now still permits a provisional auction watch; missing format prevents either classification; a fixed-price listing may use its positive current price when buy-now is absent.
- Network failure or bad HTTP status: Fetch failure exits before parsing or recording; RC-10's bounded retries apply.
- Malformed input: Invalid IDs, titles, and current prices follow the existing parser's skip rule; invalid auction metadata cannot create a false actionable deal.
- Duplicates: First Yahoo record per auction ID wins; database URL uniqueness and match keys remain authoritative.
- End-time changes: `scheduled_end_at` is a snapshot, not proof of completion; a seller can extend or end early.
- Exchange conversion: The shown buy-now or provisional current price is converted with the run's one validated yen-to-USD rate; the label prevents confusing the two percentages.
- Auction with both prices: One URL makes one row, using a buy-now deal when its buy-now price is below market and otherwise a watch when only the current price is below market.
- Newest-page limit: A discovery alert can only cover listings still in the fetched first 100 results; a near-end reminder would require another source or scan strategy.
- Existing rows: Legacy metadata stays `NULL` and URL deduplication stays in effect.

## Test plan

- Use `tests/conftest.py` temporary SQLite databases, network blocking, fake HTTP, and fake send functions; never load `.env` or use the real database.
- Use the recorded `brand_page_live.html` fixture to assert field types and UTC conversion, and `brand_page_minimal.html` for fixed-price, buy-now, and bid-only cases.
- Create small synthetic `__NEXT_DATA__` pages for missing and malformed metadata, contradictory flags, duplicate IDs, empty results, and mixed-item digest ordering.
- In `tests/test_yahoo_scrape.py`, test pure parsing and both opportunity classifications, then run `main()` with a temporary database and fake Yahoo, Frankfurter, and SMTP responses to check send-before-record behavior and stored fields.
- Revise RC-15 and RC-07 tests that currently expect an ordinary discount from a current bid; keep their other assertions intact.
- Update inline `YahooListing` test builders to set the intended sale format explicitly; a constructor that omits format must not silently imply a fixed-price listing.
- Include one under-market bid-only record with no buy-now, one auction with buy-now below market, one auction with buy-now above market and bid below market, and a fixed-price record in a mixed digest.
- Include a synthetic ¥1 opening-price watch and assert that any percentage is labeled potential and never rendered as ordinary `off` text.
- Include six immediate deals and six auction watches for one item, and assert independent caps, non-crowding, neutral overflow text, scheduled-end ordering, and one email subject count.
- In `tests/test_digest.py`, check HTML and plain text labels, the two distinctly labeled percentages in one auction-with-buy-now row, JST scheduled end and missing-end text, scraped-text escaping, and responsive table attributes.
- During verification, render a realistic mixed digest with a long Japanese title at 375px phone and desktop widths and confirm the price column and warning remain visible.
- In `tests/test_db.py`, start from a version 2 database with listing and match rows, and check version 3 migration preservation, fresh-schema parity, idempotence, rollback on injected migration failure, and constraints.
- In `tests/test_listing_matches.py`, check source mapping validation before database access, stored current price versus displayed buy-now price, duplicate URL behavior, legacy caller compatibility, URL conflicts, a second-deal SQL failure rolling back the whole batch, and connection closure.
- Update RC-10 Yahoo test doubles that intercept `record_listing_matches` to accept the new keyword argument without changing their failure behavior.
- Add a two-run case showing that a previously emailed watch is not resent when its buy-now price changes, plus a case where an initially above-market listing later qualifies and is emailed.
- Do not capture live fixtures automatically or run a live Yahoo request in tests.

## Shared-file conflicts

This feature touches both `src/schema.sql` and `src/db.py`, so do not implement it in parallel with another feature that touches either file, including RC-11, RC-13, RC-14, or RC-18.
It also touches `src/yahoo_scrape.py`, and builds on RC-10's merged batch writer and retry behavior.
`src/catalog.json` is not touched.

## Open questions

None.

## Resolved

- 2026-10-27: The user rejected suppressing bid-only auctions and said they are central to finding opportunities.
- 2026-10-27: The user chose to email bid-only auctions when first discovered if their current price is below market, instead of waiting until near the scheduled end.
- 2026-10-27: The user chose to show a potential percentage discount calculated from Yahoo's current auction price, including an opening price before any bids, clearly labeled as provisional rather than guaranteed.
- 2026-10-27: The user approved independent per-item limits of five immediate deals and five auction watches, with watches ordered by soonest scheduled end.
- 2026-10-27: The user approved one row for a dual-price auction whose buy-now price is below market, showing the lower current bid and its potential percentage separately as provisional.
- 2026-10-27: The user approved the full revised RC-16 spec.

## Amendments

- 2026-10-27: Corrected criterion 11's arithmetic example from a ¥1 current price to ¥100; at $800 market and 0.0062 USD/JPY, ¥100 gives a 99.9% potential difference when rounded to one decimal.
  This changes no alert or labeling policy.

## Rollback

Stop the jobs and restore the pre-upgrade database backup before running the previous version of the code; version 2 code intentionally refuses a version 3 database.
If the new alert policy is undesirable but the schema is healthy, deploy a follow-up code change that changes eligibility without deleting or downgrading data.
New columns can remain in version 3 and old listing rows retain their original values.
