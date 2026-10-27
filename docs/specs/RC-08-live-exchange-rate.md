# RC-08: Live exchange rate

**Status:** Draft
**Backlog item:** RC-08 in `docs/BACKLOG.md`

## Summary

The Yahoo job fetches one current JPY-to-USD reference rate per run and uses it for every listing and item in that run.
If the rate is unavailable or unusable, the job uses an explicit fallback and warns the operator.

## Motivation

`src/yahoo_scrape.py:30` fixes the conversion at `0.0062` USD per JPY.
`find_item_deals` uses that constant for every listing at `src/yahoo_scrape.py:185`, so a changing exchange rate can make the displayed USD price and discount inaccurate.
RC-07 already made Yahoo retrieval explicit and bounded, so RC-08 can add a separate bounded request without changing Yahoo parsing.

## Scope

**In scope:**

- Fetch a JPY-to-USD rate from Frankfurter's public v2 single-pair endpoint at most once for a Yahoo run that has active items and at least one parsed listing.
- Validate the response currency pair, rate, and publication date before using it.
- Apply the same resolved rate to every deal calculation in that run.
- Warn clearly when the fallback rate is used.
- Update Yahoo job tests and fake HTTP fixtures for the additional request.

**Out of scope:**

- Converting eBay prices, other currency pairs, and currency conversion in the digest renderer.
- Persisting exchange rates or changing the database schema.
- Intraday trading quotes, purchase or payment exchange rates, conversion fees, shipping, taxes, and historical repricing of earlier alerts.
- Changing auction price selection or keyword matching.
- Retrying against other providers or adding an exchange-rate dependency or API key.

## Design

Frankfurter documents `GET https://api.frankfurter.dev/v2/rate/jpy/usd` as a single-pair, no-key HTTPS endpoint.
Its v2 response is a flat JSON object with `date`, `base`, `quote`, and `rate` fields.
The default rate is a blended mid-market reference rate, published on the providers' schedules, rather than a live trading quote.
See the [official API documentation](https://frankfurter.dev/) and [official Python example](https://frankfurter.dev/python/).

The Yahoo job should obtain the rate after a successful Yahoo parse and before building deal sections.
The fetch uses `requests.get` with a 10-second timeout and `raise_for_status()`.
The date is compared with the current UTC date, since the provider dates its data in UTC.
The proposed freshness window is seven calendar days, inclusive, so weekends and short market holidays do not cause routine fallback.
One rate value is passed explicitly through the pure `build_sections` and `find_item_deals` functions, which keeps tests deterministic and prevents hidden network access.
The current constant `0.0062` remains only as the documented fallback value.

### Files touched

| File | Change |
| --- | --- |
| `src/yahoo_scrape.py` | Fetch and validate one rate, select fallback, pass it into deal calculations, and log fallback use. |
| `tests/test_yahoo_scrape.py` | Add rate parser, fallback, call-count, freshness, and end-to-end conversion cases; pass explicit rates in pure-function tests and adapt existing pipeline fixtures. |

`src/schema.sql`, `src/db.py`, `src/digest.py`, and `src/catalog.json` are read for context but do not change.
If a reusable fake response is needed, `tests/conftest.py` may be updated only for that fixture; this is not a product-code dependency.

### Schema changes and migration

None.
Existing `raincheck.db` files remain at schema version 1 and need no migration or reseeding.

### Interfaces

- `fetch_yen_to_usd_rate(today_utc: date) -> float` performs one HTTPS GET and returns the validated rate.
  It raises a specific rate-data error for invalid response shape, currencies, date, freshness, or rate; `requests` transport and HTTP errors may propagate from this function.
- `resolve_yen_to_usd_rate(today_utc: date) -> float` calls the fetch once, catches expected request and rate-data errors, logs one warning with the reason and fallback value, and returns `0.0062` for that run.
- `find_item_deals(item, listings, skip_urls, yen_to_usd)` and `build_sections(items, listings, known_urls, yen_to_usd, per_item_limit=TOP_DEALS_PER_ITEM)` require an explicit rate.
  They remain pure, calculate `price_usd = price_yen * yen_to_usd`, and reject non-finite, non-positive, boolean, or non-numeric rates with `ValueError`.
- `main()` calls the resolver at most once after successful Yahoo parsing and passes the result to `build_sections`.
  It does not request a rate when the database has no active items, Yahoo fetch or parse fails, or the parsed page has no listings.

## Acceptance criteria

1. Given a valid API object such as `{"date":"2026-10-27","base":"JPY","quote":"USD","rate":0.0067}` and a UTC run date of 2026-10-27, the fetch returns `0.0067` after one GET to the v2 single-pair endpoint with `timeout=10` and an HTTP status check.
2. Given two active items and multiple listings, `main()` makes at most one exchange-rate GET and uses its one rate for every candidate's USD price, discount, deal decision, and best-discount ordering.
3. Given a 50,000 JPY listing, an 800 USD market price, and a rate of `0.0067`, the deal price is 335 USD and the discount is 58.125 percent.
   Given the same listing and a rate of `0.0062`, they are 310 USD and 61.25 percent.
4. A published rate dated today or at most seven calendar days before the current UTC date is accepted.
   A future date or a date eight or more days old causes fallback with one warning.
5. Missing or wrong `base` or `quote`, missing or malformed `date`, a non-object JSON body, missing `rate`, `null`, string, boolean, zero, negative, NaN, or infinite `rate` causes fallback with one warning.
6. A timeout, connection error, invalid JSON, HTTP 4xx, or HTTP 5xx from the exchange-rate request causes fallback to `0.0062` with one warning; the Yahoo job can still send its digest and record the emailed deals.
7. The warning identifies that the JPY-to-USD fallback was used and includes its numeric value, without printing a traceback for an expected provider failure.
8. No active items means no Yahoo or exchange-rate GET, no email, and the existing no-items line.
   A Yahoo fetch or parse failure still propagates without an exchange-rate GET or email.
   An empty parsed Yahoo page makes no exchange-rate GET and sends no email.
9. `find_item_deals` and `build_sections` make no network or database calls and use only the rate supplied by the caller.
   An invalid supplied rate raises `ValueError` before a deal is built.
10. An otherwise qualifying listing remains unrecorded if its converted price is at or above the item's market price, so it can become a deal on a later run with a different valid rate.
11. On a successful run, the digest's existing yen and USD values and percentages reflect the selected rate, and only displayed deals are recorded after a successful send.
12. The full test suite passes without warnings, real network requests, real email, or access to the user's database.

## Edge cases

- Empty results: A valid empty Yahoo page yields no exchange request, digest, or email; an empty or non-object exchange response uses fallback.
- `None` or missing values: Missing, `null`, or wrong-type API fields use fallback; an item without a usable market price remains skipped as today.
- Network failure or bad HTTP status: Yahoo failures retain their current hard failure behavior; exchange provider failures use fallback and log one warning.
- Malformed input: Invalid JSON, malformed dates, future or stale dates, wrong currencies, and non-finite rates use fallback.
- Duplicates: Duplicate Yahoo listing IDs are still removed by `parse_listings`; they do not trigger additional exchange requests.

## Test plan

- Add a small recorded Frankfurter v2 JSON fixture or hand-written fake object with the four documented fields; make its date controllable from a frozen UTC clock.
- Fake HTTP responses for success, timeout, connection error, HTTP 4xx/5xx, invalid JSON, wrong pair, missing keys, bad rate types and values, and stale or future dates.
- Use the existing saved Yahoo minimal and malformed HTML, seeded throwaway database, and fake email fixture for pipeline tests.
- Update existing pure deal and section tests to pass an explicit `0.0062` rate, preserving their prior numeric expectations.
- Add pure tests with `0.0067` to prove the conversion, threshold crossing, sorting, and invalid-rate rejection.
- Add a job test with two tracked items and multiple deals to prove one fetch and one rate used throughout the digest.
- Verify no exchange request on no-items, empty-page, and Yahoo-failure paths, and verify fallback warnings through captured logs.
- Keep the autouse network block, dry-run email setting, and `RAINCHECK_DB_PATH` throwaway database fixture in force.

## Shared-file conflicts

This feature does not touch `src/schema.sql`, `src/db.py`, or `src/catalog.json`.
It does touch `src/yahoo_scrape.py`, so it should be sequenced after RC-07 and coordinated with any active feature editing that file.

## Open questions

1. Should provider failure use the current fixed `0.0062` rate and continue alerting, as recommended, or should the run stop without an email?
   Continuing keeps deal alerts available during short outages but can misprice them until the provider recovers.
   Stopping avoids potentially inaccurate alerts but misses time-sensitive listings.
2. Is seven calendar days the right maximum age for a published rate?
   Seven days accommodates weekends and short holidays while rejecting a feed that has stopped updating; a shorter limit catches stale data faster but invokes fallback more often.

## Rollback

Revert the source and test change to restore the fixed `0.0062` conversion.
No database rollback or migration is needed.
