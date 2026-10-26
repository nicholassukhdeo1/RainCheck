# RC-03b: Both jobs read item configuration from the database

**Status:** Approved
**Backlog item:** RC-03b in `docs/BACKLOG.md`

## Summary

Switch `src/yahoo_scrape.py` and `src/ebay_pricing.py` from `catalog.json` to `db.get_tracked_items()`, so the database is the only source of item configuration at run time.
Both jobs share one keyword matcher in a new `src/matching.py`, the Yahoo job applies exclude keywords and the effective price, eBay is queried with `search_query`, one unusable eBay median no longer stops the pricing run, and an outdated database is reported as a one-line `Error:` before any network call.

## Motivation

RC-03a made the database a complete, validated copy of the configuration, but nothing in production reads it yet (`src/db.py:556-559` says so in its docstring).
Both jobs still read `catalog.json` directly, so the database and the jobs can disagree.

- `src/yahoo_scrape.py:210` and `src/ebay_pricing.py:74` iterate `load_catalog()`.
  An item deactivated by seeding is still priced and emailed if it is in the file, and an unvalidated file edit (for example an uppercase keyword) reaches the jobs without passing `validate_catalog`.
- The Yahoo job ignores exclude keywords (`src/yahoo_scrape.py:165`), so a `DRKSHDW` or `canvas` Ramones listing can be emailed as a mainline Ramones deal, while the eBay job excludes the same listings when computing the market price (`src/ebay_pricing.py:52`).
  The two halves of the deal comparison use different item definitions.
- Keyword matching is written twice, slightly differently (`src/yahoo_scrape.py:159-165`, `src/ebay_pricing.py:50-55`).
- eBay is queried with the item name (`src/ebay_pricing.py:75`), not with `search_query`, which RC-03a validated and stored for exactly this purpose.
  For `pod shorts` that means the query `pod shorts`, which returns other brands' shorts.
- `src/ebay_pricing.py:75` raises `KeyError` on a catalog entry without `exclude_keywords`, which seeding now accepts (README "Usage" still warns about it).
- `report_item_deals` looks the brand up by name and crashes with `TypeError` when the brand row is missing (`src/yahoo_scrape.py:146-150`), and it opens a second connection to read the price (`src/yahoo_scrape.py:153`) that `TrackedItem.effective_price_usd` already carries.
- Since RC-03a, `update_market_price` raises `ValueError` for a price of 0 or below (`src/db.py:618-624`), and `refresh_market_prices` does not handle it (`src/ebay_pricing.py:76-81`), so one bad median would stop the whole run (RC-03a Amendment 1).
- `src/ebay_pricing.py:72` requests an eBay token before it first opens the database, and `src/yahoo_scrape.py:213` downloads the Yahoo page before its first database call, so an outdated database is only discovered after a network round trip.
  It then surfaces as a Python traceback ending in `SchemaVersionError` (RC-03a verification, criterion 47), instead of the clean `Error:` line that `db.main` prints for the same error.

## Scope

**In scope:**

- New `src/matching.py` with `title_matches(title, keywords, exclude_keywords)`, used by both jobs and nowhere duplicated.
- `yahoo_scrape.main()` and `ebay_pricing.refresh_market_prices()` iterate `db.get_tracked_items()` (active items only) and never call `load_catalog()`.
- `report_item_deals(item, listings)` takes a `TrackedItem`: `brand_id` comes from the item, exclude keywords apply, the deal math uses `item.effective_price_usd`, and an item without a usable price is skipped with a printed line.
- eBay queries use `item.search_query`, falling back to `item.name` when it is `NULL`.
- `refresh_market_prices` skips, with a printed line, an item whose median is `None` or rejected by `update_market_price`, and continues with the next item.
- Both jobs read the database (which checks the schema version) before any network call, and their `main()` prints a `SchemaVersionError` as one `Error: <message>` line on stderr and exits with code 1.
- When there are no active items, both jobs print one line and return without any network call.
- A 30-second timeout on both eBay requests (Open question 4).
- A one-line docstring fix in `src/db.py` for `get_tracked_items` (Open question 5).
- README "Usage" and "Project structure" updates.
- Tests for all of the above, and updates to the tests whose call signature changes (see "Superseded tests").

**Out of scope:**

- Word-aware matching, NFKC normalization, and full-width Latin letters (RC-17).
  Matching stays plain substring matching, so `low` still matches `yellow`.
- Any change to `src/schema.sql`, to `src/catalog.json` beyond the two keywords in the Resolved note, or to `src/db.py` beyond the docstring and Amendment 1.
- Recording which item a listing matched (RC-04) or updating `last_seen_at` (RC-05).
- Retries, backoff, per-item isolation of network errors, `logging` instead of `print`, and clean messages for errors other than `SchemaVersionError` (RC-10; see Open questions 2 and 3).
- The debug listing of every eBay result that `ebay_market_price` prints (`src/ebay_pricing.py:63-64`) (RC-10).
- Minimum sample size, outlier trimming, clearing a stale eBay price, and eBay summaries without `price` or `title` (RC-09; see "Known issues").
- The hardcoded yen rate (RC-08), empty per-item emails and the email layout (RC-15), multiple brands (RC-13), and `src/setup.py` (RC-14).
- Changing other keywords in `src/catalog.json` (the two keyword additions in the Resolved note are in scope).

## Design

### Files touched

| File | Change |
| --- | --- |
| `src/matching.py` (new) | `title_matches`. No imports from other RainCheck modules, no I/O. |
| `src/yahoo_scrape.py` | `report_item_deals(item, listings)` takes a `TrackedItem`; `main()` reads `get_tracked_items()` first and handles `SchemaVersionError`; imports `title_matches`; drops the `get_connection`, `get_market_price`, and `load_catalog` imports and the brand query. |
| `src/ebay_pricing.py` | `refresh_market_prices()` iterates `get_tracked_items()` before requesting a token, uses `search_query`, skips unusable medians; `ebay_market_price` uses `title_matches`; `main()` handles `SchemaVersionError`; `EBAY_TIMEOUT_SECONDS = 30` on both requests; drops the `get_item_id` and `load_catalog` imports. |
| `src/catalog.json` | `rick owens ramones`: add include keyword `ラモーンズ` and exclude keyword `ローカット` (the user's data decision, see Resolved note). |
| `src/db.py` | Docstring of `get_tracked_items` only: remove "Nothing in production calls this yet, the jobs still read catalog.json." No code change. |
| `README.md` | "Usage" and "Project structure" (criteria 36-37). |
| `tests/test_matching.py` (new) | Criteria 1-7. |
| `tests/test_yahoo_scrape.py` | Criteria 8-21; superseded updates. |
| `tests/test_ebay_pricing.py` | Criteria 22-35. |

Not modified: `src/schema.sql`, `src/setup.py`, `tests/conftest.py`, `tests/dbhelpers.py`, and every fixture file (`src/catalog.json`, `src/sendemail.py`, and `pyproject.toml` change per the Resolved note and Amendment 1).

### Schema changes and migration

None.
No table, column, or `PRAGMA user_version` changes; `SCHEMA_VERSION` stays 1.

The user's real `src/raincheck.db` was upgraded to schema version 1 and seeded in RC-03a Phase C, so no migration step is needed after this merges.
Because the jobs now read only the database, any `catalog.json` edit made since that seed has no effect until `python3 src/db.py --seed` runs; the post-merge note under "Rollback" covers this.

### Interfaces

#### `src/matching.py`

```python
def title_matches(
    title: str,
    keywords: Iterable[str],
    exclude_keywords: Iterable[str],
) -> bool
```

- Lowercases `title` with `str.lower()` and nothing else (no `strip`, no NFKC).
- Returns `False` if any exclude keyword is a substring of the lowercased title (exclude wins).
- Otherwise returns `True` if any include keyword is a substring of the lowercased title, else `False`.
- Keywords are used exactly as given; they are **not** lowercased.
  Precondition: callers pass keywords that are already normalized (stripped, lowercased, non-blank), which is what `get_tracked_items()` returns since RC-03a (`validate_catalog` normalizes at write time, `src/db.py:325-342`).
  An uppercase keyword therefore never matches (pinned by criterion 5, so a raw SQL insert with capitals is visibly wrong rather than silently tolerated).
- Empty `keywords` returns `False` for every title; empty `exclude_keywords` excludes nothing.
- Accepts tuples or lists; both are iterated at most once per call.
- RC-17 replaces the body of this one function; every caller benefits without changes.

This is exactly today's eBay logic (`src/ebay_pricing.py:50-55`), and today's Yahoo logic plus excludes.

#### `src/yahoo_scrape.py`

```python
def report_item_deals(item: TrackedItem, listings: list[YahooListing]) -> None
def main() -> None
```

`report_item_deals(item, listings)`:

1. `market_price = item.effective_price_usd`.
   If it is `None`, print `Skipping <item.name>: no usable market price.` and return: no database access, no email.
2. For each listing, skip it unless `title_matches(listing.title, item.keywords, item.exclude_keywords)`.
3. The deal math, `insert_listing(item.brand_id, ...)`, sorting, top five, body format, and `send_email(f"Daily Refresh on {item.name}", ...)` are unchanged from RC-07.

No database read happens in this function any more; the only database call is `insert_listing`.
`item.brand` is not used, so the brand lookup and its `TypeError` disappear.

`main()`:

```python
def main():
    try:
        # reading the items first opens the database, which checks the schema version,
        # so an outdated database fails before any network call
        items = get_tracked_items()
        if not items:
            print("No active items in the database. Add items to catalog.json, then run python3 src/db.py --seed.")
            return
        listings = parse_listings(fetch_yahoo_page(RICK_OWENS_URL))  # once per run, as in RC-07
        for item in items:
            report_item_deals(item, listings)
    except SchemaVersionError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise SystemExit(1)
```

The snippet fixes behavior and order, not wording of comments.
`requests.HTTPError`, `requests.Timeout`, and `YahooPageError` still propagate with a traceback and a non-zero exit, as RC-07 decided.

#### `src/ebay_pricing.py`

```python
EBAY_TIMEOUT_SECONDS = 30

def get_ebay_token() -> str                                   # adds timeout only
def ebay_market_price(query, keywords, exclude_keywords, token) -> float | None   # signature unchanged
def refresh_market_prices() -> None
def main() -> None
```

- `get_ebay_token` and `ebay_market_price` pass `timeout=EBAY_TIMEOUT_SECONDS` to `requests.post` and `requests.get` (Open question 4).
  The request URL, headers, `data`, and `params` are unchanged, so `params` stays `{"q": query, "limit": 50}`.
- `ebay_market_price` keeps a listing's price when `title_matches(item["title"], keywords, exclude_keywords)`; everything else is unchanged.

`refresh_market_prices()`:

1. `items = get_tracked_items()`, before the token request, so an outdated database fails before any network call.
2. If `items` is empty, print `No active items in the database. Add items to catalog.json, then run python3 src/db.py --seed.` and return without requesting a token.
3. `token = get_ebay_token()`, once per run.
4. For each item, in `items.id` order:
   1. `query = item.search_query or item.name`.
   2. Print `Pricing <item.name> with eBay query "<query>"`.
   3. `price = ebay_market_price(query, item.keywords, item.exclude_keywords, token)`.
   4. If `price is None`, print `No matching eBay listings for <item.name>; market price not updated.` and continue.
   5. Call `update_market_price(item.id, price)`.
      If it raises `ValueError` (a median of 0 or below, or not finite), print `eBay median for <item.name> is not a usable price (<price>); market price not updated.` and continue.
      Catching the documented `ValueError` keeps `update_market_price` the single owner of the "usable price" rule instead of copying it here.
      The `try` wraps only this call.
   6. If it returns `False` (no row with that id, which cannot happen while items are never deleted), print `Item <item.name> no longer exists; market price not stored.` and continue.
5. An HTTP error, timeout, or connection error from any eBay request propagates unchanged and stops the run, as today (Open question 3).
   Prices already stored for earlier items stay stored, because each `update_market_price` commits on its own.

`main()` calls `refresh_market_prices()` inside `try: ... except SchemaVersionError` with the same `Error:` line on stderr and `SystemExit(1)` as the Yahoo job.
`KeyError` for missing eBay credentials still propagates with a traceback (Open question 2).

#### Which errors get the clean `Error:` line

Only `db.SchemaVersionError`, wherever it is raised inside `main()`.
It is the one error whose message is written for the user and names the command that fixes it, and RC-03a already prints it this way in `db.main`.
`CatalogError` can no longer reach the jobs, because they no longer read `catalog.json`.
Everything else keeps today's behavior until RC-10 (Open question 2).

### User-visible behavior changes

Measured on the committed live fixtures with the real `src/catalog.json` keywords (the same values the user's database holds after seeding).

**Yahoo** (`tests/fixtures/yahoo/brand_page_live.html`, 100 listings):

| Item | Matching listings today (includes only) | After RC-03b (includes and excludes) | Removed by excludes |
| --- | --- | --- | --- |
| `rick owens ramones` | 1 | 1 | 0 |
| `pod shorts` | 0 | 0 | 0 |

- The one Ramones match (`RAMONES ラモーンズレザーハイカットスニーカー`, 112970 yen) contains no exclude keyword.
- The page has 24 listings containing an exclude keyword (`drkshdw`, `canvas`, `low`, or `pants`), but none of them also contains a Ramones or pod shorts include keyword, so on this snapshot the excludes change nothing.
  On other days they will: a title like `Rick Owens DRKSHDW Ramones ...` was emailed as a Ramones deal before and is dropped now.
- `low` as a substring: exactly one title contains `low` (`DRKSHDW / Mega Bumper Low`), as a whole word, and it matches no include keyword; no `yellow`-style false exclusion occurs on this page.
- Deal prices are unchanged: `effective_price_usd` applies the same rule that `get_market_price` has applied since RC-03a.

**eBay** (`tests/fixtures/ebay/search_live.json`, 50 summaries, captured with `q=rick owens ramones` only):

| Item | Query today | Query after | Include matches | After excludes | Median today | Median after |
| --- | --- | --- | --- | --- | --- | --- |
| `rick owens ramones` | `rick owens ramones` | `rick owens ramones` (its `search_query`) | 25 | 9 | 600.0 | 600.0 |
| `pod shorts` | `pod shorts` | `rick owens pod shorts` | not measurable | not measurable | not measurable | not measurable |

- Ramones: the query is identical (its `search_query` equals its name) and eBay already applied excludes, so the median is unchanged.
  The 16 excluded summaries contain `drkshdw` (14), `canvas` (6), or `low` (4), with overlap; none is a `yellow`-style false hit.
  Three other summaries contain `lows` (`jumbo lace lows`) and are excluded by substring, but they contain no Ramones keyword, so they do not affect the median (relevant for RC-17, where word matching would stop excluding `lows`).
- Pod shorts: the query changes to `rick owens pod shorts`, which should return only Rick Owens items.
  No fixture was captured with either pod shorts query, so the effect cannot be measured offline; the 50 Ramones summaries contain no `pod short` or `ポッドショーツ` title, so against that fixture the item is skipped (median `None`) and keeps its manual price of 300.0.
  The first real run after merge shows the real result in the new `Pricing ... with eBay query ...` line.

**Other changes the user will notice:**

- Editing `catalog.json` no longer affects the next run by itself; the edit reaches the jobs only after `python3 src/db.py --seed`.
- An item removed from `catalog.json` and deactivated by seeding is no longer priced or emailed.
- Per-item email order is `items.id` order; for the user's database (ids 1 and 2) that is the same order as the catalog.
- New printed lines: `Pricing ... with eBay query ...`, the eBay skip lines, and `Skipping <name>: no usable market price.` (previously silent).
- An outdated or missing database gives one `Error:` line and exit code 1 instead of a traceback, with no network request.

### Observations for the user (not changed here)

- The live Yahoo page has two Ramones listings titled only in Japanese, `ラモーンズ` (`Rick Owens リックオウエンス ラモーンズ ローカット スニーカー`, 99999 yen, and `Rick Owens DRKSHDW ラモーンズ 26SS`, 100000 yen).
  No current keyword matches them.
  Adding `ラモーンズ` to the Ramones keywords in `catalog.json` would catch the first; the second would then be dropped by the `drkshdw` exclude, which shows why excludes matter on Yahoo.
  `ローカット` (low cut) is not caught by the `low` exclude, because it is written in katakana.
- One listing is titled `Rick Owens PODS ... ポッズ`; `pod short` does not match it.
- These are catalog data choices; the user chose to make them in this item (see the Resolved note).

## Acceptance criteria

Criteria 1-35 are checked by pytest, 36-38 by the reviewer, and 39-43 by the verifier.

**Shared matcher** (`tests/test_matching.py`)

1. `title_matches("Rick Owens RAMONES Leather", ("ramones",), ())` is `True` (the title is lowercased).
2. `title_matches("Rick Owens Geobasket", ("ramones", "ramon"), ())` is `False`; `title_matches("Rick Owens Ramon High", ("ramones", "ramon"), ())` is `True` (any one include keyword is enough, and `ramon` matches as a substring).
3. `title_matches("Rick Owens DRKSHDW Ramones", ("ramones",), ("drkshdw",))` is `False` (exclude wins over include, and the exclude matches case-insensitively through the title).
4. Substring semantics are pinned until RC-17: `title_matches("Ramones Yellow", ("ramones",), ("low",))` is `False`, and `title_matches("リックオウエンス ポッドショーツ 46", ("pod short", "ポッドショーツ"), ("pants",))` is `True`.
5. Keywords are not normalized: `title_matches("rick owens ramones", ("RAMONES",), ())` is `False`.
6. `title_matches("anything", (), ())` is `False`, and `title_matches("Ramones", ["ramones"], [])` is `True` (lists are accepted like tuples).
7. For every summary in `tests/fixtures/ebay/search_live.json`, `title_matches(title, ("ramones", "ramon"), ("drkshdw", "canvas", "low"))` equals the pre-RC-03b inline rule `not any(x in title.lower() for x in excludes) and any(k in title.lower() for k in keywords)`, and exactly 9 summaries match.

**Yahoo job** (`tests/test_yahoo_scrape.py`)

8. Criteria 20-22 and 24 of RC-07 (tests `test_c45_...`, `test_c46_...`, `test_c47_...`, `test_rc07_c24_...`) hold unchanged when `report_item_deals` is given the `rick owens ramones` `TrackedItem` from `db.get_tracked_items()` on `seeded_db`.
9. With `dataclasses.replace(<ramones item>, exclude_keywords=("high",))`, `report_item_deals` on the minimal fixture stores only `URL_4` and sends one email whose body contains `URL_4` and not `URL_1`.
10. With `dataclasses.replace(<ramones item>, brand="Not In Database")`, `report_item_deals` on the minimal fixture stores the same two rows as criterion 8, each with `brand_id` equal to the item's `brand_id` (no brand lookup by name, no `TypeError`).
11. After `db.update_market_price(<ramones id>, 400.0)`, the item re-read from `get_tracked_items()` (effective price 400.0) makes `report_item_deals` on the minimal fixture store only `URL_1` and send a body containing `22.5% cheaper than market`.
12. With both price columns of `rick owens ramones` set to `NULL` and the item read from `get_tracked_items()` after that update, `report_item_deals` stores no rows, sends no email, and prints `Skipping rick owens ramones: no usable market price.` (supersedes `test_rc07_c25_...`).
13. `main()` with an exclude keyword `sneakers` added for `rick owens ramones` by raw SQL (`INSERT INTO item_keywords (item_id, keyword, kind) VALUES (?, 'sneakers', 'exclude')`) stores only `URL_1` from the minimal fixture, so the job reads excludes from the database.
14. `main()` with `pod shorts` set to `active = 0` sends exactly one email, `Daily Refresh on rick owens ramones`, and still makes exactly one Yahoo request.
15. `main()` with `db.CATALOG_PATH` monkeypatched to a path that does not exist still sends the two emails of `test_c14_...`, and `yahoo_scrape` has no attribute `load_catalog`.
16. `main()` on `legacy_db` (registered `fake_http` routes: none) raises `SystemExit` with code 1; stderr is exactly one line, starting with `Error: ` and containing `python3 src/db.py`; stderr contains no `Traceback`; `fake_http.calls == []`; no email was sent; the database file's SHA-256 is unchanged.
17. `main()` when the file at `RAINCHECK_DB_PATH` does not exist behaves as in criterion 16, with the message containing `python3 src/db.py --seed`, and no file is created at that path.
18. `main()` on a version 1 database with no items (`temp_db`) prints `No active items in the database. Add items to catalog.json, then run python3 src/db.py --seed.`, returns normally, makes no HTTP call, and sends no email.
19. `main()` on a database whose `user_version` was set to 2 behaves as in criterion 16.
20. `test_c14_...`, `test_rc07_c28_...`, and `test_rc07_c29_...` pass unchanged (one Yahoo request per run with a 30-second timeout; an HTTP 503 and an old-layout page still raise `requests.HTTPError` and `YahooPageError` from `main()` with no email and no rows).
21. `yahoo_scrape.title_matches is matching.title_matches` (the job uses the shared function).

**eBay job** (`tests/test_ebay_pricing.py`)

22. Criteria c33-c44 of RC-01 as currently written (`test_c33_...` through `test_c44_...`, and `test_c13_...`) pass unchanged, and `ebay_market_price` keeps its four-argument signature.
23. On `seeded_db`, `refresh_market_prices()` sends the search `q` values `["rick owens ramones", "pod shorts"]` in that order (the test catalog's `search_query` values).
24. After `UPDATE items SET search_query = 'ramones high' WHERE name = 'rick owens ramones'` and `UPDATE items SET search_query = NULL WHERE name = 'pod shorts'`, the `q` values are `["ramones high", "pod shorts"]`, and stdout contains `Pricing rick owens ramones with eBay query "ramones high"` and `Pricing pod shorts with eBay query "pod shorts"`.
25. With a per-query fake (see "Test plan") returning `search_ramones.json` for the Ramones query and, for `pod shorts`, three summaries `Rick Owens Pod Shorts Black` at `200.00`, `Rick Owens Pod Shorts Dust` at `300.00`, and `Rick Owens Pod Short Pants Hybrid` at `900.00`, `refresh_market_prices()` stores `market_price_usd` 900.0 for `rick owens ramones` and 250.0 for `pod shorts` (the `pants` exclude from the database applies).
26. With `pod shorts` set to `active = 0`, `refresh_market_prices()` makes one token request and one search, and `pod shorts` keeps `market_price_usd` `NULL`.
27. For each of `"0.00"` and `"-1.00"` as every Ramones summary price, `refresh_market_prices()` raises nothing, leaves `rick owens ramones` `market_price_usd` unchanged (`NULL`), prints `eBay median for rick owens ramones is not a usable price`, and still searches and stores `pod shorts` (250.0 with the criterion 25 data).
28. When the `pod shorts` search matches nothing, stdout contains `No matching eBay listings for pod shorts; market price not updated.` and its `market_price_usd` stays `NULL`.
29. When the `pod shorts` search returns HTTP 500 (after a successful Ramones search), `refresh_market_prices()` raises `requests.HTTPError`, and `rick owens ramones` keeps the 900.0 stored before the error (pins Open question 3).
30. `main()` on `legacy_db`, with no eBay credentials in the environment and no `fake_http` routes, raises `SystemExit` with code 1, writes exactly one stderr line starting with `Error: ` and containing `python3 src/db.py`, makes no HTTP call (no token request), raises no `KeyError`, and leaves the file's SHA-256 unchanged.
31. `main()` with no file at `RAINCHECK_DB_PATH` behaves as in criterion 30 with `python3 src/db.py --seed` in the message, and creates no file.
32. `refresh_market_prices()` on `temp_db` (no items) prints the `No active items ...` line from criterion 18 and makes no HTTP call.
33. `refresh_market_prices()` with `db.CATALOG_PATH` monkeypatched to a missing path still stores 900.0 for `rick owens ramones`, and `ebay_pricing` has no attribute `load_catalog` or `get_item_id`.
34. The token `POST` and every search `GET` are called with `timeout=30` (Open question 4).
35. `ebay_pricing.title_matches is matching.title_matches`.

**Reviewer**

36. README "Usage" says, in this order: edit `catalog.json`; run `python3 src/db.py --seed`; run `python3 src/ebay_pricing.py`, then `python3 src/yahoo_scrape.py`.
    It states that the jobs read items only from the database, so `catalog.json` edits take effect only after seeding; that both jobs ignore listings whose title contains an exclude keyword; that matching is case-insensitive substring matching, so `low` also matches `yellow` until RC-17; and that eBay is searched with `search_query`, or the item name when it is missing.
    The sentence saying `ebay_pricing.py` needs `exclude_keywords` "until RC-03b" is removed.
37. README "Project structure" lists `matching.py` with a one-line description.
38. `git diff main...<branch>` changes only the files in "Files touched"; the `src/db.py` hunk is the docstring line only; neither job imports `load_catalog`, `get_connection`, `get_item_id`, or `get_market_price`; no keyword `in` test on a title remains in either job outside `title_matches`; every network call in the two jobs has a timeout; and `python3 -m pytest` passes with no new warnings.

**Verifier** (throwaway clone; `RAINCHECK_DB_PATH` always points into a temp directory; `RAINCHECK_DRY_RUN=1` always set; never touches the real `src/raincheck.db`)

39. V1: `python3 src/db.py --seed` on a new temp path, then `python3 src/yahoo_scrape.py` (one live read-only Yahoo request) exits 0 and prints two dry-run emails, `Daily Refresh on rick owens ramones` and `Daily Refresh on pod shorts`; the verifier reports how many listings each email contains.
40. V2: on an unmigrated legacy copy (`git cat-file blob d632e3cdadb74b6cd385ad3d4cbf5c6fd0c85194`), each of `python3 src/yahoo_scrape.py` and `python3 src/ebay_pricing.py`, run with `HTTPS_PROXY=http://127.0.0.1:9` and `HTTP_PROXY=http://127.0.0.1:9` so that any request would fail, exits 1 with exactly one stderr line starting with `Error:` that names `python3 src/db.py`, no traceback, and the copy's SHA-256 unchanged.
    The clean message (rather than a `ProxyError` traceback) proves no request was attempted before the check.
41. V3: the same two commands with `RAINCHECK_DB_PATH` pointing at a file that does not exist exit 1 with the `--seed` message, and no file is created.
42. V4: on a database created with `python3 src/db.py` (no seed), both commands with the same proxy variables print the `No active items` line and exit 0.
43. V5 (only if the orchestrator provides eBay credentials to the verifier's environment; otherwise skipped and reported as skipped): `python3 src/ebay_pricing.py` on the V1 database (one token request and two live read-only searches) exits 0, prints `Pricing rick owens ramones with eBay query "rick owens ramones"` and `Pricing pod shorts with eBay query "rick owens pod shorts"`, and the verifier reports the resulting `market_price_usd` per item.
44. `validate_catalog` on the real `src/catalog.json` returns `rick owens ramones` with include keywords `("ramones", "ramon", "ラモーンズ")` and exclude keywords `("drkshdw", "canvas", "low", "ローカット")`, in catalog order, and `pod shorts` unchanged from RC-03a.
45. On the committed live Yahoo fixture, `title_matches` with the real catalog's `rick owens ramones` keywords keeps exactly 1 listing, whose title contains `ハイカット`, and drops the `DRKSHDW` and `ローカット` listings.

## Edge cases

- **Empty results:** no active items makes both jobs print one line and return with no network call (criteria 18, 32).
  A Yahoo page with no listings is still RC-07 behavior (`parse_listings` returns `[]` or raises).
  An eBay search with no matching summaries prints a skip line and keeps the old price (criterion 28).
  An item with no include keywords (only possible through raw SQL) matches nothing.
- **`None` or missing values:** `search_query` `NULL` falls back to the item name (criterion 24); an empty string would also fall back, because the rule is `search_query or name`.
  `effective_price_usd` `None` skips the Yahoo item with a printed line (criterion 12).
  A median of `None` skips the eBay item (criterion 28).
- **Network failure or bad HTTP status:** Yahoo errors propagate as in RC-07 (criterion 20).
  An eBay error for any item stops the run with a traceback, keeping prices already stored (criterion 29, Open question 3).
  Both eBay requests now time out after 30 seconds (criterion 34).
  No network call happens before the schema check (criteria 16-19, 30-31).
- **Malformed input:** a median of 0 or below, or a non-finite one, is skipped (criterion 27).
  An outdated, newer, missing, or non-SQLite database is a one-line `Error:` with exit 1 (criteria 16, 17, 19, 30, 31; the non-SQLite case uses the same `SchemaVersionError` path from RC-03a Amendment 1).
  An eBay summary without `price` or `title` still raises `KeyError` (unchanged; see "Known issues").
- **Duplicates:** a Yahoo listing matching two items is inserted for the first and treated as seen for the second, as today (RC-04).
  Duplicate keywords cannot reach the jobs, because seeding de-duplicates them.
- **Concurrency:** the Yahoo job reads each item's effective price once at the start of the run instead of once per item; an eBay run finishing mid-way through a Yahoo run is picked up on the next Yahoo run instead.

### Known issues (not fixed here)

| Issue | Where | Owner |
| --- | --- | --- |
| An eBay summary without `price` raises `KeyError`. The Browse API documents `currentBidPrice` as "only returned for auction items" and does not promise `price` on every summary; all 50 live summaries have it. | `src/ebay_pricing.py:56,63` | RC-09 |
| The median uses whatever survives keyword filtering (for example `Ramones Mega Bumper` at 275.00 is kept). | `src/ebay_pricing.py` | RC-09 |
| One failing eBay or Yahoo request stops the run. | both jobs | RC-10 |
| Substring matching: `low` excludes `yellow` and `lows`; full-width letters never match. | `src/matching.py` | RC-17 |
| An empty email is still sent for an item with no deals. | `src/yahoo_scrape.py` | RC-15 |

## Test plan

### Fixtures

No new fixture files.

- `seeded_db`, `temp_db`, `legacy_db`, `db_conn`, `fake_http`, `sent_emails`, `test_catalog`, and `fixtures_dir` from `tests/conftest.py`, unchanged.
- `tests/fixtures/yahoo/brand_page_minimal.html` and `brand_page_malformed.html` for the Yahoo pipeline; no title in either contains `drkshdw`, `canvas`, `low`, or `pants`, so adding excludes does not change any existing expected result.
- `tests/fixtures/ebay/search_ramones.json`, `search_empty.json`, `token.json`, and `search_live.json` (criterion 7).
- The pod shorts summaries for criteria 25, 27, and 28 are written inline in the test as Python dicts, so each test shows its whole input.
- A `ramones_item` fixture in `tests/test_yahoo_scrape.py` returns the `rick owens ramones` `TrackedItem` from `db.get_tracked_items()` on `seeded_db`, replacing the dict-based `ramones_entry` fixture.
  Tests that change the database first must read the item again afterwards, because a `TrackedItem` is a snapshot.

### Per-query eBay fake

`FakeHTTP` in `tests/conftest.py` routes on `(method, url)` only, so every search gets the same response.
Criteria 25, 27, 28, and 29 need a different response per query.
The test-writer adds a small helper in `tests/test_ebay_pricing.py` only (not `conftest.py`): it monkeypatches `requests.post` to return `token.json` and `requests.get` to look up `params["q"]` in a dict of `(status, json)` responses, records every call with its kwargs, and raises `AssertionError` for an unknown query.
It reuses `conftest.FakeResponse`.
Promoting it to `conftest.py` is left for when a second test file needs it.

### Superseded tests

| Test | Old | New |
| --- | --- | --- |
| `tests/test_yahoo_scrape.py` fixture `ramones_entry` | dict from `tests/fixtures/catalog.json` | removed; replaced by `ramones_item` (`TrackedItem` from the database) |
| `test_c45_inserts_only_the_two_matching_cheap_listings` | passes the dict | passes `ramones_item`; assertions unchanged |
| `test_c46_one_email_sorted_by_discount_with_percentages` | passes the dict | passes `ramones_item`; assertions unchanged |
| `test_c47_second_run_inserts_no_new_rows` | passes the dict | passes `ramones_item`; assertions unchanged |
| `test_rc07_c24_malformed_fixture_pipeline_rows_and_email` | passes the dict | passes `ramones_item`; assertions unchanged |
| `test_rc07_c25_null_market_price_skips_item` | sets both prices `NULL`, passes the dict (which carries no price) | sets both prices `NULL`, then reads the item from `get_tracked_items()`, passes it; also asserts the `Skipping ...` line (criterion 12) |

Every other existing test passes unchanged, in particular `test_c13_...`, `test_c14_...`, `test_c33_...` through `test_c44_...`, `test_rc07_c26_...` through `test_rc07_c29_...`, every test in `tests/test_config.py` (`test_c11_...` and `test_c12_...` still cover `db.load_catalog`, which seeding uses), and all RC-03a tests.

Before implementation, the five superseded tests fail with `TypeError: 'TrackedItem' object is not subscriptable`, and criteria 1-7 fail because `matching` does not exist; criteria 20 and 22 already pass and guard against regressions.
Test names include the criterion, for example `test_rc03b_c27_unusable_median_is_skipped_and_run_continues`.

### Test files

| File | Covers |
| --- | --- |
| `tests/test_matching.py` (new) | Criteria 1-7 |
| `tests/test_yahoo_scrape.py` | Criteria 8-21, superseded updates |
| `tests/test_ebay_pricing.py` | Criteria 22-35 |

For criteria 16, 19, and 30, compare SHA-256 with `tests/dbhelpers.sha256`, and read stderr with `capsys`.

### Verifier

Criteria 39-43, in a throwaway clone of the implementer's branch.
The verifier states in its report how many live requests it made: one Yahoo request in V1, none in V2-V4, and one token request plus two searches in V5 if it ran.

## Shared-file conflicts

Yes, but only `src/db.py`, and only a docstring line (Open question 5).
`src/schema.sql` is not touched; `src/catalog.json` gains the two keywords in the Resolved note.
If the user declines Open question 5, RC-03b touches no shared file.

In practice RC-03b should run alone either way: RC-08, RC-15, and RC-16 touch `src/yahoo_scrape.py`, RC-09 and RC-06 touch `src/ebay_pricing.py`, RC-04, RC-05, RC-13, and RC-17 depend on RC-03b, and RC-14 touches `src/db.py`.

## Open questions

**Resolved 2026-10-26:** the user approved this spec and chose the recommended option for every question below.
The user also made two catalog data decisions after seeing the live-fixture measurements:
add the include keyword `ラモーンズ` to `rick owens ramones`, because two Ramones listings on the live page are titled only in Japanese, and add the exclude keyword `ローカット` (Japanese for "low-cut"), because the English exclude `low` cannot see it and the user wants high-tops only.
On the committed live fixture this makes `rick owens ramones` match 3 listings, of which 1 (`DRKSHDW`) and 1 (`ローカット`) are excluded, leaving the same 1 high-top listing as before.
This adds `src/catalog.json` to the files touched, so RC-03b also touches a shared file and must run alone.
After merge, the user's database needs `python3 src/db.py --seed` once for these keywords to take effect.

1. **How do the jobs check the schema before any network call?**
   - Option A (recommended): each job calls `get_tracked_items()` first.
     It opens the database through `get_connection()`, which already raises `SchemaVersionError` for a missing, empty, outdated, newer, or non-SQLite file, and the jobs need the items anyway, so the check and the read are one step with no new API.
     Tradeoff: the ordering is a convention in two `main` functions, guarded by criteria 16 and 30 rather than enforced by a dedicated function.
   - Option B: a new `db.check_schema()` that opens and closes a connection, called first by each job.
     Tradeoff: an explicit name, but one more public function in the shared `src/db.py`, and a second database open right before the real read.
2. **Which errors get the one-line `Error:` treatment in the jobs?**
   Recommendation: only `SchemaVersionError`, because it is the one expected, user-fixable error with a message that names the fix.
   Tradeoff: a missing `EBAY_CLIENT_ID` (`KeyError`), an HTTP error, and `YahooPageError` still show a traceback until RC-10 adds logging and per-error handling.
   Alternative: also catch `KeyError` from `get_ebay_token` with a message naming `.env`; small, but it starts RC-10's work piecemeal.
3. **What happens when one item's eBay request fails?**
   - Option A (recommended): unchanged; the error propagates and stops the run, prices already stored stay (criterion 29).
     RC-10 owns per-item isolation, retries, and logging, and doing half of it here would mean deciding retry and logging policy twice.
     Tradeoff: an eBay 500 for item 1 still leaves item 2 unpriced for that run.
   - Option B: catch `requests.RequestException` per item, print a line, continue, and exit non-zero at the end.
     Tradeoff: more robust today, but it adds an exit-code policy that RC-10 would probably redo.
4. **Add a timeout to the two eBay requests now?**
   Recommendation: yes, `timeout=30` on both, matching the Yahoo job.
   `CLAUDE.md` requires a timeout on every network call, this item already edits both calls' file, and without it one hung eBay request can stall the run forever.
   Tradeoff: slight overlap with RC-10, which then only needs to add retries.
5. **Update the stale `get_tracked_items` docstring in `src/db.py`?**
   Recommendation: yes; after this item it is false ("Nothing in production calls this yet").
   Tradeoff: it makes RC-03b touch a shared file, so it formally runs alone; it would anyway (see "Shared-file conflicts").
   Alternative: leave it for the next item that edits `src/db.py`, accepting a wrong comment in the meantime.
6. **Should `title_matches` also lowercase the keywords?**
   Recommendation: no, as in the backlog: keywords are normalized once at write time by `validate_catalog`, and criterion 5 pins that a non-normalized keyword does not match, so bad data is visible.
   Tradeoff: a keyword inserted by hand with capitals silently matches nothing; lowercasing per call would tolerate it at no real cost, but would hide that the database holds data seeding would never write.
7. **Exit code when there are no active items?**
   Recommendation: 0 with one printed line, because "nothing to do" is not a failure, and seeding already rejects an empty catalog, so this state only arises after `python3 src/db.py` without `--seed`.
   Tradeoff: an unattended run (RC-11) with an accidentally empty watchlist would not show as failed; RC-12's alarms could revisit this.

## Rollback

- **Before merge:** close the PR; nothing else changed.
- **After merge:** revert the merge commit with a new PR.
  There is no schema or data migration, so the database needs no change.
  Data written while RC-03b ran stays valid for the old code: listings are stored the same way, and an eBay price computed with the new `pod shorts` query is just a `market_price_usd` value.
- **After merge, before the first real run (orchestrator, at the user's request):** confirm the real database is at version 1 and matches the catalog, read-only, then reseed (idempotent):

  ```bash
  sqlite3 src/raincheck.db "PRAGMA user_version;"
  .venv/bin/python src/db.py --seed
  sqlite3 -header src/raincheck.db "SELECT i.name, i.search_query, i.active, k.kind, k.keyword FROM items i JOIN item_keywords k ON k.item_id = i.id ORDER BY i.id, k.kind, k.keyword;"
  ```

  `user_version` is `1`, seeding prints `Catalog seeded.` with no `Deactivated` line, and the keyword table shows `ramon`, `ramones`, `ラモーンズ` as Ramones includes, `canvas`, `drkshdw`, `low`, `ローカット` as Ramones excludes, and `pants` as the pod shorts exclude.
  Then run `python3 src/ebay_pricing.py` before `python3 src/yahoo_scrape.py`, so `pod shorts` is priced with its new query before the next deal email.

## Amendments

### Amendment 1 (2026-10-26): review hardening

- `src/db.py`: every function that opens a connection closes it on every path, including when a query raises (for example `insert_listing` with an unknown `brand_id` raising `sqlite3.IntegrityError`).
  Today such a connection is left open until garbage collection, which shows up as a `ResourceWarning: unclosed database` attributed to whichever test happens to be running.
- `pyproject.toml`: pytest treats `ResourceWarning` and `pytest.PytestUnraisableExceptionWarning` as errors, so a leaked connection fails the suite instead of hiding as a warning.
- `src/sendemail.py`: the SMTP connection gets `timeout=30`, as the project convention requires for every network call; a hung mail server could otherwise stall the Yahoo job forever.
- New criterion 46: `insert_listing` with an unknown `brand_id` raises `sqlite3.IntegrityError` and leaves no open connection (no `ResourceWarning` under the strict warning filter).
- New criterion 47: the real SMTP path constructs `smtplib.SMTP("smtp.gmail.com", 587, timeout=30)`.
- Test fix: criterion 19's test also asserts the database file's SHA-256 is unchanged, as criterion 16 does.

