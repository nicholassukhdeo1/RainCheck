import sqlite3
from datetime import datetime, timedelta

import pytest

import db

TABLES = ["brands", "items", "item_keywords", "listings", "listing_matches"]


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _brand_id(conn):
    return conn.execute("SELECT id FROM brands WHERE name = 'Rick Owens'").fetchone()["id"]


def test_c22_init_db_twice_leaves_five_empty_tables(temp_db, db_conn):
    db.init_db()
    names = {r[0] for r in db_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in TABLES:
        assert table in names
        assert _count(db_conn, table) == 0


def test_c23_seed_inserts_brand_items_and_keywords(seeded_db, db_conn):
    brands = db_conn.execute("SELECT name, yahoo_brand_id FROM brands").fetchall()
    assert [(b["name"], b["yahoo_brand_id"]) for b in brands] == [("Rick Owens", "103538")]
    items = {
        r["name"]: (r["market_price_usd"], r["search_query"])
        for r in db_conn.execute("SELECT name, market_price_usd, search_query FROM items")
    }
    assert items == {
        "rick owens ramones": (800.0, "rick+owens+ramones"),
        "pod shorts": (300.0, "pod+shorts"),
    }
    kws = {}
    for r in db_conn.execute(
        "SELECT i.name AS n, k.keyword AS k FROM item_keywords k JOIN items i ON i.id = k.item_id"
    ):
        kws.setdefault(r["n"], set()).add(r["k"])
    assert kws == {"rick owens ramones": {"ramones", "ramon"}, "pod shorts": {"pod short"}}


def test_c24_seeding_twice_does_not_duplicate_rows(seeded_db, db_conn):
    before = {t: _count(db_conn, t) for t in ("brands", "items", "item_keywords")}
    db.seed_from_catalog()
    after = {t: _count(db_conn, t) for t in ("brands", "items", "item_keywords")}
    assert before == after
    assert before == {"brands": 1, "items": 2, "item_keywords": 3}


def test_c25_insert_new_listing_returns_true_and_stores_utc_timestamps(seeded_db, db_conn):
    brand_id = _brand_id(db_conn)
    assert db.insert_listing(brand_id, "https://x/1", "Title", 50000, "https://img/1.jpg") is True
    rows = db_conn.execute("SELECT * FROM listings").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert (row["brand_id"], row["url"], row["title"], row["price_yen"], row["image_url"]) == (
        brand_id, "https://x/1", "Title", 50000, "https://img/1.jpg",
    )
    assert row["first_seen_at"] == row["last_seen_at"]
    parsed = datetime.fromisoformat(row["first_seen_at"])
    assert parsed.utcoffset() == timedelta(0)


def test_c26_duplicate_url_returns_false_and_keeps_original_row(seeded_db, db_conn):
    brand_id = _brand_id(db_conn)
    db.insert_listing(brand_id, "https://x/1", "Original", 50000, "https://img/1.jpg")
    original = dict(db_conn.execute("SELECT * FROM listings").fetchone())
    assert db.insert_listing(brand_id, "https://x/1", "Changed", 99999, "https://img/2.jpg") is False
    rows = db_conn.execute("SELECT * FROM listings").fetchall()
    assert len(rows) == 1
    assert rows[0]["title"] == "Original"
    assert rows[0]["price_yen"] == 50000
    assert rows[0]["first_seen_at"] == original["first_seen_at"]


def test_c27_different_url_inserts_second_row(seeded_db, db_conn):
    brand_id = _brand_id(db_conn)
    assert db.insert_listing(brand_id, "https://x/1", "A", 1, "i1") is True
    assert db.insert_listing(brand_id, "https://x/2", "B", 2, "i2") is True
    assert _count(db_conn, "listings") == 2


def test_c28_insert_with_unknown_brand_raises_integrity_error(seeded_db, db_conn):
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_listing(9999, "https://x/1", "A", 1, "i1")


def test_c29_get_item_id_returns_matching_row_id(seeded_db, db_conn):
    expected = db_conn.execute(
        "SELECT id FROM items WHERE name = 'rick owens ramones'"
    ).fetchone()["id"]
    assert db.get_item_id("Rick Owens", "rick owens ramones") == expected


def test_c30_get_item_id_is_scoped_to_requested_brand(temp_db, db_conn):
    db_conn.execute("INSERT INTO brands (name, yahoo_brand_id) VALUES ('BrandA', 'a1')")
    db_conn.execute("INSERT INTO brands (name, yahoo_brand_id) VALUES ('BrandB', 'b1')")
    a = db_conn.execute("SELECT id FROM brands WHERE name='BrandA'").fetchone()["id"]
    b = db_conn.execute("SELECT id FROM brands WHERE name='BrandB'").fetchone()["id"]
    db_conn.execute("INSERT INTO items (brand_id, name) VALUES (?, 'ramones')", (a,))
    db_conn.execute("INSERT INTO items (brand_id, name) VALUES (?, 'ramones')", (b,))
    db_conn.commit()
    item_b = db_conn.execute(
        "SELECT id FROM items WHERE brand_id = ? AND name = 'ramones'", (b,)
    ).fetchone()["id"]
    item_a = db_conn.execute(
        "SELECT id FROM items WHERE brand_id = ? AND name = 'ramones'", (a,)
    ).fetchone()["id"]
    assert item_a != item_b
    assert db.get_item_id("BrandB", "ramones") == item_b
    assert db.get_item_id("BrandA", "ramones") == item_a


def test_c31_update_market_price_changes_only_target_item(seeded_db, db_conn):
    item_id = db.get_item_id("Rick Owens", "rick owens ramones")
    db.update_market_price(item_id, 912.5)
    assert db.get_market_price("Rick Owens", "rick owens ramones") == 912.5
    row = db_conn.execute("SELECT market_price_usd FROM items WHERE name='pod shorts'").fetchone()
    assert row["market_price_usd"] == 300.0


def test_c32_get_market_price_returns_none_for_null_price(seeded_db, db_conn):
    db_conn.execute("UPDATE items SET market_price_usd = NULL WHERE name = 'pod shorts'")
    db_conn.commit()
    assert db.get_market_price("Rick Owens", "pod shorts") is None
