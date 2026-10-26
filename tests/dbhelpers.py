"""Plain-sqlite3 helpers shared by the RC-03a tests.

They deliberately bypass db.get_connection so they work on legacy (version 0)
and current (version 1) databases alike, and never trigger the version check.
"""
import hashlib
import sqlite3

TABLE_ORDER = {
    "brands": "id",
    "items": "id",
    "item_keywords": "item_id, keyword",
    "listings": "id",
    "listing_matches": "listing_id, item_id",
}


def connect(path):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def rows(path, sql, params=()):
    conn = connect(path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def run(path, sql, params=()):
    conn = connect(path)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def dump_table(path, table):
    return rows(path, f"SELECT * FROM {table} ORDER BY {TABLE_ORDER[table]}")


def dump_all(path):
    """Every table's full rows, ordered by primary key."""
    return {t: dump_table(path, t) for t in TABLE_ORDER}


def user_version(path):
    return rows(path, "PRAGMA user_version")[0][0]


def set_user_version(path, version):
    conn = sqlite3.connect(path)
    try:
        conn.execute(f"PRAGMA user_version = {int(version)}")
        conn.commit()
    finally:
        conn.close()


def columns(path, table):
    return [r[1] for r in rows(path, f"PRAGMA table_info({table})")]


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def keyword_sets(path):
    """{item name: {kind: set(keywords)}} for every item that has keywords."""
    out = {}
    for name, kind, keyword in rows(
        path,
        "SELECT i.name, k.kind, k.keyword FROM item_keywords k "
        "JOIN items i ON i.id = k.item_id",
    ):
        out.setdefault(name, {}).setdefault(kind, set()).add(keyword)
    return out


def assert_legacy_data_preserved(path, before):
    """Criterion 6: the migrated database keeps every legacy row.

    `before` is dump_all() of the legacy database taken before migrating.
    """
    for table in ("brands", "listings", "listing_matches"):
        assert dump_table(path, table) == before[table], table

    items = rows(
        path,
        "SELECT id, brand_id, name, market_price_usd, search_query, "
        "manual_price_usd, active FROM items ORDER BY id",
    )
    assert items == [
        (1, 1, "rick owens ramones", 420.0, "rick+owens+ramones", None, 1),
        (2, 1, "pod shorts", 300.0, "pod+shorts", None, 1),
    ]
    old_pairs = [(r[0], r[1]) for r in before["item_keywords"]]
    new = rows(path, "SELECT item_id, keyword, kind FROM item_keywords ORDER BY item_id, keyword")
    assert [(r[0], r[1]) for r in new] == old_pairs
    assert {r[2] for r in new} == {"include"}
    assert len(new) == 6
