# RC-07: Robust Yahoo parsing

**Status:** Approved
**Backlog item:** RC-07 in `docs/BACKLOG.md`

## Summary

Replace the dead `Product__*` HTML scraping in `src/yahoo_scrape.py` with a parser for the `__NEXT_DATA__` JSON that Yahoo Auctions now embeds in every results page.
Each listing becomes one validated record, malformed records are skipped and counted, and layout drift raises a clear error instead of silently finding zero listings.

## Motivation

The live Yahoo job is broken today.
Yahoo Auctions now serves a Next.js page, and the CSS classes used at `src/yahoo_scrape.py:32-34` (`Product__priceValue`, `Product__titleLink`, `Product__imageData`) no longer exist.
The scraper therefore finds zero listings, yet `src/yahoo_scrape.py:85` still sends one empty email per catalog item every run, so the failure looks like "no deals today" instead of "broken".
This was found on 2026-10-26 (RC-01 Amendment 1), and `tests/test_live_fixtures.py::test_c48_live_yahoo_page_has_all_four_selectors` is marked `xfail(strict=True)` for this item.

The original RC-07 problem still applies to the old code and motivates the new design.
`src/yahoo_scrape.py:31-39` collects titles, prices, links, and images as four independent lists and `zip`s them, so one missing element pairs a price with the wrong listing.
Parsing one JSON object per listing removes that class of bug by construction.

Other problems in the same function, fixed here because this item rewrites it:

- `src/yahoo_scrape.py:28` calls `requests.get` with no timeout, which violates the `CLAUDE.md` convention, and never checks the HTTP status, so an error page is parsed as if it were results.
- `src/yahoo_scrape.py:28` downloads the same page once per catalog item instead of once per run (RC-01 "Known issues").
- `src/yahoo_scrape.py:90` requests `.../category/list/23000/?...`, which now answers HTTP 308 to the same path without the trailing slash.
  `requests` follows it, but every run pays an extra round trip.

### What the live page looks like (confirmed on 2026-10-26)

All facts below were checked against `tests/fixtures/yahoo/brand_page_live.html` and, for the redirect, a live request.

- The page contains `<script id="__NEXT_DATA__" type="application/json">` holding one JSON document.
  No listing links are rendered in the HTML itself (the page is client-rendered), so the JSON is the only complete source.
- The listing array is at `props.pageProps.initialState.search.items.listing.items`.
  An identical copy also exists at `props.initialState.search.items.listing.items`.
  The parser uses the `pageProps` path only, because that is where Next.js puts page data by convention.
- The live array has 100 records (the URL asks for `n=100`), all with unique `auctionId` values.
  The parent object also carries `totalResultsAvailable` (2351 at capture time), which is the total across all pages.
- Field types seen across all 100 records:

  | Field | Type | Meaning |
  | --- | --- | --- |
  | `auctionId` | string, always present | Letters and digits, for example `m1249853240`, `z711793722`, or all digits such as `1249572861`. |
  | `title` | string, always present, never blank | Seller-written; mixes English and Japanese. |
  | `price` | integer, always present | Current price in yen, tax-inclusive. For fixed-price listings it equals `buyNowPrice`. For auctions it is the current bid, or the start price if there are no bids. |
  | `buyNowPrice` | integer or `null` (18 of 100 are `null`) | Buy-it-now price in yen, tax-inclusive. |
  | `imageUrl` | string, always present | Thumbnail on `auc-pctr.c.yimg.jp`. |
  | `isFixedPrice` | boolean | 68 of 100 are fixed price. |
  | `isFleamarketItem` | boolean | 30 of 100 are PayPay Flea Market items; exactly these have IDs starting with `z`. |
  | `taxFlag` | integer 0 or 1 | 1 means the seller charges consumption tax; `price` already includes it. |
  | `endTime` | ISO 8601 string with `+09:00` | Not used by this item. |
  | `bidCount` | integer | Not used by this item. |

- Evidence that `price` is tax-inclusive: for `taxFlag: 1` records, `price` is 1.1 times the pre-tax `p=` value in the `etc` string (for example `price` 47300 with `p=43000`, and `price` 16500 with `p=15000`), while for `taxFlag: 0` records the two are equal.
  The two rows already in the user's `raincheck.db` were stored by the old HTML scraper with prices such as 88935, which is exactly 80850 times 1.1, so the old scraper also stored tax-inclusive yen.
  Using `price` keeps `listings.price_yen` consistent with existing rows.
- Yahoo embeds a JSON-LD `ItemList` in the same page with its own URL for every listing, in the same order as the array.
  For all 100 records, that URL is `https://paypayfleamarket.yahoo.co.jp/item/<auctionId>` when `isFleamarketItem` is `true`, and `https://auctions.yahoo.co.jp/jp/auction/<auctionId>` otherwise.
  This is the URL rule the parser uses, and the live-fixture test checks it against the JSON-LD list.
- The existing rows in `raincheck.db` use `https://auctions.yahoo.co.jp/jp/auction/<id>`, the same format the parser produces for auctions, so deduplication on `listings.url` carries over with no migration.
- `GET https://auctions.yahoo.co.jp/category/list/23000/?auccat=...` returns `308` with `location: /category/list/23000?auccat=23000&s1=new&o1=d&brand_id=103538&n=100` (query string kept).
  The URL without the trailing slash returns `200` with `content-type: text/html; charset=utf-8`.

## Scope

**In scope:**

- A `parse_listings(html)` function that turns a results page into a list of validated `YahooListing` records, one per JSON listing object.
- Record-level validation: records missing a required field, or with a field of the wrong type, are skipped and counted.
- Page-level drift detection: a missing, empty, or unparseable `__NEXT_DATA__` block, a missing listing path, or a non-empty array in which every record is invalid raises `YahooPageError`.
- Building each listing URL from `auctionId`, including PayPay Flea Market items.
- A `fetch_yahoo_page(url)` function with a timeout, a status check, and explicit UTF-8 decoding.
- Fetching and parsing the page once per run in `main()`, then evaluating every catalog item against the same parsed list (subject to Open question 4).
- Updating `RICK_OWENS_URL` to the canonical URL without the trailing slash.
- Replacing `tests/fixtures/yahoo/brand_page_minimal.html` with a hand-written `__NEXT_DATA__` page that keeps the same four listings and expected answers.
- Adding `tests/fixtures/yahoo/brand_page_malformed.html` with known-bad records.
- Rewriting the RC-01 Yahoo tests (criteria 14, 45, 46, 47) against the new functions, keeping their behavioral assertions.
- Replacing the strict-xfail RC-01 criterion 48 test with a passing live-fixture test.

**Out of scope:**

- Exclude keywords in the Yahoo scraper, and moving keyword configuration into the database (RC-03).
- The hardcoded yen rate `0.0062` (RC-08).
- One email per item even when there are no deals, and the email layout (RC-15).
  The only body change here is omitting the `Image:` line when a listing has no image (see Interfaces).
- Writing `listing_matches` rows (RC-04).
- Updating `last_seen_at`, detecting ended auctions, or storing `endTime`, `bidCount`, or `buyNowPrice` (RC-05).
  `YahooListing` is the natural place to add those fields later.
- Retries, backoff, and switching existing `print` calls to `logging` (RC-10).
  This item adds only the Yahoo GET timeout and status check, because it rewrites that call.
- A custom `User-Agent` header or any other anti-blocking measure (revisit in RC-10 or RC-11 if Yahoo blocks requests).
- Fetching more than the first 100 results (pagination).
- Multi-brand URLs (RC-13).
- Changing the deal rule (any listing cheaper than the market price is a deal) or the auction-versus-fixed-price semantics (see Open question 1 and the proposed follow-up item).
- Any change to `src/schema.sql`, `src/db.py`, `src/catalog.json`, `src/sendemail.py`, or `scripts/capture_fixtures.py`.
- Re-capturing the live fixture.
  The committed `brand_page_live.html` is used as is.

## Design

### Files touched

| File | Change |
| --- | --- |
| `src/yahoo_scrape.py` | Add `YahooPageError`, `YahooListing`, `fetch_yahoo_page`, `parse_listings`, and module constants; replace `yahoo_jp_scrape(item_dict, brand_url)` with `report_item_deals(item_dict, listings)`; `main()` fetches and parses once; update `RICK_OWENS_URL`; remove the unused `re` import. |
| `tests/fixtures/yahoo/brand_page_minimal.html` | Rewritten as a hand-written `__NEXT_DATA__` page with the same four listings (see "Fixtures"). |
| `tests/fixtures/yahoo/brand_page_malformed.html` (new) | Hand-written `__NEXT_DATA__` page with valid, invalid, and duplicate records with known outcomes. |
| `tests/test_yahoo_parse.py` (new) | Unit tests for `parse_listings` and the page-level errors. |
| `tests/test_yahoo_scrape.py` | Rewrite criteria 14, 45, 46, 47 tests for the new functions; add fetch and pipeline tests. |
| `tests/test_live_fixtures.py` | Replace the strict-xfail `test_c48_...` with the live parsing test (criteria 27-29 below). |

`tests/conftest.py` is reused unchanged.
The fixture names `fixtures_dir`, `seeded_db`, `db_conn`, `fake_http`, `sent_emails`, and `test_catalog` are used exactly as RC-01 defines them.

### Schema changes and migration

None.
`listings` rows are written with the same columns and the same URL format for auctions as before, so an existing `raincheck.db` needs no migration and existing rows still deduplicate.
PayPay Flea Market listings are stored with `https://paypayfleamarket.yahoo.co.jp/item/<id>` URLs; the old scraper never stored any, so there is nothing to reconcile.

### Interfaces

All in `src/yahoo_scrape.py`.

```python
RICK_OWENS_URL = "https://auctions.yahoo.co.jp/category/list/23000?auccat=23000&s1=new&o1=d&brand_id=103538&n=100"
YAHOO_TIMEOUT_SECONDS = 30
LISTING_PATH = ("props", "pageProps", "initialState", "search", "items", "listing", "items")
AUCTION_URL_PREFIX = "https://auctions.yahoo.co.jp/jp/auction/"
FLEAMARKET_URL_PREFIX = "https://paypayfleamarket.yahoo.co.jp/item/"

class YahooPageError(Exception): ...

@dataclass(frozen=True)
class YahooListing:
    auction_id: str
    url: str
    title: str
    price_yen: int
    image_url: str | None

def fetch_yahoo_page(url: str) -> str
def parse_listings(html: str) -> list[YahooListing]
def report_item_deals(item_dict: dict, listings: list[YahooListing]) -> None
def main() -> None
```

`yahoo_jp_scrape` is removed.
Nothing outside `src/yahoo_scrape.py` and its tests calls it (the reference in `src/setup.py:5` is a commented-out import of a module that no longer exists).

#### `YahooPageError`

Raised when the page as a whole cannot be turned into a listing array.
It means "Yahoo changed its layout or returned something that is not a results page", never "there were no results".
The message must say what was missing and must name `__NEXT_DATA__` or the listing path, so a user reading a traceback knows where to look.

#### `fetch_yahoo_page(url)`

- Calls `requests.get(url, timeout=YAHOO_TIMEOUT_SECONDS)` exactly once.
  The call passes no other arguments; redirects are followed by `requests` by default.
- Calls `response.raise_for_status()`, so HTTP 4xx and 5xx raise `requests.HTTPError`.
- Returns `response.content.decode("utf-8", errors="replace")`.
  Decoding explicitly avoids `requests` guessing ISO-8859-1 if Yahoo ever omits the charset, which would garble Japanese titles; `errors="replace"` means a stray invalid byte cannot crash the run.
- Network errors (`requests.Timeout`, `requests.ConnectionError`) propagate unchanged.
  RC-10 adds retries.

#### `parse_listings(html)`

Steps, in order:

1. Parse `html` with `BeautifulSoup(html, "html.parser")` and find `script` with `id="__NEXT_DATA__"`.
   If there is none, or its `.string` is `None` or blank, raise `YahooPageError`.
2. `json.loads` the script text.
   On `json.JSONDecodeError`, raise `YahooPageError` from the decode error (so `__cause__` is set).
3. Walk `LISTING_PATH` one key at a time.
   If any step is not a `dict` or lacks the key, raise `YahooPageError` naming the dotted path.
   If the final value is not a `list`, raise `YahooPageError`.
4. If the list is empty, return `[]`.
   An empty array is a legitimate "no results" page, not drift.
5. Validate each record in array order (rules below).
   Invalid records are skipped.
   A valid record whose `auction_id` was already seen earlier in the array is dropped as a duplicate (first occurrence wins).
6. If the array was non-empty and zero records were valid, raise `YahooPageError` whose message contains `0 of <N>` where `<N>` is the array length.
   Every record failing validation means the record shape drifted, which must be as loud as the path disappearing.
7. If any records were skipped as invalid or dropped as duplicates, log exactly one `WARNING` through `logging.getLogger(__name__)` (logger name `yahoo_scrape`), with the message `Skipped <S> of <N> Yahoo listing records: <I> invalid, <D> duplicate`.
   No warning is logged when nothing was skipped.
8. Return the valid records as `YahooListing` objects in array order.

Record validation rules (a record is valid only if all required rules pass):

| Field | Rule | If it fails |
| --- | --- | --- |
| record itself | Must be a `dict`. | Skip (invalid). |
| `auctionId` | Required. Must be a `str` matching `^[A-Za-z0-9]+$` (non-empty, letters and digits only). | Skip (invalid). The pattern stops a malformed ID from producing a URL with `/`, `?`, or `..` in it. |
| `title` | Required. Must be a `str` that is non-empty after `.strip()`. Stored stripped. | Skip (invalid). |
| `price` | Required. Must be an `int`, not a `bool`, and at least 1. | Skip (invalid). `bool` is excluded explicitly because `True` is an `int` in Python. Floats and numeric strings are rejected rather than converted, because they would mean the field changed meaning. |
| `imageUrl` | Optional. Used when it is a non-empty `str`. | Stored as `None` (the record is still valid). `listings.image_url` is nullable. |
| `isFleamarketItem` | Optional. Only the value `True` counts as a flea market item. | Missing or any other value means a normal auction URL. |

URL rule: `FLEAMARKET_URL_PREFIX + auction_id` when `isFleamarketItem is True`, else `AUCTION_URL_PREFIX + auction_id`.
`price_yen` is the record's `price` (current tax-inclusive price), not `buyNowPrice`; see Open question 1.

#### `report_item_deals(item_dict, listings)`

This is the old `yahoo_jp_scrape` minus fetching and parsing.
Behavior that is kept exactly:

- Looks up the brand ID and the market price the same way (`src/yahoo_scrape.py:15-24`).
  If the market price is `None`, it returns immediately: no database writes, no email.
- A listing matches when any keyword in `item_dict["keywords"]` is a substring of `listing.title.lower()`.
- `price_usd = listing.price_yen * 0.0062`; `pct_cheaper = (market_price - price_usd) / market_price * 100`.
- A listing with `pct_cheaper > 0` is passed to `insert_listing(brand_id, listing.url, listing.title, listing.price_yen, listing.image_url)`; only listings for which it returns `True` are candidates for the email.
- Candidates are sorted by `pct_cheaper` descending, and the top five go into the body with the same lines and formatting as today.
- `send_email(f"Daily Refresh on {item_dict['item name']}", body)` is called exactly once per call, even with zero candidates (RC-15 changes that).
- The same `print` lines are kept.

One change: the `Image: <url>` body line is omitted for a listing whose `image_url` is `None`, instead of printing `Image: None`.

#### `main()`

```python
def main():
    catalog = load_catalog()
    listings = parse_listings(fetch_yahoo_page(RICK_OWENS_URL))
    for item in catalog:
        report_item_deals(item, listings)
```

- Exactly one HTTP GET per run.
- `requests.HTTPError`, network errors, and `YahooPageError` propagate out of `main()` uncaught.
  Because fetching and parsing happen before the loop, a broken page produces no database writes and no emails, and the process exits non-zero with a traceback that names the problem.
  This is deliberate: an unattended run (RC-11) reports failure through the Lambda error metric, which RC-12 alarms on, and that only works if the exception escapes.
  See Open question 2 for the alternative.
- Order matters: the catalog is loaded first so a bad `catalog.json` fails before any network call.

### Why `logging` for one warning when the rest of the module uses `print`

The skip warning is new code, and `logging` lets tests assert on it with pytest's `caplog` without capturing stdout.
With no logging configuration, Python prints `WARNING` and above to stderr, so the user sees it today with no setup.
Existing `print` calls are left alone; RC-10 converts them.

## Acceptance criteria

Test names use the prefix `test_rc07_c<N>_` so they never collide with RC-01's `test_c<N>_` names.
RC-01 tests that are rewritten keep their RC-01 names (criteria 19-22 below say which).

### Parsing: the minimal fixture

1. `parse_listings` on `brand_page_minimal.html` returns exactly four `YahooListing` objects, in fixture order, equal to:
   `("t1000000001", "https://auctions.yahoo.co.jp/jp/auction/t1000000001", "Rick Owens Ramones High Top 42", 50000, "https://auc-pctr.c.yimg.jp/i/test/1.jpg")`, and likewise for rows 2-4 of the fixture table.
2. Listing 1's `price_yen` is `50000` (its `price`), not `90000` (its `buyNowPrice`).
3. No `WARNING` is logged by logger `yahoo_scrape` while parsing the minimal fixture.

### Parsing: record-level problems (malformed fixture)

4. `parse_listings` on `brand_page_malformed.html` returns exactly five listings, with `auction_id` values in this order: `x1000000001`, `z1000000002`, `x1000000003`, `x1000000004`, `2000000018`.
5. The `z1000000002` listing has URL `https://paypayfleamarket.yahoo.co.jp/item/z1000000002`; the other four have `https://auctions.yahoo.co.jp/jp/auction/<id>` URLs.
6. The `x1000000001` listing has title `Rick Owens Ramones Mixed 42` (surrounding whitespace stripped) and `price_yen` `30000` (its first occurrence, not the duplicate's `1`, and not its `buyNowPrice` of `45000`).
7. The `x1000000003` listing (no `imageUrl` key) and the `x1000000004` listing (`imageUrl: null`) both have `image_url is None`.
8. The `x1000000004` listing's title is exactly `Rick Owens </script> テスト`, proving that `\u003c` escapes and Japanese text decode correctly.
9. Exactly one `WARNING` from logger `yahoo_scrape` is logged, and its message is `Skipped 13 of 18 Yahoo listing records: 12 invalid, 1 duplicate`.
10. None of the twelve invalid records (rows 5-16 of the malformed fixture table) appears in the result, checked by `auction_id` where the record has a string one.

### Parsing: page-level problems (built inline in the test)

11. A page with no `__NEXT_DATA__` script (for example the old `Product__*` layout) raises `YahooPageError` whose message contains `__NEXT_DATA__`.
12. An empty string raises `YahooPageError`.
13. A `__NEXT_DATA__` script with empty or whitespace-only content raises `YahooPageError`.
14. A `__NEXT_DATA__` script containing `{"props":` (truncated JSON) raises `YahooPageError` whose `__cause__` is a `json.JSONDecodeError`.
15. Valid JSON with `props.pageProps.initialState.search` but no `items` key raises `YahooPageError` whose message contains `props.pageProps.initialState.search.items.listing.items`.
16. Valid JSON whose top level is a list (`[]`) raises `YahooPageError`.
17. Valid JSON where the listing path holds an object instead of a list raises `YahooPageError`.
18. Valid JSON where the listing path holds `[]` returns `[]`, raises nothing, and logs no warning.
19. Valid JSON where the listing path holds three records that are all invalid (for example each missing `price`) raises `YahooPageError` whose message contains `0 of 3`.

### Pipeline

20. (Rewrites RC-01 criterion 45, test name `test_c45_inserts_only_the_two_matching_cheap_listings` kept.)
    With `seeded_db`, `sent_emails`, and `listings = parse_listings(<minimal fixture>)`, `report_item_deals(<ramones catalog entry>, listings)` inserts exactly two rows into `listings`: the `t1000000001` and `t1000000004` URLs with `price_yen` 50000 and 100000.
21. (Rewrites RC-01 criterion 46, name kept.)
    The same call records one email with subject `Daily Refresh on rick owens ramones` whose body contains the `t1000000001` link before the `t1000000004` link and contains `61.3% cheaper than market` and `22.5% cheaper than market`.
22. (Rewrites RC-01 criterion 47, name kept.)
    Calling `report_item_deals` a second time with the same arguments inserts no new rows.
23. (Rewrites RC-01 criterion 14, name kept, and extends it.)
    With `fake_http` serving the minimal fixture at `RICK_OWENS_URL`, `test_catalog`, `seeded_db`, `sent_emails`, and the working directory set to `tmp_path`, `yahoo_scrape.main()` records the emails `Daily Refresh on rick owens ramones` then `Daily Refresh on pod shorts`, and `fake_http.calls` contains exactly one call: `GET` to `RICK_OWENS_URL` whose kwargs include `timeout` equal to `30`.
24. With `seeded_db`, `sent_emails`, and `listings = parse_listings(<malformed fixture>)`, `report_item_deals(<ramones catalog entry>, listings)` inserts exactly three rows, including `https://paypayfleamarket.yahoo.co.jp/item/z1000000002` with `price_yen` 40000 and `image_url` `NULL` for `x1000000003`.
    The email body lists the `x1000000003` URL, then `x1000000001`, then `z1000000002` (discounts 84.5%, 76.75%, 69%), and contains `Image: ` exactly twice.
25. With `seeded_db`, `sent_emails`, and the Ramones item's `market_price_usd` set to `NULL` via `db_conn`, `report_item_deals(<ramones entry>, <minimal listings>)` inserts no rows and records no email.

### Fetching and failure behavior

26. `yahoo_scrape.RICK_OWENS_URL == "https://auctions.yahoo.co.jp/category/list/23000?auccat=23000&s1=new&o1=d&brand_id=103538&n=100"`.
27. With `fake_http` serving `text="<p>ok</p>"` at some URL, `fetch_yahoo_page(url)` returns `"<p>ok</p>"`, and the single recorded call's kwargs include `timeout` equal to `30`.
    (Tests check only `timeout`, so RC-10 can add headers or other arguments without breaking them.)
28. When `fake_http` returns status `503` for `RICK_OWENS_URL`, `yahoo_scrape.main()` (with `test_catalog`, `seeded_db`, `sent_emails`) raises `requests.HTTPError`, records no email, and leaves `listings` empty.
29. When `fake_http` serves the old-layout page (any HTML without `__NEXT_DATA__`) for `RICK_OWENS_URL`, `yahoo_scrape.main()` raises `YahooPageError`, records no email, and leaves `listings` empty.
    This is the regression test for today's production bug.

### Live fixture

30. (Replaces RC-01 criterion 48; the `xfail` marker and the `Product__*` selector test are removed.)
    `parse_listings` on `brand_page_live.html` returns as many listings as the raw array at `LISTING_PATH` has records (100 in the committed fixture), with unique `auction_id` values, and logs no warning.
31. For the live fixture, the list of parsed `url` values equals, in order, the `url` values of the JSON-LD `ItemList` in the same page (`<script type="application/ld+json">` whose `@type` is `ItemList`).
    This checks the URL rule, including PayPay Flea Market items, against Yahoo's own links.
32. For the live fixture, every listing has a non-empty `title`, an `int` `price_yen` of at least 1, and an `image_url` that is `None` or starts with `https://`.
33. RC-01 criteria 49, 50, and 53 still pass unchanged.

### Whole suite

34. `python3 -m pytest` passes with no `xfail` or `xpass` entries, and with no network access.
    (Checked by the reviewer and verifier.)
35. `src/yahoo_scrape.py` no longer contains the strings `Product__` or `zip(`, and does not import `re`.
    (Checked by the reviewer.)

## Edge cases

- **Empty results:** an empty listing array returns `[]` with no error (criterion 18); `main()` then sends today's empty email per item, which RC-15 changes.
  A non-empty array with zero valid records is treated as drift and raises (criterion 19).
- **`None` or missing values:** `price: null`, missing `title`, missing `auctionId`, and similar records are skipped and counted (criteria 9, 10).
  Missing or `null` `imageUrl` keeps the record with `image_url = None` (criterion 7), and the email omits its image line (criterion 24).
  A `NULL` market price skips the item entirely (criterion 25).
- **Network failure or bad HTTP status:** the GET has a 30-second timeout; HTTP 4xx or 5xx raises `requests.HTTPError` out of `main()` before any database write or email (criterion 28).
  Timeouts and connection errors propagate the same way; retries are RC-10.
- **Malformed input:** missing or empty `__NEXT_DATA__`, invalid JSON, a missing path, a non-list at the path, or a non-dict top level all raise `YahooPageError` (criteria 11-17).
  Wrong types inside a record skip that record (criterion 10).
  A title containing `</script>` is safe because it arrives JSON-escaped as `\u003c/script>` (criterion 8).
  `auctionId` values with characters other than letters and digits are rejected so they cannot produce a misleading URL.
- **Duplicates:** a repeated `auctionId` within one page keeps the first occurrence and counts the rest in the warning (criteria 6, 9).
  Across runs, `insert_listing`'s `INSERT OR IGNORE` on the unique URL still deduplicates (criterion 22), including against rows stored by the old scraper, which used the same auction URL format.
- **Encoding:** the response body is decoded as UTF-8 regardless of headers, so Japanese titles survive.

## Test plan

### Fixtures

Both hand-written fixtures are small HTML pages shaped like the live one:

```html
<!DOCTYPE html>
<html lang="ja">
<head><meta charset="utf-8"><title>Test brand page</title></head>
<body>
<div id="__next"></div>
<script id="__NEXT_DATA__" type="application/json">{ ...JSON... }</script>
</body>
</html>
```

The JSON is `{"props": {"pageProps": {"initialState": {"search": {"items": {"listing": {"items": [ ...records... ], "totalResultsAvailable": <n>}}}}}}, "page": "/category/list/[categoryId]"}`.
Every `<` inside the JSON must be written as `\u003c`, as Next.js and the capture script do.
Each valid record carries the fields the live page carries for these concepts, with the same types: `auctionId`, `title`, `price`, `buyNowPrice`, `bidCount`, `isFixedPrice`, `isFleamarketItem`, `imageUrl`, and `endTime` (`"2026-11-01T21:00:00+09:00"`), unless the table says a field is missing or different.

**`brand_page_minimal.html`** (replaces the old `Product__*` version; same four listings and expected answers as RC-01):

| # | `auctionId` | `title` | `price` | `buyNowPrice` | `isFixedPrice` | `bidCount` | `imageUrl` | Expected at market 800 USD |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `t1000000001` | `Rick Owens Ramones High Top 42` | 50000 | 90000 | false | 3 | `https://auc-pctr.c.yimg.jp/i/test/1.jpg` | 310.00 USD, 61.25% cheaper, deal |
| 2 | `t1000000002` | `Rick Owens Ramones Leather 44` | 200000 | 200000 | true | 0 | `.../test/2.jpg` | 1240.00 USD, not a deal |
| 3 | `t1000000003` | `Rick Owens Geobasket 43` | 10000 | null | false | 0 | `.../test/3.jpg` | no keyword match, skipped |
| 4 | `t1000000004` | `Rick Owens Ramon Sneakers` | 100000 | 100000 | true | 0 | `.../test/4.jpg` | 620.00 USD, 22.5% cheaper, deal |

All four have `isFleamarketItem: false`.
Row 1's `buyNowPrice` of 90000 would give 30.25%, not 61.25%, so criterion 21 also pins the choice of `price`.

**`brand_page_malformed.html`** (18 records, in this order):

| # | Record | Expected |
| --- | --- | --- |
| 1 | `x1000000001`, title `"  Rick Owens Ramones Mixed 42  "`, `price` 30000, `buyNowPrice` 45000, auction, image `.../test/m1.jpg` | Kept; title stripped; `price_yen` 30000 |
| 2 | `z1000000002`, `Rick Owens Ramones フリマ 43`, `price` 40000, `buyNowPrice` 40000, `isFixedPrice` true, `isFleamarketItem` true, image `.../test/m2.jpg` | Kept; flea market URL |
| 3 | `x1000000003`, `Rick Owens Ramones No Image`, `price` 20000, no `imageUrl` key | Kept; `image_url` `None` |
| 4 | `x1000000004`, title `Rick Owens \u003c/script> テスト` in the JSON, `price` 15000, `imageUrl: null` | Kept; `image_url` `None`; title decodes to `Rick Owens </script> テスト` |
| 5 | no `auctionId` key | Invalid |
| 6 | `auctionId: ""` | Invalid |
| 7 | `auctionId: "../evil"` | Invalid |
| 8 | `x1000000008`, no `title` key | Invalid |
| 9 | `x1000000009`, `title: "   "` | Invalid |
| 10 | `x1000000010`, `price: "50000"` (string) | Invalid |
| 11 | `x1000000011`, `price: null` | Invalid |
| 12 | `x1000000012`, `price: true` | Invalid |
| 13 | `x1000000013`, `price: 0` | Invalid |
| 14 | `x1000000014`, `price: 1500.5` | Invalid |
| 15 | the string `"not a listing"` | Invalid |
| 16 | `null` | Invalid |
| 17 | `x1000000001` again, title `Rick Owens Ramones Mixed 42`, `price` 1 | Duplicate, dropped |
| 18 | `2000000018` (digits only), `Rick Owens Geobasket 18`, `price` 25000 | Kept; auction URL |

Invalid rows 6-14 are otherwise complete, valid-looking records, so each one fails for exactly the stated reason.
Totals: 18 records, 5 kept, 12 invalid, 1 duplicate.
For the pipeline test (criterion 24) with the Ramones keywords, rows 1, 2, and 3 match; row 4 and row 18 do not.
Expected discounts at 800 USD: row 3 is 124.00 USD (84.5%), row 1 is 186.00 USD (76.75%), row 2 is 248.00 USD (69.0%).

**Page-level cases (criteria 11-19)** are built inline in `tests/test_yahoo_parse.py` with a small helper that wraps a raw string or a `json.dumps` result in the same HTML shell.
Inline strings are clearer than nine near-empty fixture files, because each test shows its whole input.

**`brand_page_live.html`** is used as committed; it is not re-captured in this item.

### Test files

| File | Covers | Fixtures used |
| --- | --- | --- |
| `tests/test_yahoo_parse.py` (new) | Criteria 1-19 | `fixtures_dir`, `caplog`, minimal and malformed fixtures, inline pages |
| `tests/test_yahoo_scrape.py` (rewritten) | Criteria 20-29 | `fake_http`, `seeded_db`, `test_catalog`, `sent_emails`, `db_conn`, `monkeypatch`, `tmp_path` |
| `tests/test_live_fixtures.py` (edited) | Criteria 30-33 | `fixtures_dir`, `caplog`, `brand_page_live.html` |

Tests that need the parsed minimal listings should call `parse_listings` on the fixture text instead of constructing `YahooListing` objects by hand, so pipeline tests also exercise the real parser.
Criteria 34 and 35 are checked by the reviewer or verifier.

## Shared-file conflicts

No.
This item does not touch `src/schema.sql`, `src/db.py`, or `src/catalog.json`.

It does rewrite most of `src/yahoo_scrape.py`, which RC-03, RC-04, RC-05, RC-08, RC-10, RC-13, and RC-15 also touch.
None of those should run in parallel with RC-07, and RC-07 should land first because the live job is broken until it does.
It also edits `tests/test_yahoo_scrape.py` and `tests/test_live_fixtures.py`, which any of those items may extend.

## Supersedes in RC-01

- RC-01 criteria 14, 45, 46, and 47 are rewritten here (criteria 20-23) with the same behavioral assertions.
- RC-01 criterion 48 and its strict `xfail` are replaced by criteria 30-32.
- RC-01 "Known issues" rows fixed here: the zip-based pairing, the unchecked Yahoo status, the Yahoo GET timeout (the eBay and SMTP timeouts remain RC-10), and the once-per-item download (if Open question 4 is answered as recommended).

## Open questions

**Resolved 2026-10-26:** the user approved the spec as one PR and chose the recommended option for every question below.
The comparable price is `price` (current, tax-inclusive yen), a parse failure raises `YahooPageError` out of `main()` before any database write or email, PayPay Flea Market listings are included, and the page is fetched once per run.

1. **Which price is the comparable price?**
   - Option A (recommended): `price`, the current tax-inclusive price.
     For fixed-price listings, including flea market items, this is the price you would pay.
     For auctions it is the current bid, or the start price with no bids.
     It matches what the old scraper stored, so existing rows and future rows mean the same thing.
     Tradeoff: an auction's current bid understates what it will sell for, so a cheap-start auction (live examples start at 1 yen) looks like a huge deal.
     That problem already existed with the old scraper and is not made worse here.
   - Option B: for auctions that have a `buyNowPrice`, use it; otherwise use `price`.
     Tradeoff: the buy-now price is a guaranteed price, but only some auctions have one, so the field would mean different things on different rows, and auctions without buy-now still understate.
   - Recommendation: Option A in this item, plus a new backlog item, "Auction-aware deals", to decide how auctions are treated (for example, compare buy-now prices only, exclude auctions with low bid counts, or label "current bid" in the email).
     That item would store `isFixedPrice`, `buyNowPrice`, and `endTime`, which pairs naturally with RC-05.
2. **What happens when the page cannot be parsed?**
   - Option A (recommended): raise `YahooPageError` out of `main()` before any database write or email, so the run fails with a clear traceback and a non-zero exit code.
     Once RC-11 and RC-12 land, this becomes a Lambda error and a CloudWatch alarm with no extra code.
     Tradeoff: while the job runs by hand, the user only notices if they read the terminal; there is no email.
   - Option B: catch `YahooPageError` in `main()`, send one "RainCheck could not read Yahoo" email, then exit non-zero.
     Tradeoff: the user hears about drift by email even today, but this adds a second notification path that RC-12 would duplicate, and a broken SMTP setup would hide the original error.
   - Option C: log and continue with zero listings, as today.
     Not recommended; it is the silent failure that caused this item.
3. **Include PayPay Flea Market listings?**
   They are 30 of the 100 live results, all fixed price, and Yahoo shows them in the same results page.
   - Option A (recommended): include them, with the `paypayfleamarket.yahoo.co.jp/item/<id>` URL Yahoo itself uses.
     Tradeoff: the email may link to a second Yahoo site, and buying from it can work differently (for example, proxy services may treat it differently).
   - Option B: skip records with `isFleamarketItem: true`.
     Tradeoff: fewer, possibly cheaper, fixed-price deals.
4. **Fetch the page once per run instead of once per item?**
   - Option A (recommended): once per run, as in the `main()` design above.
     Every item sees the same snapshot, the run makes one request instead of one per item (kinder to Yahoo and less likely to be rate-limited), and a fetch or parse failure stops the run before any item sends an email.
     It also generalizes to RC-13 as "once per brand".
     Tradeoff: the per-item function no longer fetches, so its signature changes (`report_item_deals(item_dict, listings)` replaces `yahoo_jp_scrape(item_dict, brand_url)`).
   - Option B: keep one fetch per item.
     Tradeoff: no behavior change for a two-item catalog, but N requests per run, items may see different snapshots, and a failure partway through leaves some items emailed and others not.
     If chosen, `main()` moves the fetch and parse inside the loop; every other part of this spec is unchanged, and criterion 23 expects one GET per catalog item instead of one.
5. **Is one pull request right?**
   Recommended: yes.
   The source change is one module of about 150 lines; the rest is tests and two small fixtures, and the pieces only make sense together because the live job is broken until all of them land.

## Rollback

Revert the merge commit.
No schema or data migration is involved.
Rows written after the change use the same columns and, for auctions, the same URL format as before; flea market rows (`paypayfleamarket.yahoo.co.jp` URLs) are harmless to the old code.
Note that reverting restores the old scraper, which finds zero listings on the current Yahoo page, so a rollback restores the broken state rather than a working one.
