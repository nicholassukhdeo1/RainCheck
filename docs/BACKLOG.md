# RainCheck Backlog

Prioritized list of features and fixes.
Each item goes through the feature workflow in `CLAUDE.md`: planner spec, user approval, tests, implementation, review, verification, user merge.

Items are ordered by dependency, not just value.
"Touches" lists shared files, because items touching the same shared files must run one at a time.

**Sizes:** S is under a day, M is one to three days, L is a multi-part feature that the planner should consider splitting.

---

## Phase 0: Foundation

These run first and one at a time, because everything else depends on them.

### RC-01: Test and configuration foundation

- **Size:** M
- **Depends on:** nothing
- **Touches:** `src/db.py`, `src/ebay_pricing.py`, `src/yahoo_scrape.py`, `src/sendemail.py`, new `tests/`
- **Why:** RainCheck has zero tests, and both data sources are live websites.
  Every later item needs a way to prove it works without hitting the internet, sending email, or touching the real database.
- **Scope:**
  - Add `pytest` and a `tests/` directory with a temporary-database fixture built from `src/schema.sql`.
  - Capture fixtures: a saved Yahoo results page and a recorded eBay search response.
  - Make the database path configurable through an environment variable, defaulting to the current location.
  - Resolve `catalog.json` relative to the source file instead of the current working directory.
  - Add a dry-run mode that prints the email instead of sending it.
  - Cover today's behavior with tests: seeding, `insert_listing` deduplication, `get_item_id`, `update_market_price`, and the eBay median with include and exclude keywords.
- **Resume angle:** Built a fixture-based test suite for a web-scraping and API pipeline, with isolated test databases and no network dependence.

### RC-02: Stop committing the database file

- **Size:** S
- **Depends on:** RC-01
- **Touches:** `.gitignore`, `README.md`
- **Why:** `src/raincheck.db` is tracked in git.
  A binary database that changes on every run bloats history and can leak scraped data into the public repo.
- **Scope:** Untrack the file without deleting the local copy, ignore `*.db`, and document how to rebuild it with `python3 db.py` and the seed step.
- **Resume angle:** Repository hygiene; small, but expected.

---

## Phase 1: Data model correctness

These change the schema, so they run one at a time.

### RC-03a: Item configuration in the database: schema, migration, upsert seeding

- **Size:** M
- **Depends on:** RC-01, RC-02
- **Touches:** `src/schema.sql`, `src/db.py`, `src/catalog.json`, `README.md`, tests
- **Why:** The database cannot store exclude keywords, seeding never propagates catalog edits, and the catalog price and eBay price overwrite each other in one column.
- **Scope:** Schema version 1 with a tested migration, validated upsert seeding, a read API for item configuration, the `pod shorts` data fix, and a backup-first upgrade of the user's database.
- **Resume angle:** Designed a versioned SQLite migration and an idempotent, validated configuration sync with clear data ownership.

### RC-03b: Both jobs read item configuration from the database

- **Size:** M
- **Depends on:** RC-03a
- **Touches:** `src/yahoo_scrape.py`, `src/ebay_pricing.py`, new `src/matching.py`, `README.md`, tests
- **Why:** After RC-03a the database holds the full configuration, but both jobs still read `catalog.json`, the Yahoo job ignores exclude keywords, and eBay is queried with the item name.
- **Scope:**
  - `yahoo_scrape.main()` and `ebay_pricing.refresh_market_prices()` iterate `db.get_tracked_items()` (active items only) and never call `load_catalog()`.
  - One shared `title_matches(title, keywords, exclude_keywords)` function used by both jobs: lowercase the title, any include keyword matches, no exclude keyword matches.
  - The Yahoo job applies exclude keywords and judges deals against `effective_price_usd`, skipping items without a usable price.
  - eBay queries use `search_query`, falling back to the item name when it is `NULL`.
  - `refresh_market_prices` skips an item whose eBay median is not a usable price (0 or below), because `update_market_price` now rejects it, so one item cannot stop the whole run.
  - `report_item_deals` takes a `TrackedItem`, so the brand lookup crash and the `KeyError` on missing `exclude_keywords` disappear.
  - Update the README "Usage" section: edit `catalog.json`, run `python3 src/db.py --seed`, then run the jobs.
- **Resume angle:** Refactored to a single source of truth, eliminating configuration drift between two data pipelines.

### RC-04: Record which item each listing matched

- **Size:** M
- **Depends on:** RC-03b
- **Touches:** `src/db.py`, `src/yahoo_scrape.py`
- **Why:** The `listing_matches` table has existed since the schema was written and has never been used.
- **Scope:** Write a `listing_matches` row whenever a listing matches an item.
  The planner must address an existing design decision: listings are currently stored only when they are deals, so that a listing whose price later drops still triggers an alert.
- **Resume angle:** Modeled a many-to-many relationship between scraped listings and tracked items.

### RC-05: Listing lifecycle tracking

- **Size:** M
- **Depends on:** RC-03b
- **Touches:** `src/db.py`, `src/yahoo_scrape.py`
- **Why:** `last_seen_at` is set once and never updated, and `ended_at` and `final_price_yen` are never written.
- **Scope:** Update `last_seen_at` when a listing is seen again.
  The planner should investigate whether ended auctions and final prices can be detected reliably, and propose that as a separate item if not.
- **Resume angle:** Tracked listing lifecycle over time with upserts.

### RC-06: Market price history

- **Size:** M
- **Depends on:** RC-03b
- **Touches:** `src/schema.sql`, `src/db.py`, `src/ebay_pricing.py`
- **Why:** Each pricing run overwrites the last one, so there is no record of how an item's market price moves.
- **Scope:** Add a history table storing item, median price, sample size, and timestamp for every run, keeping `items.market_price_usd` as the latest value.
- **Resume angle:** Time-series price history enabling trend analysis.

---

## Phase 2: Pricing and scraping quality

These mostly touch different files, so some can run in parallel.
RC-07 and RC-08 both touch `src/yahoo_scrape.py` and must run one after the other.

### RC-07: Robust Yahoo parsing

- **Size:** M
- **Depends on:** RC-01
- **Touches:** `src/yahoo_scrape.py`, `tests/test_yahoo_scrape.py`, `tests/test_live_fixtures.py`, Yahoo test fixtures
- **Why:** Yahoo Auctions now serves a Next.js page, so the `Product__*` classes the scraper looks for no longer exist.
  The live job finds zero listings and still sends one empty email per item every run, so the breakage looks like "no deals".
  The old design also zipped four independent lists, so any mismatch paired a price with the wrong listing.
  The Yahoo request has no timeout or status check, and the same page is downloaded once per item.
- **Scope:** Parse the embedded `__NEXT_DATA__` JSON into one validated record per listing, skip and count malformed records, and build listing URLs from `auctionId` (including PayPay Flea Market items).
  Raise a clear error when the page layout drifts instead of silently finding nothing.
  Add a timeout and status check to the Yahoo request, fetch the page once per run, and use the canonical URL without the trailing slash.
  Test against a hand-written page, a malformed page, and the sanitized live page, and replace the RC-01 strict-xfail selector test.
- **Resume angle:** Rebuilt a scraper around a site's embedded JSON data after a front-end redesign, with schema validation and loud failure on layout drift.

### RC-08: Live exchange rate

- **Size:** S
- **Depends on:** RC-07
- **Touches:** `src/yahoo_scrape.py`
- **Why:** Yen to dollar conversion is hardcoded as `0.0062`, so every deal calculation drifts as the exchange rate moves.
- **Scope:** Fetch the current rate from a free source once per run, with a timeout and a documented fallback.
- **Resume angle:** Integrated a live currency feed with graceful fallback.

### RC-09: Statistically robust pricing

- **Size:** M
- **Depends on:** RC-01
- **Touches:** `src/ebay_pricing.py`
- **Why:** The market price is a raw median of whatever survives keyword filtering.
  A price can be set from only two or three listings, and outliers are filtered only by keywords.
- **Scope:** Require a minimum sample size before updating a price, trim outliers, and report the sample size used.
- **Resume angle:** Applied outlier filtering and minimum-sample thresholds to market price estimation.

### RC-10: Reliability and logging

- **Size:** M
- **Depends on:** RC-01, and best done after Phase 1
- **Touches:** all modules
- **Why:** No network call has a timeout, so one hung request can stall a whole run, which matters even more once it runs unattended on Lambda.
  Failures are reported with `print`, and one failing item can stop the run.
- **Scope:** Add timeouts everywhere, retry with backoff on HTTP 429 and 5xx responses, switch from `print` to the `logging` module, and isolate per-item failures so one bad item does not stop the others.
- **Resume angle:** Added fault tolerance and structured logging to an unattended data pipeline.

---

## Phase 3: AWS deployment

The resume centerpiece.
The project is locked to Region `us-east-2`.

### RC-11: Daily run on AWS Lambda

- **Size:** L (planner should split)
- **Depends on:** RC-01, RC-10
- **Touches:** new `infra/` or `template.yaml`, new handler module
- **Go/no-go first:** fetch the Yahoo results page from AWS CloudShell.
  If Yahoo blocks AWS addresses, stop and rethink before building anything.
- **Why:** Today nothing runs unless the user types a command on their Mac.
- **Scope:**
  - Add a Lambda handler that runs eBay pricing, then the Yahoo scrape.
  - Store the SQLite database in S3: download it to `/tmp`, run, and upload it back.
  - Move secrets to SSM Parameter Store.
  - Trigger the function daily with EventBridge Scheduler.
  - Define everything in an AWS SAM template.
- **Resume angle:** Deployed a serverless, scheduled data pipeline on AWS Lambda with EventBridge, S3, and SSM, defined as infrastructure as code.

### RC-12: Cost and failure alerts

- **Size:** S
- **Depends on:** RC-11
- **Touches:** SAM template
- **Why:** An unattended job that fails silently is worse than no job.
- **Scope:** Add a budget alert, plus a CloudWatch alarm that notifies the user when the Lambda function errors.
- **Resume angle:** Production monitoring and cost guardrails.

---

## Phase 4: Product

### RC-13: Multi-brand support

- **Size:** M
- **Depends on:** RC-03b
- **Touches:** `src/schema.sql`, `src/db.py`, `src/yahoo_scrape.py`
- **Why:** The Yahoo brand ID `103538` and the Rick Owens page URL are hardcoded, so RainCheck can only ever watch one brand.
- **Scope:** Store each brand's Yahoo ID in the database and build the search URL per brand.
- **Resume angle:** Generalized a single-brand scraper into a configurable multi-brand system.

### RC-14: Item management command line tool

- **Size:** M
- **Depends on:** RC-03a
- **Touches:** `src/setup.py` (replaced), `src/db.py`
- **Why:** `setup.py` hardcodes the brand, size, and Ramones keywords for every item you add, which is the root cause of the `pod shorts` bug.
  It also writes to `catalog.json` instead of the database.
  It writes entries without `exclude_keywords`, so `ebay_pricing.py` crashes with `KeyError: 'exclude_keywords'` on any item added through it (found in the RC-02 review).
- **Scope:** Replace it with commands to add, list, update, and remove items, with real per-item keywords, writing to the database.
- **Resume angle:** Built a command line interface for managing tracked items.

### RC-15: Better deal emails

- **Size:** S
- **Depends on:** RC-01
- **Touches:** `src/sendemail.py`, `src/yahoo_scrape.py`
- **Why:** Today every item sends its own email, even when there are no deals, which produces the empty emails seen during testing.
- **Scope:** Send one digest per run, skip sending when there are no new deals, and lay deals out as an HTML table with image, price, market price, and discount.
- **Resume angle:** Designed a daily digest notification.

### RC-16: Auction-aware deals

- **Size:** M
- **Depends on:** RC-07, and best paired with RC-05
- **Touches:** `src/schema.sql`, `src/db.py`, `src/yahoo_scrape.py`
- **Why:** RC-07 compares Yahoo's current price against the market price.
  For an auction that is the current bid, so a listing that starts at 1 yen looks like a huge deal even though it will end far higher.
- **Scope:** Store whether a listing is fixed price, its buy-now price, and its end time.
  Treat fixed-price and buy-now listings as real deals, and decide how to judge running auctions (for example, only near their end time).
- **Resume angle:** Modeled auction versus fixed-price listings to cut false-positive deal alerts.

### RC-17: Word-aware keyword matching

- **Size:** S
- **Depends on:** RC-03b
- **Touches:** `src/matching.py`, tests
- **Why:** Matching is plain substring matching, so the exclude keyword `low` also removes `yellow` and `follow`, and full-width Latin letters in Japanese titles (for example `ＲＡＭＯＮＥＳ`) never match ASCII keywords.
- **Scope:** Normalize titles and keywords with Unicode NFKC before lowercasing, and match Latin keywords on word boundaries while keeping substring matching for Japanese text, which has no spaces.
  Test against the live Yahoo and eBay fixtures to measure how many matches change.
- **Resume angle:** Improved precision of text matching across English and Japanese listing titles.
