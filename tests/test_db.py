import gc
import sqlite3
import warnings
from datetime import datetime, timedelta, timezone

import pytest

import db

TABLES = ["brands", "items", "item_keywords", "listings", "listing_matches", "ebay_listing_prices"]


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _brand_id(conn):
    return conn.execute("SELECT id FROM brands WHERE name = 'Rick Owens'").fetchone()["id"]


def test_rc06_c1_init_db_twice_leaves_empty_schema_tables(temp_db, db_conn):
    db.init_db()
    names = {r[0] for r in db_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in TABLES:
        assert table in names
        assert _count(db_conn, table) == 0


def test_c23_seed_inserts_brand_items_and_keywords(seeded_db, db_conn):
    brands = db_conn.execute("SELECT name, yahoo_brand_id FROM brands").fetchall()
    assert [(b["name"], b["yahoo_brand_id"]) for b in brands] == [("Rick Owens", "103538")]
    items = {
        r["name"]: (r["manual_price_usd"], r["market_price_usd"], r["search_query"])
        for r in db_conn.execute(
            "SELECT name, manual_price_usd, market_price_usd, search_query FROM items"
        )
    }
    assert items == {
        "rick owens ramones": (800.0, None, "rick owens ramones"),
        "pod shorts": (300.0, None, "pod shorts"),
    }
    kws = {}
    for r in db_conn.execute(
        "SELECT i.name AS n, k.kind AS kind, k.keyword AS k "
        "FROM item_keywords k JOIN items i ON i.id = k.item_id"
    ):
        kws.setdefault((r["n"], r["kind"]), set()).add(r["k"])
    assert kws == {
        ("rick owens ramones", "include"): {"ramones", "ramon"},
        ("rick owens ramones", "exclude"): {"drkshdw", "canvas", "low", "fake"},
        ("pod shorts", "include"): {"pod short", "pod shorts"},
        ("pod shorts", "exclude"): {"pants", "fake"},
    }


def test_c24_seeding_twice_does_not_duplicate_rows(seeded_db, db_conn):
    before = {t: _count(db_conn, t) for t in ("brands", "items", "item_keywords")}
    db.seed_from_catalog()
    after = {t: _count(db_conn, t) for t in ("brands", "items", "item_keywords")}
    assert before == after
    assert before == {"brands": 1, "items": 2, "item_keywords": 10}
    kinds = dict(
        db_conn.execute("SELECT kind, COUNT(*) FROM item_keywords GROUP BY kind").fetchall()
    )
    assert kinds == {"include": 4, "exclude": 6}


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
    row = db_conn.execute(
        "SELECT market_price_usd, manual_price_usd FROM items WHERE name='pod shorts'"
    ).fetchone()
    assert row["market_price_usd"] is None
    assert row["manual_price_usd"] == 300.0


def test_c32_get_market_price_returns_none_for_null_price(seeded_db, db_conn):
    db_conn.execute(
        "UPDATE items SET market_price_usd = NULL, manual_price_usd = NULL WHERE name = 'pod shorts'"
    )
    db_conn.commit()
    assert db.get_market_price("Rick Owens", "pod shorts") is None


# --- RC-03a: read API and lookups (criteria 30-37) ---------------------------


def _items_table(conn):
    return [tuple(r) for r in conn.execute("SELECT * FROM items ORDER BY id").fetchall()]


def test_rc03a_c30_get_tracked_items_returns_full_configuration(seeded_db, db_conn):
    ids = [r["id"] for r in db_conn.execute("SELECT id FROM items ORDER BY id")]
    brand_id = _brand_id(db_conn)

    tracked = db.get_tracked_items()

    assert all(isinstance(t, db.TrackedItem) for t in tracked)
    assert tracked == [
        db.TrackedItem(
            id=ids[0],
            brand_id=brand_id,
            brand="Rick Owens",
            name="rick owens ramones",
            search_query="rick owens ramones",
            keywords=("ramon", "ramones"),
            exclude_keywords=("canvas", "drkshdw", "fake", "low"),
            manual_price_usd=800.0,
            market_price_usd=None,
        ),
        db.TrackedItem(
            id=ids[1],
            brand_id=brand_id,
            brand="Rick Owens",
            name="pod shorts",
            search_query="pod shorts",
            keywords=("pod short", "pod shorts"),
            exclude_keywords=("fake", "pants"),
            manual_price_usd=300.0,
            market_price_usd=None,
        ),
    ]


# (market_price_usd, manual_price_usd, expected effective price)
PRICE_CASES = [
    pytest.param(912.5, 800.0, 912.5, id="ebay-wins"),
    pytest.param(None, 800.0, 800.0, id="null-market-falls-back"),
    pytest.param(0, 800.0, 800.0, id="zero-market-falls-back"),
    pytest.param(-1, 800.0, 800.0, id="negative-market-falls-back"),
    pytest.param(float("inf"), 800.0, 800.0, id="infinite-market-falls-back"),
    pytest.param(None, None, None, id="nothing-usable"),
    pytest.param(0, None, None, id="zero-market-and-no-manual"),
    pytest.param(-1, None, None, id="negative-market-and-no-manual"),
]


def _set_prices(conn, market, manual):
    conn.execute(
        "UPDATE items SET market_price_usd = ?, manual_price_usd = ? WHERE name = 'rick owens ramones'",
        (market, manual),
    )
    conn.commit()


@pytest.mark.parametrize("market,manual,expected", PRICE_CASES)
def test_rc03a_c31_effective_price_rule(seeded_db, db_conn, market, manual, expected):
    _set_prices(db_conn, market, manual)
    tracked = {t.name: t for t in db.get_tracked_items()}
    assert tracked["rick owens ramones"].effective_price_usd == expected


@pytest.mark.parametrize("market,manual,expected", PRICE_CASES)
def test_rc03a_c33_get_market_price_uses_the_effective_price_rule(
    seeded_db, db_conn, market, manual, expected
):
    _set_prices(db_conn, market, manual)
    assert db.get_market_price("Rick Owens", "rick owens ramones") == expected


def test_rc03a_c32_inactive_items_are_not_tracked(seeded_db, db_conn):
    db_conn.execute("UPDATE items SET active = 0 WHERE name = 'pod shorts'")
    db_conn.commit()
    assert [t.name for t in db.get_tracked_items()] == ["rick owens ramones"]


def test_rc03a_c32_no_active_items_gives_empty_list(seeded_db, db_conn):
    db_conn.execute("UPDATE items SET active = 0 WHERE id > 0")
    db_conn.commit()
    assert db.get_tracked_items() == []


def test_rc03a_c32_item_without_exclude_rows_has_empty_exclude_tuple(seeded_db, db_conn):
    brand_id = _brand_id(db_conn)
    db_conn.execute("INSERT INTO items (brand_id, name) VALUES (?, 'plain item')", (brand_id,))
    item_id = db_conn.execute("SELECT id FROM items WHERE name = 'plain item'").fetchone()["id"]
    db_conn.execute("INSERT INTO item_keywords (item_id, keyword) VALUES (?, 'plain')", (item_id,))
    db_conn.commit()

    plain = [t for t in db.get_tracked_items() if t.name == "plain item"][0]

    assert plain.exclude_keywords == ()
    assert plain.keywords == ("plain",)


def test_rc03a_c34_unknown_brand_returns_none(seeded_db):
    assert db.get_item_id("No Such Brand", "pod shorts") is None
    assert db.get_market_price("No Such Brand", "pod shorts") is None


def test_rc03a_c34_unknown_item_of_known_brand_returns_none(seeded_db):
    assert db.get_item_id("Rick Owens", "no such item") is None
    assert db.get_market_price("Rick Owens", "no such item") is None


def test_rc03a_c35_get_item_id_returns_inactive_items(seeded_db, db_conn):
    expected = db_conn.execute("SELECT id FROM items WHERE name = 'pod shorts'").fetchone()["id"]
    db_conn.execute("UPDATE items SET active = 0 WHERE name = 'pod shorts'")
    db_conn.commit()
    assert db.get_item_id("Rick Owens", "pod shorts") == expected


def test_rc10_c28_update_market_price_changes_cell_without_operational_print(
    seeded_db, db_conn, capsys
):
    item_id = db.get_item_id("Rick Owens", "rick owens ramones")
    before = _items_table(db_conn)
    capsys.readouterr()

    assert db.update_market_price(item_id, 912.5) is True

    assert capsys.readouterr().out == ""
    after = _items_table(db_conn)
    cols = [r[1] for r in db_conn.execute("PRAGMA table_info(items)")]
    market = cols.index("market_price_usd")
    for old, new in zip(before, after):
        if old[0] == item_id:
            changed = [cols[i] for i in range(len(cols)) if old[i] != new[i]]
            assert changed == ["market_price_usd"]
            assert new[market] == 912.5
        else:
            assert old == new


def test_rc03a_c36_update_market_price_for_unknown_id_returns_false(seeded_db, db_conn, capsys):
    before = _items_table(db_conn)
    capsys.readouterr()

    assert db.update_market_price(9999, 912.5) is False

    assert "Market price updated!" not in capsys.readouterr().out
    assert _items_table(db_conn) == before


@pytest.mark.parametrize(
    "bad_price",
    [0, -1, float("nan"), float("inf"), True, None, "900"],
    ids=["zero", "negative", "nan", "inf", "bool", "none", "string"],
)
def test_rc03a_c37_update_market_price_rejects_invalid_prices(seeded_db, db_conn, bad_price):
    item_id = db.get_item_id("Rick Owens", "rick owens ramones")
    before = _items_table(db_conn)

    with pytest.raises(ValueError):
        db.update_market_price(item_id, bad_price)

    assert _items_table(db_conn) == before


def test_rc03b_c46_insert_listing_with_unknown_brand_raises_and_leaks_no_connection(seeded_db):
    def attempt():
        with pytest.raises(sqlite3.IntegrityError):
            db.insert_listing(99999, "https://x/orphan", "Title", 50000, None)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        attempt()
        # the exception (and its traceback frames) are gone once attempt() returns, so an
        # unclosed connection is now garbage and collecting it emits the ResourceWarning
        gc.collect()

    leaks = [w for w in caught if issubclass(w.category, ResourceWarning)]
    assert leaks == []


# ---- RC-15 get_known_listing_urls (criteria 35-38) -----------------------------------


def test_rc15_c35_returns_only_the_stored_urls_for_lists_and_generators(seeded_db):
    brand_id = db.get_tracked_items()[0].brand_id
    db.insert_listing(brand_id, "https://x/1", "T1", 1000, None)
    db.insert_listing(brand_id, "https://x/2", "T2", 2000, None)
    assert db.get_known_listing_urls(["https://x/1", "https://x/3", "https://x/1"]) == {"https://x/1"}
    gen = (u for u in ["https://x/1", "https://x/3", "https://x/1"])
    assert db.get_known_listing_urls(gen) == {"https://x/1"}


def test_rc15_c36_no_urls_returns_empty_set_without_opening_the_database(tmp_path):
    path = tmp_path / "test.db"
    assert not path.exists()
    assert db.get_known_listing_urls([]) == set()
    assert db.get_known_listing_urls(iter(())) == set()
    assert not path.exists()


def test_rc15_c37_lookup_writes_nothing_and_leaks_no_connection(seeded_db):
    from dbhelpers import sha256

    brand_id = db.get_tracked_items()[0].brand_id
    db.insert_listing(brand_id, "https://x/1", "T1", 1000, None)
    before = sha256(seeded_db)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert db.get_known_listing_urls(["https://x/1", "https://x/9"]) == {"https://x/1"}
        gc.collect()
    assert [w for w in caught if issubclass(w.category, ResourceWarning)] == []
    assert sha256(seeded_db) == before


def test_rc15_c38_legacy_database_raises_schema_version_error(legacy_db):
    with pytest.raises(db.SchemaVersionError):
        db.get_known_listing_urls(["https://x/1"])


# ---- RC-05 existing listing observation timestamps (criteria 6-8, 10) ---------------


def test_rc05_c6_refreshes_distinct_existing_urls_from_one_shot_iterable(
    seeded_db, db_conn, monkeypatch
):
    brand_id = _brand_id(db_conn)
    db.insert_listing(brand_id, "https://x/1", "One", 1000, "https://img/1")
    db.insert_listing(brand_id, "https://x/2", "Two", 2000, "https://img/2")
    db_conn.execute(
        "UPDATE listings SET last_seen_at = ? WHERE url IN (?, ?)",
        ("2020-01-01T00:00:00+00:00", "https://x/1", "https://x/2"),
    )
    db_conn.commit()
    before = {
        row[0]: tuple(row)
        for row in db_conn.execute(
            "SELECT url, first_seen_at, last_seen_at, title, price_yen, image_url "
            "FROM listings ORDER BY url"
        )
    }

    consumed = []

    def urls():
        for url in (
            "https://x/1",
            "https://x/1",
            "https://x/2",
            "https://x/unknown",
        ):
            consumed.append(url)
            yield url

    real_connect = db.get_connection
    opened = []

    class TrackedConnection:
        def __init__(self):
            self.connection = real_connect()
            self.closed = False

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def close(self):
            self.closed = True
            self.connection.close()

    def tracked_connection():
        connection = TrackedConnection()
        opened.append(connection)
        return connection

    monkeypatch.setattr(db, "get_connection", tracked_connection)
    observed_at = datetime(2035, 4, 5, 6, 7, 8, tzinfo=timezone.utc)
    changed = db.update_last_seen_at(urls(), observed_at)

    assert len(opened) == 1
    assert opened[0].closed is True
    assert consumed == ["https://x/1", "https://x/1", "https://x/2", "https://x/unknown"]
    assert changed == 2
    rows = db_conn.execute(
        "SELECT url, first_seen_at, last_seen_at, title, price_yen, image_url "
        "FROM listings ORDER BY url"
    ).fetchall()
    for row in rows:
        previous = before[row["url"]]
        assert row["first_seen_at"] == previous[1]
        assert row["last_seen_at"] == observed_at.isoformat()
        assert tuple(row)[3:] == previous[3:]
    assert _count(db_conn, "listings") == 2


def test_rc05_c7_empty_urls_return_zero_without_opening_connection(
    monkeypatch, tmp_path
):
    path = tmp_path / "must-not-be-created.db"

    def unexpected_connection():
        raise AssertionError("empty input must not open a database connection")

    monkeypatch.setattr(db, "get_connection", unexpected_connection)

    assert db.update_last_seen_at(iter(()), datetime(2035, 1, 1, tzinfo=timezone.utc)) == 0
    assert not path.exists()


@pytest.mark.parametrize(
    "bad_observation",
    [
        datetime(2035, 1, 1),
        datetime(2035, 1, 1, tzinfo=timezone(timedelta(hours=2))),
    ],
    ids=["naive", "non-utc"],
)
def test_rc05_c8_rejects_naive_or_non_utc_time_before_connecting(
    monkeypatch, bad_observation
):
    def unexpected_connection():
        raise AssertionError("invalid observation time must be rejected before connecting")

    monkeypatch.setattr(db, "get_connection", unexpected_connection)

    with pytest.raises(ValueError):
        db.update_last_seen_at(["https://x/1"], bad_observation)


def test_rc05_c8_equal_or_older_observation_does_not_move_timestamp_back(
    seeded_db, db_conn
):
    brand_id = _brand_id(db_conn)
    db.insert_listing(brand_id, "https://x/1", "One", 1000, None)
    stored = "2035-04-05T06:07:08+00:00"
    db_conn.execute(
        "UPDATE listings SET last_seen_at = ? WHERE url = ?", (stored, "https://x/1")
    )
    db_conn.commit()

    assert db.update_last_seen_at(
        ["https://x/1"], datetime.fromisoformat(stored)
    ) == 0
    assert db.update_last_seen_at(
        ["https://x/1"], datetime(2035, 4, 5, 6, 7, 7, tzinfo=timezone.utc)
    ) == 0
    assert db_conn.execute(
        "SELECT last_seen_at FROM listings WHERE url = ?", ("https://x/1",)
    ).fetchone()[0] == stored


def test_rc05_c10_batch_failure_rolls_back_all_rows_and_closes_connection(
    seeded_db, db_conn, monkeypatch
):
    brand_id = _brand_id(db_conn)
    db.insert_listing(brand_id, "https://x/1", "One", 1000, None)
    db.insert_listing(brand_id, "https://x/2", "Two", 2000, None)
    old = "2020-01-01T00:00:00+00:00"
    db_conn.execute(
        "UPDATE listings SET last_seen_at = ? WHERE url IN (?, ?)",
        (old, "https://x/1", "https://x/2"),
    )
    db_conn.execute(
        "CREATE TRIGGER reject_second_lifecycle_update "
        "BEFORE UPDATE OF last_seen_at ON listings "
        "WHEN OLD.url = 'https://x/2' "
        "BEGIN SELECT RAISE(ABORT, 'injected lifecycle failure'); END"
    )
    db_conn.commit()

    real_connect = db.get_connection
    observed = {}

    class TrackedConnection:
        def __init__(self):
            self.connection = real_connect()
            self.closed = False

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def close(self):
            self.closed = True
            self.connection.close()

    def tracked_connection():
        connection = TrackedConnection()
        observed["connection"] = connection
        return connection

    monkeypatch.setattr(db, "get_connection", tracked_connection)
    with pytest.raises(sqlite3.IntegrityError, match="injected lifecycle failure"):
        db.update_last_seen_at(
            ["https://x/1", "https://x/2"],
            datetime(2035, 4, 5, tzinfo=timezone.utc),
        )

    assert observed["connection"].closed is True
    rows = db_conn.execute("SELECT last_seen_at FROM listings ORDER BY url").fetchall()
    assert [row[0] for row in rows] == [old, old]
