"""Fixture databases for the RC-22 tests, built with plain sqlite3 and literal values.

Every expected result in the RC-22 tests was worked out by hand from the literals in
this module (see docs/specs/RC-22-sql-reporting.md, "Test plan").  Nothing here
aggregates, ranks, or otherwise computes a report value: the builders only INSERT.

Version 5 databases are built from src/schema.sql and stamped 5 directly, so they do
not depend on db.SCHEMA_VERSION.  Until the implementer lands schema version 5, the
builders that insert deal-time values fail with "no column named deal_kind", which
is the intended "feature missing" failure.
"""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_V5 = ROOT / "src" / "schema.sql"
SCHEMA_V4 = ROOT / "tests" / "fixtures" / "db" / "schema_v4.sql"

AUCTION = "https://auctions.yahoo.co.jp/jp/auction/"

DEAL_COLUMNS = (
    "deal_kind", "deal_price_yen", "deal_market_price_usd",
    "deal_yen_to_usd", "deal_rate_is_fallback", "deal_discount_pct",
)

_INSERT_LISTING = (
    "INSERT INTO listings (id, brand_id, url, title, price_yen, image_url, "
    "first_seen_at, last_seen_at, is_fixed_price, buy_now_price_yen) "
    "VALUES (?,?,?,?,?,?,?,?,?,?)"
)
_INSERT_PAIR = (
    "INSERT INTO listing_matches (listing_id, item_id, matched_at, "
    "deal_kind, deal_price_yen, deal_market_price_usd, deal_yen_to_usd, "
    "deal_rate_is_fallback, deal_discount_pct) VALUES (?,?,?,?,?,?,?,?,?)"
)
_INSERT_ITEM = (
    "INSERT INTO items (id, brand_id, name, market_price_usd, manual_price_usd, active) "
    "VALUES (?,?,?,?,?,?)"
)
_INSERT_OBSERVATION = (
    "INSERT INTO ebay_listing_prices (item_id, ebay_listing_id, first_price_usd, first_seen_at) "
    "VALUES (?,?,?,?)"
)

NO_DEAL = (None, None, None, None, None, None)


@contextmanager
def _open_v5(path, *, foreign_keys):
    """Create the version 5 schema, commit when the body succeeds, and always close the connection.

    Closing on failure matters: the builders fail on purpose until schema version 5 exists, and an
    unclosed connection would surface as a ResourceWarning in some unrelated later test.
    """
    conn = sqlite3.connect(path)
    try:
        conn.execute(f"PRAGMA foreign_keys = {'ON' if foreign_keys else 'OFF'}")
        conn.executescript(SCHEMA_V5.read_text(encoding="utf-8"))
        conn.execute("PRAGMA user_version = 5")
        yield conn
        conn.commit()
    finally:
        conn.close()


def _listing(n, brand_id, price_yen, seen_at, *, fixed=1, buy_now=None):
    return (
        n, brand_id, f"{AUCTION}t{n}", f"Listing {n}", price_yen, None,
        seen_at, seen_at, fixed, buy_now,
    )


def build_f0(path):
    """F0: a fresh, empty version 5 database."""
    with _open_v5(path, foreign_keys=True):
        pass
    return path


def build_f1(path):
    """F1: the main fixture for the report, trends, and doctor query tests."""
    with _open_v5(path, foreign_keys=True) as conn:
        conn.executemany(
            "INSERT INTO brands (id, name, yahoo_brand_id) VALUES (?,?,?)",
            [(1, "Rick Owens", "103538"), (2, "Undercover", "100"), (3, "Kapital", "200")],
        )
        conn.executemany(_INSERT_ITEM, [
            (1, 1, "Ramones", 400.0, None, 1),
            (2, 1, "Pod Shorts", None, 300.0, 1),
            (3, 2, "Scab Jacket", None, None, 1),
            (4, 1, "Geobasket", 500.0, None, 0),
            (5, 2, "Bones Tee", None, 100.0, 1),
            (6, 2, "Hoodie", 0.0, None, 1),
        ])
        conn.executemany(
            "INSERT INTO item_keywords (item_id, keyword, kind) VALUES (?,?,?)",
            [
                (1, "ramones", "include"),
                (2, "pod short", "include"),
                (3, "fake", "exclude"),
                (4, "geobasket", "include"),
                (5, "bones", "include"),
            ],
        )
        t_l1 = "2026-09-06T00:13:01.331953+00:00"
        t_l2 = "2026-10-20T00:00:00+00:00"
        t_l3 = "2026-10-20T00:00:00.500000+00:00"
        t_l4 = "2026-11-01T08:30:00.000001+00:00"
        t_l5 = "2026-11-05T09:00:00+00:00"
        t_l6 = "2026-10-31T23:59:59.999999+00:00"
        t_l7 = "2026-10-15T12:00:00+00:00"
        conn.executemany(_INSERT_LISTING, [
            _listing(1, 1, 50000, t_l1),
            _listing(2, 1, 59200, t_l2),
            # auction_buy_now: the listing stores the lower current bid, the pair the buy-now price
            _listing(3, 1, 20000, t_l3, fixed=0, buy_now=41200),
            _listing(4, 1, 48000, t_l4),
            _listing(5, 1, 2000, t_l5, fixed=0),
            _listing(6, 2, 10000, t_l6),
            _listing(7, 1, 81000, t_l7),
        ])
        conn.executemany(_INSERT_PAIR, [
            (1, 1, t_l1) + NO_DEAL,
            (2, 1, t_l2, "fixed_price", 59200, 400.0, 0.005, 0, 26.0),
            (3, 1, t_l3, "auction_buy_now", 41200, 400.0, 0.005, 0, 48.5),
            (4, 2, t_l4, "fixed_price", 48000, 300.0, 0.005, 0, 20.0),
            (4, 1, t_l4, "fixed_price", 48000, 400.0, 0.005, 0, 40.0),
            (5, 1, t_l5, "auction_watch", 2000, 400.0, 0.005, 0, 97.5),
            (7, 4, t_l7, "fixed_price", 81000, 500.0, 0.005, 0, 19.0),
            (6, 5, t_l6, "fixed_price", 10000, 100.0, 0.0062, 1, 38.0),
        ])
        # item 1 is inserted in this order on purpose: rest:b before legacy:a, e before d
        conn.executemany(_INSERT_OBSERVATION, [
            (1, "rest:b", 100.0, "2026-10-01T00:00:00+00:00"),
            (1, "legacy:a", 110.0, "2026-10-01T00:00:00+00:00"),
            (1, "legacy:c", 130.0, "2026-10-02T00:00:00.250000+00:00"),
            (1, "legacy:e", 120.0, "2026-10-03T00:00:00+00:00"),
            (1, "legacy:d", 90.0, "2026-10-03T00:00:00+00:00"),
            (2, "legacy:z", 250.0, "2026-10-05T00:00:00+00:00"),
            (4, "legacy:g", 400.0, "2026-10-06T00:00:00+00:00"),
        ])
    return path


def build_f2(path):
    """F2: deliberately corrupt rows; built with foreign keys OFF, the only way to create them."""
    with _open_v5(path, foreign_keys=False) as conn:
        conn.executemany(
            "INSERT INTO brands (id, name, yahoo_brand_id) VALUES (?,?,?)",
            [(1, "Rick Owens", "103538"), (2, "Undercover", "100")],
        )
        conn.executemany(_INSERT_ITEM, [
            (1, 1, "Ramones", None, 400.0, 1),
            (2, 2, "Scab Jacket", None, 300.0, 1),
            (3, 94, "Orphan Brand Item", None, 200.0, 0),  # brand 94 does not exist
        ])
        conn.executemany(
            "INSERT INTO item_keywords (item_id, keyword, kind) VALUES (?,?,?)",
            [
                (1, "ramones", "include"),
                (2, "scab", "include"),
                (3, "orphan", "include"),
                (997, "ghost", "include"),  # item 997 does not exist
            ],
        )
        ok = "2026-10-01T12:00:00+00:00"
        conn.executemany(_INSERT_LISTING, [
            _listing(10, 1, 60000, ok),
            _listing(11, 95, 1000, ok),  # brand 95 does not exist
            # listing 12 has brand 2 but is matched to item 1 (brand 1); first_seen_at is noncanonical
            (12, 2, f"{AUCTION}t12", "Listing 12", 1000, None,
             "2026-10-01 12:00:00", ok, 1, None),
            _listing(13, 1, 60000, ok),
        ])
        conn.executemany(_INSERT_PAIR, [
            (10, 1, ok, "fixed_price", 60000, 400.0, 0.005, 0, 50.0),       # stored 50.0, computed 25.0
            (13, 1, ok, "fixed_price", 60000, 400.0, 0.005, 0, 25.0005),    # within tolerance
            (12, 1, "2026-10-01T12:00:00Z") + NO_DEAL,                       # noncanonical matched_at
            (99, 1, ok) + NO_DEAL,                                           # listing 99 missing
            (10, 999, ok) + NO_DEAL,                                         # item 999 missing
            (98, 998, ok) + NO_DEAL,                                         # listing 98 and item 998 missing
        ])
        observations = []
        for item_id in (1, 2):
            for n in range(1, 6):
                observations.append((item_id, f"legacy:{item_id}{n}", 100.0 + n, f"2026-10-0{n}T00:00:00+00:00"))
        # replace item 2's fifth observation with one whose fractional part is too short
        observations[-1] = (2, "legacy:short", 105.0, "2026-10-01T12:00:00.25+00:00")
        observations.append((996, "legacy:x", 120.0, ok))  # item 996 missing
        conn.executemany(_INSERT_OBSERVATION, observations)
    return path


def build_single_row(path):
    """One brand, one item, one priced fixed-price deal of 12.3 percent (criterion 29)."""
    with _open_v5(path, foreign_keys=True) as conn:
        conn.execute("INSERT INTO brands (id, name, yahoo_brand_id) VALUES (1, 'Rick Owens', '103538')")
        conn.execute(_INSERT_ITEM, (1, 1, "Ramones", 500.0, None, 1))
        seen = "2026-10-12T03:04:05+00:00"
        conn.execute(_INSERT_LISTING, _listing(1, 1, 87700, seen))
        conn.execute(_INSERT_PAIR, (1, 1, seen, "fixed_price", 87700, 500.0, 0.005, 0, 12.3))
    return path


def build_pre_v5_only(path):
    """One brand whose only pair predates version 5: every deal-time column NULL (criterion 30)."""
    with _open_v5(path, foreign_keys=True) as conn:
        conn.execute("INSERT INTO brands (id, name, yahoo_brand_id) VALUES (1, 'Kapital', '200')")
        conn.execute(_INSERT_ITEM, (1, 1, "Century Denim", 300.0, None, 1))
        seen = "2026-09-06T00:13:01.331953+00:00"
        conn.execute(_INSERT_LISTING, _listing(1, 1, 40000, seen))
        conn.execute(_INSERT_PAIR, (1, 1, seen) + NO_DEAL)
    return path


def build_watch_only(path):
    """One brand whose only pair is an auction watch (criterion 31)."""
    with _open_v5(path, foreign_keys=True) as conn:
        conn.execute("INSERT INTO brands (id, name, yahoo_brand_id) VALUES (1, 'Undercover', '100')")
        conn.execute(_INSERT_ITEM, (1, 1, "Scab Jacket", 400.0, None, 1))
        seen = "2026-11-05T09:00:00+00:00"
        conn.execute(_INSERT_LISTING, _listing(1, 1, 2000, seen, fixed=0))
        conn.execute(_INSERT_PAIR, (1, 1, seen, "auction_watch", 2000, 400.0, 0.005, 0, 97.5))
    return path


def build_warning_only(path):
    """A healthy database whose only finding is a warning: an item with no eBay observations."""
    with _open_v5(path, foreign_keys=True) as conn:
        conn.execute("INSERT INTO brands (id, name, yahoo_brand_id) VALUES (1, 'Rick Owens', '103538')")
        conn.execute(_INSERT_ITEM, (1, 1, "Ramones", None, 400.0, 1))
        conn.execute("INSERT INTO item_keywords (item_id, keyword, kind) VALUES (1, 'ramones', 'include')")
    return path


def build_v4_database(path):
    """A version 4 database with at least one row in every table, for migration tests."""
    conn = sqlite3.connect(path)
    try:
        _fill_v4(conn)
        conn.commit()
    finally:
        conn.close()
    return path


def _fill_v4(conn):
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_V4.read_text(encoding="utf-8"))
    conn.executemany(
        "INSERT INTO brands (id, name, yahoo_brand_id) VALUES (?,?,?)",
        [(1, "Rick Owens", "103538"), (2, "Undercover", "100")],
    )
    conn.executemany(
        "INSERT INTO items (id, brand_id, name, market_price_usd, search_query, "
        "manual_price_usd, active, config_source) VALUES (?,?,?,?,?,?,?,?)",
        [
            (1, 1, "Ramones", 412.5, "rick+owens+ramones", 400.0, 1, "catalog"),
            (2, 1, "Pod Shorts", None, None, 300.0, 1, "cli"),
            (3, 2, "Scab Jacket", None, "scab", None, 0, "unclaimed"),
        ],
    )
    conn.executemany(
        "INSERT INTO item_keywords (item_id, keyword, kind) VALUES (?,?,?)",
        [(1, "ramones", "include"), (1, "fake", "exclude"), (2, "pod short", "include")],
    )
    conn.executemany(
        "INSERT INTO listings (id, brand_id, url, title, price_yen, image_url, first_seen_at, "
        "last_seen_at, ended_at, final_price_yen, is_fixed_price, buy_now_price_yen, scheduled_end_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (1, 1, f"{AUCTION}t1", "Ramones High", 50000, "https://img.test/1.jpg",
             "2026-09-06T00:13:01.331953+00:00", "2026-09-07T00:00:00+00:00", None, None, None, None, None),
            (2, 1, f"{AUCTION}t2", "Pod Shorts", 48000, None,
             "2026-10-20T00:00:00+00:00", "2026-10-20T00:00:00+00:00",
             "2026-10-22T00:00:00+00:00", 47000, 0, 90000, "2026-10-22T00:00:00+00:00"),
            (3, 2, f"{AUCTION}t3", "Scab Jacket", 10000, None,
             "2026-10-21T00:00:00+00:00", "2026-10-21T00:00:00+00:00", None, None, 1, None, None),
        ],
    )
    conn.executemany(
        "INSERT INTO listing_matches (listing_id, item_id, matched_at) VALUES (?,?,?)",
        [
            (1, 1, "2026-09-06T00:13:01.331953+00:00"),
            (2, 2, "2026-10-20T00:00:00+00:00"),
            (2, 1, "2026-10-20T00:00:00+00:00"),
            (3, 3, "2026-10-21T00:00:00+00:00"),
        ],
    )
    conn.executemany(_INSERT_OBSERVATION, [
        (1, "legacy:a", 110.0, "2026-10-01T00:00:00+00:00"),
        (1, "legacy:b", 100.0, "2026-10-01T00:00:00+00:00"),
        (2, "legacy:z", 250.0, "2026-10-05T00:00:00.250000+00:00"),
    ])
    conn.commit()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
