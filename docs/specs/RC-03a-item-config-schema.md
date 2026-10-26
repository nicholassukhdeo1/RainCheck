# RC-03a: Item configuration in the database (schema, migration, upsert seeding)

**Status:** Approved
**Backlog item:** RC-03 in `docs/BACKLOG.md` (first half of a proposed split; see Open question 1)

## Summary

Make the database a complete and correct copy of the item configuration: store exclude keywords, separate the catalog's manual price from the eBay-derived price, and turn seeding into a validated, atomic upsert that propagates every `catalog.json` edit.
Add a versioned, tested migration so the user's existing `raincheck.db` is upgraded in place without losing listings or eBay prices, plus a read API (`get_tracked_items`) that RC-03b will switch both jobs onto.

## Motivation

There are two sources of truth for item configuration today, and they disagree.

- `seed_from_catalog` (`src/db.py:74-107`) uses `INSERT OR IGNORE` for items and keywords, so an edit to an existing item's price, search query, or keywords in `catalog.json` never reaches the database.
  Removed keywords are never deleted either.
  The last committed copy of the user's database (git blob `d632e3c`) shows the result: both items still carry the keyword `high top`, which `catalog.json` dropped long ago, and `pod shorts` still has `ramones` and `ramon`.
- Exclude keywords exist only in `catalog.json` (`src/catalog.json:7-11,19-21`); the schema has nowhere to store them (`src/schema.sql:17-21`).
- `item_keywords` is written but never read; `src/yahoo_scrape.py:165` and `src/ebay_pricing.py:75` read keywords from `catalog.json`.
- `items.market_price_usd` has two writers that overwrite each other: seeding writes the catalog's manual price, and `update_market_price` (`src/db.py:124-132`) writes the eBay median.
  `market_price_source` defaults to `'manual'` and nothing ever changes it, so it is wrong for every eBay-priced row.
  In blob `d632e3c`, `rick owens ramones` has `market_price_usd = 420.0` with source `'manual'`, while `catalog.json` says 800.0, so 420.0 can only have come from eBay.
  A naive upsert of the catalog price would silently replace that eBay price with 800.0 on every seed.
- Nothing validates the catalog.
  A `market_price` of `0` reaches `src/yahoo_scrape.py:172` as a divisor and raises `ZeroDivisionError`; a missing `exclude_keywords` key crashes `src/ebay_pricing.py:75` with `KeyError` (README line 91).
- `get_item_id` and `get_market_price` (`src/db.py:134-156`) raise `TypeError: 'NoneType' object is not subscriptable` for an unknown brand or item.
- Keywords are stored as typed, so an uppercase keyword never matches the lowercased title.
- The `pod shorts` entry carries Ramones keywords (`src/catalog.json:22-26`), so it can never match a pod shorts listing on eBay or Yahoo.

There is no schema versioning at all (`PRAGMA user_version` is 0 in blob `d632e3c`), and `init_db` only runs `CREATE TABLE IF NOT EXISTS`, which cannot add a column to an existing table.
So any schema change, here and in RC-06 and RC-13, first needs a safe migration path.

## Scope

**In scope:**

- Schema version 1: exclude keywords (`item_keywords.kind`), a separate catalog-owned `items.manual_price_usd`, an `items.active` flag, and removal of the misleading `items.market_price_source`.
- A small migration framework in `src/db.py` based on `PRAGMA user_version`, with migration 0 to 1, run by `python3 src/db.py` (with or without `--seed`).
- `get_connection()` refuses to open a database whose schema version is not current, with a message naming the command that fixes it.
- Catalog validation (`validate_catalog`) and a transactional upsert (`seed_from_catalog`) that propagates edits, replaces keyword sets, deactivates items removed from the catalog, and never touches eBay-owned data.
- Keyword normalization (strip and lowercase) at write time.
- `get_tracked_items()` read API returning active items with keywords, excludes, and prices.
- `get_item_id` and `get_market_price` return `None` for unknown names; `get_market_price` returns the effective price (eBay if valid, else manual); `update_market_price` validates its input and reports whether a row changed.
- `db.main` prints catalog and schema errors cleanly and exits 1.
- Fix `src/catalog.json`: `pod shorts` keywords, and `search_query` values as plain text.
- Tests, a legacy-database fixture built from the pre-RC-03 schema in git history, and updates to tests whose pinned behavior this changes.
- README "Database" section update, and the setup.py note in "Usage".
- A verifier rehearsal (Phase B) and a backup-first upgrade procedure for the user's real database (Phase C).

**Out of scope (RC-03b, see "Proposed split"):**

- Switching `src/yahoo_scrape.py` and `src/ebay_pricing.py` to read configuration from the database.
  In RC-03a both jobs keep reading keywords from `catalog.json`; their source files are not modified.
- Applying exclude keywords in the Yahoo job.
- Querying eBay with `search_query` instead of the item name.
- A shared keyword-matching function, and the brand lookup crash at `src/yahoo_scrape.py:147-150`.

**Out of scope (other items):**

- Word-boundary keyword matching (`low` matching `yellow`) and full-width or NFKC normalization of titles (proposed new item RC-17).
- Storing a brand's Yahoo ID per brand; seeding still uses `103538` (RC-13).
  A second brand now fails with a clear `CatalogError` instead of a `TypeError`.
- Changing `src/setup.py` (RC-14).
- Storing `size` (accepted in the catalog, ignored).
- Automatic backups made by the code before migrating (see Open question 6).
- Market price history (RC-06), listing matches (RC-04), logging (RC-10).

## Proposed split

RC-03 as written in the backlog is one schema change plus two job rewrites.
One PR containing all of it would mix a data migration that must be reviewed very carefully with two unrelated behavior changes in the jobs.

- **RC-03a (this spec):** everything that touches the shared files `src/schema.sql`, `src/db.py`, and `src/catalog.json`.
  After it lands, the database holds the full, correct configuration, the user's database is migrated, and nothing the jobs do changes (Yahoo deal math still sees the same price for every item; see criterion 41).
- **RC-03b (backlog text below):** both jobs read from `db.get_tracked_items()` only, Yahoo applies exclude keywords, eBay uses `search_query`, and keyword matching becomes one shared function.
  It touches only `src/yahoo_scrape.py`, `src/ebay_pricing.py`, one new module, tests, and the README, none of which are shared files.

## Design

### Files touched

| File | Change |
| --- | --- |
| `src/schema.sql` | Schema version 1 (below). Comment at the top saying the file describes the latest version and that existing databases are upgraded by `MIGRATIONS` in `db.py`. |
| `src/db.py` | `SCHEMA_VERSION`, `MIGRATIONS`, `SchemaVersionError`, `CatalogError`, `CatalogEntry`, `TrackedItem`, `SeedResult`; rewritten `init_db`, `seed_from_catalog`; new `validate_catalog`, `get_tracked_items`; changed `get_connection`, `get_item_id`, `get_market_price`, `update_market_price`, `main`. |
| `src/catalog.json` | `pod shorts` keywords; both `search_query` values as plain text (Open questions 7 and 8). |
| `tests/conftest.py` | Add the `legacy_db` fixture. Existing fixtures are unchanged. |
| `tests/fixtures/db/schema_v0.sql` (new) | Byte-for-byte copy of `git show 8a7f409:src/schema.sql`. |
| `tests/fixtures/db/legacy_v0_rows.sql` (new) | Rows mirroring blob `d632e3c` (below). |
| `tests/fixtures/catalog.json` | `search_query` values become `"rick owens ramones"` and `"pod shorts"` (a `+` is now rejected). |
| `tests/test_migration.py` (new) | Criteria 1-12. |
| `tests/test_seed_catalog.py` (new) | Criteria 13-29. |
| `tests/test_db.py` | New criteria 30-37; updates listed under "Superseded tests". |
| `tests/test_db_cli.py`, `tests/test_config.py`, `tests/test_ebay_pricing.py`, `tests/test_yahoo_scrape.py` | Updates listed under "Superseded tests"; criteria 38-40 in `test_db_cli.py`. |
| `README.md` | "Database" section and the `setup.py` note in "Usage" (criteria 43-44). |

Not modified: `src/yahoo_scrape.py`, `src/ebay_pricing.py`, `src/setup.py`, `src/sendemail.py`.

### Schema changes and migration

#### Schema version 1 (`src/schema.sql`)

Only `items` and `item_keywords` change; `brands`, `listings`, `listing_matches`, and both indexes are unchanged.
Column order matches what the migration produces (SQLite appends added columns and closes the gap of a dropped one), so a fresh and a migrated database have identical `PRAGMA table_info`.

```sql
CREATE TABLE IF NOT EXISTS items (
    id                INTEGER PRIMARY KEY,
    brand_id          INTEGER NOT NULL REFERENCES brands(id),
    name              TEXT NOT NULL,
    market_price_usd  REAL,
    search_query      TEXT,
    manual_price_usd  REAL CHECK (manual_price_usd IS NULL OR manual_price_usd > 0),
    active            INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    UNIQUE (brand_id, name)
);

CREATE TABLE IF NOT EXISTS item_keywords (
    item_id  INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    keyword  TEXT NOT NULL,
    kind     TEXT NOT NULL DEFAULT 'include' CHECK (kind IN ('include', 'exclude')),
    PRIMARY KEY (item_id, keyword)
);
```

Column ownership after this change, which is the rule that stops seeding from clobbering eBay prices:

| Column | Meaning | Only writer |
| --- | --- | --- |
| `items.manual_price_usd` | Fallback price typed by the user (`market_price` in `catalog.json`). | `seed_from_catalog` |
| `items.market_price_usd` | Latest eBay-derived market price (RC-06 keeps this as the latest value). | `update_market_price` |
| `items.search_query`, `items.active`, `item_keywords` | Item configuration. | `seed_from_catalog` |

The **effective price** used for deal math is `market_price_usd` if it is a finite number greater than 0, else `manual_price_usd` if it is greater than 0, else `None`.
`market_price_source` is dropped because "which column is set" now carries that information exactly.

The primary key `(item_id, keyword)` deliberately allows a keyword to be either include or exclude for an item, never both, because an item that includes and excludes the same word can never match anything.

#### Version tracking

- `SCHEMA_VERSION = 1` in `src/db.py`.
- The version is stored in `PRAGMA user_version`, a 32-bit integer in the database header that SQLite reserves for applications and that changes transactionally.
- Every database created before RC-03a has `user_version = 0` (confirmed on blob `d632e3c`).
- `MIGRATIONS` is a module-level dict mapping a target version to a tuple of SQL statements.
  Migration 1:

  ```python
  MIGRATIONS = {
      1: (
          "ALTER TABLE items ADD COLUMN manual_price_usd REAL CHECK (manual_price_usd IS NULL OR manual_price_usd > 0)",
          "ALTER TABLE items ADD COLUMN active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))",
          "ALTER TABLE item_keywords ADD COLUMN kind TEXT NOT NULL DEFAULT 'include' CHECK (kind IN ('include', 'exclude'))",
          "ALTER TABLE items DROP COLUMN market_price_source",
      ),
  }
  ```

- `ALTER TABLE ... DROP COLUMN` needs SQLite 3.35 or later.
  The user's `.venv` has SQLite 3.49.1 and the system `python3` has 3.53.3.
  `init_db` checks `sqlite3.sqlite_version_info >= (3, 35, 0)` before migrating and raises `SchemaVersionError` naming the version otherwise, before changing anything.
- Migration 1 was prototyped on a copy of blob `d632e3c` during planning with Python 3.13 and SQLite 3.49.1: it succeeds, preserves every row, and a failure injected after the last `ALTER` rolls back to the exact original schema with `user_version` 0.

#### How `init_db()` decides what to do

`init_db()` opens its own connection in autocommit mode (`isolation_level=None`) with foreign keys on, so it controls transactions explicitly.
It never uses `executescript` on an existing database, because `executescript` commits any open transaction first.

| State of the file at `get_db_path()` | Action |
| --- | --- |
| Missing, or no user tables (`sqlite_master` has no `type='table'` row whose name does not start with `sqlite_`) | Create everything from `src/schema.sql` and set `user_version = SCHEMA_VERSION`, atomically. |
| `user_version == SCHEMA_VERSION` | Nothing to do. |
| `0 < user_version < SCHEMA_VERSION` | Apply each pending migration in order. |
| `user_version == 0` and the legacy shape (all five tables exist, `items` has `market_price_source` and no `manual_price_usd`) | Apply migrations 1 to `SCHEMA_VERSION`. |
| `user_version == 0` with any other set of tables | Raise `SchemaVersionError` ("unrecognized database shape; not migrating"), change nothing. |
| `user_version > SCHEMA_VERSION` | Raise `SchemaVersionError` ("created by a newer RainCheck"), change nothing. |

Each migration runs as one transaction: `BEGIN IMMEDIATE`, every statement in order, `PRAGMA user_version = <target>`, `COMMIT`.
Any exception triggers `ROLLBACK` and is re-raised unchanged, so the database is exactly as it was before that migration.
`BEGIN IMMEDIATE` takes the write lock up front, so a job running at the same time makes the migration fail with `database is locked` instead of interleaving.

Output:

- After each applied migration: `Migrated database at <path> from schema version <n-1> to <n>.`
- Always last, unchanged from RC-01: `Database initialized at <path>.`

#### What happens to existing data (migration 0 to 1)

| Data | After migration |
| --- | --- |
| `brands`, `listings`, `listing_matches` | Every row and column unchanged. |
| `items.id`, `brand_id`, `name`, `market_price_usd`, `search_query` | Unchanged. In particular every existing `market_price_usd` is preserved in place (Open question 3). |
| `items.market_price_source` | Dropped. In every existing database it holds `'manual'`, which carries no information. |
| `items.manual_price_usd` | `NULL` until the next `--seed` fills it from `catalog.json`. Until then the effective price is the preserved `market_price_usd`, exactly what the jobs use today. |
| `items.active` | `1` for every row. |
| `item_keywords` | Every row kept, with `kind = 'include'` (exclude keywords never reached the database before). Stale keywords disappear at the next `--seed`. |

Migration does not read `catalog.json`.
Seeding is a separate, explicit step, so the user can inspect the migrated database before any configuration changes.

#### Migration trigger

Migration runs only when the user runs `python3 src/db.py` or `python3 src/db.py --seed`.
The jobs never migrate: `get_connection()` checks `user_version` and raises `SchemaVersionError` if it is not `SCHEMA_VERSION`, with a message that includes the database path, the found and expected versions, and the instruction `back up the database, then run python3 src/db.py`.
For a database with no tables the message says to run `python3 src/db.py --seed`.
This means a job can never run against a half-understood schema, and a migration never happens without the user deciding to run it (Open question 6).

### Interfaces

All in `src/db.py`.

```python
SCHEMA_VERSION = 1
MIGRATIONS: dict[int, tuple[str, ...]]

class SchemaVersionError(RuntimeError): ...
class CatalogError(ValueError): ...

@dataclass(frozen=True)
class CatalogEntry:
    brand: str
    name: str
    search_query: str | None
    manual_price_usd: float | None
    keywords: tuple[str, ...]          # normalized, de-duplicated, catalog order
    exclude_keywords: tuple[str, ...]  # normalized, de-duplicated, catalog order

@dataclass(frozen=True)
class TrackedItem:
    id: int
    brand_id: int
    brand: str
    name: str
    search_query: str | None
    keywords: tuple[str, ...]          # sorted
    exclude_keywords: tuple[str, ...]  # sorted
    manual_price_usd: float | None
    market_price_usd: float | None

    @property
    def effective_price_usd(self) -> float | None: ...

@dataclass(frozen=True)
class SeedResult:
    added: tuple[tuple[str, str], ...]        # (brand, name), catalog order
    updated: tuple[tuple[str, str], ...]      # existing items present in the catalog, catalog order
    deactivated: tuple[tuple[str, str], ...]  # by items.id

def get_connection() -> sqlite3.Connection
def init_db() -> None
def validate_catalog(entries: object) -> list[CatalogEntry]
def seed_from_catalog(catalog_path=None) -> SeedResult
def get_tracked_items() -> list[TrackedItem]
def get_item_id(brand_name: str, item_name: str) -> int | None
def get_market_price(brand_name: str, item_name: str) -> float | None
def update_market_price(item_id: int, price: float) -> bool
def main(argv=None) -> None
```

`get_db_path`, `load_catalog`, and `insert_listing` are unchanged.
`get_connection` remains the only function that returns a connection to callers, and `sqlite3.connect` still appears only in `src/db.py`.

#### `validate_catalog(entries)`

Pure function, no database access.
Collects every problem in the whole catalog, then raises one `CatalogError` whose message starts with `Catalog is invalid:` followed by one line per problem.
Entry-level lines look like `- entry 2 ("pod shorts"): market_price must be a number greater than 0`, where the position is 1-based and the quoted name is omitted if the name itself is invalid.

Rules:

| Input | Rule |
| --- | --- |
| Top level | Must be a non-empty list. `[]` is rejected, because syncing an empty catalog would deactivate every item (Open question 9). |
| Entry | Must be an object. |
| Keys | Allowed: `brand`, `item name`, `size`, `search_query`, `market_price`, `keywords`, `exclude_keywords`. Any other key is an error, so a typo such as `exclude_keyword` cannot silently become "no excludes". |
| `brand`, `item name` | Required strings, non-blank after `strip()`; stored stripped. |
| `keywords` | Required list with at least one keyword. |
| `exclude_keywords` | Optional; missing means `[]`. If present, a list. |
| Each keyword (both lists) | A string, non-blank after `strip()`. Normalized with `strip()` then `lower()` (the same `lower()` the jobs apply to titles). Duplicates after normalization are kept once, first occurrence wins. |
| Both lists together | A normalized keyword may not appear in both lists. |
| `market_price` | Optional; missing or `null` means no manual price. Otherwise an `int` or `float`, not a `bool`, finite (Python's `json` module accepts `NaN` and `Infinity`), and greater than 0. Stored as `float`. |
| `search_query` | Optional; missing or `null` means none. Otherwise a string that is non-blank after `strip()`, at most 100 characters after `strip()` (eBay truncates longer `q` values), with no `+` and no `*`. Stored stripped. |
| `size` | Accepted and ignored. |
| Uniqueness | No two entries may share the same stripped `(brand, item name)`. |

#### `seed_from_catalog(catalog_path=None)`

1. `load_catalog(catalog_path)`; `FileNotFoundError` and `json.JSONDecodeError` propagate unchanged.
2. `validate_catalog(...)`; on `CatalogError` nothing has been opened or written.
3. Open `get_connection()` (raises `SchemaVersionError` on an outdated database) and run everything below in one transaction; any exception rolls the whole seed back and is re-raised.
4. Brands: insert `(brand, '103538')` if no brand with that name exists.
   If that insert violates the `yahoo_brand_id` uniqueness (a second brand), raise `CatalogError` saying only one brand is supported until RC-13, naming the brand.
5. Items: `INSERT ... ON CONFLICT (brand_id, name) DO UPDATE` setting `manual_price_usd`, `search_query`, and `active = 1`.
   `market_price_usd` is never written by seeding, on insert or on update.
6. Keywords: for each catalog item, `DELETE FROM item_keywords WHERE item_id = ?`, then insert every include keyword with `kind = 'include'` and every exclude keyword with `kind = 'exclude'`.
7. Deactivate: every item with `active = 1` whose id was not upserted in this run gets `active = 0`.
   Its prices, keywords, and any rows referencing it are untouched.
   Items are never deleted, because `listing_matches` (RC-04) and price history (RC-06) reference `items.id` and `listing_matches` cascades on delete (Open question 4).
8. Commit, then print `Catalog seeded.` (unchanged, RC-02 criterion 7) followed by one line per deactivated item: `Deactivated item no longer in catalog.json: <brand> / <name>`.
9. Return a `SeedResult`.

Brands that no longer appear in the catalog are left as they are.

#### `get_tracked_items()`

Returns every item with `active = 1`, ordered by `items.id` (insertion order, which matches catalog order for a fresh seed and keeps today's per-item email order), with its brand name and keyword sets.
Items without exclude keywords have `exclude_keywords == ()`.
`effective_price_usd` applies the effective price rule above.
RC-03a adds it with tests but no production caller; RC-03b switches both jobs to it.

#### Lookups and price updates

- `get_item_id(brand, name)` returns the id regardless of `active`, or `None` when the brand or item does not exist.
- `get_market_price(brand, name)` returns the effective price, or `None` when the item does not exist or has no usable price.
  The deal math in `src/yahoo_scrape.py:172` therefore never receives 0 or a negative number from the database, which removes the `ZeroDivisionError` without touching that file.
- `update_market_price(item_id, price)` raises `ValueError` unless `price` is an `int` or `float`, not a `bool`, finite, and greater than 0, before opening the database.
  It updates only `market_price_usd`, returns `True` if a row changed and `False` if no item has that id (today's silent no-op, now observable), and prints `Market price updated!` only when it returns `True`.
  `src/ebay_pricing.py` ignores the return value, so it is unaffected.

#### `main(argv=None)`

Unchanged from RC-02, except that `CatalogError` and `SchemaVersionError` are caught, printed to stderr as `Error: <message>`, and turned into `SystemExit(1)`.
`FileNotFoundError` and `json.JSONDecodeError` from loading the catalog still propagate (RC-02 criterion 12).

### `src/catalog.json`

- `rick owens ramones`: `search_query` becomes `"rick owens ramones"`; keywords, excludes, and price unchanged.
- `pod shorts`: `keywords` becomes `["pod short", "ポッドショーツ"]` (the user's choice, Open question 8) and `search_query` becomes `"rick owens pod shorts"` (both subject to Open questions 7 and 8).
- In RC-03a `search_query` is stored but still not used by any job; RC-03b makes eBay use it.

### Superseded tests

These tests pin behavior that RC-03a deliberately changes, and the test-writer updates them as described.

| Test | Old assertion | New assertion |
| --- | --- | --- |
| `test_db.py::test_c23_...` | `market_price_usd` 800 and 300 after seeding; keywords from all rows | `manual_price_usd` 800.0 and 300.0, `market_price_usd` `NULL`, `search_query` from the updated fixture; include and exclude keyword sets by `kind` |
| `test_db.py::test_c24_...` | `item_keywords` count 3 | count 7 (3 include, 4 exclude), unchanged by a second seed |
| `test_db.py::test_c31_...` | `pod shorts` `market_price_usd` stays 300.0 | stays `NULL`, and its `manual_price_usd` stays 300.0 |
| `test_db.py::test_c32_...` | `NULL` market price gives `None` | covered by criterion 33 (manual fallback, then `None` when both are missing) |
| `test_db_cli.py::test_rc02_c6_...` | items with `market_price_usd` and `+` queries; keyword list | `manual_price_usd`, plain-text queries, keywords with `kind` |
| `test_db_cli.py::test_rc02_c8_...` | 3 keyword rows | 7 keyword rows |
| `test_config.py::test_c09_...` | calls `get_connection()` on two new files | calls `init_db()` for each path, then `get_connection()` |
| `test_ebay_pricing.py::test_c43_...` | `pod shorts` `market_price_usd` 300.0 | `pod shorts` `market_price_usd` `NULL` and `get_market_price` returns 300.0 |
| `test_yahoo_scrape.py::test_rc07_c25_...` | sets `market_price_usd` to `NULL` to skip the item | sets both price columns to `NULL` |

## Acceptance criteria

Criteria 1-40 are checked by pytest, 41-44 by the reviewer, 45-50 by the verifier in Phase B, and 51 by the orchestrator in Phase C.

**Fresh database and version checks** (`tests/test_migration.py`)

1. `init_db()` on a path that does not exist creates the five tables, sets `PRAGMA user_version` to 1, gives `items` exactly the columns `id, brand_id, name, market_price_usd, search_query, manual_price_usd, active` in that order and `item_keywords` exactly `item_id, keyword, kind`.
2. On a fresh database, inserting an item with `manual_price_usd` of `0` or `-1`, or `active = 2`, or a keyword with `kind = 'other'`, raises `sqlite3.IntegrityError`; an item inserted without `active` gets 1, and a keyword inserted without `kind` gets `'include'`.
3. Running `init_db()` twice leaves `user_version` 1 and every table's rows unchanged.
4. `get_connection()` raises `SchemaVersionError` whose message contains `python3 src/db.py` when the database file does not exist, when it is a legacy version 0 database (and `user_version` is still 0 afterwards), and when `user_version` is 2; it returns a working connection when `user_version` is 1.

**Migration** (`tests/test_migration.py`, using `legacy_db`)

5. After `init_db()` on `legacy_db`, `user_version` is 1, and for every table `PRAGMA table_info`, `PRAGMA index_list` (names), and `PRAGMA foreign_key_list` equal those of a freshly created database.
6. After migration, every row of `brands`, `listings`, and `listing_matches` is identical (all columns) to before; `items` keeps `id`, `brand_id`, `name`, `market_price_usd` (420.0 and 300.0), and `search_query`, with `manual_price_usd` `NULL` and `active` 1; `item_keywords` has the same `(item_id, keyword)` pairs as before, all with `kind = 'include'`.
7. After migration, `PRAGMA integrity_check` returns `ok` and `PRAGMA foreign_key_check` returns no rows.
8. A second `init_db()` on the migrated database prints no `Migrated` line and leaves every table identical.
9. With `MIGRATIONS[1]` extended (via `monkeypatch`) by the statement `INSERT INTO no_such_table VALUES (1)`, `init_db()` on `legacy_db` raises `sqlite3.OperationalError`, and afterwards `user_version` is 0, `items` still has `market_price_source` and no `manual_price_usd`, `item_keywords` has no `kind`, and every row of every table equals the pre-migration rows.
10. Migration stdout contains `Migrated database at <path> from schema version 0 to 1.` before `Database initialized at <path>.`
11. With `user_version` set to 2 on a fresh version 1 database, `init_db()` raises `SchemaVersionError` and the file's SHA-256 is unchanged.
12. A database with `user_version` 0 and one table `foo`, and a legacy database whose `items` table has had `market_price_source` removed by hand, each make `init_db()` raise `SchemaVersionError` with the file's SHA-256 unchanged.

**Seeding** (`tests/test_seed_catalog.py`; catalogs other than the fixture are written to `tmp_path`)

13. `seeded_db` with the updated test catalog has one brand `('Rick Owens', '103538')`; items `rick owens ramones` with `manual_price_usd` 800.0, `market_price_usd` `NULL`, `search_query` `rick owens ramones`, `active` 1, and `pod shorts` with 300.0, `NULL`, `pod shorts`, 1; include keywords `{ramones, ramon}` and `{pod short}`; exclude keywords `{drkshdw, canvas, low}` and `{pants}`.
14. Seeding the same catalog again leaves `brands`, `items`, and `item_keywords` identical (all rows and columns) and returns `SeedResult(added=(), updated=(("Rick Owens", "rick owens ramones"), ("Rick Owens", "pod shorts")), deactivated=())`; the first seed returned both items in `added`.
15. After changing `rick owens ramones` in the catalog to `market_price` 650, `search_query` `ramones high`, `keywords` `["ramones", "geobasket"]`, and `exclude_keywords` `["canvas"]` and reseeding, that item has the same `id`, `manual_price_usd` 650.0, the new `search_query`, include keywords exactly `{ramones, geobasket}`, and exclude keywords exactly `{canvas}`; `pod shorts` is unchanged.
16. After `update_market_price(<ramones id>, 912.5)` and a reseed with `market_price` 650, `market_price_usd` is still 912.5, `manual_price_usd` is 650.0, and `get_market_price("Rick Owens", "rick owens ramones")` returns 912.5.
17. Reseeding with a catalog that omits `pod shorts` leaves its row in place with `active` 0 and unchanged `market_price_usd`, `manual_price_usd`, `search_query`, and keyword rows; a `listing_matches` row referencing it is unchanged; the result's `deactivated` is `(("Rick Owens", "pod shorts"),)`; stdout contains `Deactivated item no longer in catalog.json: Rick Owens / pod shorts`.
18. Reseeding again with the full catalog sets `pod shorts` back to `active` 1 with the same `id`, its `market_price_usd` preserved, and it appears in `updated`, not `added`.
19. Keywords `["  RAMONES ", "ramones", "Ramon"]` are stored as exactly `ramones` and `ramon`.
20. An entry with no `exclude_keywords` key, no `market_price` key, and no `search_query` key seeds with no exclude rows, `manual_price_usd` `NULL`, and `search_query` `NULL`; an entry with `"market_price": null` and `"search_query": null` behaves the same.
21. An entry with `"size": 42` seeds normally, and no column or row stores 42.
22. Each invalid catalog below makes `seed_from_catalog` raise `CatalogError` and leaves every table identical to before (starting from a seeded database):
    `{}` at top level; `[]`; an entry that is a string; missing `brand`; missing `item name`; missing `keywords`; `"brand": "   "`; `"keywords": []`; `"keywords": "ramones"`; a keyword `""`; a keyword `5`; `"exclude_keywords": "low"`; `"market_price"` of `0`, `-5`, `true`, `"800"`, `NaN`, and `Infinity` (the last two written literally into the JSON file); `"search_query"` of `5`, `"  "`, a 101-character string, `"rick+owens"`, and `"rick*"`; an unknown key `exclude_keyword`; two entries with the same brand and name (one with surrounding spaces); `Low` in `keywords` and `low` in `exclude_keywords`.
23. A catalog whose entry 1 has `"market_price": 0` and entry 2 has an unknown key `colour` raises one `CatalogError` whose message contains `Catalog is invalid:`, `entry 1`, `market_price`, `entry 2`, and `colour`.
24. A catalog with one valid entry followed by one invalid entry writes nothing: the valid entry's changes are not in the database.
25. On a database seeded with `Rick Owens`, a catalog containing a changed `rick owens ramones` plus an item of brand `Other Brand` raises `CatalogError` mentioning `Other Brand` and `RC-13`, and every table is identical to before (the `rick owens ramones` change was rolled back).
26. `seed_from_catalog()` on a legacy version 0 database raises `SchemaVersionError` and changes nothing.
27. On `legacy_db` after `init_db()`, seeding the test catalog keeps both item ids, keeps `rick owens ramones` `market_price_usd` at 420.0 with `manual_price_usd` 800.0, removes the stale `high top` rows from both items and `ramones`/`ramon` from `pod shorts`, sets the keyword sets exactly as in criterion 13, and leaves `listings` and `listing_matches` identical.
28. `validate_catalog` on the parsed `src/catalog.json` succeeds, and the returned `pod shorts` entry contains none of `ramones`, `ramon`, `high top` in `keywords`, and no `search_query` contains `+`.
29. `validate_catalog` on the parsed test catalog returns two `CatalogEntry` objects equal to the expected normalized values.

**Read API and lookups** (`tests/test_db.py`)

30. On `seeded_db`, `get_tracked_items()` returns two `TrackedItem` objects in `items.id` order with the expected brand, names, `search_query`, sorted keyword tuples (`("ramon", "ramones")`, `("canvas", "drkshdw", "low")`, `("pod short",)`, `("pants",)`), and `manual_price_usd`, with `market_price_usd` `None`.
31. `effective_price_usd` is `market_price_usd` when it is greater than 0; otherwise `manual_price_usd` when `market_price_usd` is `NULL`, `0`, or `-1` (set by raw SQL); and `None` when neither is usable.
32. An item with `active = 0` is not returned by `get_tracked_items()`, and an item with no exclude rows has `exclude_keywords == ()`.
33. `get_market_price` follows the same effective price rule as criterion 31.
34. `get_item_id` and `get_market_price` return `None`, without raising, for an unknown brand, and for a known brand with an unknown item.
35. `get_item_id` returns the id of an item with `active = 0`.
36. `update_market_price(<id>, 912.5)` returns `True`, changes only that item's `market_price_usd`, and leaves its `manual_price_usd` unchanged; `update_market_price(9999, 912.5)` returns `False` and changes nothing.
37. `update_market_price(<id>, p)` raises `ValueError` and changes nothing for each `p` in `0`, `-1`, `float("nan")`, `float("inf")`, `True`, `None`, and `"900"`.

**Command line** (`tests/test_db_cli.py`)

38. `db.main([])` on `legacy_db` migrates it (criterion 6 holds) and prints the `Migrated` line.
39. `db.main(["--seed"])` with an invalid catalog raises `SystemExit` with code 1, stderr starts with `Error: Catalog is invalid:`, and no item rows were changed.
40. `db.main([])` on a database with `user_version` 2 raises `SystemExit` with code 1 and stderr contains `Error:`.

**Reviewer**

41. `git diff main...<branch>` shows no change to `src/yahoo_scrape.py`, `src/ebay_pricing.py`, `src/setup.py`, or `src/sendemail.py`, and the full suite passes with `python3 -m pytest`.
42. Every `UPDATE` and `DELETE` added in `src/db.py` has a `WHERE`; all SQL that takes values is parameterized (the `NOT IN` list in step 7 is built from `?` placeholders only); `sqlite3.connect` appears only in `src/db.py`.
43. The README "Database" section replaces the "Seeding only adds..." bullet and states: seeding syncs `catalog.json` into the database (edits propagate, removed items are deactivated, not deleted); `market_price` in the catalog is a fallback and never overwrites an eBay-derived price; the database has a schema version and `python3 src/db.py` upgrades it after you back it up; the jobs refuse to run on an outdated database; and catalog validation errors are reported per entry.
44. The README `setup.py` note says that entries written by `setup.py` need their `keywords` fixed and their `search_query` rewritten with spaces (or removed) before `python3 src/db.py --seed`, and still need `exclude_keywords` for `ebay_pricing.py` until RC-03b.

**Verifier (Phase B)**

45. B1 on a legacy copy: `python3 src/db.py` prints the `Migrated` line, `user_version` is 1, the `SENTINEL` listing is present, the `listings` dump hash matches the pre-migration hash, and `rick owens ramones` `market_price_usd` is 420.0.
46. B2: `python3 src/db.py --seed` on the B1 database then gives the item and keyword rows from criterion 27 (with the real `src/catalog.json` values), and `PRAGMA integrity_check` is `ok`.
47. B3: `RAINCHECK_DRY_RUN=1 python3 src/yahoo_scrape.py` on an unmigrated legacy copy exits non-zero with a traceback ending in `SchemaVersionError` that names `python3 src/db.py`, prints no dry-run email, and leaves the copy's SHA-256 unchanged.
    The job fetches the Yahoo page before its first database call, so this makes one live read-only request.
48. B4: `RAINCHECK_DRY_RUN=1 python3 src/yahoo_scrape.py` on the B2 database (live, read-only Yahoo request) completes, printing one dry-run email per catalog item.
49. B5: in a fresh clone, `python3 src/db.py --seed` on no database exits 0 and gives `user_version` 1 with the real catalog's rows.
50. B6: the migration test suite passes in a fresh clone, and `tests/fixtures/db/schema_v0.sql` is byte-identical to `git show 8a7f409:src/schema.sql`.

**After merge (orchestrator, Phase C)**

51. After steps C0-C10, the user's `src/raincheck.db` has `user_version` 1, `PRAGMA integrity_check` is `ok`, the `listings` dump hash equals the one recorded in C4, and every `market_price_usd` recorded in C4 is unchanged unless the user explicitly cleared it in C9.

## Edge cases

- **Empty results:** an empty catalog is rejected (criterion 22) instead of deactivating every item; `get_tracked_items()` on a database with no active items returns `[]`.
- **`None` or missing values:** missing or `null` `market_price` and `search_query` are stored as `NULL`, and a missing `exclude_keywords` is `[]` (criterion 20); an item with neither price usable has effective price `None`, which the Yahoo job already treats as "skip" (`src/yahoo_scrape.py:154`).
  Unknown brand or item names return `None` instead of raising (criterion 34).
- **Network failure or bad HTTP status:** not applicable; nothing in RC-03a uses the network.
  Criteria 47 and 48 each make one read-only live Yahoo request, in the verifier only.
- **Malformed input:** every catalog shape problem is a `CatalogError` with a per-entry message and no partial writes (criteria 22-25); `NaN` and `Infinity`, which `json.load` accepts, are rejected; `update_market_price` rejects non-positive and non-finite prices (criterion 37); an unrecognized or newer database is never migrated (criteria 11-12).
- **Duplicates:** duplicate keywords collapse to one; duplicate `(brand, item name)` entries are rejected; the same keyword in include and exclude is rejected; reseeding is idempotent (criterion 14).
- **Interrupted or concurrent migration:** each migration is one transaction, so a crash or a failed statement leaves the old schema intact (criterion 9); `BEGIN IMMEDIATE` makes a migration fail with `database is locked` rather than interleave with a running job.
- **Item renamed in `catalog.json`:** identity is `(brand, item name)`, so a rename is the old item deactivated plus a new item added; the old item's eBay price stays on the old row.
  The deactivation line in the output makes this visible; carrying a price across a rename is a job for the RC-14 command line tool.
- **Old code against a new database:** running a pre-RC-03a checkout against a version 1 database works for seeding but ignores `manual_price_usd`, so items never priced by eBay would be skipped by the old Yahoo job; see Rollback.

## Test plan

### Fixtures

- `tests/fixtures/db/schema_v0.sql`: byte-for-byte copy of the pre-RC-03 schema, `git show 8a7f409:src/schema.sql` (unchanged since commit `7984136`).
  Criterion 50 has the verifier confirm the bytes; a pytest test for this is optional and, if added, must skip when `git` is unavailable, like `tests/test_repo_hygiene.py`.
- `tests/fixtures/db/legacy_v0_rows.sql`: `INSERT` statements mirroring the shape of blob `d632e3c`, the last committed copy of the user's database:
  - `brands`: `(1, 'Rick Owens', '103538')`.
  - `items`: `(1, 1, 'rick owens ramones', 420.0, 'manual', 'rick+owens+ramones')` and `(2, 1, 'pod shorts', 300.0, 'manual', 'pod+shorts')`.
  - `item_keywords`: `ramones`, `ramon`, `high top` for both items.
  - `listings`: ids 1 and 2, brand 1, `https://auctions.yahoo.co.jp/jp/auction/u1243433255` at 88935 yen and `https://auctions.yahoo.co.jp/jp/auction/p1243375576` at 70339 yen, synthetic titles, `auc-pctr.c.yimg.jp` image URLs, `first_seen_at` and `last_seen_at` `2026-09-06T00:13:01.331953+00:00` and `2026-09-06T00:13:01.333810+00:00`, `ended_at` and `final_price_yen` `NULL`.
  - `listing_matches`: one row `(1, 1, '2026-09-06T00:13:01.331953+00:00')`; the real database has none, but this proves migration and seeding never touch matches.
- `legacy_db` fixture in `tests/conftest.py`: calls `assert_db_is_temp(tmp_path)`, builds the database at `RAINCHECK_DB_PATH` with plain `sqlite3` from the two SQL files (never through `db.init_db`), leaves `user_version` 0, and returns the path.
  No test reads or copies the real `src/raincheck.db`.
- `tests/fixtures/catalog.json`: only the two `search_query` values change.
- Catalog variants for criteria 15-25 are written to `tmp_path` inside each test and loaded with `seed_from_catalog(path)`, so each test shows its whole input.

### Test files

| File | Covers | Fixtures used |
| --- | --- | --- |
| `tests/test_migration.py` (new) | Criteria 1-12 | `temp_db`, `legacy_db`, `monkeypatch`, `capsys`, `tmp_path` |
| `tests/test_seed_catalog.py` (new) | Criteria 13-29 | `seeded_db`, `test_catalog`, `legacy_db`, `db_conn`, `capsys`, `tmp_path` |
| `tests/test_db.py` | Criteria 30-37, plus superseded updates | `seeded_db`, `db_conn` |
| `tests/test_db_cli.py` | Criteria 38-40, plus superseded updates | `legacy_db`, `fresh_path`, `capsys` |
| `tests/test_config.py`, `tests/test_ebay_pricing.py`, `tests/test_yahoo_scrape.py` | Superseded updates only | existing |

Tests that compare "every table identical" should read each table with `SELECT * FROM <t> ORDER BY <primary key columns>` through a plain `sqlite3` connection, so they work on both legacy and current schemas.
Test names include the criterion, for example `test_rc03a_c9_failed_migration_rolls_back_everything`.
Before implementation, criteria 1-40 fail for the right reason (missing columns, functions, or behavior), except criterion 3, which already passes and guards idempotency.

### Phase B: verifier rehearsal

The verifier works in a throwaway clone (`git clone /Users/nicholassukhdeo/Desktop/RainCheck-main <tmp>/clone`, then check out the implementer's branch), with `RAINCHECK_DB_PATH` always pointing into `<tmp>` and `RAINCHECK_DRY_RUN=1` always set.
It never reads, copies, or writes the real `src/raincheck.db`.

- **Legacy copy:** `git -C <tmp>/clone cat-file blob d632e3cdadb74b6cd385ad3d4cbf5c6fd0c85194 > <tmp>/legacy.db` (the last committed copy of the user's database, so it has the real legacy shape), then add `INSERT INTO listings (brand_id, url, title, price_yen, first_seen_at, last_seen_at) VALUES (1, 'https://example.test/SENTINEL', 'SENTINEL', 1, 'x', 'x')`.
  Record `sqlite3 <tmp>/legacy.db ".dump listings" | shasum -a 256`.
  Make a second copy, `<tmp>/legacy-untouched.db`, for B3.
- **B1-B2:** criteria 45-46.
- **B3:** criterion 47.
- **B4:** criterion 48.
  The verifier states in its report that B3 and B4 each made one live read-only Yahoo request.
- **B5-B6:** criteria 49-50.

### Phase C: upgrading the user's real database (orchestrator, after merge, at the user's request)

The main session runs these steps one at a time in `/Users/nicholassukhdeo/Desktop/RainCheck-main`, shows the user every result, and stops at the first unexpected one.
Subagents never touch the real database.
Phase C must happen before the next real run of either job, because after RC-03a merges the jobs refuse to open a version 0 database.

- **C0.** No RainCheck process is running: `pgrep -fl "yahoo_scrape|ebay_pricing|db.py"` prints nothing.
- **C1.** `grep -c '^RAINCHECK_DB_PATH' src/.env .env 2>/dev/null` prints only zero counts, so the live database really is `src/raincheck.db`.
- **C2.** `ls src/raincheck.db-*` finds no side files.
  If a `-journal` file exists, run `sqlite3 src/raincheck.db "PRAGMA integrity_check;"` once (opening rolls it back) and confirm it is gone.
- **C3.** `git status --short` is empty and `git ls-files src/raincheck.db` prints nothing (untracked since RC-02).
- **C4.** Back up and record:

  ```bash
  mkdir -p ~/RainCheck-backups
  cp -p src/raincheck.db ~/RainCheck-backups/raincheck-pre-rc03a.db
  shasum -a 256 src/raincheck.db ~/RainCheck-backups/raincheck-pre-rc03a.db
  sqlite3 ~/RainCheck-backups/raincheck-pre-rc03a.db "PRAGMA integrity_check; PRAGMA user_version;"
  sqlite3 src/raincheck.db ".dump listings" | shasum -a 256
  sqlite3 -header src/raincheck.db "SELECT COUNT(*) AS listings FROM listings; SELECT id, name, market_price_usd, market_price_source FROM items ORDER BY id;"
  ```

  The hashes match, the integrity check is `ok`, `user_version` is `0`, and the `listings` hash and the item price table are recorded for C8.
  If `user_version` is not `0`, stop.
- **C5.** `git switch main && git pull --ff-only origin main`, then `grep -c manual_price_usd src/schema.sql` prints a non-zero count.
- **C6.** Rehearse on a copy of the backup, never on the live file:

  ```bash
  cp -p ~/RainCheck-backups/raincheck-pre-rc03a.db ~/RainCheck-backups/rehearsal-rc03a.db
  RAINCHECK_DB_PATH="$HOME/RainCheck-backups/rehearsal-rc03a.db" .venv/bin/python src/db.py --seed
  ```

  Then run the C8 checks against the rehearsal file.
  Optionally, with the user's go-ahead, `RAINCHECK_DB_PATH="$HOME/RainCheck-backups/rehearsal-rc03a.db" RAINCHECK_DRY_RUN=1 .venv/bin/python src/yahoo_scrape.py` confirms the Yahoo job runs on the migrated copy (one live read-only request, no email, writes only to the rehearsal copy).
- **C7.** Migrate the live database without seeding: `.venv/bin/python src/db.py` prints `Migrated database at .../src/raincheck.db from schema version 0 to 1.`
- **C8.** Verify:

  ```bash
  sqlite3 src/raincheck.db "PRAGMA user_version; PRAGMA integrity_check; PRAGMA foreign_key_check;"
  sqlite3 src/raincheck.db ".dump listings" | shasum -a 256
  sqlite3 -header src/raincheck.db "SELECT id, name, market_price_usd, manual_price_usd, active FROM items ORDER BY id;"
  ```

  `user_version` is `1`, integrity is `ok`, the foreign key check prints nothing, the `listings` hash equals C4's, and every `market_price_usd` equals C4's.
- **C9.** Seed: `.venv/bin/python src/db.py --seed`, then rerun the item query from C8 and `sqlite3 -header src/raincheck.db "SELECT item_id, kind, keyword FROM item_keywords ORDER BY item_id, kind, keyword;"`.
  Show the user each item's preserved `market_price_usd` next to its new `manual_price_usd`.
  If the user confirms that a preserved value never came from eBay (a value equal to the catalog price is the usual sign, as expected for `pod shorts`), the orchestrator clears it for that item only, with the user's explicit confirmation per item: `sqlite3 src/raincheck.db "UPDATE items SET market_price_usd = NULL WHERE id = <id>;"`.
  Otherwise nothing is changed.
- **C10.** Keep `~/RainCheck-backups/raincheck-pre-rc03a.db` at least until both jobs have completed one real run; delete `rehearsal-rc03a.db` whenever convenient.

## Shared-file conflicts

Yes: this touches all three shared files, `src/schema.sql`, `src/db.py`, and `src/catalog.json`.
It must run alone; no other item may be in progress in parallel, in particular RC-04, RC-05, RC-06, RC-13, and RC-14, which depend on it.
It also edits `tests/conftest.py` (additive), several existing test files, and `README.md`.

RC-03b, by contrast, touches no shared file and could run in parallel with items that do not touch the two job files, but in practice it should follow RC-03a directly, because RC-04 and RC-05 build on both.

## Known issues found (not fixed here)

| Issue | Where | Owner |
| --- | --- | --- |
| Both jobs still read keywords from `catalog.json`; `item_keywords` is still not read in production. | `src/yahoo_scrape.py:165,210`, `src/ebay_pricing.py:74-79` | RC-03b |
| The Yahoo job ignores exclude keywords. | `src/yahoo_scrape.py:165` | RC-03b |
| eBay is queried with the item name instead of `search_query`. | `src/ebay_pricing.py:75` | RC-03b |
| `ebay_pricing.py` raises `KeyError` on catalog entries without `exclude_keywords` (seeding now accepts them). | `src/ebay_pricing.py:75` | RC-03b (fixed by reading the database) |
| `report_item_deals` crashes with `TypeError` when the item's brand is not in the database. | `src/yahoo_scrape.py:147-150` | RC-03b |
| Keyword matching is plain substring matching (`low` matches `yellow`), and full-width Latin letters in Japanese titles never match ASCII keywords. | `src/yahoo_scrape.py:165`, `src/ebay_pricing.py:52,55` | Proposed RC-17 |
| Seeding hardcodes `yahoo_brand_id = '103538'`; a second brand is now a clear `CatalogError`. | `src/db.py` seeding | RC-13 |
| `setup.py` writes `+` in `search_query` (now rejected by seeding) and Ramones keywords for every item. | `src/setup.py:36,38` | RC-14 |
| Full-sync seeding deactivates any item not in `catalog.json`; once RC-14 adds items directly to the database, seeding from `catalog.json` would deactivate them. | `src/db.py` seeding | RC-14 must retire `catalog.json` as the authoring source or make seeding additive |
| An eBay price is never cleared, so a stale eBay price outlives a period with no matching listings. | `src/ebay_pricing.py:76-77` | RC-09 |

## Open questions

**Resolved 2026-10-26:** the user approved this spec and the split into RC-03a, RC-03b, and RC-17, and chose the recommended option for questions 1 to 7, 9, and 10.
For question 8, the real `pod shorts` keywords are `["pod short", "ポッドショーツ"]`; the test catalog keeps `["pod short"]`.
For question 3, the user decided in advance for step C9: keep 420.0 as the eBay-derived `market_price_usd` for `rick owens ramones`, and clear the `market_price_usd` of `pod shorts` (its 300.0 equals the catalog price and never came from eBay), so it falls back to its manual price of 300.0.

1. **Split RC-03 into RC-03a and RC-03b?**
   Recommendation: **yes**, as described under "Proposed split".
   RC-03a is the risky part (a migration of real data) and benefits from a review that is not distracted by job changes; RC-03b is then a straightforward refactor against a tested API.
   Tradeoff: two PRs and two review cycles, and for a short time `get_tracked_items()` exists without a production caller and the database holds exclude keywords that only `catalog.json`-reading code uses.
   Alternative: one PR with everything, roughly twice the diff, all of it on shared files.
2. **Who owns the market price?**
   - Option A (recommended): two columns with one writer each, as designed: `manual_price_usd` from the catalog, `market_price_usd` from eBay, effective price is eBay if usable else manual.
     Seeding can never clobber an eBay price, the manual price survives as a fallback when eBay has nothing (useful once RC-09 refuses to price thin samples), and `market_price_source` is dropped because it is redundant.
     Tradeoff: one more column and an "effective price" rule that readers must use; existing tests that read `market_price_usd` after seeding change.
   - Option B: keep one column; `update_market_price` sets `market_price_source = 'ebay'`, and seeding writes the catalog price only while the source is `'manual'`.
     Tradeoff: smaller schema change, but once eBay prices an item, the manual price is gone from the database and editing it in the catalog silently does nothing, which is the same kind of invisible drift this item exists to remove.
3. **How are existing prices classified during migration?**
   Every legacy row says `'manual'`, but that label is wrong for eBay-priced rows, and the database cannot tell which is which.
   - Option A (recommended): the migration preserves every existing `market_price_usd` as the eBay-derived value (no data lost, no guessing in code), and step C9 shows each value next to the catalog price so you decide, per item, whether to clear one that never came from eBay.
     For your data this most likely means keeping 420.0 for `rick owens ramones` and clearing 300.0 for `pod shorts`.
     Tradeoff: one manual decision during Phase C.
   - Option B: move every existing value into `manual_price_usd`.
     Tradeoff: the 420.0 eBay price is overwritten by the catalog's 800.0 at the next seed, and Yahoo deals are judged against 800.0 until `ebay_pricing.py` runs again.
   - Option C: code a heuristic (a legacy value equal to the catalog price is manual, otherwise eBay).
     Tradeoff: automatic and right for your two rows, but it is permanent code that exists for one upgrade, and it misclassifies an old catalog edit that never reached the database.
4. **What happens to items removed from `catalog.json`?**
   - Option A (recommended): deactivate (`active = 0`), keeping the row, its eBay price, and its keywords; re-adding the item reactivates the same row.
     Tradeoff: one more column and a filter in every reader; the database keeps rows you no longer care about.
   - Option B: delete.
     Tradeoff: simplest, but `listing_matches` cascades on delete (RC-04 history would vanish) and price history (RC-06) would be orphaned or block the delete.
   - Option C: leave them active.
     Tradeoff: a removed item keeps being priced and emailed, which is the current drift.
   Removed keywords, by contrast, are deleted, because nothing references them.
5. **Where do exclude keywords live?**
   - Option A (recommended): a `kind` column on `item_keywords`.
     One table, one query per item, and the existing primary key `(item_id, keyword)` makes "include and exclude the same word" impossible.
     Tradeoff: needs an `ALTER TABLE`, which needs the migration framework (needed anyway for the price columns).
   - Option B: a separate `item_exclude_keywords` table.
     Tradeoff: `CREATE TABLE IF NOT EXISTS` would add it with no migration, but it duplicates the table's structure and the include/exclude conflict could only be enforced in Python.
6. **How is the migration triggered?**
   - Option A (recommended): only by `python3 src/db.py` (with or without `--seed`), and `get_connection()` refuses outdated databases with a message telling you to back up and run that command.
     Tradeoff: one explicit step after each schema change; a job run before it fails loudly instead of working.
   - Option B: automatically inside `get_connection()`.
     Tradeoff: no extra step, but a cron job or Lambda run (RC-11) would migrate a database nobody backed up, possibly while another process holds it.
   Separately, the code could copy the database to a timestamped backup before migrating.
   Recommendation: not now, because Phase C does a verified backup by hand and an automatic copy inside `src/` adds files to manage; reconsider for RC-11, where the database lives in S3 and versioned buckets give backups for free.
7. **What does `search_query` mean?**
   Recommendation: the plain-text eBay keyword query (spaces mean AND, at most 100 characters per the Browse API documentation), used by RC-03b with the item name as fallback; `+` and `*` are rejected so a URL-style value from `setup.py` cannot silently become a literal `+` in the eBay search.
   Proposed values: `rick owens ramones` and `rick owens pod shorts`; today eBay is queried with `pod shorts`, which also returns other brands' shorts.
   Tradeoff: `setup.py` output needs a hand edit before seeding until RC-14; the alternative of converting `+` to spaces on seeding is friendlier but is a permanent compatibility shim for a tool that is about to be replaced.
8. **What are the right `pod shorts` keywords?**
   Recommendation: `["pod short"]` (matches "pod short" and "pod shorts", and mirrors the test catalog), with `exclude_keywords` `["pants"]` unchanged.
   You know how Yahoo sellers title these better than any spec; consider adding a Japanese form if listings commonly use one, for example `ポッドショーツ`.
   Tradeoff: more keywords catch more listings but also more false matches.
9. **Reject an empty catalog?**
   Recommendation: yes, because syncing `[]` would deactivate every item, which is almost always an accident such as a truncated file.
   Tradeoff: you cannot intentionally deactivate everything by emptying the catalog; set the items aside in another file instead, or wait for RC-14's remove command.
10. **Keep the catalog key name `market_price`?**
    Recommendation: yes; it maps to `manual_price_usd`, and keeping it avoids touching `setup.py` and every fixture.
    Tradeoff: the key name no longer matches the column it fills; the README explains the mapping.

## Rollback

- **Before merge:** close the PR; nothing in the main checkout or the real database has changed.
- **After merge, before Phase C:** revert the merge commit with a new PR; the real database is still version 0 and the old code works with it.
- **After Phase C:** reverting the code alone is not enough, because pre-RC-03a code would read `market_price_usd` only, and items whose price lives in `manual_price_usd` would be skipped by the Yahoo job.
  Two options, both run by the orchestrator with a fresh backup first (C4):
  - Restore `~/RainCheck-backups/raincheck-pre-rc03a.db` over `src/raincheck.db`, losing only listings first seen since Phase C (those deals may be emailed once more).
  - Or apply a manual down-migration in one transaction: `ALTER TABLE items ADD COLUMN market_price_source TEXT DEFAULT 'manual'`, `UPDATE items SET market_price_usd = manual_price_usd WHERE market_price_usd IS NULL`, drop `manual_price_usd`, `active`, and `item_keywords.kind` (first deleting exclude rows with `DELETE FROM item_keywords WHERE kind = 'exclude'`), and `PRAGMA user_version = 0`.
    This keeps all listings; it is documented here but not tested, so prefer the restore unless many new listings would be lost.

## Backlog text for the split and new items

For the orchestrator to apply to `docs/BACKLOG.md` if the user approves the split (this spec does not edit the backlog).

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
  - `report_item_deals` takes a `TrackedItem`, so the brand lookup crash and the `KeyError` on missing `exclude_keywords` disappear.
  - Update the README "Usage" section: edit `catalog.json`, run `python3 src/db.py --seed`, then run the jobs.
- **Resume angle:** Refactored to a single source of truth, eliminating configuration drift between two data pipelines.

### RC-17: Word-aware keyword matching (new)

- **Size:** S
- **Depends on:** RC-03b
- **Touches:** `src/matching.py`, tests
- **Why:** Matching is plain substring matching, so the exclude keyword `low` also removes `yellow` and `follow`, and full-width Latin letters in Japanese titles (for example `ＲＡＭＯＮＥＳ`) never match ASCII keywords.
- **Scope:** Normalize titles and keywords with Unicode NFKC before lowercasing, and match Latin keywords on word boundaries while keeping substring matching for Japanese text, which has no spaces.
  Test against the live Yahoo and eBay fixtures to measure how many matches change.
- **Resume angle:** Improved precision of text matching across English and Japanese listing titles.
