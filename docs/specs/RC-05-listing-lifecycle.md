# RC-05: Listing lifecycle tracking

**Status:** Approved
**Backlog item:** RC-05 in `docs/BACKLOG.md`

## Summary

Refresh `last_seen_at` when a previously stored listing appears again in a valid Yahoo results page.
Leave ended-auction detection and final sale prices for a separate item until a reliable source and matching method are established.

## Motivation

`src/db.py:631-648` sets `last_seen_at` when `record_listing_match` first creates a listing, but its URL-conflict path never changes that timestamp.
`src/yahoo_scrape.py:237-260` reads known URLs to suppress repeat alerts, then returns without a database write when there is no new digest.
Consequently, a stored listing can appear on several later runs while its `last_seen_at` still equals `first_seen_at`.
The existing `ended_at` and `final_price_yen` columns in `src/schema.sql` are also unwritten, but disappearance from the newest 100 results does not establish that an auction ended.
Yahoo's [auction guide](https://auctions.yahoo.co.jp/guide/guide/bid02.html) describes both early ending and automatic extension, and Yahoo's [developer notices](https://developer.yahoo.co.jp/changelog/auctions.html) report termination of the old Auctions Web API.
Yahoo also has [completed-auction search results](https://auctions.yahoo.co.jp/closedsearch/closedsearch/auction/0), but the current job does not consume them and these sources do not establish a stable, documented lookup by the stored auction ID.

## Scope

**In scope:**

- Update `last_seen_at` for already stored listing URLs present in a successfully fetched and parsed results page, including runs with no new deals or no sent email.
- Preserve the original `first_seen_at`, listing metadata, match rows, and the rule that only successfully emailed deals create new listing rows.
- Use one UTC observation timestamp per job run and make repeated URLs within one update harmless.

**Out of scope:**

- Setting `ended_at` or `final_price_yen`, inferring auction completion from absence, or fetching individual listing or completed-search pages.
- Recording listings that have never been emailed, including above-market listings and deals omitted by the digest limit.
- Tracking a price history, changing stored `price_yen`, changing alert eligibility, or sending additional email.
- Changing Yahoo parsing, keyword matching, currency conversion, or auction-aware deal rules.

## Design

### Files touched

| File | Change |
| --- | --- |
| `src/db.py` | Add an existing-only batch operation to refresh `last_seen_at` for observed URLs. |
| `src/yahoo_scrape.py` | Call that operation after a successful fetch and parse, before checking whether a digest exists. |
| `tests/test_db.py` | Test timestamp changes, existing-only behavior, duplicates, empty input, and connection closure. |
| `tests/test_yahoo_scrape.py` | Test repeat observations and the no-digest and failed-send paths with saved HTML and a throwaway database. |

### Schema changes and migration

None.
`src/schema.sql` already has `first_seen_at`, `last_seen_at`, `ended_at`, and `final_price_yen` in schema version 1.
An existing version 1 `raincheck.db` needs no migration and retains its historical timestamps unchanged until a stored URL is observed again.
The job must not run a migration or access the user's real database during verification.

### Interfaces

Add `update_last_seen_at(urls: Iterable[str], observed_at: datetime) -> int` to `db.py`.
The operation consumes its URL iterable once, deduplicates it, and updates only rows already present in `listings`.
It rejects a naive or non-UTC `observed_at` with `ValueError` before opening a database connection and stores the timestamp in the existing UTC ISO 8601 format.
It returns the number of distinct stored rows whose `last_seen_at` moved forward.
An empty URL collection returns zero without opening a connection.
For nonempty input, one transaction covers the updates and every connection closes on success or failure.
Use parameterized SQL and a `WHERE` clause that prevents a stale observation from moving `last_seen_at` backward.

After `parse_listings` succeeds, `yahoo_scrape.main()` computes a UTC observation time and passes the parsed URLs to this operation.
The operation runs whether or not the page yields a digest and before any email attempt.
Only existing rows can change, so a failed send still leaves newly discovered deals unstored and eligible for retry.
Keep `get_known_listing_urls` read-only and keep `record_listing_match` responsible for inserting emailed deals.
`first_seen_at` and `last_seen_at` remain equal when a listing is first inserted after a successful send.

### Ended auctions and final prices

The active results page shows a limited, newest-first slice, so a missing URL may merely have fallen beyond page 1 or been omitted for another reason.
Marking such a row ended would be a false inference.
Yahoo's public completed-search page displays closing prices and times, but this spec does not assume its undocumented embedded JSON remains stable or that a search result can always be joined by URL or auction ID.
Investigate these fields separately with captured examples of live, ended-with-sale, and ended-without-sale listings and a documented or empirically validated ID mapping.

## Acceptance criteria

1. Given a stored listing with an earlier UTC `last_seen_at`, one successful Yahoo run whose parsed page contains that URL sets `last_seen_at` to that run's UTC observation time and leaves `first_seen_at`, title, price, image, brand, and match rows unchanged.
2. A successful run with only previously stored listings and no new deals updates their `last_seen_at`, sends no email, and creates no listing or match rows.
3. A successful run with both stored and new listing URLs updates only the stored rows before email, and creates new listing and match rows only for deals included in a successfully sent digest.
4. If email delivery fails, previously stored listings on the parsed page retain their refreshed `last_seen_at`, while new deals and their match rows remain absent and can be offered on the next run.
5. A URL absent from the parsed page keeps its prior `last_seen_at`, `ended_at`, and `final_price_yen` values.
6. The database operation ignores unknown URLs, handles repeated URLs once, returns the count of distinct existing rows whose timestamp advanced, and does not create rows.
7. An empty URL iterable returns zero without opening a database connection or changing data.
8. An observation timestamp at or before a stored `last_seen_at` does not move it backward, and a naive or non-UTC timestamp raises `ValueError` before opening a connection.
9. A fetch error, bad HTTP status, or malformed Yahoo page causes no lifecycle update, email, or new listing write.
10. The batch operation commits all timestamp changes together and closes its connection on both success and SQL failure.
11. Schema version stays at 1, and `ended_at` and `final_price_yen` are never populated by this feature.

## Edge cases

- Empty results: A valid page with no listings passes an empty iterable to the database operation, which changes nothing, and no email is sent.
- `None` or missing values: `YahooListing.url` is validated by the existing parser; nullable image URLs have no effect on timestamp refresh, and a missing URL never reaches this operation from the job.
- Network failure or bad HTTP status: The job exits before parsing and before any lifecycle database write.
- Malformed input: A page rejected by `parse_listings` cannot refresh timestamps, while records individually rejected by the parser are not treated as observations.
- Duplicates: The parser already keeps one record per auction ID; the database operation also deduplicates URL inputs before updating.
- Clock skew: An older observation cannot reduce a stored `last_seen_at`.
- Page truncation: Absence from the newest 100 results is not evidence that the auction ended.

## Test plan

- Use `tests/conftest.py` temporary SQLite fixtures and seeded item rows; never use the user's `src/raincheck.db`.
- In `tests/test_db.py`, seed a stored listing with known UTC timestamps and match row, then assert later observation updates only `last_seen_at`.
- Cover two existing URLs, one unknown URL, duplicate URLs, a generator consumed once, empty input, stale and equal timestamps, and naive/non-UTC timestamps.
- Inject a SQL error during a multi-URL update and assert rollback and closed connection.
- In `tests/test_yahoo_scrape.py`, use the saved Yahoo HTML fixture and mocked fetch/email to run the job twice and assert the second run refreshes stored timestamps without another email.
- Include a mixed known/new page and a failed send, then assert known timestamps refresh while new deal rows remain absent until a later successful send.
- Use malformed HTML and mocked HTTP errors to assert no timestamp writes.
- No new recorded API JSON or live network fixture is required for this limited scope.

## Shared-file conflicts

This feature touches `src/db.py`.
Do not implement it in parallel with RC-06, RC-13, RC-14, or RC-16, which also touch `src/db.py` or `src/schema.sql`.
It also touches `src/yahoo_scrape.py`, so coordinate its implementation with RC-08 and RC-10.
It does not touch `src/schema.sql` or `src/catalog.json`.

## Open questions

1. Should RC-05 stop at refreshing `last_seen_at`, leaving ended-auction detection and final-price capture for a separate backlog item?
   Recommendation: yes.
   This makes RC-05 small and testable without guessing an auction ended when it disappeared from a truncated active-results page.
   The tradeoff is that `ended_at` and `final_price_yen` remain empty until the separate item validates Yahoo's completed-result lookup and the cases where a listing ends without a sale.

## Resolved

- 2026-10-27: The user approved this spec and the recommendation to handle ended-auction detection and final-price capture as a separate future item.

## Rollback

Revert the changes to `src/db.py` and `src/yahoo_scrape.py` to stop future timestamp refreshes.
No schema rollback is needed.
Already updated `last_seen_at` values can remain because they record actual observations and do not affect deal selection.
