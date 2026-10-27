# RC-18: Auction completion and final-price source feasibility

**Status:** Draft
**Backlog item:** RC-18 in `docs/BACKLOG.md`

## Summary

Establish whether Yahoo's public pages can identify the completed state, sale outcome, actual end time, and final sale price of a stored RainCheck auction by its exact auction ID.
The current evidence supports a sold example but does not pass the unsold and relisting gate, so this draft specifies a no-go for production completion writes until that evidence is captured.

## Motivation

`src/schema.sql:49-50` has nullable `listings.ended_at` and `listings.final_price_yen`, but no writer.
`src/db.py:781-809` refreshes `last_seen_at` only when a stored URL appears again, and `src/yahoo_scrape.py:399-401` reads just the newest active results page.
A missing URL may have fallen outside that page, so it cannot prove an auction ended or what it sold for.
`src/yahoo_scrape.py:115-124` retains an auction ID while parsing active results, and the stored URL contains that ID, which gives a possible exact lookup key.

### Evidence as of 2026-10-27

- Yahoo's public [completed search for Rick Owens](https://auctions.yahoo.co.jp/closedsearch/closedsearch/2084199087) is labeled as sold items and shows a sale price and end time for each displayed result.
  Its link for auction `f1247065602` resolves to a [public item page](https://auctions.yahoo.co.jp/jp/auction/f1247065602) that says the auction ended, shows `89,000円`, and identifies auction ID `f1247065602` and an end time in its item details.
  This demonstrates one same-ID sold example, not a stable parser contract for all auctions.
- Yahoo's [auction ID guide](https://support.yahoo-net.jp/PccAuctions/s/article/H000005256) says the ID identifies an auction and appears in its URL.
  Yahoo's [relisting guide](https://support.yahoo-net.jp/SccAuctions/s/article/H000008902) says a relisting with no bidders retains the same auction ID and URL, while one with bids gets a new ID and URL.
  Therefore exact ID alone may identify a listing URL but cannot always distinguish successive no-bid listing cycles.
- Yahoo's [seller completion guide](https://support.yahoo-net.jp/PccAuctions/s/article/H000005180) distinguishes ended auctions with and without a buyer in the seller's account, but does not establish that both outcomes are machine-readable on a public page.
  Its sold-results search is not evidence that unsold auctions are included.
- Yahoo's [page-retention guide](https://support.yahoo-net.jp/PccAuctions/s/article/H000008859) says pages with a buyer may remain for up to five years and those without a buyer for 120 days, with earlier removal possible.
  A missing or inaccessible page is therefore not proof of an unsold auction.
- The committed `tests/fixtures/yahoo/brand_page_live.html` is an active-results snapshot; there is no saved sold, unsold, relisted, or deleted item-page fixture.
  Direct public lookups of several IDs from that active fixture did not produce inspectable pages through the available web tool, so they did not close the evidence gap.

Yahoo does not document an API or stable embedded JSON contract for completed item pages in the reviewed sources.
The visible page text for a sold example also uses a current-price label, so a parser must establish sale outcome independently before treating that amount as `final_price_yen`.
These observations support further source discovery, not production writes.

## Scope

**In scope:**

- Define an evidence gate for exact auction identity, ended state, actual end time, sold price, and ended-without-sale state.
- Capture sanitized, dated public examples of sold, unsold, still-active, no-bid relisted, removed or expired, and malformed pages without using credentials or the real database.
- Check whether the same auction URL can represent multiple auction cycles, and whether the public page exposes a stable cycle identifier.
- Record a pass or no-go decision with the supporting page URLs, capture dates, visible fields, and limitations before implementation is approved.
- Preserve `ended_at` and `final_price_yen` as `NULL` when the gate fails or a page is unavailable or ambiguous.

**Out of scope:**

- Production fetches, database writes, scheduled completion checks, schema migrations, or email changes in this feasibility item.
- Inferring completion from absence in the active top 100, a scheduled end time, a timeout, an HTTP error, or a removed page.
- Treating the current bid or start price as a final sale price without an independently verified sale outcome.
- Reading a seller's private account, using credentials, scraping buyer identities, or contacting sellers.
- Recovering historical prices from third-party archives or estimating the final price.

## Design

### Files touched

| File | Change |
| --- | --- |
| `docs/specs/RC-18-auction-completion-feasibility.md` | Record evidence, gate, no-go outcome, and decisions. |

If the gate later passes, write a separate implementation amendment or smaller RC-18b spec before editing code.
That implementation would likely touch `src/db.py` for candidate selection and exact-ID updates, `src/yahoo_scrape.py` or a separate Yahoo completion module for bounded fetch and parse, and `tests/test_db.py` plus a new completion test file and sanitized Yahoo page fixtures.
It may touch `src/schema.sql` if a distinct auction-cycle key or outcome column is required.
The future spec must name every actual file after the source shape and cycle semantics are established.

### Schema changes and migration

None in this feasibility item.
`PRAGMA user_version` remains 3 and the existing database is not accessed or migrated.
The existing nullable columns are insufficient to represent repeated no-bid cycles under one URL if the user wants a history of each cycle.
If the evidence gate proves that case and the user wants cycle history, the future implementation spec must define a new table or distinct cycle key, a versioned migration from the then-current schema, and a verified backup-first upgrade procedure that preserves every existing row.
Do not assign `final_price_yen = 0` for an unsold auction; `NULL` means no verified sale price.

### Interfaces

There is no new production function in this feasibility item.
The gate's evidence record must include the exact request URL, HTTP status, capture timestamp, whether authentication was required, the auction ID from both the requested URL and page, the page's visible completion and buyer/sale labels, the displayed price label and integer yen amount if sold, the displayed end time and timezone, and whether a relisting reused the URL.
Sanitize fixtures before committing: remove seller and buyer identifiers, cookies, tracking data, and irrelevant HTML while preserving the exact evidence fields and their containing structure.
Never include a private account page as a fixture.

**Go gate for a future writer:**

1. At least one public sold example, one public ended-without-sale example, one still-active example, and one no-bid relisted example have inspectable responses and stable exact-ID evidence.
2. The sold example has an unambiguous sale outcome, a positive integer yen sale price, and an actual end time with known timezone; the unsold example has an unambiguous ended-without-buyer signal and an actual end time, with no invented final price.
3. A page with the correct URL but a different or missing page auction ID is rejected.
4. A no-bid relisting exposes a distinct public cycle key that can be stored and compared; otherwise the reused URL cannot support a reliable permanent completion record and the writer is no-go.
5. A deleted, expired, blocked, malformed, or login-only page remains unknown; a retry policy and bounded request budget are specified before production work.
6. Each evidence shape can be captured as a sanitized fixture and checked offline against expected identity, status, time, and price.

**No-go outcome now:** the sold example passes only part of the gate.
No inspectable public unsold and relisted examples or stable cycle discriminator have been established, so do not implement a writer for `ended_at` or `final_price_yen` from the current evidence.
If further public discovery cannot satisfy the gate, close RC-18 with this limitation documented and leave the nullable columns untouched.

## Acceptance criteria

1. The evidence record cites an exact Yahoo sold-results entry and its linked item page with the same auction ID, its displayed sold amount, and an end time.
2. The evidence record distinguishes Yahoo's documented sold and unsold seller states from what has actually been verified on public pages.
3. The evidence record states that no-bid relisting may reuse an auction ID and URL, so URL equality alone does not establish a unique completed cycle.
4. The gate requires sanitized, inspectable sold, unsold, active, and relisted examples and rejects missing identity, ambiguous outcome, absent actual end time, or an unverified price.
5. The current result is marked no-go because an unsold public example and a relisted-cycle discriminator are not established.
6. No source code, test, fixture, schema, catalog, or configuration file changes in this feasibility item; no real database or credentials are accessed.
7. In any future implementation, an active result missing from the newest page, a scheduled end in the past, an HTTP 404, and an expired or malformed page leave `ended_at` and `final_price_yen` unchanged.
8. In any future implementation, a verified ended-without-sale record has `final_price_yen = NULL`; a positive final yen value is stored only for a verified sale of the exact auction identity and cycle.

## Edge cases

- Empty results: An empty completed search proves nothing about a stored auction because of filters, pagination, and retention limits.
- `None` or missing values: Missing auction ID, status, end time, or sold amount fails the gate for that page; an unsold outcome has no sale price.
- Network failure or bad HTTP status: Timeout, 403, 404, 429, and 5xx leave the auction state unknown and require no database change.
- Malformed input: A page without the expected item ID or with contradictory active and ended markers is ambiguous and must not be recorded as completed.
- Duplicates: Repeated completed-search rows for one ID are not independent sales; same-ID no-bid relistings may be separate cycles and require an explicit model.
- Later buyer cancellation: Yahoo's displayed buyer status may change after an apparent sale, so a future spec must decide whether stored results are immutable first observations or revisable current outcomes.

## Test plan

No automated tests are added for a documentation-only feasibility result.
For the gate, save sanitized public HTML or the documented response format for one sold, one unsold, one active, one no-bid relisted, and one removed or expired listing, with the exact original URLs and capture dates recorded in this spec.
Compare the sold search result's auction ID, price, and end time against the exact item page; inspect unsold and relisted pages for an ID and cycle discriminator.
If the gate passes, the next spec must require offline parser fixtures for every state, a temporary schema-versioned database, exact-ID matching, malformed/duplicate cases, and fake HTTP responses for all network failures.
It must also verify no state change on an unknown page, idempotent repeated observations, transaction rollback, connection closure, and no access to `src/raincheck.db`.

## Shared-file conflicts

This feasibility spec touches none of `src/schema.sql`, `src/db.py`, or `src/catalog.json`, so its documentation work can run beside code work.
Any future RC-18 implementation touching `src/db.py` or `src/schema.sql` cannot run in parallel with RC-13, RC-14, or another feature changing those shared files.

## Open questions

1. Is a limited, public-page feasibility study acceptable before any code change?
   Recommendation: yes, and keep production writes gated on inspectable sold, unsold, active, and relisted evidence.
   Tradeoff: final-price capture is delayed, but the database will not claim a sale that cannot be verified.
2. If Yahoo reuses one URL for no-bid relistings, should RainCheck track every auction cycle or only the latest visible state?
   Recommendation: track cycles only if Yahoo exposes a stable public cycle key; otherwise leave completion unknown for that URL.
   Tradeoff: a cycle table adds schema and migration work, while using only the latest state can erase a prior unsold completion.
3. Should a later buyer cancellation revise a previously captured sale outcome?
   Recommendation: defer this decision until public examples show what Yahoo exposes and how it changes.
   Tradeoff: an immutable first result is simpler, while a revisable status better reflects a canceled transaction.

## Rollback

This item changes documentation only, so reverting the spec commit fully rolls it back.
No database rollback is required.
