# RC-18: Auction completion and final-price source feasibility

**Status:** Approved
**Backlog item:** RC-18 in `docs/BACKLOG.md`

## Summary

Establish whether Yahoo's public pages can identify the completed state, sale outcome, actual end time, and final sale price of a stored RainCheck auction by its exact auction ID.
The verified yen amounts in this study are individual Yahoo Auctions Japan sale observations, not a market-value estimate.
The current evidence supports a sold example but does not pass the unsold and relisting gate, so production completion writes remain no-go until that evidence is captured.

## Motivation

`src/schema.sql:49-50` has nullable `listings.ended_at` and `listings.final_price_yen`, but no writer.
`src/db.py:781-809` refreshes `last_seen_at` only when a stored URL appears again, and `src/yahoo_scrape.py:399-401` reads just the newest active results page.
A missing URL may have fallen outside that page, so it cannot prove an auction ended or what it sold for.
`src/yahoo_scrape.py:115-124` retains an auction ID while parsing active results, and the stored URL contains that ID, which gives a possible exact lookup key.
RainCheck's existing `items.market_price_usd` is an eBay-based US market estimate.
If a later feature derives a market value from completed Yahoo Auctions Japan sales, it must identify that value explicitly as a Yahoo Japan market-value estimate in its storage and presentation, separate from the eBay-based US estimate.

### Evidence as of 2026-10-27

- Yahoo's public [completed search for Rick Owens](https://auctions.yahoo.co.jp/closedsearch/closedsearch/2084199087) is headed as completed/sold items and includes sold amount, bid count, and displayed end date and time.
  Its `f1247065602` result links to the [same-ID public item page](https://auctions.yahoo.co.jp/jp/auction/f1247065602).
  The captured search row states `落札89,000円`, 42 bids, and `10/10 23:00終了`.
  The public item page says the auction ended, shows `89,000円`, 42 bids, ID `f1247065602`, and start and end datetimes of 2026-10-05 07:00 and 2026-10-10 23:00.
  The reader supplied the search result as crawled five days before this study and the item page as crawled the prior week; neither response exposed an HTTP status.
  This is strong evidence for one observed Yahoo Japan sale price in yen, but the page does not label a timezone or establish a stable parser contract.
- Yahoo's [auction ID guide](https://support.yahoo-net.jp/PccAuctions/s/article/H000005256) says the ID is assigned to an auction and appears in its URL.
  Yahoo's [relisting guide](https://support.yahoo-net.jp/PccAuctions/s/article/H000005195) explicitly says a relisting with no bidders keeps the same auction ID and item URL, while a relisting after a bidder was present changes both.
  This documents the exact-ID collision risk, but no public page observed in this study exposed a stable discriminator for two no-bid cycles sharing the same ID.
- Yahoo's [seller completion guide](https://support.yahoo-net.jp/PccAuctions/s/article/H000005180) says the seller checks the private ended-items area to see whether a winner exists.
  It describes the private labels `落札者あり` and `落札者なし`; the public item pages inspected here did not expose an equivalent unequivocal outcome field.
- A public ended item page, [ID `f1234271025`](https://auctions.yahoo.co.jp/jp/auction/f1234271025), says it has ended and shows zero bids and the displayed amount `318,725円`.
  It does not explicitly state that no buyer exists, and the amount may be a fixed-price amount, so it is not accepted as proof of an ended-without-sale result.
  The reader identified its page snapshot as crawled about four months before this study, and did not expose an HTTP status.
- A public ended item page, [ID `c1143076331`](https://auctions.yahoo.co.jp/jp/auction/c1143076331), has a seller description saying it was not sold and is being relisted.
  The page displays one bid for the visible auction, but the description could refer to an earlier cycle.
  The earlier cycle's ID and outcome cannot be verified, so this is rejected as proof of a no-bid relisting.
- Yahoo's public [active Rick Owens brand results](https://auctions.yahoo.co.jp/brand/3538/) exposed currently active rows with remaining time and bid counts.
  The results page snapshot was crawled the day before this study.
  Following representative item URLs `https://auctions.yahoo.co.jp/jp/auction/b1249690167` and `https://auctions.yahoo.co.jp/jp/auction/q1248425007` returned web-reader cache misses, not HTTP responses, so no exact-ID active item detail response was captured.
  This is partial evidence of active result rows, not an accepted active-page fixture.
- A direct public lookup of [ID `e1231185612`](https://auctions.yahoo.co.jp/jp/auction/e1231185612) returned HTTP 404 through the available reader.
  This is a removed or unavailable-page observation only; it does not prove whether the auction sold, ended unsold, was removed early, or expired.
  Yahoo's [page-retention guide](https://support.yahoo-net.jp/PccAuctions/s/article/H000008859) says pages with a buyer may remain for up to five years and pages without a buyer for 120 days, with deletion possible earlier.
- Sanitized excerpts from these pages and their exact source URLs are in `docs/specs/RC-18-evidence/`.
  They are evidence notes transcribed from the public reader output, not raw HTML or parser contract fixtures.
  A capture date records when the reader output was inspected; where the reader exposed a crawl age, that age is recorded separately.
  No authentication, seller account, buyer account, or credential was used.

Yahoo does not document an API or stable embedded JSON contract for completed item pages in the reviewed sources.
The sold item page uses a current-price label even after the auction ended, so a parser must establish sale outcome independently before treating that amount as `final_price_yen`.
The search result's explicit sold label is useful corroboration, but it does not solve the missing unsold state, exact-cycle identity, timezone, or durable public-field contract.
These observations support further source discovery, not production writes.

## Scope

**In scope:**

- Define an evidence gate for exact auction identity, ended state, actual end time, sold price, and ended-without-sale state.
- Capture sanitized, dated public examples of sold, unsold, still-active, no-bid relisted, removed or expired, and malformed pages without using credentials or the real database.
- Check whether the same auction URL can represent multiple auction cycles, and whether the public page exposes a stable cycle identifier.
- Record a pass or no-go decision with the supporting page URLs, capture dates, visible fields, and limitations before implementation is approved.
- Distinguish an exact auction's observed final yen price from any market-value estimate derived from multiple sales.
- Preserve `ended_at` and `final_price_yen` as `NULL` when the gate fails or a page is unavailable or ambiguous.

**Out of scope:**

- Production fetches, database writes, scheduled completion checks, schema migrations, or email changes in this feasibility item.
- Inferring completion from absence in the active top 100, a scheduled end time, a timeout, an HTTP error, or a removed page.
- Treating the current bid or start price as a final sale price without an independently verified sale outcome.
- Reading a seller's private account, using credentials, scraping buyer identities, or contacting sellers.
- Recovering historical prices from third-party archives or estimating the final price.
- Computing a Yahoo Japan market-value aggregate, blending Yahoo and eBay valuations, replacing `items.market_price_usd`, or presenting an observed auction sale price as a market estimate.

## Design

### Files touched

| File | Change |
| --- | --- |
| `docs/specs/RC-18-auction-completion-feasibility.md` | Record evidence, gate, no-go outcome, and decisions. |
| `docs/specs/RC-18-evidence/` | Store sanitized dated excerpts of inspected public pages and retrieval failures. |

If the gate later passes, write a separate implementation amendment or smaller RC-18b spec before editing code.
Any later Yahoo Japan market-value estimate requires its own explicit spec, with a source label and distinct storage and presentation from the existing eBay-based US market estimate.
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

1. At least one public sold example, one unequivocal public ended-without-sale example, one still-active item detail page, and one no-bid relisted example have inspectable responses and stable exact-ID evidence.
2. The sold example has an unambiguous sale outcome, a positive integer yen sale price, and an actual end time with known timezone; the unsold example has an unambiguous ended-without-buyer signal and an actual end time, with no invented final price.
3. A page with the correct URL but a different or missing page auction ID is rejected.
4. A no-bid relisting exposes a distinct public cycle key that can be stored and compared; otherwise the reused URL cannot support a reliable permanent completion record and the writer is no-go.
5. A deleted, expired, blocked, malformed, or login-only page remains unknown; a retry policy and bounded request budget are specified before production work.
6. Each evidence shape can be captured as a sanitized fixture and checked offline against expected identity, status, time, and price.

**No-go outcome now:** the sold example passes only part of the gate.
The ended zero-bid page is ambiguous, the seller-described relist lacks verifiable earlier-cycle identity, active item detail requests were not inspectable, and no stable public cycle discriminator has been established.
The sold page also omits a timezone label.
Do not implement a writer for `ended_at` or `final_price_yen` from the current evidence.
If further public discovery cannot satisfy the gate, close RC-18 with this limitation documented and leave the nullable columns untouched.

## Acceptance criteria

1. The evidence record cites an exact Yahoo sold-results entry and its linked item page with the same auction ID, its displayed sold amount, and an end time.
2. The evidence record distinguishes Yahoo's documented sold and unsold seller states from what has actually been verified on public pages.
3. The evidence record states that no-bid relisting may reuse an auction ID and URL, so URL equality alone does not establish a unique completed cycle.
4. The gate requires sanitized, inspectable sold, unsold, active, and relisted examples and rejects missing identity, ambiguous outcome, absent actual end time, or an unverified price.
5. The current result is marked no-go because no unequivocal unsold public example, inspectable active item detail, or relisted-cycle discriminator is established; the sold example also omits an explicit timezone.
6. This feasibility item changes documentation and sanitized evidence notes only; it does not change source code, tests, parser fixtures, schema, catalog, or configuration, and it does not access the real database or credentials.
7. In any future implementation, an active result missing from the newest page, a scheduled end in the past, an HTTP 404, and an expired or malformed page leave `ended_at` and `final_price_yen` unchanged.
8. In any future implementation, a verified ended-without-sale record has `final_price_yen = NULL`; a positive final yen value is stored only for a verified sale of the exact auction identity and cycle.
9. The evidence record labels verified prices as individual Yahoo Auctions Japan sale observations in yen and does not label them as a market-value estimate.
10. This feasibility item defines no Yahoo Japan market-value aggregate or combined valuation, and leaves the eBay-based US estimate in `items.market_price_usd` unchanged.

## Edge cases

- Empty results: An empty completed search proves nothing about a stored auction because of filters, pagination, and retention limits.
- `None` or missing values: Missing auction ID, status, end time, or sold amount fails the gate for that page; an unsold outcome has no sale price.
- Network failure or bad HTTP status: Timeout, 403, 404, 429, and 5xx leave the auction state unknown and require no database change.
- Malformed input: A page without the expected item ID or with contradictory active and ended markers is ambiguous and must not be recorded as completed.
- Duplicates: Repeated completed-search rows for one ID are not independent sales; same-ID no-bid relistings may be separate cycles and require an explicit model.
- Later buyer cancellation: Yahoo's displayed buyer status may change after an apparent sale, so a future spec must decide whether stored results are immutable first observations or revisable current outcomes.

## Test plan

No automated tests are added for a documentation-only feasibility result.
For the gate, save sanitized public HTML or the documented response format for one sold, one unequivocal unsold, one active, one no-bid relisted, and one removed or expired listing, with the exact original URLs and capture dates recorded in this spec.
Compare the sold search result's auction ID, price, and end time against the exact item page; inspect unsold and relisted pages for an ID and cycle discriminator.
If the gate passes, the next spec must require offline parser fixtures for every state, a temporary schema-versioned database, exact-ID matching, malformed/duplicate cases, and fake HTTP responses for all network failures.
It must also verify no state change on an unknown page, idempotent repeated observations, transaction rollback, connection closure, and no access to `src/raincheck.db`.

## Shared-file conflicts

This feasibility spec touches none of `src/schema.sql`, `src/db.py`, or `src/catalog.json`, so its documentation work can run beside code work.
Any future RC-18 implementation touching `src/db.py` or `src/schema.sql` cannot run in parallel with RC-13, RC-14, or another feature changing those shared files.

## Open questions

1. Should a later buyer cancellation revise a previously captured sale outcome?
   Recommendation: defer this decision until public examples show what Yahoo exposes and how it changes.
   Tradeoff: an immutable first result is simpler, while a revisable status better reflects a canceled transaction.

### Resolved 2026-10-27

- The user approved the public-page feasibility study and its sold, unsold, active, and relisted evidence gate before any production completion writer.
- If a no-bid relisting reuses an auction ID and URL, RainCheck may track a completed cycle only when Yahoo exposes a stable public cycle key; otherwise completion remains unknown for that URL.
- This approved feasibility scope studies exact sale outcomes and observed yen prices only.
  Computing a Yahoo Japan market-value estimate requires a separate spec and explicit label, distinct from the existing eBay-based US market estimate.

### Amendment 2026-10-27: public evidence outcome

- The public study verified one completed-search row and exact-ID item page for an observed Yahoo Japan sale price of 89,000 yen on auction `f1247065602`.
  The observed amount is an individual sale observation, not a Yahoo Japan market-value estimate.
- The public page for auction `f1234271025` is ended and has zero bids, but it does not unambiguously say no buyer exists.
  Its reader snapshot was about four months old, and it is excluded as proof of an unsold completion.
- Auction `c1143076331` has seller prose stating it was not sold and is being relisted, while its page displays one bid.
  The prose may refer to an earlier cycle, while the bid count describes the visible one.
  Without the earlier cycle's ID or a cycle key, this page cannot prove a no-bid relisting or completion state.
- Yahoo's official no-bid relisting guidance confirms that the auction ID and URL can be reused.
  The study found no public cycle key that can distinguish repeated no-bid cycles.
- Public active result rows were observed, but the available reader could not inspect representative item-detail URLs due cache misses.
  A requested removed-page example returned 404, which remains an unknown outcome.
- The sold public item page and completed search display a date and time without an explicit timezone label.
  The gate therefore remains closed even for writing an end time until timezone semantics are independently verified.
- Final result: no-go for production completion writes.
  Keep `ended_at` and `final_price_yen` unchanged until a future study closes each evidence gap with sanitized public examples.
- Any future Yahoo Japan market-value estimation remains a separate feature and must carry a Yahoo Japan label.
  It must remain distinct from the current eBay-based US estimate.

## Rollback

This item changes documentation only, so reverting the spec commit fully rolls it back.
No database rollback is required.
