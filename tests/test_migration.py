"""Schema creation, version checks, and version 0 -> 2 migration regressions."""
import sqlite3

import pytest

import db
from conftest import assert_db_is_temp
from dbhelpers import (
    assert_legacy_data_preserved,
    columns,
    dump_all,
    rows,
    run,
    set_user_version,
    sha256,
    user_version,
)

TABLES = ("brands", "items", "item_keywords", "listings", "listing_matches", "ebay_listing_prices")
ITEMS_COLUMNS = [
    "id", "brand_id", "name", "market_price_usd", "search_query", "manual_price_usd", "active",
]


@pytest.fixture
def fresh_path(tmp_path):
    assert_db_is_temp(tmp_path)
    path = tmp_path / "test.db"
    assert not path.exists()
    return path


def _schema_shape(path):
    """table_info, index names, and foreign keys for every table."""
    shape = {}
    for table in TABLES:
        shape[table] = {
            "table_info": rows(path, f"PRAGMA table_info({table})"),
            "indexes": sorted(r[1] for r in rows(path, f"PRAGMA index_list({table})")),
            "foreign_keys": sorted(rows(path, f"PRAGMA foreign_key_list({table})")),
        }
    return shape


# --- Fresh database and version checks (1-4) ---------------------------------


def test_rc06_c1_fresh_init_creates_version_2_schema(fresh_path):
    db.init_db()

    names = {r[0] for r in rows(fresh_path, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(TABLES) <= names
    assert user_version(fresh_path) == 2
    assert columns(fresh_path, "items") == ITEMS_COLUMNS
    assert columns(fresh_path, "item_keywords") == ["item_id", "keyword", "kind"]


def _seed_brand_and_item(path):
    run(path, "INSERT INTO brands (id, name, yahoo_brand_id) VALUES (1, 'B', 'b1')")
    run(path, "INSERT INTO items (id, brand_id, name) VALUES (1, 1, 'x')")


@pytest.mark.parametrize("bad_price", [0, -1])
def test_rc03a_c2_manual_price_must_be_positive(fresh_path, bad_price):
    db.init_db()
    run(fresh_path, "INSERT INTO brands (id, name, yahoo_brand_id) VALUES (1, 'B', 'b1')")
    with pytest.raises(sqlite3.IntegrityError):
        run(
            fresh_path,
            "INSERT INTO items (brand_id, name, manual_price_usd) VALUES (1, 'x', ?)",
            (bad_price,),
        )


def test_rc03a_c2_active_must_be_0_or_1(fresh_path):
    db.init_db()
    run(fresh_path, "INSERT INTO brands (id, name, yahoo_brand_id) VALUES (1, 'B', 'b1')")
    with pytest.raises(sqlite3.IntegrityError):
        run(fresh_path, "INSERT INTO items (brand_id, name, active) VALUES (1, 'x', 2)")


def test_rc03a_c2_keyword_kind_must_be_include_or_exclude(fresh_path):
    db.init_db()
    _seed_brand_and_item(fresh_path)
    with pytest.raises(sqlite3.IntegrityError):
        run(fresh_path, "INSERT INTO item_keywords (item_id, keyword, kind) VALUES (1, 'k', 'other')")


def test_rc03a_c2_defaults_are_active_1_and_kind_include(fresh_path):
    db.init_db()
    _seed_brand_and_item(fresh_path)
    run(fresh_path, "INSERT INTO item_keywords (item_id, keyword) VALUES (1, 'k')")
    assert rows(fresh_path, "SELECT active, manual_price_usd FROM items") == [(1, None)]
    assert rows(fresh_path, "SELECT kind FROM item_keywords") == [("include",)]


def test_rc03a_c3_init_db_twice_keeps_version_and_rows(fresh_path):
    db.init_db()
    _seed_brand_and_item(fresh_path)
    run(fresh_path, "INSERT INTO item_keywords (item_id, keyword, kind) VALUES (1, 'k', 'exclude')")
    before = dump_all(fresh_path)

    db.init_db()

    assert user_version(fresh_path) == 2
    assert dump_all(fresh_path) == before


def test_rc03a_c4_get_connection_refuses_missing_database(fresh_path):
    with pytest.raises(db.SchemaVersionError) as exc:
        db.get_connection()
    assert "python3 src/db.py" in str(exc.value)


def test_rc03a_c4_get_connection_refuses_legacy_database_and_leaves_it_alone(legacy_db):
    before = dump_all(legacy_db)
    with pytest.raises(db.SchemaVersionError) as exc:
        db.get_connection()
    assert "python3 src/db.py" in str(exc.value)
    assert user_version(legacy_db) == 0
    assert dump_all(legacy_db) == before


def test_rc03a_c4_get_connection_refuses_newer_database(temp_db):
    set_user_version(temp_db, 3)
    with pytest.raises(db.SchemaVersionError) as exc:
        db.get_connection()
    assert "python3 src/db.py" in str(exc.value)


def test_rc03a_c4_get_connection_works_on_current_database(temp_db):
    assert user_version(temp_db) == 2
    conn = db.get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
    finally:
        conn.close()


# --- Migration (5-10) --------------------------------------------------------


def test_rc03a_c5_migrated_schema_matches_a_fresh_database(legacy_db, tmp_path, monkeypatch):
    db.init_db()
    assert user_version(legacy_db) == 2
    migrated = _schema_shape(legacy_db)

    other = tmp_path / "fresh_reference.db"
    monkeypatch.setenv("RAINCHECK_DB_PATH", str(other))
    db.init_db()

    assert _schema_shape(other) == migrated


def test_rc03a_c6_migration_preserves_every_legacy_row(legacy_db):
    before = dump_all(legacy_db)
    assert len(before["listing_matches"]) == 1  # the fixture really has a match to protect

    db.init_db()

    assert_legacy_data_preserved(legacy_db, before)


def test_rc03a_c7_migrated_database_passes_integrity_and_fk_checks(legacy_db):
    db.init_db()
    assert user_version(legacy_db) == 2
    assert rows(legacy_db, "PRAGMA integrity_check") == [("ok",)]
    assert rows(legacy_db, "PRAGMA foreign_key_check") == []


def test_rc03a_c8_second_init_after_migration_is_a_noop(legacy_db, capsys):
    db.init_db()
    capsys.readouterr()
    after_first = dump_all(legacy_db)

    db.init_db()

    out = capsys.readouterr().out
    assert "Migrated" not in out
    assert user_version(legacy_db) == 2
    assert dump_all(legacy_db) == after_first


def test_rc03a_c9_failed_migration_rolls_back_everything(legacy_db, monkeypatch):
    before = dump_all(legacy_db)
    items_cols = columns(legacy_db, "items")
    keyword_cols = columns(legacy_db, "item_keywords")
    monkeypatch.setitem(
        db.MIGRATIONS, 1, db.MIGRATIONS[1] + ("INSERT INTO no_such_table VALUES (1)",)
    )

    with pytest.raises(sqlite3.OperationalError):
        db.init_db()

    assert user_version(legacy_db) == 0
    assert columns(legacy_db, "items") == items_cols
    assert "market_price_source" in items_cols and "manual_price_usd" not in items_cols
    assert columns(legacy_db, "item_keywords") == keyword_cols
    assert "kind" not in keyword_cols
    assert dump_all(legacy_db) == before


def test_rc03a_c10_migration_message_precedes_initialized_message(legacy_db, capsys):
    db.init_db()

    out = capsys.readouterr().out
    migrated = f"Migrated database at {legacy_db} from schema version 0 to 2."
    initialized = f"Database initialized at {legacy_db}."
    assert migrated in out
    assert initialized in out
    assert out.index(migrated) < out.index(initialized)


# --- Refusals that must not change the file (11-12) --------------------------


def test_rc03a_c11_newer_version_is_refused_and_file_is_untouched(temp_db):
    set_user_version(temp_db, 3)
    before = sha256(temp_db)

    with pytest.raises(db.SchemaVersionError):
        db.init_db()

    assert sha256(temp_db) == before


def test_rc03a_c12_unrecognized_shape_is_refused_and_file_is_untouched(fresh_path):
    conn = sqlite3.connect(fresh_path)
    conn.execute("CREATE TABLE foo (a INTEGER)")
    conn.commit()
    conn.close()
    assert user_version(fresh_path) == 0
    before = sha256(fresh_path)

    with pytest.raises(db.SchemaVersionError):
        db.init_db()

    assert sha256(fresh_path) == before


def test_rc03a_c12_legacy_missing_market_price_source_is_refused(legacy_db):
    conn = sqlite3.connect(legacy_db)
    conn.execute("ALTER TABLE items DROP COLUMN market_price_source")
    conn.commit()
    conn.close()
    before = sha256(legacy_db)

    with pytest.raises(db.SchemaVersionError):
        db.init_db()

    assert sha256(legacy_db) == before


# --- Amendment 1: a file that is not a SQLite database (52) -------------------


def test_rc03a_c52_non_sqlite_file_is_refused_everywhere_and_left_untouched(
    fresh_path, capsys
):
    fresh_path.write_text("not a database")
    before = sha256(fresh_path)

    with pytest.raises(db.SchemaVersionError) as get_exc:
        db.get_connection()
    assert "not a sqlite database" in str(get_exc.value).lower()

    with pytest.raises(db.SchemaVersionError) as init_exc:
        db.init_db()
    assert "not a sqlite database" in str(init_exc.value).lower()

    capsys.readouterr()
    with pytest.raises(SystemExit) as main_exc:
        db.main([])
    assert main_exc.value.code == 1
    assert "Error:" in capsys.readouterr().err

    assert sha256(fresh_path) == before
