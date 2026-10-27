# RC-09: Statistically robust eBay pricing

**Status:** Approved
**Backlog item:** RC-09 in `docs/BACKLOG.md`

## Summary

Estimate each tracked item's eBay market price from a minimum number of distinct, usable listings after outlier removal.
Print the candidate and retained sample counts for each item so a stored price has visible support.

## Motivation

`ebay_market_price` currently takes the median of every title match on one eBay search page, even when only one or two prices match (`src/ebay_pricing.py:41-67`).
It assumes every summary has a title and numeric price, and its debug print sorts all summaries by price (`src/ebay_pricing.py:50-63`).
One thin or malformed result set can therefore produce an unreliable price or abort the run.
`refresh_market_prices` writes every usable median without a sample threshold (`src/ebay_pricing.py:85-98`).

The [eBay Browse search guide](https://developer.ebay.com/develop/guides/buy/inventory-discovery-and-refresh-guide) confirms that `limit` bounds one page and `offset` selects another page.
The [eBay marketplace guide](https://developer.ebay.com/api-docs/static/gs_understand-ebay-marketplaces.html) says marketplace prices can be converted into the viewer's currency.
RainCheck must still check each summary's `price.currency` before storing a USD benchmark.

## Scope

**In scope:**

- Validate each Browse summary before it can contribute a USD price.
- Count distinct valid title matches, remove price outliers, and require at least five retained listings before updating the database.
- Report the valid candidate count, outlier count, retained count, and skip reason for each tracked item.
- Preserve the current one-page search, database price fallback, and network error behavior.
- Update the README's description of how market prices are computed.

**Out of scope:**

- Fetching additional eBay pages, changing query ranking, or changing the 50-result request limit.
- Sold/completed listing prices, shipping, taxes, negotiated Best Offer amounts, currency conversion, and condition or size normalization.
- Changing keyword matching or catalog data (RC-17 owns matching).
- Clearing or aging an existing stored eBay price after a thin or empty result; see Open questions.
- Retries, per-item HTTP failure isolation, and replacing `print` with logging (RC-10).

## Design

### Files touched

| File | Change |
| --- | --- |
| `src/ebay_pricing.py` | Validate and deduplicate summaries, calculate a robust estimate, enforce the sample threshold, and print per-item counts. |
| `tests/test_ebay_pricing.py` | Replace assumptions that two or three results can set a price; add estimator, malformed summary, duplicate, and database preservation cases. |
| `tests/fixtures/ebay/search_ramones.json` | Add enough distinct valid matching summaries for the existing refresh test scenario while preserving its include/exclude cases. |
| `README.md` | Describe the minimum sample, trimming, counts, and retained-price behavior. |

Reuse `tests/fixtures/ebay/search_empty.json` unchanged for empty results and `tests/fixtures/ebay/search_live.json` unchanged as the recorded API shape.
New small JSON fixtures under `tests/fixtures/ebay/` may be added for malformed, duplicate, and outlier examples.
`tests/conftest.py` already blocks network and uses a temporary database, so it needs no change.
`src/matching.py` remains the shared matcher and needs no change.

### Schema changes and migration

None.
Existing `raincheck.db` files stay at schema version 1 and need no migration.
The sample counts are printed for the current run and are not persisted.

### Interfaces

Keep `ebay_market_price(query, keywords, exclude_keywords, token)` and its existing request URL, `q`, `limit=50`, `EBAY_US` header, bearer token, and 30-second timeout.
Change its successful return to a frozen `PriceEstimate` with `price_usd: float | None`, `candidate_count: int`, `outlier_count: int`, and `retained_count: int`.
`price_usd` is `None` when the retained count is below five.
The counts refer to distinct, valid USD title matches before trimming, removed outliers, and remaining listings, respectively.
An empty search returns counts of zero and `price_usd=None`.
`refresh_market_prices()` prints these counts for each item and calls `update_market_price()` only when `price_usd` is present.
An existing stored market price remains unchanged when an estimate is unavailable; when it is already `NULL`, `effective_price_usd` continues to use `manual_price_usd` if available (`src/db.py:80-90`).

Process each `itemSummaries` row in API order.
Accept a row only when it is an object with a nonempty string `itemId`, a string `title` accepted by `title_matches`, a `price` object with `currency == "USD"`, and a finite numeric `value` greater than zero.
Accept numeric strings or JSON numbers as price values; reject booleans, `NaN`, infinity, missing values, nonnumeric strings, and other types.
Deduplicate by `itemId` after validation, keeping the first valid appearance, so a malformed first copy cannot hide a valid later copy.
Rows failing validation do not count toward any sample count and do not stop the item.
Treat an absent `itemSummaries` key as an empty search.
Raise `ValueError` for a response whose top level is not an object or whose present `itemSummaries` value is not a list; no price is written for that item.

For the valid candidate prices, calculate the median `m` and the median absolute deviation `MAD = median(abs(price - m))`.
When `MAD > 0`, remove a price when `abs(price - m) / (1.4826 * MAD) > 3.5`; a value exactly on the boundary stays.
When `MAD == 0`, retain only prices equal to `m`.
Do one filtering pass using the original candidate median and MAD; do not recalculate the cutoff after removals.
Take the ordinary median of retained prices only when at least five remain.
Do not round before storing; the existing SQLite `REAL` value and display behavior remain.
This conservative rule may reject a genuinely multimodal market; the skipped update and counts make that visible.

The official [Browse API overview](https://developer.ebay.com/develop/api/buy) confirms this method is a search over item summaries and requires an application access token.
The method's price and currency fields are represented in the existing recorded response and small fixture.
The project uses one page on purpose; `candidate_count` is a count of examined summaries, not the API's `total` across all pages.

## Acceptance criteria

1. A 200 response with five distinct, title-matching USD listings priced `100, 110, 120, 130, 140` returns `PriceEstimate(120.0, 5, 0, 5)` and refresh stores `120.0` for that item.
2. Four otherwise valid listings return `price_usd=None`, `candidate_count=4`, `outlier_count=0`, `retained_count=4`, and refresh does not call `update_market_price` for that item.
3. The five valid prices `100, 105, 110, 115, 120` plus `1000` remove only `1000`, return a price of `110.0`, and report six candidates, one outlier, and five retained.
4. The five valid prices `100, 100, 100, 100, 100` plus `1000` exercise `MAD == 0`, remove `1000`, and store `100.0` from five retained listings.
5. If outlier removal leaves fewer than five listings, `price_usd` is `None` and the existing stored price is unchanged.
6. A duplicate valid `itemId` contributes once, using the first valid occurrence's price; a malformed first appearance followed by a valid copy contributes once using the valid copy.
7. Missing or non-string `title`, missing or blank `itemId`, missing or non-object `price`, missing or non-USD `currency`, zero, negative, nonnumeric, boolean, `NaN`, and infinite prices are skipped without an exception and never counted.
8. An empty `itemSummaries` list or missing key returns `PriceEstimate(None, 0, 0, 0)`; refresh prints a no-sample skip reason and keeps the stored price.
9. A non-object top-level JSON value or non-list `itemSummaries` raises `ValueError` before any update for that item.
10. For each item whose search returns a valid response, refresh prints its name and the three sample counts, then prints either the stored estimate or a specific insufficient-sample reason; no raw listing titles or prices are dumped.
11. An item with insufficient data does not prevent later tracked items from being priced.
   An HTTP 500 or request timeout raises the existing `requests` exception, writes no price for the failing item, prints no sample counts for that item, and stops the run before searching later items.
12. The search makes exactly one GET for each item reached before the run completes or stops on a request error, with the existing request parameters and 30-second timeout; the token request is unchanged.
13. A skipped update leaves both `market_price_usd` and `manual_price_usd` unchanged in a throwaway database; `effective_price_usd` follows the existing eBay-then-manual rule.
14. README text states that the estimate comes from the first 50 search results, requires five retained distinct listings, applies the specified outlier rule, and prints counts without storing them.

## Edge cases

- Empty results: return zero counts and leave any stored price in place.
- `None` or missing values: skip invalid summaries as specified above; a missing `itemSummaries` key is empty.
- Network failure or bad HTTP status: keep the current `requests` exception behavior and 30-second timeout; the failing item is not updated.
- Malformed input: skip malformed rows; reject malformed response containers with `ValueError`.
- Duplicates: count each valid `itemId` at most once, with the first valid price winning.
- Tied prices and zero MAD: keep prices equal to the candidate median; skip an update if fewer than five survive.
- Fewer than five after trimming: print counts and preserve the stored price.

## Test plan

Use existing offline HTTP fakes, `seeded_db`, `db_conn`, and the saved `search_ramones.json` and `search_empty.json` responses.
Update the small `search_ramones.json` fixture and synthetic POD_SHORTS responses to include five distinct usable `itemId` values where existing tests require a successful update.
Add compact recorded-shape JSON fixtures or synthetic responses for outliers, duplicate IDs, bad currency, malformed fields, and a non-list response container.
Check the returned estimate and the printed counts for each case, then assert both persisted columns in the temporary database for success and skip paths.
Keep all tests offline and make no real eBay request or email send.

## Shared-file conflicts

This feature does not touch `src/schema.sql`, `src/db.py`, or `src/catalog.json`.
It can run alongside features that edit those shared files, but another feature changing `src/ebay_pricing.py`, `tests/test_ebay_pricing.py`, or the small eBay fixture will conflict.

## Open questions

1. **Threshold and outlier rule.**
   Recommendation: approve the five-retained-listing minimum and the median absolute deviation rule above.
   This gives a clear confidence floor and resists extreme asking prices while allowing existing 50-result searches to work.
   A higher floor such as ten is more conservative but will skip more niche items; a relative price band is simpler but less adaptive to different item prices.
2. **What should happen to an older eBay price when today's sample is insufficient?**
   Recommendation: preserve it, as this spec does, because a single thin search should not erase the best prior estimate and the existing manual fallback remains available only when the stored eBay price is `NULL`.
   The tradeoff is that an older eBay price can remain stale; clearing it would require a database write path and would immediately switch deal math to the manual price or skip the item.
3. **Should RC-09 fetch more than the first 50 eBay summaries when fewer than five survive?**
   Recommendation: keep one page for this PR and report the sample count.
   Pagination could rescue niche items, but it increases API calls, rate use, runtime, and failure modes; it can be a separate backlog item after real skip rates are known.

## Resolved

- 2026-10-27: The user approved the five-retained-listing threshold, median absolute deviation outlier rule, preservation of the prior stored price when data are insufficient, and the existing first-page limit of 50 eBay results.

## Rollback

Revert the RC-09 code and README changes.
No schema rollback or data migration is needed.
Prices already stored by RC-09 remain valid `market_price_usd` values; if the user wants to replace one, rerun the prior pricing job against a throwaway database first to inspect the effect, then decide whether to refresh the live database.
