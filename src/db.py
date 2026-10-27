from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import statistics
from typing import Iterable

import argparse
import math
import sqlite3
import json
import os
import sys

from dotenv import load_dotenv

SRC_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = SRC_DIR / "raincheck.db"
SCHEMA_PATH = SRC_DIR / "schema.sql"
CATALOG_PATH = SRC_DIR / "catalog.json"

# the schema version this code expects. it lives inside the database file
# itself (PRAGMA user_version), so every database knows which shape it has.
# schema.sql always describes this version.
SCHEMA_VERSION = 2

# how to upgrade an existing database, one step at a time.
# the key is the version you END UP at, the value is the statements to get there.
# each one is a tuple (not a list) so a test can extend it with `+ (...)`.
# never edit a migration after it has shipped, add a new number instead.
MIGRATIONS = {
    1: (
        # the catalog's price, kept apart from the eBay price so seeding can't overwrite eBay
        "ALTER TABLE items ADD COLUMN manual_price_usd REAL CHECK (manual_price_usd IS NULL OR manual_price_usd > 0)",
        # 1 = tracked, 0 = removed from catalog.json (every existing row stays tracked)
        "ALTER TABLE items ADD COLUMN active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))",
        # existing keywords were all includes, so the default 'include' is correct for them
        "ALTER TABLE item_keywords ADD COLUMN kind TEXT NOT NULL DEFAULT 'include' CHECK (kind IN ('include', 'exclude'))",
        # it said 'manual' on every row, so it never carried any information
        "ALTER TABLE items DROP COLUMN market_price_source",
    ),
    2: (
        "CREATE TABLE ebay_listing_prices ("
        "item_id INTEGER NOT NULL REFERENCES items(id), "
        "ebay_listing_id TEXT NOT NULL, "
        "first_price_usd REAL NOT NULL CHECK (first_price_usd > 0), "
        "first_seen_at TEXT NOT NULL, "
        "PRIMARY KEY (item_id, ebay_listing_id))",
        "CREATE INDEX idx_ebay_listing_prices_item_seen "
        "ON ebay_listing_prices (item_id, first_seen_at, ebay_listing_id)",
    ),
}

# only one brand is supported until RC-13 stores a Yahoo id per brand
DEFAULT_YAHOO_BRAND_ID = "103538"

# the catalog keys we understand. anything else is probably a typo
# (like "exclude_keyword"), and a typo must not quietly mean "no excludes".
ALLOWED_CATALOG_KEYS = {
    "brand", "item name", "size", "search_query",
    "market_price", "keywords", "exclude_keywords",
}


class SchemaVersionError(RuntimeError):
    """The database file is not at the schema version this code expects."""


class CatalogError(ValueError):
    """catalog.json is invalid, or cannot be seeded as written."""


@dataclass(frozen=True)
class CatalogEntry:
    """One validated, normalized catalog.json entry."""
    brand: str
    name: str
    search_query: str | None
    manual_price_usd: float | None
    keywords: tuple[str, ...]          # normalized, de-duplicated, catalog order
    exclude_keywords: tuple[str, ...]  # normalized, de-duplicated, catalog order


def _usable_price(value):
    """True for a real number greater than 0 (not NULL, 0, negative, NaN, or inf)."""
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value) and value > 0


def _effective_price(market_price_usd, manual_price_usd):
    """
    The price deal math should use.
    The eBay price wins when it is usable, then the catalog's manual price,
    and None when neither is, which the Yahoo job treats as "skip this item".
    """
    if _usable_price(market_price_usd):
        return market_price_usd
    if _usable_price(manual_price_usd):
        return manual_price_usd
    return None


@dataclass(frozen=True)
class TrackedItem:
    """One active item with everything the jobs need to know about it."""
    id: int
    brand_id: int
    brand: str
    name: str
    search_query: str | None
    keywords: tuple[str, ...]          # sorted
    exclude_keywords: tuple[str, ...]  # sorted
    manual_price_usd: float | None     # from catalog.json, written only by seeding
    market_price_usd: float | None     # from eBay, written only by update_market_price

    @property
    def effective_price_usd(self):
        return _effective_price(self.market_price_usd, self.manual_price_usd)


@dataclass(frozen=True)
class SeedResult:
    """What a seed run did. Each entry is (brand, item name)."""
    added: tuple[tuple[str, str], ...]        # catalog order
    updated: tuple[tuple[str, str], ...]      # already in the db and still in the catalog
    deactivated: tuple[tuple[str, str], ...]  # no longer in the catalog, in id order


@dataclass(frozen=True)
class EbayListingPrice:
    """One validated, canonical eBay listing ask in USD."""
    ebay_listing_id: str
    price_usd: float


@dataclass(frozen=True)
class PoolResult:
    """Counts and price produced from one item's cumulative listing pool."""
    item_found: bool
    new_count: int
    duplicate_count: int
    candidate_count: int
    outlier_count: int
    retained_count: int
    mean_usd: float | None
    price_updated: bool


def get_db_path():
    """
    Returns the database file to use.
    RAINCHECK_DB_PATH wins when it is set to something non-empty,
    otherwise we fall back to src/raincheck.db.
    This runs on every call (not once at import) so tests can point
    the code at a throwaway database while the process is running.
    """
    return os.environ.get("RAINCHECK_DB_PATH") or str(DEFAULT_DB_PATH)


def load_catalog(path=None):
    """
    Reads the catalog JSON and returns it as a list of dicts.
    The default path is built from this file's location, not the
    current directory, so every script works no matter where you run it.
    CATALOG_PATH is looked up at call time so tests can swap it out.
    """
    # utf-8 on purpose: keywords can be Japanese, and the default
    # encoding depends on the machine
    with open(path or CATALOG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _user_tables(conn):
    """Names of the tables a person made (SQLite keeps its own sqlite_* ones too)."""
    names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    return {n for n in names if not n.startswith("sqlite_")}


def _read_user_version(conn, path):
    """
    Reads PRAGMA user_version, the first statement that actually reads the file.
    A path that exists but is not a SQLite database (a text file, say) fails
    here with DatabaseError, so turn that into a clear SchemaVersionError.
    OperationalError (locked, can't open) is a DatabaseError subclass too, but
    that is not "not a database", so it is let through unchanged.
    """
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    except sqlite3.OperationalError:
        conn.close()
        raise
    except sqlite3.DatabaseError:
        conn.close()
        raise SchemaVersionError(
            f"The file at {path} is not a SQLite database, so it was not touched. "
            "Check RAINCHECK_DB_PATH."
        ) from None


def get_connection():
    """
    Opens a connection to the SQLite database.
    row_factory lets you access columns by name (row['title'])
    instead of by index (row[1]), which is far more readable.

    It refuses a database that is not at SCHEMA_VERSION. The jobs never
    migrate on their own, so a job can't run against a schema it doesn't
    understand, and nothing changes your data without you running db.py.
    """
    path = get_db_path()
    # sqlite3.connect quietly creates a missing file, so look first
    if not os.path.exists(path):
        raise SchemaVersionError(
            f"No database found at {path}. Run python3 src/db.py --seed to create it."
        )

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row

    # why close on every error path: a connection that is never closed is only freed at
    # garbage collection, which shows up as a ResourceWarning in some random later place.
    # (`with sqlite3.connect(...)` would not help, it commits but does not close.)
    try:
        version = _read_user_version(conn, path)
        if version != SCHEMA_VERSION:
            has_tables = bool(_user_tables(conn))
            if not has_tables:
                raise SchemaVersionError(
                    f"The database at {path} is empty. Run python3 src/db.py --seed to create it."
                )
            newer = " It was created by a newer RainCheck." if version > SCHEMA_VERSION else ""
            raise SchemaVersionError(
                f"The database at {path} is at schema version {version} "
                f"but this code expects version {SCHEMA_VERSION}.{newer} "
                "Back up the database, then run python3 src/db.py"
            )

        # SQLite ignores FOREIGN KEY constraints unless you turn them on,
        # and it's per-connection, so this has to run every single time.

        # basically a key refers to one of these tables that you make.
        # and there's restrictions that you'll set on these tables for
        # various reasons. a run is a row in a table. like row 3
        # could contain the row with all of the data about the
        # marathon you ran in shoe #7.
        conn.execute("PRAGMA foreign_keys = ON")
    except BaseException:
        conn.close()
        raise
    return conn


def _is_legacy_v0_shape(conn, tables):
    """
    True for a database made before schema versioning existed:
    all five tables, items still has market_price_source and no manual_price_usd.
    We only migrate shapes we recognize, never a random file.
    """
    needed = {"brands", "items", "item_keywords", "listings", "listing_matches"}
    if not needed <= tables:
        return False
    item_columns = {r[1] for r in conn.execute("PRAGMA table_info(items)")}
    return "market_price_source" in item_columns and "manual_price_usd" not in item_columns


def _create_fresh(conn):
    """Builds every table from schema.sql and stamps the version, all or nothing."""
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        schema = f.read()
    # executescript is fine here because the file is brand new and we open the
    # transaction ourselves inside the script. PRAGMA can't take a ? placeholder,
    # so the version is formatted in, and it is an int constant from this file.
    script = f"BEGIN IMMEDIATE;\n{schema}\nPRAGMA user_version = {int(SCHEMA_VERSION)};\nCOMMIT;"
    try:
        conn.executescript(script)
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _migrate(conn, from_version):
    """
    Applies migrations from_version+1 .. SCHEMA_VERSION, each in its own transaction.
    We don't use executescript here because it commits any open transaction first.
    """
    # DROP COLUMN needs SQLite 3.35. check before touching anything
    if sqlite3.sqlite_version_info < (3, 35, 0):
        raise SchemaVersionError(
            f"SQLite {sqlite3.sqlite_version} is too old to migrate the database "
            "(3.35 or newer is needed for DROP COLUMN). Nothing was changed."
        )

    for target in range(from_version + 1, SCHEMA_VERSION + 1):
        # BEGIN IMMEDIATE grabs the write lock now, so a job running at the same
        # time makes this fail with "database is locked" instead of interleaving
        conn.execute("BEGIN IMMEDIATE")
        try:
            # we chose this step from a version read BEFORE the lock. another
            # process may have migrated while we waited, so look again now that
            # nobody else can write. stopping here gives a clear message instead
            # of a confusing "duplicate column name" halfway through.
            if conn.execute("PRAGMA user_version").fetchone()[0] != target - 1:
                raise SchemaVersionError(
                    f"The database at {get_db_path()} was upgraded concurrently by "
                    "another process, so this migration was stopped. "
                    "Run python3 src/db.py again to check its state."
                )
            for statement in MIGRATIONS[target]:
                conn.execute(statement)
            # the version bump is inside the transaction, so it rolls back with it.
            # (PRAGMA can't take a ? placeholder, and target is an int from range().)
            conn.execute(f"PRAGMA user_version = {int(target)}")
            conn.execute("COMMIT")
        except BaseException:
            # any failure puts the file back exactly as it was before this step.
            # only roll back if a transaction is still open: SQLite sometimes
            # rolls back by itself (disk full), and a second ROLLBACK would hide
            # the real error behind "no transaction is active"
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    if from_version < SCHEMA_VERSION:
        print(
            f"Migrated database at {get_db_path()} from schema version "
            f"{from_version} to {SCHEMA_VERSION}."
        )


def init_db():
    """
    Creates the database, or upgrades an old one in place. Safe to re-run.
    Missing or empty file: build it from schema.sql.
    Older recognized schema: run the pending migrations.
    Newer or unrecognized: refuse and change nothing.
    """
    # isolation_level=None = autocommit, so we decide exactly when transactions begin
    conn = sqlite3.connect(get_db_path(), isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        version = _read_user_version(conn, get_db_path())
        tables = _user_tables(conn)

        if version > SCHEMA_VERSION:
            raise SchemaVersionError(
                f"The database at {get_db_path()} is at schema version {version}, "
                f"newer than this code's version {SCHEMA_VERSION}. "
                "It was created by a newer RainCheck, so it was not changed."
            )
        if version == 0 and not tables:
            _create_fresh(conn)
        elif version == SCHEMA_VERSION:
            pass
        elif version > 0 or _is_legacy_v0_shape(conn, tables):
            _migrate(conn, version)
        else:
            raise SchemaVersionError(
                f"The database at {get_db_path()} has an unrecognized shape "
                "(schema version 0 but not a RainCheck database), so it was not migrated."
            )
    finally:
        conn.close()
    # show which file we touched so it is obvious when RAINCHECK_DB_PATH is in play
    print(f"Database initialized at {get_db_path()}.")


def _check_keywords(label, key, value, problems):
    """
    Validates one keyword list and returns it normalized (stripped, lowercased,
    de-duplicated, first occurrence wins). The jobs lowercase titles, so a keyword
    stored with capitals could never match.
    """
    if not isinstance(value, list):
        problems.append(f"{label}: {key} must be a list")
        return ()
    out = []
    for kw in value:
        if not isinstance(kw, str) or not kw.strip():
            problems.append(f"{label}: every item in {key} must be a non-blank string")
            continue
        normalized = kw.strip().lower()
        if normalized not in out:
            out.append(normalized)
    return tuple(out)


def validate_catalog(entries):
    """
    Checks a parsed catalog.json and returns a list of CatalogEntry.
    Pure function: no database access. It collects EVERY problem and raises one
    CatalogError, so you can fix the whole file in one pass.
    """
    problems = []

    if not isinstance(entries, list) or not entries:
        # an empty list would deactivate every item when seeded, almost always a mistake
        raise CatalogError("Catalog is invalid:\n- the catalog must be a non-empty list of entries")

    result = []
    first_seen = {}  # (brand, name) -> entry number, to catch duplicates
    for number, entry in enumerate(entries, start=1):
        label = f"entry {number}"
        if not isinstance(entry, dict):
            problems.append(f"- {label}: must be an object")
            continue

        entry_problems = []
        brand = entry.get("brand")
        name = entry.get("item name")
        brand_ok = isinstance(brand, str) and bool(brand.strip())
        name_ok = isinstance(name, str) and bool(name.strip())
        # the quoted name is left out when the name itself is invalid
        if name_ok:
            label = f'{label} ("{name.strip()}")'

        for key in entry:
            if key not in ALLOWED_CATALOG_KEYS:
                entry_problems.append(f"{label}: unknown key {key!r}")
        if not brand_ok:
            entry_problems.append(f"{label}: brand must be a non-blank string")
        if not name_ok:
            entry_problems.append(f"{label}: item name must be a non-blank string")

        # keywords are required and need at least one, excludes are optional
        keywords = ()
        if "keywords" not in entry:
            entry_problems.append(f"{label}: keywords is required")
        else:
            keywords = _check_keywords(label, "keywords", entry["keywords"], entry_problems)
            if entry["keywords"] == []:
                entry_problems.append(f"{label}: keywords needs at least one keyword")
        excludes = ()
        if "exclude_keywords" in entry:
            excludes = _check_keywords(label, "exclude_keywords", entry["exclude_keywords"], entry_problems)
        # a word that is both include and exclude means the item can never match
        both = [k for k in keywords if k in excludes]
        if both:
            entry_problems.append(f"{label}: {both} is in both keywords and exclude_keywords")

        # market_price: missing or null means no manual price
        manual_price = None
        price = entry.get("market_price")
        if price is not None:
            ok = isinstance(price, (int, float)) and not isinstance(price, bool)
            if ok:
                try:
                    manual_price = float(price)
                except OverflowError:  # a JSON integer too big for a float
                    ok = False
                else:
                    # json.load happily accepts NaN and Infinity, so check for those too
                    ok = math.isfinite(manual_price) and manual_price > 0
            if not ok:
                manual_price = None
                entry_problems.append(f"{label}: market_price must be a number greater than 0")

        # search_query is plain text for eBay: spaces mean AND, so + and * are rejected
        search_query = entry.get("search_query")
        if search_query is not None:
            if not isinstance(search_query, str) or not search_query.strip():
                entry_problems.append(f"{label}: search_query must be a non-blank string")
                search_query = None
            else:
                search_query = search_query.strip()
                if len(search_query) > 100:  # eBay truncates longer queries
                    entry_problems.append(f"{label}: search_query must be at most 100 characters")
                if "+" in search_query or "*" in search_query:
                    entry_problems.append(
                        f"{label}: search_query must be plain text with spaces, without + or *"
                    )

        if brand_ok and name_ok:
            key = (brand.strip(), name.strip())
            if key in first_seen:
                entry_problems.append(f"{label}: duplicates entry {first_seen[key]}")
            else:
                first_seen[key] = number

        problems.extend(f"- {p}" for p in entry_problems)
        if not entry_problems:
            result.append(CatalogEntry(
                brand=brand.strip(),
                name=name.strip(),
                search_query=search_query,
                manual_price_usd=manual_price,
                keywords=keywords,
                exclude_keywords=excludes,
            ))

    if problems:
        raise CatalogError("Catalog is invalid:\n" + "\n".join(problems))
    return result


#helps us read the brands and items in catalog and store it into our db
def seed_from_catalog(catalog_path=None):
    """
    Syncs catalog.json into the database (an upsert) and returns a SeedResult.
    Edits to existing items propagate, keyword sets are replaced, and items
    removed from the catalog are deactivated, never deleted.
    It never writes market_price_usd, that column belongs to the eBay job.
    """
    # validate before opening the database, so a bad file changes nothing
    entries = validate_catalog(load_catalog(catalog_path))

    conn = get_connection()
    try:
        # one transaction for the whole seed: any error rolls everything back.
        # IMMEDIATE takes the write lock up front.
        conn.execute("BEGIN IMMEDIATE")
        added, updated, seen_ids = [], [], []

        for entry in entries:
            # the question marks are placeholder values for those columns.
            brand_row = conn.execute(
                "SELECT id FROM brands WHERE name = ?",
                (entry.brand,),
            ).fetchone()
            if brand_row is None:
                try:
                    cursor = conn.execute(
                        "INSERT INTO brands (name, yahoo_brand_id) VALUES (?,?)",
                        (entry.brand, DEFAULT_YAHOO_BRAND_ID),
                    )
                except sqlite3.IntegrityError:
                    # yahoo_brand_id is UNIQUE and we hardcode it, so a 2nd brand collides
                    raise CatalogError(
                        f"Cannot add brand {entry.brand!r}: only one brand is supported "
                        "until RC-13 stores a Yahoo brand id per brand."
                    ) from None
                brand_id = cursor.lastrowid
            else:
                brand_id = brand_row["id"]

            existing = conn.execute(
                "SELECT id FROM items WHERE brand_id = ? AND name = ?",
                (brand_id, entry.name),
            ).fetchone()
            # upsert: new items are inserted, existing ones get the catalog's values.
            # market_price_usd is left out on purpose so an eBay price survives.
            conn.execute(
                "INSERT INTO items (brand_id, name, manual_price_usd, search_query, active) "
                "VALUES (?,?,?,?,1) "
                "ON CONFLICT (brand_id, name) DO UPDATE SET "
                "manual_price_usd = excluded.manual_price_usd, "
                "search_query = excluded.search_query, "
                "active = 1",
                (brand_id, entry.name, entry.manual_price_usd, entry.search_query),
            )
            item_id = conn.execute(
                "SELECT id FROM items WHERE brand_id = ? AND name = ?",
                (brand_id, entry.name),
            ).fetchone()["id"]
            seen_ids.append(item_id)
            (added if existing is None else updated).append((entry.brand, entry.name))

            # replace the keyword set so removed keywords really disappear
            conn.execute("DELETE FROM item_keywords WHERE item_id = ?", (item_id,))
            for keyword in entry.keywords:
                conn.execute(
                    "INSERT INTO item_keywords (item_id,keyword,kind) VALUES (?,?,'include')",
                    (item_id, keyword),
                )
            for keyword in entry.exclude_keywords:
                conn.execute(
                    "INSERT INTO item_keywords (item_id,keyword,kind) VALUES (?,?,'exclude')",
                    (item_id, keyword),
                )

        # anything still active that this run did not touch is gone from the catalog.
        # deactivate (not delete) so listing matches and price history stay valid.
        # the IN list is only "?" placeholders, the ids themselves are passed as values
        placeholders = ",".join("?" for _ in seen_ids)
        gone = conn.execute(
            "SELECT items.id, brands.name AS brand, items.name AS name "
            "FROM items JOIN brands ON brands.id = items.brand_id "
            f"WHERE items.active = 1 AND items.id NOT IN ({placeholders}) "
            "ORDER BY items.id",
            seen_ids,
        ).fetchall()
        for row in gone:
            conn.execute("UPDATE items SET active = 0 WHERE id = ?", (row["id"],))

        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()

    print("Catalog seeded.")
    deactivated = tuple((row["brand"], row["name"]) for row in gone)
    for brand, name in deactivated:
        print(f"Deactivated item no longer in catalog.json: {brand} / {name}")
    return SeedResult(added=tuple(added), updated=tuple(updated), deactivated=deactivated)


def get_tracked_items():
    """
    Every active item with its keywords and prices, in id order.
    """
    # closing() closes the connection even if a query raises
    with closing(get_connection()) as conn:
        item_rows = conn.execute(
            "SELECT items.id, items.brand_id, brands.name AS brand, items.name, "
            "items.search_query, items.manual_price_usd, items.market_price_usd "
            "FROM items JOIN brands ON brands.id = items.brand_id "
            "WHERE items.active = 1 ORDER BY items.id"
        ).fetchall()
        keyword_rows = conn.execute(
            "SELECT item_id, keyword, kind FROM item_keywords "
            "WHERE item_id IN (SELECT id FROM items WHERE active = 1) "
            "ORDER BY keyword"
        ).fetchall()

    include, exclude = {}, {}
    for row in keyword_rows:
        target = include if row["kind"] == "include" else exclude
        target.setdefault(row["item_id"], []).append(row["keyword"])

    return [
        TrackedItem(
            id=row["id"],
            brand_id=row["brand_id"],
            brand=row["brand"],
            name=row["name"],
            search_query=row["search_query"],
            keywords=tuple(include.get(row["id"], ())),
            exclude_keywords=tuple(exclude.get(row["id"], ())),
            manual_price_usd=row["manual_price_usd"],
            market_price_usd=row["market_price_usd"],
        )
        for row in item_rows
    ]


def insert_listing(brand_id,url,title,price_yen,image_url):
    now = datetime.now(timezone.utc).isoformat()
    # closing() closes the connection even when the insert raises (an unknown brand_id
    # gives IntegrityError), so a failed insert can't leak an open connection
    with closing(get_connection()) as conn:
        # the execute func returns if smth was inserted or not.
        ifInserted = conn.execute(
            "INSERT OR IGNORE INTO listings (brand_id, url, title, price_yen, image_url, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?)",
            (brand_id,url,title,price_yen,image_url,now,now)
        )
        conn.commit()
        # how u will check if smth was inserted or not (if it was a new listing)
        is_new = (ifInserted.rowcount == 1)
    return is_new


def record_listing_match(brand_id, url, title, price_yen, image_url, item_id) -> bool:
    """Store a listing and its tracked-item association in one transaction.

    Returns True only when this call created the listing row. Existing listing
    metadata is left alone, while a new item association can still be recorded.
    """
    now = datetime.now(timezone.utc).isoformat()
    with closing(get_connection()) as conn:
        # Keep the listing and its association together if either foreign key fails.
        conn.execute("BEGIN IMMEDIATE")
        try:
            inserted = conn.execute(
                "INSERT INTO listings "
                "(brand_id, url, title, price_yen, image_url, first_seen_at, last_seen_at) "
                "VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(url) DO NOTHING",
                (brand_id, url, title, price_yen, image_url, now, now),
            )
            listing = conn.execute(
                "SELECT id FROM listings WHERE url = ?",
                (url,),
            ).fetchone()
            conn.execute(
                "INSERT INTO listing_matches (listing_id, item_id, matched_at) "
                "VALUES (?,?,?) "
                "ON CONFLICT(listing_id, item_id) DO NOTHING",
                (listing["id"], item_id, now),
            )
            conn.commit()
            return inserted.rowcount == 1
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise


def get_known_listing_urls(urls: Iterable[str]) -> set[str]:
    """Returns the subset of urls already stored in listings. Read-only: never writes or commits."""
    # consume the iterable once and de-duplicate it
    wanted = set(urls)
    if not wanted:
        # nothing to look up, so don't even open the database
        return set()
    with closing(get_connection()) as conn:
        # one indexed lookup per url (listings.url is UNIQUE); at most ~100 urls per run
        return {
            url for url in wanted
            if conn.execute("SELECT 1 FROM listings WHERE url = ?", (url,)).fetchone() is not None
        }


def update_last_seen_at(urls: Iterable[str], observed_at: datetime) -> int:
    """Refresh previously stored listings observed at a later UTC time."""
    if not isinstance(observed_at, datetime) or observed_at.utcoffset() != timedelta(0):
        raise ValueError("observed_at must be a UTC datetime")

    # Preserve page order while consuming a one-shot iterable and ignoring repeated URLs.
    unique_urls = dict.fromkeys(urls)
    if not unique_urls:
        return 0

    timestamp = observed_at.isoformat()
    with closing(get_connection()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            changed = 0
            for url in unique_urls:
                cursor = conn.execute(
                    "UPDATE listings SET last_seen_at = ? "
                    "WHERE url = ? AND last_seen_at < ?",
                    (timestamp, url, timestamp),
                )
                changed += cursor.rowcount
            conn.commit()
            return changed
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise


def record_ebay_listing_prices(item_id, listings, observed_at=None) -> PoolResult:
    """Keep first asks permanently and update the current price from the full pool."""
    if isinstance(item_id, bool) or not isinstance(item_id, int) or item_id <= 0:
        raise ValueError("item_id must be a positive integer")

    if observed_at is None:
        timestamp = datetime.now(timezone.utc).isoformat()
    elif isinstance(observed_at, str):
        try:
            parsed = datetime.fromisoformat(observed_at)
        except ValueError as exc:
            raise ValueError("observed_at must be an ISO timestamp in UTC") from exc
        if parsed.utcoffset() != timedelta(0):
            raise ValueError("observed_at must include a UTC offset")
        timestamp = parsed.isoformat()
    else:
        raise ValueError("observed_at must be an ISO timestamp in UTC")

    validated = []
    try:
        for listing in listings:
            listing_id = listing.ebay_listing_id
            price = listing.price_usd
            if (
                not isinstance(listing_id, str)
                or not listing_id.strip()
                or listing_id != listing_id.strip()
                or not (
                    listing_id.startswith("legacy:") and bool(listing_id[7:])
                    or listing_id.startswith("rest:") and bool(listing_id[5:])
                )
            ):
                raise ValueError("listing ID must be a canonical eBay listing ID")
            if isinstance(price, bool) or not isinstance(price, (int, float)):
                raise ValueError("listing price must be a finite positive number")
            price = float(price)
            if not math.isfinite(price) or price <= 0:
                raise ValueError("listing price must be a finite positive number")
            validated.append((listing_id, price))
    except (AttributeError, TypeError, OverflowError) as exc:
        raise ValueError("listings must contain valid listing IDs and prices") from exc

    with closing(get_connection()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if conn.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone() is None:
                conn.commit()
                return PoolResult(False, 0, 0, 0, 0, 0, None, False)

            new_count = 0
            for listing_id, price in validated:
                cursor = conn.execute(
                    "INSERT INTO ebay_listing_prices "
                    "(item_id, ebay_listing_id, first_price_usd, first_seen_at) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(item_id, ebay_listing_id) DO NOTHING",
                    (item_id, listing_id, price, timestamp),
                )
                new_count += cursor.rowcount

            prices = [row[0] for row in conn.execute(
                "SELECT first_price_usd FROM ebay_listing_prices WHERE item_id = ? "
                "ORDER BY first_seen_at, ebay_listing_id",
                (item_id,),
            )]
            candidate_count = len(prices)
            if prices:
                median = statistics.median(prices)
                mad = statistics.median(abs(price - median) for price in prices)
                if mad == 0:
                    retained = [price for price in prices if price == median]
                else:
                    cutoff = 3.5 * 1.4826 * mad
                    retained = [
                        price for price in prices if abs(price - median) <= cutoff
                    ]
            else:
                retained = []

            retained_count = len(retained)
            outlier_count = candidate_count - retained_count
            mean_usd = statistics.mean(retained) if retained_count >= 5 else None
            price_updated = False
            # A repeated or empty search is read-only for the current price.
            if new_count and mean_usd is not None:
                cursor = conn.execute(
                    "UPDATE items SET market_price_usd = ? WHERE id = ?",
                    (mean_usd, item_id),
                )
                price_updated = cursor.rowcount == 1

            conn.commit()
            return PoolResult(
                True, new_count, len(validated) - new_count, candidate_count,
                outlier_count, retained_count, mean_usd, price_updated,
            )
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise


def update_market_price(item_id, price):
    """
    Compatibility setter for direct callers; the eBay job uses the listing pool writer.
    Returns True if a row changed, False if no item has that id.
    """
    # checked before opening the database so a bad price never gets near it.
    # bool is a subclass of int in Python, so True would sneak through without the check.
    if (
        isinstance(price, bool)
        or not isinstance(price, (int, float))
        or not math.isfinite(price)
        or price <= 0
    ):
        raise ValueError(f"price must be a number greater than 0, got {price!r}")

    with closing(get_connection()) as conn:
        cursor = conn.execute(
            "UPDATE items SET market_price_usd = ? WHERE id = ?",
            (price,item_id)
        )
        conn.commit()
        changed = (cursor.rowcount == 1)
    if changed:
        print("Market price updated!")
    return changed

def get_item_id(brand_name,item_name):
    """Returns the item's id (active or not), or None if the brand or item doesn't exist."""
    with closing(get_connection()) as conn:
        row = conn.execute(
            "SELECT items.id FROM items JOIN brands ON brands.id = items.brand_id "
            "WHERE brands.name = ? AND items.name = ?",
            (brand_name,item_name)
        ).fetchone()
    # no conn commit bc we are accessing only, not changing.
    return None if row is None else row["id"]

def get_market_price(brand_name,item_name):
    """
    Returns the effective price: the eBay price if it is usable, else the
    catalog's manual price, else None (also None for an unknown item).
    Never 0 or negative, so the deal math can't divide by zero.
    """
    item_id = get_item_id(brand_name,item_name)
    if item_id is None:
        return None
    with closing(get_connection()) as conn:
        row = conn.execute(
            "SELECT market_price_usd, manual_price_usd FROM items WHERE id = ?",
            (item_id,)
        ).fetchone()
    return _effective_price(row["market_price_usd"], row["manual_price_usd"])


def main(argv=None):
    # parse first so --help and bad arguments exit before any database file is created
    parser = argparse.ArgumentParser(
        description="Create the RainCheck database, or upgrade an existing one to the current schema."
    )
    parser.add_argument(
        "--seed",
        action="store_true",
        help="after creating the tables, seed brands, items, and keywords from catalog.json",
    )
    args = parser.parse_args(argv)

    # load .env here so RAINCHECK_DB_PATH set there is honored, like the other scripts
    load_dotenv()
    try:
        init_db()
        if args.seed:
            seed_from_catalog()
    except (CatalogError, SchemaVersionError) as e:
        # these are problems you can fix, so print a clean message instead of a traceback
        print(f"Error: {e}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
