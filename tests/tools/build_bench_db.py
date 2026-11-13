"""Build the deterministic RC-22 benchmark database used for the index decision.

Usage: python3 tests/tools/build_bench_db.py <new database path>

The database is at schema version 5 and holds 5 brands, 200 items (180 active), 3 include and 1
exclude keyword per item, 500,000 eBay observations (2,500 per item, in batches of 50 that share
one canonical first_seen_at), 50,000 listings, and 55,000 listing matches (10 percent of listings
match two items, 80 percent of pairs carry deal-time values, 10 percent of those are watches).

It only INSERTs generated data.  It never runs ANALYZE, because production never does and the
query plans must reflect production statistics.  It refuses an existing path and the real
src/raincheck.db.  It is a script for the benchmark process, not collected by pytest.
"""
import random
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "src" / "schema.sql"
REAL_DB_PATH = ROOT / "src" / "raincheck.db"
SEED = 20261110
SCHEMA_VERSION = 5

BRANDS = 5
ITEMS = 200
ITEMS_PER_BRAND = ITEMS // BRANDS
OBSERVATIONS_PER_ITEM = 2500
BATCH_SIZE = 50
LISTINGS = 50_000
DOUBLE_MATCH_LISTINGS = 5_000  # 10 percent of listings match two items
BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _stamp(minutes):
    return (BASE_TIME + timedelta(minutes=minutes)).isoformat()


def _brand_of_item(item_id):
    return (item_id - 1) // ITEMS_PER_BRAND + 1


def build(path):
    rng = random.Random(SEED)
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.execute("BEGIN")
        conn.executemany(
            "INSERT INTO brands (id, name, yahoo_brand_id) VALUES (?,?,?)",
            [(b, f"Brand {b}", str(100000 + b)) for b in range(1, BRANDS + 1)],
        )
        conn.executemany(
            "INSERT INTO items (id, brand_id, name, market_price_usd, manual_price_usd, active) "
            "VALUES (?,?,?,?,?,?)",
            [
                (i, _brand_of_item(i), f"Item {i}", round(rng.uniform(50, 1500), 2),
                 round(rng.uniform(50, 1500), 2), 0 if i % 10 == 0 else 1)
                for i in range(1, ITEMS + 1)
            ],
        )
        keywords = []
        for i in range(1, ITEMS + 1):
            keywords += [(i, f"kw{i}a", "include"), (i, f"kw{i}b", "include"),
                         (i, f"kw{i}c", "include"), (i, f"bad{i}", "exclude")]
        conn.executemany("INSERT INTO item_keywords (item_id, keyword, kind) VALUES (?,?,?)", keywords)

        observations = []
        for i in range(1, ITEMS + 1):
            for n in range(OBSERVATIONS_PER_ITEM):
                batch = n // BATCH_SIZE
                observations.append(
                    (i, f"b{i}-{n:05d}", round(rng.uniform(20, 2000), 2), _stamp(batch * 60 + i))
                )
        conn.executemany(
            "INSERT INTO ebay_listing_prices (item_id, ebay_listing_id, first_price_usd, first_seen_at) "
            "VALUES (?,?,?,?)",
            observations,
        )

        listings, matches = [], []
        pair_index = 0
        for n in range(1, LISTINGS + 1):
            brand = rng.randint(1, BRANDS)
            seen = _stamp(rng.randint(0, 60 * 24 * 300))
            price = rng.randint(1000, 300000)
            listings.append((
                n, brand, f"https://auctions.yahoo.co.jp/jp/auction/b{n}", f"Bench listing {n}", price,
                None, seen, seen, 1, None,
            ))
            first = (brand - 1) * ITEMS_PER_BRAND + rng.randint(1, ITEMS_PER_BRAND)
            chosen = [first]
            if n <= DOUBLE_MATCH_LISTINGS:
                second = first
                while second == first:
                    second = (brand - 1) * ITEMS_PER_BRAND + rng.randint(1, ITEMS_PER_BRAND)
                chosen.append(second)
            for item_id in chosen:
                if pair_index % 10 < 8:  # 80 percent of pairs carry deal-time values
                    kind = "auction_watch" if (pair_index // 10) % 10 == 0 else (
                        "fixed_price" if pair_index % 2 == 0 else "auction_buy_now")
                    deal = (kind, price, round(rng.uniform(50, 1500), 2), 0.0062, 0,
                            round(rng.uniform(1, 90), 4))
                else:
                    deal = (None,) * 6
                matches.append((n, item_id, seen) + deal)
                pair_index += 1
        conn.executemany(
            "INSERT INTO listings (id, brand_id, url, title, price_yen, image_url, first_seen_at, "
            "last_seen_at, is_fixed_price, buy_now_price_yen) VALUES (?,?,?,?,?,?,?,?,?,?)",
            listings,
        )
        conn.executemany(
            "INSERT INTO listing_matches (listing_id, item_id, matched_at, deal_kind, deal_price_yen, "
            "deal_market_price_usd, deal_yen_to_usd, deal_rate_is_fallback, deal_discount_pct) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            matches,
        )
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.execute("COMMIT")
    except BaseException:
        conn.close()
        Path(path).unlink(missing_ok=True)
        raise
    conn.close()


def main(argv):
    if len(argv) != 2:
        print("usage: build_bench_db.py <new database path>", file=sys.stderr)
        return 2
    path = Path(argv[1])
    if path.resolve() == REAL_DB_PATH.resolve() or path.exists():
        print(f"Error: refusing to write to {path}: it exists or is the real database.", file=sys.stderr)
        return 1
    build(path)
    print(f"Built benchmark database at {path}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
