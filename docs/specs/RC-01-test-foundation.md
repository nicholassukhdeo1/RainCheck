# RC-01: Test and configuration foundation

**Status:** Approved
**Backlog item:** RC-01 in `docs/BACKLOG.md`

## Summary

Add a pytest suite that runs fully offline against a throwaway SQLite database, plus the three configuration switches that make that possible: a database path environment variable, catalog resolution relative to the source tree, and an email dry-run mode.
The suite pins today's working behavior so later backlog items can change code with confidence.

## Motivation

RainCheck has no tests, and both data sources are live websites.
Every later backlog item needs a way to prove it works without hitting the internet, sending email, or touching the real `src/raincheck.db`.

Several things in today's code make that impossible or unsafe:

- The database path is a module constant fixed at import time (`src/db.py:7`), so nothing can point the code at a test database.
- `src/ebay_pricing.py:8-9` reads `EBAY_CLIENT_ID` and `EBAY_CLIENT_SECRET` at import time with `os.environ[...]`.
  Importing the module without those variables raises `KeyError`.
  The implementer works in a fresh git worktree, which has no `.env` (it is gitignored), so the module cannot even be imported there.
  This is the one change in this spec that is strictly required before any eBay test can exist.
- `src/ebay_pricing.py:75` and `src/yahoo_scrape.py:94` open `"catalog.json"` relative to the current working directory, so both scripts only work when run from inside `src/`.
  `src/db.py:9` already resolves `CATALOG_PATH` correctly, but nothing else uses it.
- `src/sendemail.py:12-35` always sends real email through Gmail SMTP.
  There is no way to run the Yahoo job end to end without emailing the user.
- `src/ebay_pricing.py:90-94` has no `main()` function, so entry-point behavior cannot be tested, and it fetches an OAuth token twice (once at line 92 and again inside `refresh_market_prices`).
- `src/sendemail.py:9` and `src/ebay_pricing.py:6` call `load_dotenv()` at import time.
  Under pytest that would silently load the user's real secrets from `src/.env` into the test process.

## Scope

**In scope:**

- pytest configuration, a development requirements file, and a `tests/` directory with shared fixtures in `tests/conftest.py`.
- An offline guarantee for every test: no network sockets, no real email, no `.env` loading, no real database.
- A `RAINCHECK_DB_PATH` environment variable read at connection time, defaulting to today's location.
- A `load_catalog()` helper in `src/db.py`, used by both jobs, so they work from any working directory.
- A `RAINCHECK_DRY_RUN` environment variable that makes `send_email` print the email instead of sending it.
- Moving eBay credential reading from import time into `get_ebay_token()`.
- A `main()` function in `src/ebay_pricing.py` and `src/yahoo_scrape.py`, called from their `if __name__ == "__main__":` blocks.
- Hand-written, deterministic fixtures for Yahoo HTML, eBay search JSON, the eBay token response, and a test catalog.
- A capture script, `scripts/capture_fixtures.py`, that the user runs once to record a live Yahoo page and a live eBay search response (see Open question 1).
- Tests covering: schema init, seeding, `insert_listing` deduplication, `get_item_id`, `update_market_price`, `get_market_price`, `get_ebay_token`, the eBay median with include and exclude keywords, `refresh_market_prices`, `send_email` (dry run and real path with SMTP faked), and one happy-path run of `yahoo_jp_scrape` against the hand-written page.
- Adding the missing `EBAY_CLIENT_ID` and `EBAY_CLIENT_SECRET`, plus the two new variables, to `.env.example`.
- A short "Configuration" and "Running tests" section in `README.md`.

**Out of scope:**

- Fixing any bug listed under "Known issues found (not fixed here)" below, except the double token fetch, which disappears naturally when `main()` is extracted.
- Adding request timeouts, retries, or `logging` to existing code (RC-10).
  New code in this spec (the capture script) does use timeouts.
- Changing Yahoo parsing, the zip-based pairing, or the exchange rate (RC-07, RC-08).
- Changing seeding semantics, keyword storage, or the `pod shorts` data (RC-03).
- Changing email content or the "one email per item, even when empty" behavior (RC-15).
- Untracking `src/raincheck.db` or rewriting the stale parts of `README.md` (RC-02).
- `src/setup.py` (RC-14 replaces it).
  It also reads `catalog.json` from the working directory, but it is left alone here.
- Any schema change.
- CI (GitHub Actions).
  Worth a follow-up item once the suite exists.

## Design

### Files touched

| File | Change |
| --- | --- |
| `src/db.py` | Add `DEFAULT_DB_PATH`, `get_db_path()`, `load_catalog()`; `get_connection()` uses `get_db_path()`; `seed_from_catalog()` uses `load_catalog()`; `init_db()` prints the path it initialized; `__main__` block calls `load_dotenv()` before `init_db()`. |
| `src/ebay_pricing.py` | Remove module-level credential reads and base64 encoding; read them inside `get_ebay_token()`; `refresh_market_prices()` uses `load_catalog()`; add `main()`. |
| `src/yahoo_scrape.py` | Add `main()` that iterates `load_catalog()`; `__main__` block calls `main()`. |
| `src/sendemail.py` | Add dry-run handling at the top of `send_email()`. |
| `pyproject.toml` (new) | pytest configuration only. |
| `requirements-dev.txt` (new) | `-r requirements.txt` plus pinned `pytest`. |
| `tests/conftest.py` (new) | Shared fixtures (names fixed below). |
| `tests/test_db.py` (new) | Database tests. |
| `tests/test_config.py` (new) | DB path, catalog resolution, import-safety tests. |
| `tests/test_ebay_pricing.py` (new) | eBay token, median, refresh tests. |
| `tests/test_sendemail.py` (new) | Dry-run and real-path tests. |
| `tests/test_yahoo_scrape.py` (new) | Yahoo happy-path test. |
| `tests/test_live_fixtures.py` (new) | Shape checks on the captured live fixtures. |
| `tests/fixtures/...` (new) | See "Fixture layout" below. |
| `scripts/capture_fixtures.py` (new) | One-off live capture, run by the user only. |
| `.env.example` | Add `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET`, and commented-out `RAINCHECK_DB_PATH` and `RAINCHECK_DRY_RUN`. |
| `README.md` | Add "Configuration" and "Running tests" sections; do not rewrite other sections. |

`src/schema.sql` and `src/catalog.json` are read but not modified.

### Schema changes and migration

None.
Existing `raincheck.db` files keep working unchanged, and with `RAINCHECK_DB_PATH` unset the code opens the same file it opens today.

### Interfaces

#### `src/db.py`

```python
SRC_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = SRC_DIR / "raincheck.db"
SCHEMA_PATH = SRC_DIR / "schema.sql"
CATALOG_PATH = SRC_DIR / "catalog.json"

def get_db_path() -> str
def load_catalog(path=None) -> list[dict]
def seed_from_catalog(catalog_path=None) -> None
```

- `get_db_path()` returns `os.environ["RAINCHECK_DB_PATH"]` when that variable is set to a non-empty value, otherwise `str(DEFAULT_DB_PATH)`.
  It is evaluated on every call, never cached at import time, so tests and later items (such as the Lambda handler in RC-11) can change it at runtime.
  A relative value is resolved by SQLite against the current working directory, which is standard behavior and is documented in the README.
  The function does not create missing parent directories; `sqlite3` raises `sqlite3.OperationalError` in that case, which is the desired loud failure.
- `get_connection()` calls `sqlite3.connect(get_db_path())`; everything else about it is unchanged.
  It remains the only place in `src/` that calls `sqlite3.connect`.
- The old `DB_PATH` constant is removed.
  Nothing outside `src/db.py` uses it today.
- `load_catalog(path=None)` opens `path` if given, otherwise the module attribute `CATALOG_PATH` looked up at call time (so `monkeypatch.setattr(db, "CATALOG_PATH", ...)` works), and returns the parsed JSON list.
  Errors (`FileNotFoundError`, `json.JSONDecodeError`) propagate unchanged.
- `seed_from_catalog(catalog_path=None)` behaves exactly as today, except it reads entries through `load_catalog(catalog_path)`.
- `init_db()` prints `Database initialized at <path>.` instead of `Database initialized.`, so the user can see which file was touched.
- The `if __name__ == "__main__":` block calls `load_dotenv()` and then `init_db()`, so `RAINCHECK_DB_PATH` set in `.env` is honored by `python3 db.py` the same way it is by the other two scripts.
  The block may stay where it is or move to the end of the file; behavior is the same.

#### `src/ebay_pricing.py`

```python
def get_ebay_token() -> str
def ebay_market_price(query, keywords, exclude_keywords, token)  # unchanged
def refresh_market_prices() -> None
def main() -> None
```

- Module import performs no environment reads and raises nothing when `EBAY_CLIENT_ID` or `EBAY_CLIENT_SECRET` is unset.
  `load_dotenv()` at import stays as is.
- `get_ebay_token()` reads both variables with `os.environ[...]` when called, builds the Basic header from base64 of `"<id>:<secret>"`, and otherwise sends the same request as today.
  A missing variable raises `KeyError` at call time.
- `refresh_market_prices()` iterates `load_catalog()` instead of `open("catalog.json")`; otherwise unchanged (one token per run, `item["item name"]` as the query, skip on `None`).
- `main()` calls `refresh_market_prices()` once.
  The `__main__` block calls `main()` and nothing else, which removes the duplicate token request at line 92.

#### `src/yahoo_scrape.py`

```python
def yahoo_jp_scrape(item_dict, brand_url)  # unchanged
def main() -> None
```

- `main()` iterates `load_catalog()` and calls `yahoo_jp_scrape(item, RICK_OWENS_URL)` for each item.
- The `__main__` block calls `main()`.
- `yahoo_jp_scrape` itself is not modified.

#### `src/sendemail.py`

```python
DRY_RUN_ENV = "RAINCHECK_DRY_RUN"

def is_dry_run() -> bool
def send_email(subject, body) -> None
```

- `is_dry_run()` reads `RAINCHECK_DRY_RUN` at call time.
  It returns `False` only when the variable is unset or its stripped, lowercased value is one of `""`, `"0"`, `"false"`, `"no"`, `"off"`.
  Any other value returns `True`.
  This fails safe: a typo such as `RAINCHECK_DRY_RUN=ture` suppresses email rather than sending it.
- When `is_dry_run()` is `True`, `send_email` must not import-use, construct, or call `smtplib.SMTP`, must not read `SENDER_PASSWORD`, and prints exactly this block to stdout:

  ```
  [DRY RUN] Email not sent (RAINCHECK_DRY_RUN is set).
  To: <recipient>
  Subject: <subject>
  <body>
  ```

  `<recipient>` is `RECEIVER_EMAIL`, else `SENDER_EMAIL`, else the literal `(not configured)`.
  `<body>` is printed verbatim, HTML included, because that is what would have been sent.
- When `is_dry_run()` is `False`, behavior is byte-for-byte today's behavior.
- Dry run affects email only.
  Database writes still happen, so a dry run against the real database marks listings as seen and they will not be emailed later.
  The README must say to pair `RAINCHECK_DRY_RUN=1` with a throwaway `RAINCHECK_DB_PATH`.

#### `scripts/capture_fixtures.py`

- Run by the user only, from the repository root: `python3 scripts/capture_fixtures.py`.
  Agents never run it.
- Loads `src/.env` and then a root `.env` if they exist, without overriding variables already set.
- Fetches `RICK_OWENS_URL` (imported from `yahoo_scrape`) with `timeout=30`, calls `raise_for_status()`, removes seller data (see Amendment 1), and writes the page to `tests/fixtures/yahoo/brand_page_live.html`.
- Gets an eBay token, searches for `rick owens ramones` with the same headers and parameters as `ebay_market_price`, `timeout=30`, and writes the JSON to `tests/fixtures/ebay/search_live.json` (indent 2, UTF-8, `ensure_ascii=False`).
  Before writing, it deletes the `seller` and `itemLocation` objects from every entry in `itemSummaries`, so no seller usernames, feedback data, or partial postal codes are committed.
  The access token is never written to disk or printed.
- Does not touch any database and does not send email.
- Prints the two output paths and the number of item summaries captured.

### pytest configuration

`pyproject.toml` contains only:

```toml
[tool.pytest]
minversion = "9.0"
testpaths = ["tests"]
pythonpath = ["src"]
addopts = ["-ra", "--strict-markers"]
```

`pythonpath = ["src"]` puts `src/` on `sys.path`, so tests use the same flat imports the scripts use today (`import db`, `from ebay_pricing import ...`).
No package restructuring and no `__init__.py` files are needed.
The command to run the suite is `python3 -m pytest` from the repository root.

`requirements-dev.txt`:

```
-r requirements.txt
pytest==9.1.1
```

### Shared fixtures in `tests/conftest.py`

These names are the contract later items reuse.

**Module level, before any test module is imported:** replace `dotenv.load_dotenv` with a no-op that returns `False`.
Because `src/` modules do `from dotenv import load_dotenv`, they bind the no-op and never read `src/.env` during tests.
A comment must explain why this is done at import time instead of in a fixture.

**Autouse fixtures (apply to every test automatically):**

- `_isolated_env`: deletes `SENDER_EMAIL`, `SENDER_PASSWORD`, `RECEIVER_EMAIL`, `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET`; sets `RAINCHECK_DB_PATH` to `<tmp_path>/test.db`; sets `RAINCHECK_DRY_RUN=1`.
  Tests that need a credential or the real email path set or unset it explicitly with `monkeypatch`.
- `_block_network`: monkeypatches `socket.socket.connect` and `socket.socket.connect_ex` to raise `RuntimeError("Network access is blocked in tests")`.
  This catches `requests`, `smtplib`, and anything else that opens a TCP connection, even code that bypasses the HTTP fake.

**Opt-in fixtures:**

| Fixture | Returns | Behavior |
| --- | --- | --- |
| `fixtures_dir` | `pathlib.Path` | Absolute path to `tests/fixtures/`. |
| `temp_db` | `pathlib.Path` | Calls `db.init_db()` against the autouse `RAINCHECK_DB_PATH` and returns that path. |
| `test_catalog` | `pathlib.Path` | Monkeypatches `db.CATALOG_PATH` to `tests/fixtures/catalog.json` and returns that path. |
| `seeded_db` | `pathlib.Path` | Depends on `temp_db` and `test_catalog`; calls `db.seed_from_catalog()`; returns the DB path. |
| `db_conn` | `sqlite3.Connection` | A `db.get_connection()` to the temp DB for direct assertions; closed at teardown. |
| `fake_http` | `FakeHTTP` | Replaces `requests.get` and `requests.post` (module attributes) with a router. |
| `sent_emails` | `list[tuple[str, str]]` | Monkeypatches `yahoo_scrape.send_email` to append `(subject, body)` and send nothing. |

`FakeHTTP` (defined in `conftest.py`):

- `add(method, url, *, json=None, text=None, status=200)` registers a response for an exact `(method, url)` pair; query parameters are not part of the match.
  The same response is returned for every matching call.
- `calls` is a list of `(method, url, kwargs)` in call order, so tests can assert on `params`, `headers`, and `data`.
- Unregistered `(method, url)` pairs raise `AssertionError` naming the URL.
- Accepts and records any keyword arguments, including `timeout`, so RC-10 adding timeouts does not break existing tests.
- The returned `FakeResponse` has `status_code`, `text`, `content`, `json()`, and `raise_for_status()`, which raises `requests.HTTPError` when `status >= 400`, matching real `requests`.

### Fixture layout

```
tests/fixtures/
├── catalog.json                    # hand-written test catalog
├── yahoo/
│   ├── brand_page_minimal.html     # hand-written, deterministic
│   └── brand_page_live.html        # captured by scripts/capture_fixtures.py
└── ebay/
    ├── token.json                  # hand-written, shape from eBay OAuth docs
    ├── search_ramones.json         # hand-written, shape from Browse API spec v1.20.4
    ├── search_empty.json           # hand-written, no itemSummaries key
    └── search_live.json            # captured by scripts/capture_fixtures.py
```

**`catalog.json`**: one brand, `Rick Owens`, two items.

- `rick owens ramones`: `market_price` 800.0, `search_query` `rick+owens+ramones`, `keywords` `["ramones", "ramon"]`, `exclude_keywords` `["drkshdw", "canvas", "low"]`, `size` 42.
- `pod shorts`: `market_price` 300.0, `search_query` `pod+shorts`, `keywords` `["pod short"]`, `exclude_keywords` `["pants"]`, `size` 42.

**`token.json`**, matching the sample in eBay's client credentials documentation:

```json
{"access_token": "test-token-not-real", "expires_in": 7200, "token_type": "Application Access Token"}
```

**`search_ramones.json`**: top-level `href`, `total` 6, `limit` 50, `offset` 0, and `itemSummaries` with these six entries.
Each entry has `itemId`, `title`, `price` as `{"value": "<string>", "currency": "USD"}` (the Browse API returns `value` as a string), `buyingOptions` `["FIXED_PRICE"]`, and `itemWebUrl`.

| # | `title` | `price.value` |
| --- | --- | --- |
| 1 | `Rick Owens Ramones High Top Black 42` | `"700.00"` |
| 2 | `Rick Owens RAMONES Leather Sneakers` | `"900.00"` |
| 3 | `Rick Owens Ramones Low Sneakers` | `"300.00"` |
| 4 | `Rick Owens DRKSHDW Ramones Canvas` | `"250.00"` |
| 5 | `Rick Owens Geobasket` | `"1200.00"` |
| 6 | `Rick Owens Ramon High` | `"1000.00"` |

**`search_empty.json`**: `{"href": "...", "total": 0, "limit": 50, "offset": 0}` with no `itemSummaries` key.

**`brand_page_minimal.html`**: a small, valid HTML page with four product cards, each containing, in this order, an `<h3>` with the title, an `<a class="Product__titleLink js-browseHistory-add js-rapid-override" href="...">`, a `<span class="Product__priceValue u-textRed">`, and an `<img class="Product__imageData" src="...">`.
The class strings must match `src/yahoo_scrape.py:33-35` exactly, because `find_all(class_="a b c")` matches the exact attribute string.
The page must contain no other `<h3>` elements.

| # | Title | Price text | Link | Expected outcome at market 800 USD |
| --- | --- | --- | --- | --- |
| 1 | `Rick Owens Ramones High Top 42` | `50,000円` | `https://auctions.yahoo.co.jp/jp/auction/t1000000001` | 310.00 USD, 61.25% cheaper, deal |
| 2 | `Rick Owens Ramones Leather 44` | `200,000円` | `https://auctions.yahoo.co.jp/jp/auction/t1000000002` | 1240.00 USD, not a deal |
| 3 | `Rick Owens Geobasket 43` | `10,000円` | `https://auctions.yahoo.co.jp/jp/auction/t1000000003` | no keyword match, skipped |
| 4 | `Rick Owens Ramon Sneakers` | `100,000円` | `https://auctions.yahoo.co.jp/jp/auction/t1000000004` | 620.00 USD, 22.5% cheaper, deal |

Image URLs follow the same pattern (`https://auc-pctr.c.yimg.jp/i/test/1.jpg` and so on).

## Acceptance criteria

### Harness and safety

1. `pip install -r requirements-dev.txt` followed by `python3 -m pytest` from the repository root runs the whole suite and passes, with no `src/.env` present and with no network access.
2. A test that calls `requests.get("https://example.com")` without `fake_http` fails with `RuntimeError` whose message contains `Network access is blocked`.
3. During any test, `os.environ.get("RAINCHECK_DB_PATH")` points inside that test's `tmp_path`, and `os.environ.get("RAINCHECK_DRY_RUN") == "1"`.
4. During any test, `SENDER_PASSWORD`, `EBAY_CLIENT_ID`, and `EBAY_CLIENT_SECRET` are absent from `os.environ`, even when `src/.env` exists on disk.
5. Running the suite does not change the modification time or size of `src/raincheck.db`.
   (Checked by the verifier, not by a unit test.)

### Configuration

6. With `RAINCHECK_DB_PATH` unset, `db.get_db_path()` returns the absolute path of `src/raincheck.db`.
7. With `RAINCHECK_DB_PATH` set to an empty string, `db.get_db_path()` returns the same default as criterion 6.
8. With `RAINCHECK_DB_PATH` set to `<tmp>/x.db`, `db.init_db()` creates `<tmp>/x.db` containing the tables `brands`, `items`, `item_keywords`, `listings`, and `listing_matches`, and prints a line containing `<tmp>/x.db`.
9. Changing `RAINCHECK_DB_PATH` between two `db.get_connection()` calls in the same process makes the second connection open the new file (the path is not cached at import).
10. `grep -rn "sqlite3.connect" src/` matches only `src/db.py`.
    (Checked by the reviewer.)
11. `db.load_catalog()` returns the contents of `src/catalog.json` when the current working directory is a temporary directory unrelated to the repository.
12. `db.load_catalog(path)` returns the contents of the given file.
13. `refresh_market_prices()` succeeds with the current working directory set to `tmp_path` (with `fake_http`, `test_catalog`, `seeded_db`, and eBay credentials set via `monkeypatch`), proving it no longer depends on the working directory.
14. `yahoo_scrape.main()` succeeds with the current working directory set to `tmp_path` (with `fake_http`, `test_catalog`, `seeded_db`, and `sent_emails`), calling `send_email` once per catalog item.
15. With `EBAY_CLIENT_ID` and `EBAY_CLIENT_SECRET` unset, `importlib.reload(ebay_pricing)` raises nothing.
16. With `EBAY_CLIENT_ID` unset, `ebay_pricing.get_ebay_token()` raises `KeyError`.

### Dry run

17. With `RAINCHECK_DRY_RUN=1` and `smtplib.SMTP` monkeypatched to raise `AssertionError` if constructed, `send_email("Subj", "<b>Body</b>")` returns normally, and stdout contains `[DRY RUN] Email not sent`, `Subject: Subj`, and `<b>Body</b>`.
18. Dry run works with `SENDER_EMAIL`, `SENDER_PASSWORD`, and `RECEIVER_EMAIL` all unset, and prints `To: (not configured)`.
19. Dry run prints `To: <RECEIVER_EMAIL>` when it is set, and `To: <SENDER_EMAIL>` when only `SENDER_EMAIL` is set.
20. `is_dry_run()` returns `True` for `1`, `true`, `TRUE`, `yes`, `on`, and `ture`, and `False` for unset, `""`, `0`, `false`, `False`, `no`, and `off`.
21. With `RAINCHECK_DRY_RUN` unset, `SENDER_EMAIL=a@example.com`, `SENDER_PASSWORD=pw`, `RECEIVER_EMAIL=b@example.com`, and `smtplib.SMTP` replaced by a recording fake, `send_email("Subj", "Body")` constructs the fake with `("smtp.gmail.com", 587)`, calls `starttls()`, calls `login("a@example.com", "pw")`, and calls `sendmail("a@example.com", "b@example.com", <message>)` where `<message>` contains `Subject: Subj`.

### Database behavior (today's behavior, pinned)

22. `init_db()` run twice on the same file raises nothing, and afterwards all five tables exist and are empty.
23. After `seed_from_catalog()` with the test catalog: `brands` has one row with `name = 'Rick Owens'` and `yahoo_brand_id = '103538'`; `items` has two rows whose `market_price_usd` and `search_query` match the catalog; `item_keywords` has `ramones` and `ramon` for the Ramones item and `pod short` for the shorts item.
24. Running `seed_from_catalog()` a second time leaves the row counts of `brands`, `items`, and `item_keywords` unchanged.
25. `insert_listing(brand_id, url, title, 50000, image_url)` on a new URL returns `True` and stores one row with those values, where `first_seen_at == last_seen_at`, the value parses with `datetime.fromisoformat`, and it carries a UTC offset of zero.
26. Calling `insert_listing` again with the same URL but a different title and price returns `False`, and the stored row still has the original title, price, and `first_seen_at`.
27. `insert_listing` with a different URL returns `True`, giving two rows.
28. `insert_listing` with a `brand_id` that does not exist raises `sqlite3.IntegrityError` (foreign keys are on).
29. `get_item_id("Rick Owens", "rick owens ramones")` returns the `id` of that row in `items`.
30. With two brands inserted directly by SQL, each having an item named `ramones`, `get_item_id` returns the item belonging to the requested brand.
    (Inserted directly, because seeding cannot create a second brand today; see Known issues.)
31. `update_market_price(item_id, 912.5)` sets that item's `market_price_usd` to `912.5`, leaves every other item unchanged, and `get_market_price("Rick Owens", "rick owens ramones")` then returns `912.5`.
32. `get_market_price` returns `None` for an item whose `market_price_usd` is `NULL`.

### eBay pricing

33. `get_ebay_token()` with `EBAY_CLIENT_ID=id` and `EBAY_CLIENT_SECRET=secret` sends one POST to `https://api.ebay.com/identity/v1/oauth2/token` with header `Authorization: Basic aWQ6c2VjcmV0` (base64 of `id:secret`), header `Content-Type: application/x-www-form-urlencoded`, and form data `grant_type=client_credentials` and `scope=https://api.ebay.com/oauth/api_scope`, and returns `test-token-not-real` from `token.json`.
34. `get_ebay_token()` raises `requests.HTTPError` when the token endpoint returns HTTP 401.
35. `ebay_market_price("rick owens ramones", ["ramones", "ramon"], ["drkshdw", "canvas", "low"], "tok")` against `search_ramones.json` returns `900.0` (median of 700, 900, 1000).
36. The same call sends one GET to `https://api.ebay.com/buy/browse/v1/item_summary/search` with `params == {"q": "rick owens ramones", "limit": 50}`, header `Authorization: Bearer tok`, and header `X-EBAY-C-MARKETPLACE-ID: EBAY_US`.
37. With `keywords=["ramones"]` and the same excludes, the result is `800.0` (median of 700 and 900), showing an even-count median and that keyword matching is case-insensitive on the title (`RAMONES` in row 2 matches).
38. With `keywords=["ramon"]` and `exclude_keywords=[]`, the result is `700.0` (median of 700, 900, 300, 250, 1000), showing that excludes are what remove rows 3 and 4.
39. A listing matching both an include and an exclude keyword is excluded (row 3 contains `ramones` and `low`).
40. With `keywords=["geobasket"]` and `exclude_keywords=["geobasket"]`, the result is `None`.
41. Against `search_empty.json` (no `itemSummaries` key), the result is `None`.
42. Against a response whose `itemSummaries` is `[]`, the result is `None`.
43. `refresh_market_prices()` with the test catalog requests exactly one token, then one search per catalog item, stores `900.0` as the Ramones item's `market_price_usd`, and leaves the `pod shorts` item at `300.0` because no listing contains `pod short`.
44. `ebay_pricing.main()` requests exactly one token per run (one POST to the token URL in `fake_http.calls`).

### Yahoo scrape (happy path only)

45. With `seeded_db`, `fake_http` serving `brand_page_minimal.html` for `RICK_OWENS_URL`, and `sent_emails`, calling `yahoo_jp_scrape(<ramones catalog entry>, RICK_OWENS_URL)` inserts exactly two rows into `listings`: the `t1000000001` and `t1000000004` URLs with `price_yen` 50000 and 100000.
46. The same call records one email with subject `Daily Refresh on rick owens ramones` whose body contains the `t1000000001` link before the `t1000000004` link (sorted by discount, largest first) and contains `61.3% cheaper than market` and `22.5% cheaper than market`.
47. Calling it a second time inserts no new rows into `listings`.

### Live fixtures

48. `tests/fixtures/yahoo/brand_page_live.html` parsed with BeautifulSoup yields at least one element for each of the four selectors used in `src/yahoo_scrape.py:32-35`.
    This test is marked as a strict expected failure (`xfail(strict=True)`) whose reason names RC-07, because Yahoo's page no longer matches these selectors (see Amendment 1).
49. `tests/fixtures/ebay/search_live.json` has an `itemSummaries` list where every entry has a string `title`, every entry that has `price` has a `price.value` that `float()` accepts and `price.currency == "USD"`, and no entry has a `seller` key.
53. In `tests/fixtures/yahoo/brand_page_live.html`, the embedded `<script id="__NEXT_DATA__">` JSON is present and parses, and no object in it that has an `auctionId` key also has a `seller` key.
50. If either live file is missing, its test fails (not skips) with a message telling the user to run `python3 scripts/capture_fixtures.py`.

### Documentation

51. `.env.example` lists `EBAY_CLIENT_ID` and `EBAY_CLIENT_SECRET`, and shows `RAINCHECK_DB_PATH` and `RAINCHECK_DRY_RUN` commented out with a one-line explanation each.
52. `README.md` has a "Configuration" section describing both variables, including the warning that dry run still writes to the database, and a "Running tests" section with the two commands from criterion 1.

## Edge cases

- **Empty results:** eBay with no `itemSummaries` key or an empty list returns `None` (criteria 41, 42).
  A Yahoo page with no matching cards is not tested here, because today it still sends an empty email, which RC-15 changes.
- **`None` or missing values:** `get_market_price` returns `None` for a `NULL` price (criterion 32), and `yahoo_jp_scrape` returns early in that case (existing behavior, not newly tested).
  Unknown brand or item names in `get_item_id` crash today with `TypeError`; that is a known issue and deliberately not pinned by a test, so whoever fixes it does not have to delete a test first.
- **Network failure or bad HTTP status:** a token request returning 401 raises `requests.HTTPError` (criterion 34).
  Search failures, timeouts, and retries are not pinned, because RC-10 changes them.
  Accidental real network access fails loudly (criterion 2).
- **Malformed input:** malformed Yahoo HTML belongs to RC-07.
  A malformed `catalog.json` makes `load_catalog` raise `json.JSONDecodeError`, unchanged from today, and is not tested here.
  `RAINCHECK_DB_PATH` pointing into a missing directory raises `sqlite3.OperationalError` and is not tested here.
- **Duplicates:** same-URL listings are deduplicated and not updated (criterion 26); repeated seeding does not duplicate rows (criterion 24); a second Yahoo run inserts nothing new (criterion 47).
- **Environment typos:** `RAINCHECK_DRY_RUN` with an unrecognized value means dry run (criterion 20).

## Test plan

| File | Covers | Fixtures used |
| --- | --- | --- |
| `tests/test_config.py` | Criteria 2, 3, 4, 6-9, 11-12, 15-16 | autouse, `temp_db`, `monkeypatch`, `tmp_path` |
| `tests/test_db.py` | Criteria 22-32 | `temp_db`, `seeded_db`, `db_conn`, `test_catalog` |
| `tests/test_sendemail.py` | Criteria 17-21 | `monkeypatch`, `capsys` |
| `tests/test_ebay_pricing.py` | Criteria 13, 33-44 | `fake_http`, `seeded_db`, `token.json`, `search_ramones.json`, `search_empty.json` |
| `tests/test_yahoo_scrape.py` | Criteria 14, 45-47 | `fake_http`, `seeded_db`, `sent_emails`, `brand_page_minimal.html` |
| `tests/test_live_fixtures.py` | Criteria 48-50 | `brand_page_live.html`, `search_live.json` |

Criteria 1, 5, 10, 51, and 52 are checked by the reviewer or verifier rather than by a test.

Each test name should include the criterion number it covers, for example `test_c26_duplicate_url_returns_false_and_keeps_original_row`.

All eBay fixtures are built to match the official Browse API OpenAPI spec (version 1.20.4, `SearchPagedCollection` and `ItemSummary`) and the client credentials grant sample response, both confirmed on 2026-10-26.
Notable facts from those sources: `price.value` is a string; `limit` defaults to 50 with a maximum of 200; `currentBidPrice` is only returned for auction items; and the documentation does not promise that `price` is present on every item summary.

## Shared-file conflicts

Yes.
This touches `src/db.py` and reads (but does not modify) `src/schema.sql` and `src/catalog.json`.
It must not run in parallel with any other item that touches those files.
It also touches all four modules under `src/` and `README.md`, which RC-02 also edits, so in practice nothing should run in parallel with RC-01.

## Known issues found (not fixed here)

| Issue | Where | Owner |
| --- | --- | --- |
| Titles, prices, links, and images are zipped as four independent lists, so a mismatch pairs data from different listings. | `src/yahoo_scrape.py:32-40` | RC-07 |
| The Yahoo response status is never checked; an error page is parsed as if it were results. | `src/yahoo_scrape.py:29-30` | RC-07 or RC-10 |
| No network call has a timeout (Yahoo GET, eBay token POST, eBay search GET, SMTP connection), which violates the convention in `CLAUDE.md`. | `src/yahoo_scrape.py:29`, `src/ebay_pricing.py:22,40`, `src/sendemail.py:28` | RC-10 |
| `send_email` catches every exception and only prints it, so a failed send looks like a successful run. | `src/sendemail.py:34-35` | RC-10 |
| The same Yahoo page is downloaded once per catalog item instead of once per run. | `src/yahoo_scrape.py:29,96-97` | Not owned; suggest adding to RC-07 or RC-13 |
| An email is sent for every item even when there are no deals. | `src/yahoo_scrape.py:86` | RC-15 |
| Exclude keywords are ignored by the Yahoo scraper. | `src/yahoo_scrape.py:47` | RC-03 |
| Seeding uses `INSERT OR IGNORE`, so catalog edits never reach the database. | `src/db.py:68` | RC-03 |
| The `pod shorts` item has Ramones keywords. | `src/catalog.json:27-31` | RC-03 |
| The eBay query uses the item name, not the stored `search_query`, which is never read anywhere. | `src/ebay_pricing.py:78` | RC-03 |
| Seeding hardcodes `yahoo_brand_id = "103538"`, which is `UNIQUE`; a second brand is silently ignored and the next `SELECT` then crashes with `TypeError`. | `src/db.py:58-64` | RC-13 |
| `get_item_id` and `get_market_price` crash with `TypeError: 'NoneType' object is not subscriptable` for an unknown brand or item. | `src/db.py:113-120` | Not owned; suggest RC-03, which reworks item lookup |
| The debug print loop indexes `item["price"]["value"]` for every result, but the Browse API does not guarantee `price` on every item summary, so one such item crashes pricing. | `src/ebay_pricing.py:64-65` | RC-09 |
| Keyword matching is plain substring matching, so the exclude keyword `low` also removes titles containing `yellow` or `follow`; keywords are also not lowercased, so an uppercase keyword in the catalog would never match. | `src/ebay_pricing.py:51-56`, `src/yahoo_scrape.py:47` | RC-09 (eBay) and RC-03 (shared keyword handling) |
| A `market_price_usd` of `0` causes `ZeroDivisionError`. | `src/yahoo_scrape.py:55` | RC-03 (validation on upsert) |
| The yen rate is hardcoded at `0.0062`. | `src/yahoo_scrape.py:54` | RC-08 |
| `seed_from_catalog` is not reachable from any command; the `__main__` block in `db.py` sits in the middle of the file and only runs `init_db`. | `src/db.py:45-46` | RC-02 (documents the seed step) or RC-14 |
| `README.md` describes `ebay_scrape.py` and per-item JSON files that no longer exist and does not mention the database or eBay credentials. | `README.md` | RC-02 |
| `setup.py` reads and writes `catalog.json` relative to the working directory and hardcodes Ramones keywords. | `src/setup.py:14,38,47` | RC-14 |
| `.env.example` is missing `EBAY_CLIENT_ID` and `EBAY_CLIENT_SECRET`. | `.env.example` | Fixed here (criterion 51) |
| `ebay_pricing.py` requests an OAuth token twice per run. | `src/ebay_pricing.py:92-94` | Fixed here as a side effect of `main()` (criterion 44) |

## Open questions

**Resolved 2026-10-26:** the user approved the spec as one PR and chose the recommended option for every question below.
Live fixtures use Option A (capture script), the dry-run switch is `RAINCHECK_DRY_RUN` (environment variable only), the database path variable is `RAINCHECK_DB_PATH`, and network blocking uses the hand-rolled `_block_network` fixture.

1. **How do live fixtures get into `tests/fixtures/`?**
   - Option A (recommended): the implementer writes `scripts/capture_fixtures.py` and all hand-written fixtures.
     Before the review stage, the user runs `python3 scripts/capture_fixtures.py` once in the worktree (with `src/.env` copied in, or credentials exported in the shell) and the two captured files are committed in the same PR.
     All behavioral assertions use the hand-written fixtures, so they are deterministic and their expected numbers are obvious; the live files only prove the hand-written ones match reality (criteria 48-49).
     Tradeoff: one manual step for the user, and the Yahoo page adds roughly 0.5 to 2 MB to the repository each time it is refreshed.
   - Option B: hand-written fixtures only; defer live capture to RC-07 (Yahoo) and RC-09 (eBay).
     Tradeoff: smaller PR and no manual step, but nothing checks that the hand-written fixtures still look like the real sites, and RC-07 needs a real page anyway.
   - Option C: the user captures fixtures before the test stage with commands given in the spec.
     Tradeoff: the capture logic is not saved in the repository, so refreshing fixtures later means reconstructing it.
2. **Dry-run switch name and form.**
   Recommended: an environment variable only, `RAINCHECK_DRY_RUN`, with the fail-safe parsing above.
   An environment variable works the same for `yahoo_scrape.py`, a future Lambda handler (RC-11), and tests, and it needs no argument parsing.
   Alternative: also add a `--dry-run` flag to `yahoo_scrape.py`.
   Tradeoff: slightly nicer to type, but two switches for one behavior, and the flag would have to be added again for every entry point.
   Naming alternative: `RAINCHECK_EMAIL_DRY_RUN`, which is more precise because database writes still happen.
   Recommendation is still `RAINCHECK_DRY_RUN`, documented clearly, because it is shorter and later items may extend it to other side effects.
3. **Database path variable name.**
   Recommended: `RAINCHECK_DB_PATH`, defaulting to `src/raincheck.db` resolved from the location of `db.py`.
   The `RAINCHECK_` prefix avoids clashing with other tools' variables.
4. **Network blocking: hand-rolled guard or the `pytest-socket` plugin?**
   Recommended: the hand-rolled `_block_network` fixture (about ten lines), because it adds no dependency and is easy to read and explain.
   Alternative: `pytest-socket` with `--disable-socket`, which is a maintained, well-known plugin and also blocks sockets during collection.
   Tradeoff: one more pinned dependency, in exchange for slightly broader coverage.
5. **Should this be one PR?**
   Recommended: yes.
   Source changes are small (about 60 lines across four files); most of the diff is tests and fixtures, which are easy to review.
   If the user prefers smaller PRs, split into RC-01a (configuration: `get_db_path`, `load_catalog`, dry run, import-safe credentials, `main()` functions, pytest harness, and criteria 1-21) and RC-01b (fixtures, capture script, and criteria 22-52).

## Rollback

All source changes are backward compatible: with `RAINCHECK_DB_PATH` and `RAINCHECK_DRY_RUN` unset, the scripts open the same database and send the same email as before.
To roll back, revert the merge commit.
No data migration is involved, and existing `raincheck.db` files are untouched.

## Amendments

### Amendment 1 (2026-10-26, approved by the user): Yahoo page redesign

Verification found that Yahoo Auctions now serves a Next.js page.
Listing data lives in an embedded `<script id="__NEXT_DATA__">` JSON block, and the `Product__*` classes used by `src/yahoo_scrape.py:32-35` no longer exist, so the live scraper finds zero listings.
That is a bug in today's scraper, owned by RC-07, not by this item.

- Criterion 48 stays, but is marked `xfail(strict=True)` with a reason naming RC-07.
  Strict means it fails the suite if it unexpectedly passes, so RC-07 must update it.
- Each listing object in `__NEXT_DATA__` carries a `seller` object with the seller's user ID, display name, and city.
  The capture script removes `seller` from every object that has an `auctionId` key before writing the page, matching how the eBay capture strips seller data.
  The page is otherwise written unchanged, with the JSON block re-serialized in place.
- New criterion 53 checks that no seller data reaches the committed Yahoo fixture.

