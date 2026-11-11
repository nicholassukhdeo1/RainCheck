"""RC-22 criteria 1-7 (+ 46): schema version 5, its migration, and the schema checks.

The DDL itself is the implementer's job.  These tests assert the resulting columns and
constraints through PRAGMA table_info and by attempting INSERTs, never by reading
the DDL text.
"""
import gc
import sqlite3
import warnings

import pytest

import db
import rc22_fixtures
from conftest import assert_db_is_temp
from dbhelpers import columns, connect, dump_all, rows, set_user_version, user_version

TABLES = ("brands", "items", "item_keywords", "listings", "listing_matches", "ebay_listing_prices")
NEW_COLUMNS = [
    "deal_kind", "deal_price_yen", "deal_market_price_usd",
    "deal_yen_to_usd", "deal_rate_is_fallback", "deal_discount_pct",
]
NEW_TYPES = ["TEXT", "INTEGER", "REAL", "REAL", "INTEGER", "REAL"]
OLD_COLUMNS = ["listing_id", "item_id", "matched_at"]
VALID_DEAL = ("fixed_price", 50000, 400.0, 0.005, 0, 25.0)
BACK_UP_MESSAGE = "Back up the database, then run python3 src/db.py"


@pytest.fixture
def db_path(tmp_path):
    assert_db_is_temp(tmp_path)
    path = tmp_path / "test.db"
    assert not path.exists()
    return path


def _shape(path):
    """table_info, index names, and foreign keys for every table."""
    shape = {}
    for table in TABLES:
        shape[table] = {
            "table_info": rows(path, f"PRAGMA table_info({table})"),
            "indexes": sorted(r[1] for r in rows(path, f"PRAGMA index_list({table})")),
            "foreign_keys": sorted(rows(path, f"PRAGMA foreign_key_list({table})")),
        }
    return shape


@pytest.fixture(params=["fresh", "migrated"])
def v5_db(request, db_path):
    """The same version 5 schema reached two ways: created fresh, or migrated from version 4."""
    if request.param == "migrated":
        rc22_fixtures.build_v4_database(db_path)
    db.init_db()
    return db_path


def _insert_pair(path, deal_values, *, listing_id=1, item_id=1):
    conn = connect(path)
    try:
        conn.execute("INSERT OR IGNORE INTO brands (id, name, yahoo_brand_id) VALUES (50, 'Probe', 'p50')")
        conn.execute(
            "INSERT OR IGNORE INTO items (id, brand_id, name) VALUES (?, 50, 'probe item')", (item_id + 100,)
        )
        conn.execute(
            "INSERT OR IGNORE INTO listings (id, brand_id, url, title, price_yen, first_seen_at, last_seen_at) "
            "VALUES (?, 50, ?, 't', 1000, 'x', 'x')",
            (listing_id + 100, f"https://probe.test/{listing_id}"),
        )
        conn.execute(
            "INSERT INTO listing_matches (listing_id, item_id, matched_at, "
            "deal_kind, deal_price_yen, deal_market_price_usd, deal_yen_to_usd, "
            "deal_rate_is_fallback, deal_discount_pct) VALUES (?,?,?,?,?,?,?,?,?)",
            (listing_id + 100, item_id + 100, "2026-10-01T00:00:00+00:00", *deal_values),
        )
        conn.commit()
    finally:
        conn.close()


# --- Criterion 1 -------------------------------------------------------------


def test_rc22_c1_schema_version_constant_is_5():
    assert db.SCHEMA_VERSION == 5


def test_rc22_c1_fresh_init_creates_version_5_with_six_deal_columns_in_order(db_path):
    db.init_db()

    assert user_version(db_path) == 5
    info = rows(db_path, "PRAGMA table_info(listing_matches)")
    assert [r[1] for r in info] == OLD_COLUMNS + NEW_COLUMNS
    assert [r[2] for r in info[3:]] == NEW_TYPES
    # all six are nullable, with no default
    assert [(r[3], r[4]) for r in info[3:]] == [(0, None)] * 6
    # the original primary key is untouched
    assert [r[5] for r in info[:3]] == [1, 2, 0]


# --- Criteria 2 and 3 --------------------------------------------------------


def test_rc22_c2_migrated_v4_database_matches_a_fresh_database(db_path, tmp_path, monkeypatch):
    rc22_fixtures.build_v4_database(db_path)
    assert user_version(db_path) == 4

    db.init_db()
    assert user_version(db_path) == 5
    migrated = _shape(db_path)

    fresh_path = tmp_path / "fresh_reference.db"
    monkeypatch.setenv("RAINCHECK_DB_PATH", str(fresh_path))
    db.init_db()
    fresh = _shape(fresh_path)

    for table in TABLES:
        assert migrated[table] == fresh[table], table


def test_rc22_c3_migration_keeps_every_row_and_nulls_the_new_columns(db_path):
    rc22_fixtures.build_v4_database(db_path)
    before = dump_all(db_path)
    assert all(before[table] for table in TABLES), "the v4 fixture must have rows in every table"

    db.init_db()

    after = dump_all(db_path)
    for table in TABLES:
        if table == "listing_matches":
            continue
        assert after[table] == before[table], table
    # listing_matches: the three original columns are unchanged, the six new ones are all NULL
    assert [row[:3] for row in after["listing_matches"]] == before["listing_matches"]
    assert [row[3:] for row in after["listing_matches"]] == [(None,) * 6] * len(before["listing_matches"])
    assert rows(db_path, "PRAGMA integrity_check") == [("ok",)]
    assert rows(db_path, "PRAGMA foreign_key_check") == []


def test_rc22_c3_init_db_on_a_version_5_database_is_a_no_op(v5_db):
    before = dump_all(v5_db)
    db.init_db()
    assert user_version(v5_db) == 5
    assert dump_all(v5_db) == before


# --- Criterion 4 -------------------------------------------------------------


def test_rc22_c4_all_null_and_all_set_deal_columns_are_accepted(v5_db):
    _insert_pair(v5_db, (None,) * 6, listing_id=1)
    _insert_pair(v5_db, VALID_DEAL, listing_id=2)
    stored = rows(
        v5_db,
        "SELECT listing_id, deal_kind, deal_price_yen, deal_market_price_usd, deal_yen_to_usd, "
        "deal_rate_is_fallback, deal_discount_pct FROM listing_matches ORDER BY listing_id",
    )
    assert stored == [(101, None, None, None, None, None, None), (102, *VALID_DEAL)]


@pytest.mark.parametrize("only_set", range(6), ids=NEW_COLUMNS)
def test_rc22_c4_a_single_deal_column_without_the_others_is_rejected(v5_db, only_set):
    values = [None] * 6
    values[only_set] = VALID_DEAL[only_set]
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _insert_pair(v5_db, tuple(values))


@pytest.mark.parametrize("missing", range(6), ids=NEW_COLUMNS)
def test_rc22_c4_all_but_one_deal_column_set_is_rejected(v5_db, missing):
    values = list(VALID_DEAL)
    values[missing] = None
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _insert_pair(v5_db, tuple(values))


# --- Criterion 5 -------------------------------------------------------------


@pytest.mark.parametrize(
    "column_index, bad_value",
    [
        (0, "bogus"),
        (0, ""),
        (0, "FIXED_PRICE"),
        (1, 0),
        (1, -1),
        (2, 0),
        (2, -1.5),
        (3, 0),
        (3, -0.005),
        (4, 2),
        (4, -1),
        (5, 0),
        (5, -1.0),
        (5, 100),
        (5, 100.5),
    ],
)
def test_rc22_c5_out_of_range_deal_values_are_rejected_by_a_check(v5_db, column_index, bad_value):
    values = list(VALID_DEAL)
    values[column_index] = bad_value
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _insert_pair(v5_db, tuple(values))


@pytest.mark.parametrize("kind", ["fixed_price", "auction_buy_now", "auction_watch"])
def test_rc22_c5_each_of_the_three_kinds_is_accepted(v5_db, kind):
    _insert_pair(v5_db, (kind, *VALID_DEAL[1:]))
    assert rows(v5_db, "SELECT deal_kind FROM listing_matches") == [(kind,)]


@pytest.mark.parametrize("fallback", [0, 1])
def test_rc22_c5_fallback_flag_accepts_zero_and_one(v5_db, fallback):
    _insert_pair(v5_db, (*VALID_DEAL[:4], fallback, VALID_DEAL[5]))
    assert rows(v5_db, "SELECT deal_rate_is_fallback FROM listing_matches") == [(fallback,)]


@pytest.mark.parametrize("discount", [0.0001, 12.3, 99.9999])
def test_rc22_c5_discount_strictly_between_zero_and_hundred_is_accepted(v5_db, discount):
    _insert_pair(v5_db, (*VALID_DEAL[:5], discount))
    assert rows(v5_db, "SELECT deal_discount_pct FROM listing_matches") == [(discount,)]


# --- Criterion 6 -------------------------------------------------------------


def test_rc22_c6_migration_failing_on_its_last_statement_leaves_version_4_untouched(db_path, monkeypatch):
    rc22_fixtures.build_v4_database(db_path)
    before = dump_all(db_path)
    original = db.MIGRATIONS[5]
    monkeypatch.setitem(db.MIGRATIONS, 5, original + ("INSERT INTO no_such_table VALUES (1)",))

    with pytest.raises(sqlite3.OperationalError):
        db.init_db()

    assert user_version(db_path) == 4
    assert columns(db_path, "listing_matches") == OLD_COLUMNS
    assert dump_all(db_path) == before
    assert rows(db_path, "PRAGMA integrity_check") == [("ok",)]


# --- Criterion 7 -------------------------------------------------------------


def _read_only():
    return db.get_read_only_connection()


def _refusal_message(open_fn):
    """The SchemaVersionError message from `open_fn`; closes the connection if it was wrongly opened."""
    try:
        conn = open_fn()
    except db.SchemaVersionError as exc:
        return str(exc)
    conn.close()
    pytest.fail("the database was not refused")


@pytest.mark.parametrize("opener", [db.get_connection, "read_only"], ids=["get_connection", "get_read_only_connection"])
def test_rc22_c7_version_4_database_is_refused_with_the_back_up_message(db_path, opener):
    rc22_fixtures.build_v4_database(db_path)
    message = _refusal_message(_read_only if opener == "read_only" else opener)
    assert "schema version 4" in message
    assert "expects version 5" in message
    assert BACK_UP_MESSAGE in message
    assert user_version(db_path) == 4


@pytest.mark.parametrize("opener", [db.get_connection, "read_only"], ids=["get_connection", "get_read_only_connection"])
def test_rc22_c7_version_6_database_is_refused_as_from_a_newer_raincheck(db_path, opener):
    rc22_fixtures.build_f0(db_path)
    set_user_version(db_path, 6)
    message = _refusal_message(_read_only if opener == "read_only" else opener)
    assert "schema version 6" in message
    assert "newer RainCheck" in message
    assert user_version(db_path) == 6


# --- Read-only connection contract (Interfaces) ------------------------------


def test_rc22_read_only_connection_opens_a_current_database_and_cannot_write(db_path):
    rc22_fixtures.build_f1(db_path)
    conn = db.get_read_only_connection()
    try:
        assert conn.row_factory is sqlite3.Row
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        assert conn.execute("SELECT name FROM brands WHERE id = 1").fetchone()["name"] == "Rick Owens"
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO brands (name, yahoo_brand_id) VALUES ('x', 'x')")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE brands SET name = 'x' WHERE id = 1")
    finally:
        conn.close()
    assert rows(db_path, "SELECT name FROM brands WHERE id = 1") == [("Rick Owens",)]


def test_rc22_read_only_connection_refuses_a_missing_file_with_the_same_message_and_creates_nothing(db_path):
    with pytest.raises(db.SchemaVersionError) as read_only_error:
        db.get_read_only_connection()
    with pytest.raises(db.SchemaVersionError) as normal_error:
        db.get_connection()
    assert str(read_only_error.value) == str(normal_error.value)
    assert "No database found" in str(read_only_error.value)
    assert not db_path.exists()


def test_rc22_read_only_connection_refuses_empty_and_non_sqlite_files(db_path):
    db_path.write_bytes(b"")
    with pytest.raises(db.SchemaVersionError, match="is empty"):
        db.get_read_only_connection()
    db_path.write_text("this is not a database", encoding="utf-8")
    with pytest.raises(db.SchemaVersionError, match="not a SQLite database"):
        db.get_read_only_connection()
    assert db_path.read_text(encoding="utf-8") == "this is not a database"


def test_rc22_read_only_connection_works_for_a_path_with_spaces_and_a_hash(tmp_path, monkeypatch):
    odd = tmp_path / "my dir"
    odd.mkdir()
    path = odd / "raincheck #1.db"
    monkeypatch.setenv("RAINCHECK_DB_PATH", str(path))
    rc22_fixtures.build_f1(path)
    conn = db.get_read_only_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM brands").fetchone()[0] == 3
    finally:
        conn.close()


def test_rc22_read_only_connection_error_paths_leave_no_open_connection(db_path):
    rc22_fixtures.build_v4_database(db_path)

    def attempt():
        with pytest.raises(db.SchemaVersionError):
            db.get_read_only_connection()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        attempt()
        gc.collect()
    assert [w for w in caught if issubclass(w.category, ResourceWarning)] == []


# --- Criterion 46 (migration statement count) and MIN_POOL_PRICES ------------


def test_rc22_c46_migration_5_is_six_alter_statements_plus_at_most_one_index():
    statements = db.MIGRATIONS[5]
    assert isinstance(statements, tuple)
    assert len(statements) in (6, 7)
    for statement in statements[:6]:
        assert statement.startswith("ALTER TABLE listing_matches ADD COLUMN")
    if len(statements) == 7:
        assert statements[6].startswith("CREATE INDEX")


def test_rc22_files_min_pool_prices_constant_is_5():
    assert db.MIN_POOL_PRICES == 5
