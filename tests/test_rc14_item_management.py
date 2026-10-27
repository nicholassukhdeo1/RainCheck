"""RC-14 item management CLI, ownership, and schema migration acceptance tests."""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import db
from dbhelpers import columns, rows, user_version

ROOT = Path(__file__).resolve().parent.parent
SETUP = ROOT / "src" / "setup.py"
CATALOG = ROOT / "tests" / "fixtures" / "catalog.json"


def run_setup(db_path, *args, cwd):
    env = os.environ.copy()
    env["RAINCHECK_DB_PATH"] = str(db_path)
    env["RAINCHECK_DRY_RUN"] = "1"
    return subprocess.run(
        [sys.executable, str(SETUP), *args], cwd=cwd, env=env,
        text=True, capture_output=True, check=False,
    )


def snapshot(path):
    conn = sqlite3.connect(path)
    try:
        return {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall()
            for table in (
                "brands", "items", "item_keywords", "listings",
                "listing_matches", "ebay_listing_prices",
            )
        }
    finally:
        conn.close()


def insert_history(path, item_id=1):
    conn = sqlite3.connect(path)
    try:
        brand_id = conn.execute("SELECT brand_id FROM items WHERE id = ?", (item_id,)).fetchone()[0]
        conn.execute(
            "INSERT INTO listings (brand_id, url, title, price_yen, first_seen_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (brand_id, "https://example.test/auction-1", "Pod Shorts", 12000, "t1", "t1"),
        )
        listing_id = conn.execute("SELECT id FROM listings WHERE url = ?", ("https://example.test/auction-1",)).fetchone()[0]
        conn.execute(
            "INSERT INTO listing_matches (listing_id, item_id, matched_at) VALUES (?, ?, ?)",
            (listing_id, item_id, "t1"),
        )
        conn.execute(
            "INSERT INTO ebay_listing_prices (item_id, ebay_listing_id, first_price_usd, first_seen_at) "
            "VALUES (?, ?, ?, ?)",
            (item_id, "ebay-1", 200.0, "t1"),
        )
        conn.commit()
    finally:
        conn.close()


def test_rc14_c1_import_is_inert_and_help_does_not_create_database(tmp_path):
    db_path = tmp_path / "not-created.db"
    imported = subprocess.run(
        [sys.executable, "-c", "import setup"], cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src"), "RAINCHECK_DB_PATH": str(db_path)},
        text=True, capture_output=True, check=False,
    )
    assert imported.returncode == 0, imported.stderr
    assert not db_path.exists()
    result = run_setup(db_path, "--help", cwd=tmp_path)
    assert result.returncode == 0
    assert "usage:" in result.stdout.lower()
    assert not db_path.exists()


def test_rc14_c2_list_orders_items_and_shows_configuration_and_inactive_rows(temp_db, seeded_db, tmp_path):
    conn = sqlite3.connect(temp_db)
    conn.execute("INSERT INTO items (brand_id,name,active) VALUES (1,'inactive sample',0)")
    conn.commit()
    conn.close()
    active = run_setup(temp_db, "list", cwd=tmp_path)
    assert active.returncode == 0, active.stderr
    assert active.stdout.index("rick owens ramones") < active.stdout.index("pod shorts")
    assert "inactive sample" not in active.stdout
    assert "source" in active.stdout.lower()
    assert "ramones" in active.stdout.lower()
    assert "canvas" in active.stdout.lower()
    all_items = run_setup(temp_db, "list", "--all", cwd=tmp_path)
    assert all_items.returncode == 0, all_items.stderr
    assert "inactive" in all_items.stdout.lower()


def test_rc14_c2_empty_list_is_successful_and_explicit(temp_db, tmp_path):
    result = run_setup(temp_db, "list", "--all", cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "no items" in result.stdout.lower() or "empty" in result.stdout.lower()


def test_rc14_c3_c4_add_normalizes_keywords_and_keeps_optional_values_null(seeded_db, temp_db, tmp_path):
    result = run_setup(
        temp_db, "add", "--name", "Pod Shorts Custom", "--keyword", " Pod Short ",
        "--keyword", "pod short", "--search-query", "pod shorts exact", cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    item = rows(temp_db, "SELECT id, manual_price_usd, search_query FROM items WHERE name='Pod Shorts Custom'")
    assert len(item) == 1
    assert rows(temp_db, "SELECT name FROM brands") == [("Rick Owens",)]
    assert rows(temp_db, "SELECT keyword, kind FROM item_keywords WHERE item_id=?", (item[0][0],)) == [("pod short", "include")]
    assert item[0][1:] == (None, "pod shorts exact")
    tracked = [value for value in db.get_tracked_items() if value.id == item[0][0]]
    assert len(tracked) == 1


def test_rc14_c4_add_omitted_price_and_query_are_null_and_job_readable(seeded_db, temp_db, tmp_path):
    result = run_setup(temp_db, "add", "--name", "Custom Item", "--keyword", "custom", cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    item_id, manual, query = rows(temp_db, "SELECT id,manual_price_usd,search_query FROM items WHERE name='Custom Item'")[0]
    assert (manual, query) == (None, None)
    assert item_id in [item.id for item in db.get_tracked_items()]


def test_rc14_c3_unknown_brand_does_not_create_brand_or_item(seeded_db, temp_db, tmp_path):
    before = snapshot(temp_db)
    result = run_setup(temp_db, "add", "--brand", "Missing Brand", "--name", "Thing", "--keyword", "thing", cwd=tmp_path)
    assert result.returncode == 1
    assert result.stderr.startswith("Error:")
    assert snapshot(temp_db) == before


def test_rc14_c5_reactivation_reuses_id_and_preserves_market_price_and_history(seeded_db, temp_db, tmp_path):
    item_id = db.get_item_id("Rick Owens", "pod shorts")
    db.update_market_price(item_id, 777.0)
    insert_history(temp_db, item_id)
    conn = sqlite3.connect(temp_db)
    conn.execute("UPDATE items SET active=0 WHERE id=?", (item_id,))
    conn.commit()
    conn.close()
    before_history = {name: snapshot(temp_db)[name] for name in ("listings", "listing_matches", "ebay_listing_prices")}
    result = run_setup(temp_db, "add", "--name", "pod shorts", "--keyword", "new pod", "--manual-price", "250", cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    row = rows(temp_db, "SELECT id, active, market_price_usd, manual_price_usd FROM items WHERE name='pod shorts'")[0]
    assert row == (item_id, 1, 777.0, 250.0)
    assert rows(temp_db, "SELECT config_source FROM items WHERE id=?", (item_id,)) == [("cli",)]
    assert rows(temp_db, "SELECT keyword,kind FROM item_keywords WHERE item_id=?", (item_id,)) == [("new pod", "include")]
    assert {name: snapshot(temp_db)[name] for name in before_history} == before_history


def test_rc14_c6_duplicate_active_add_is_atomic(seeded_db, temp_db, tmp_path):
    before = snapshot(temp_db)
    result = run_setup(temp_db, "add", "--name", "pod shorts", "--keyword", "pod", cwd=tmp_path)
    assert result.returncode == 1
    assert result.stderr.startswith("Error:")
    assert snapshot(temp_db) == before


def test_rc14_c7_update_manual_fallback_keeps_ebay_and_each_listing_price(seeded_db, temp_db, tmp_path):
    item_id = db.get_item_id("Rick Owens", "pod shorts")
    db.update_market_price(item_id, 777.0)
    insert_history(temp_db, item_id)
    conn = sqlite3.connect(temp_db)
    conn.execute("INSERT INTO listings (brand_id,url,title,price_yen,first_seen_at,last_seen_at) VALUES (1,?,?,?,?,?)",
                 ("https://example.test/auction-2", "Second Pod", 24000, "t2", "t2"))
    listing_id = conn.execute("SELECT id FROM listings WHERE url=?", ("https://example.test/auction-2",)).fetchone()[0]
    conn.execute("INSERT INTO listing_matches VALUES (?, ?, ?)", (listing_id, item_id, "t2"))
    conn.commit()
    conn.close()
    result = run_setup(temp_db, "update", str(item_id), "--manual-price", "250", cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert rows(temp_db, "SELECT manual_price_usd, market_price_usd FROM items WHERE id=?", (item_id,)) == [(250.0, 777.0)]
    tracked = next(item for item in db.get_tracked_items() if item.id == item_id)
    assert tracked.effective_price_usd == 777.0
    assert rows(temp_db, "SELECT price_yen FROM listings ORDER BY id") == [(12000,), (24000,)]


@pytest.mark.parametrize(
    ("change_args", "expected_search_query", "expected_manual_price"),
    [
        (("--manual-price", "250"), "legacy query", 250.0),
        (("--search-query", "updated query"), "updated query", 125.0),
    ],
)
def test_rc14_c7_update_unclaimed_legacy_item_without_keywords_preserves_empty_set(
    tmp_path, monkeypatch, change_args, expected_search_query, expected_manual_price
):
    path = tmp_path / "legacy-no-keywords.db"
    conn = sqlite3.connect(path)
    conn.executescript((ROOT / "tests" / "fixtures" / "db" / "schema_v3.sql").read_text())
    conn.execute("INSERT INTO brands (id,name,yahoo_brand_id) VALUES (1,'Rick Owens','103538')")
    conn.execute(
        "INSERT INTO items (id,brand_id,name,market_price_usd,search_query,manual_price_usd,active) "
        "VALUES (7,1,'legacy item',400,'legacy query',125,1)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("RAINCHECK_DB_PATH", str(path))
    db.init_db()
    assert rows(path, "SELECT config_source FROM items WHERE id=7") == [("unclaimed",)]
    assert rows(path, "SELECT keyword,kind FROM item_keywords WHERE item_id=7") == []

    result = run_setup(path, "update", "7", *change_args, cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert rows(path, "SELECT search_query,manual_price_usd FROM items WHERE id=7") == [
        (expected_search_query, expected_manual_price)
    ]
    assert rows(path, "SELECT keyword,kind FROM item_keywords WHERE item_id=7") == []


def test_rc14_c8_repeated_keywords_replace_sets_and_clear_flags_are_supported(seeded_db, temp_db, tmp_path):
    item_id = db.get_item_id("Rick Owens", "pod shorts")
    result = run_setup(temp_db, "update", str(item_id), "--keyword", " POD ", "--keyword", "Pod", "--exclude-keyword", "fake", "--clear-search-query", "--clear-manual-price", cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert rows(temp_db, "SELECT keyword,kind FROM item_keywords WHERE item_id=? ORDER BY kind,keyword", (item_id,)) == [("fake", "exclude"), ("pod", "include")]
    assert rows(temp_db, "SELECT search_query,manual_price_usd FROM items WHERE id=?", (item_id,)) == [(None, None)]


def test_rc14_c9_remove_hides_item_but_preserves_all_related_rows(seeded_db, temp_db, tmp_path):
    item_id = db.get_item_id("Rick Owens", "pod shorts")
    insert_history(temp_db, item_id)
    before = snapshot(temp_db)
    removed = run_setup(temp_db, "remove", str(item_id), cwd=tmp_path)
    assert removed.returncode == 0, removed.stderr
    assert item_id not in [item.id for item in db.get_tracked_items()]
    assert "pod shorts" not in run_setup(temp_db, "list", cwd=tmp_path).stdout
    assert "inactive" in run_setup(temp_db, "list", "--all", cwd=tmp_path).stdout.lower()
    after = snapshot(temp_db)
    for table in ("listings", "listing_matches", "ebay_listing_prices", "item_keywords"):
        assert after[table] == before[table]
    second = run_setup(temp_db, "remove", str(item_id), cwd=tmp_path)
    assert second.returncode == 0, second.stderr
    assert "already inactive" in second.stdout.lower()


@pytest.mark.parametrize("command", [("update", "99999", "--manual-price", "5"), ("remove", "99999")])
def test_rc14_c10_unknown_id_fails_without_changes(seeded_db, temp_db, tmp_path, command):
    before = snapshot(temp_db)
    result = run_setup(temp_db, *command, cwd=tmp_path)
    assert result.returncode == 1
    assert result.stderr.startswith("Error:")
    assert snapshot(temp_db) == before


def test_rc14_c1_argument_syntax_error_exits_two_without_database_changes(temp_db, tmp_path):
    before = snapshot(temp_db)
    result = run_setup(temp_db, "update", "1", "--manual-price", "1", "--clear-manual-price", cwd=tmp_path)
    assert result.returncode == 2
    assert snapshot(temp_db) == before


@pytest.mark.parametrize(
    "args",
    [
        ("add", "--name", "  ", "--keyword", "valid"),
        ("add", "--name", "valid", "--keyword", "  "),
        ("add", "--name", "valid", "--keyword", "valid", "--manual-price", "0"),
        ("add", "--name", "valid", "--keyword", "valid", "--manual-price", "nan"),
        ("add", "--name", "valid", "--keyword", "valid", "--search-query", "x+y"),
        ("add", "--name", "valid", "--keyword", "valid", "--search-query", "x*"),
        ("add", "--name", "valid", "--keyword", "valid", "--search-query", "x" * 101),
        ("add", "--name", "valid", "--keyword", "same", "--exclude-keyword", " SAME "),
    ],
)
def test_rc14_c11_invalid_values_fail_before_any_write(seeded_db, temp_db, tmp_path, args):
    before = snapshot(temp_db)
    result = run_setup(temp_db, *args, cwd=tmp_path)
    assert result.returncode == 1
    assert result.stderr.startswith("Error:")
    assert snapshot(temp_db) == before


def test_rc14_c13_seed_skips_cli_and_unclaimed_rows_and_reports_skipped(seeded_db, temp_db, tmp_path, test_catalog):
    source = rows(temp_db, "SELECT id FROM items WHERE name='pod shorts'")[0][0]
    conn = sqlite3.connect(temp_db)
    conn.execute("UPDATE items SET config_source='cli', manual_price_usd=222 WHERE id=?", (source,))
    conn.execute("INSERT INTO items (brand_id,name,search_query,manual_price_usd) VALUES (1,'database-only','private query',91)")
    conn.execute("INSERT INTO item_keywords (item_id,keyword) VALUES ((SELECT id FROM items WHERE name='database-only'),'private')")
    conn.commit()
    conn.close()
    result = db.seed_from_catalog(test_catalog)
    assert ("Rick Owens", "pod shorts") in result.skipped
    assert rows(temp_db, "SELECT config_source,manual_price_usd FROM items WHERE id=?", (source,)) == [("cli", 222.0)]
    assert rows(temp_db, "SELECT active,search_query,manual_price_usd FROM items WHERE name='database-only'") == [(1, "private query", 91.0)]


def test_rc14_c12_seed_failure_rolls_back_entire_operation(seeded_db, temp_db, test_catalog):
    before = snapshot(temp_db)
    conn = sqlite3.connect(temp_db)
    conn.execute(
        "CREATE TRIGGER reject_seed_keywords BEFORE INSERT ON item_keywords "
        "BEGIN SELECT RAISE(ABORT, 'injected seed failure'); END"
    )
    conn.commit()
    conn.close()
    with pytest.raises(sqlite3.IntegrityError, match="injected seed failure"):
        db.seed_from_catalog(test_catalog)
    assert snapshot(temp_db) == before


def test_rc14_c12_failed_seed_closes_its_database_connection(seeded_db, temp_db, test_catalog, monkeypatch):
    conn = sqlite3.connect(temp_db)
    conn.execute(
        "CREATE TRIGGER reject_seed_keywords BEFORE INSERT ON item_keywords "
        "BEGIN SELECT RAISE(ABORT, 'injected seed failure'); END"
    )
    conn.commit()
    conn.close()
    original_get_connection = db.get_connection
    observed = []

    class ObservedConnection:
        def __init__(self, connection):
            self.connection = connection
            self.closed = False

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def close(self):
            self.closed = True
            self.connection.close()

    def tracked_get_connection():
        connection = ObservedConnection(original_get_connection())
        observed.append(connection)
        return connection

    monkeypatch.setattr(db, "get_connection", tracked_get_connection)
    with pytest.raises(sqlite3.IntegrityError, match="injected seed failure"):
        db.seed_from_catalog(test_catalog)
    assert len(observed) == 1
    assert observed[0].closed


def test_rc14_c12_add_failure_rolls_back_partial_item(seeded_db, temp_db, tmp_path):
    before = snapshot(temp_db)
    conn = sqlite3.connect(temp_db)
    conn.execute(
        "CREATE TRIGGER reject_added_keywords BEFORE INSERT ON item_keywords "
        "WHEN NEW.keyword = 'rollback' BEGIN SELECT RAISE(ABORT, 'injected add failure'); END"
    )
    conn.commit()
    conn.close()
    add = run_setup(temp_db, "add", "--name", "rollback item", "--keyword", "rollback", cwd=tmp_path)
    assert add.returncode == 1
    assert add.stderr.startswith("Error:")
    assert snapshot(temp_db) == before


@pytest.mark.parametrize("operation", ["update", "remove", "adopt"])
def test_rc14_c12_item_operation_sql_failure_rolls_back(operation, seeded_db, temp_db, tmp_path):
    item_id = db.get_item_id("Rick Owens", "pod shorts")
    before = snapshot(temp_db)
    conn = sqlite3.connect(temp_db)
    if operation == "update":
        conn.execute(
            "CREATE TRIGGER reject_updated_keywords BEFORE INSERT ON item_keywords "
            "WHEN NEW.keyword='blocked' BEGIN SELECT RAISE(ABORT, 'injected update failure'); END"
        )
        args = ("update", str(item_id), "--keyword", "blocked")
    elif operation == "remove":
        conn.execute(
            "CREATE TRIGGER reject_item_deactivation BEFORE UPDATE OF active ON items "
            "BEGIN SELECT RAISE(ABORT, 'injected remove failure'); END"
        )
        args = ("remove", str(item_id))
    else:
        conn.execute(
            "CREATE TRIGGER reject_adopted_keywords BEFORE INSERT ON item_keywords "
            "BEGIN SELECT RAISE(ABORT, 'injected adopt failure'); END"
        )
        args = ("adopt", str(item_id))
    conn.commit()
    conn.close()
    result = run_setup(temp_db, *args, cwd=tmp_path)
    assert result.returncode == 1
    assert result.stderr.startswith("Error:")
    assert snapshot(temp_db) == before


def test_rc14_c12_migration_failure_rolls_back_schema_and_version(tmp_path, monkeypatch):
    migration_path = tmp_path / "rollback-migration.db"
    _version3_database(migration_path)
    monkeypatch.setenv("RAINCHECK_DB_PATH", str(migration_path))
    original = db.MIGRATIONS.get(4, ())
    monkeypatch.setitem(db.MIGRATIONS, 4, original + ("INSERT INTO no_such_table VALUES (1)",))
    with pytest.raises(sqlite3.OperationalError):
        db.init_db()
    assert user_version(migration_path) == 3
    assert "config_source" not in columns(migration_path, "items")
    monkeypatch.setitem(db.MIGRATIONS, 4, original)


def _version3_database(path):
    conn = sqlite3.connect(path)
    try:
        conn.executescript((ROOT / "tests" / "fixtures" / "db" / "schema_v3.sql").read_text())
        conn.execute("INSERT INTO brands VALUES (1,'Rick Owens','103538')")
        conn.execute("INSERT INTO items VALUES (41,1,'pod shorts',777,'database query',222,1)")
        conn.execute("INSERT INTO item_keywords VALUES (41,'database-only','include')")
        conn.execute("INSERT INTO listings (id,brand_id,url,title,price_yen,first_seen_at,last_seen_at) VALUES (51,1,'https://example.test/migrated','Pod Shorts',19000,'t','t')")
        conn.execute("INSERT INTO listing_matches VALUES (51,41,'t')")
        conn.execute("INSERT INTO ebay_listing_prices VALUES (41,'ebay-history',205,'t')")
        conn.execute("INSERT INTO items VALUES (42,1,'database-only',88,'private query',99,1)")
        conn.execute("INSERT INTO item_keywords VALUES (42,'private keyword','include')")
        conn.commit()
    finally:
        conn.close()


def test_rc14_c14_c15_v3_upgrade_and_reseed_preserve_unclaimed_items_and_history(tmp_path, monkeypatch):
    path = tmp_path / "v3.db"
    _version3_database(path)
    monkeypatch.setenv("RAINCHECK_DB_PATH", str(path))
    before = rows(path, "SELECT id,brand_id,name,market_price_usd,search_query,manual_price_usd,active FROM items")
    db.init_db()
    assert user_version(path) == 4
    assert rows(path, "SELECT config_source FROM items WHERE id=41") == [("unclaimed",)]
    db.seed_from_catalog(CATALOG)
    assert rows(path, "SELECT id,brand_id,name,market_price_usd,search_query,manual_price_usd,active FROM items WHERE id IN (41,42) ORDER BY id") == before
    assert rows(path, "SELECT keyword,kind FROM item_keywords WHERE item_id=41") == [("database-only", "include")]
    assert rows(path, "SELECT listing_id,item_id,matched_at FROM listing_matches") == [(51,41,"t")]
    assert rows(path, "SELECT item_id,ebay_listing_id,first_price_usd,first_seen_at FROM ebay_listing_prices") == [(41,"ebay-history",205.0,"t")]
    assert rows(path, "SELECT id,config_source,active,search_query,manual_price_usd,market_price_usd FROM items WHERE id=42") == [(42,"unclaimed",1,"private query",99.0,88.0)]
    assert rows(path, "SELECT keyword,kind FROM item_keywords WHERE item_id=42") == [("private keyword","include")]
    assert rows(path, "PRAGMA integrity_check") == [("ok",)]
    assert rows(path, "PRAGMA foreign_key_check") == []


def test_rc14_c16_adoption_and_seed_reconciliation_preserve_item_id_and_market_price(seeded_db, temp_db, tmp_path):
    item_id = db.get_item_id("Rick Owens", "pod shorts")
    db.update_market_price(item_id, 777.0)
    insert_history(temp_db, item_id)
    before_history = {name: snapshot(temp_db)[name] for name in ("listings", "listing_matches", "ebay_listing_prices")}
    # Ownership is an explicit opt-in; implementation exposes it through the CLI.
    adopted = run_setup(temp_db, "adopt", str(item_id), cwd=tmp_path)
    assert adopted.returncode == 0, adopted.stderr
    assert rows(temp_db, "SELECT id,config_source,market_price_usd FROM items WHERE id=?", (item_id,)) == [(item_id,"catalog",777.0)]
    assert {name: snapshot(temp_db)[name] for name in before_history} == before_history
    entries = json.loads(CATALOG.read_text(encoding="utf-8"))
    next(entry for entry in entries if entry["item name"] == "pod shorts")["market_price"] = 555.0
    changed_catalog = tmp_path / "changed-catalog.json"
    changed_catalog.write_text(json.dumps(entries), encoding="utf-8")
    db.seed_from_catalog(changed_catalog)
    assert rows(temp_db, "SELECT config_source,manual_price_usd,market_price_usd FROM items WHERE id=?", (item_id,)) == [("catalog",555.0,777.0)]
    assert {name: snapshot(temp_db)[name] for name in before_history} == before_history


def test_rc14_c17_missing_adoption_identity_is_atomic(seeded_db, temp_db, tmp_path):
    item_id = db.get_item_id("Rick Owens", "pod shorts")
    conn = sqlite3.connect(temp_db)
    conn.execute("UPDATE items SET name='not in catalog' WHERE id=?", (item_id,))
    conn.commit()
    conn.close()
    before = snapshot(temp_db)
    result = run_setup(temp_db, "adopt", str(item_id), cwd=tmp_path)
    assert result.returncode == 1
    assert snapshot(temp_db) == before


def test_rc14_c17_adopting_unknown_id_fails_without_changes(seeded_db, temp_db, tmp_path):
    before = snapshot(temp_db)
    result = run_setup(temp_db, "adopt", "99999", cwd=tmp_path)
    assert result.returncode == 1
    assert result.stderr.startswith("Error:")
    assert snapshot(temp_db) == before


@pytest.mark.parametrize("catalog_text", ["not json", "[]", '[{"brand":" ","item name":"x"}]'])
def test_rc14_c17_invalid_adoption_catalog_fails_without_changes(seeded_db, temp_db, tmp_path, catalog_text):
    item_id = db.get_item_id("Rick Owens", "pod shorts")
    invalid_catalog = tmp_path / "invalid-catalog.json"
    invalid_catalog.write_text(catalog_text, encoding="utf-8")
    before = snapshot(temp_db)
    with pytest.raises((db.CatalogError, ValueError, json.JSONDecodeError)):
        db.adopt_catalog_item(item_id, catalog_path=invalid_catalog)
    assert snapshot(temp_db) == before


def test_rc14_c17_adopt_reports_invalid_multiline_catalog_as_one_error_line(
    seeded_db, temp_db, tmp_path, monkeypatch, capsys
):
    import setup

    item_id = db.get_item_id("Rick Owens", "pod shorts")
    invalid_catalog = tmp_path / "invalid-adoption-catalog.json"
    invalid_catalog.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(db, "CATALOG_PATH", invalid_catalog)
    before = snapshot(temp_db)

    status = setup.main(["adopt", str(item_id)])
    captured = capsys.readouterr()

    assert status == 1
    assert captured.err.startswith("Error:")
    assert len(captured.err.strip().splitlines()) == 1
    assert snapshot(temp_db) == before


def test_rc14_c18_seed_keeps_catalog_rows_managed_and_market_price(seeded_db, temp_db):
    item_id = db.get_item_id("Rick Owens", "pod shorts")
    db.update_market_price(item_id, 777.0)
    db.seed_from_catalog(CATALOG)
    assert rows(temp_db, "SELECT config_source,market_price_usd FROM items WHERE id=?", (item_id,)) == [("catalog",777.0)]


def test_rc14_c19_fresh_schema_matches_migrated_v3_schema(tmp_path, monkeypatch):
    fresh = tmp_path / "fresh.db"
    db_path = tmp_path / "migrated.db"
    _version3_database(db_path)
    monkeypatch.setenv("RAINCHECK_DB_PATH", str(db_path))
    db.init_db()
    migrated_shape = (columns(db_path, "items"), rows(db_path, "PRAGMA table_info(items)"))
    monkeypatch.setenv("RAINCHECK_DB_PATH", str(fresh))
    db.init_db()
    fresh_shape = (columns(fresh, "items"), rows(fresh, "PRAGMA table_info(items)"))
    assert migrated_shape == fresh_shape
    assert "config_source" in columns(fresh, "items")
    assert user_version(fresh) == 4


def test_rc14_c20_readme_documents_complete_cli_lifecycle():
    readme = (ROOT / "README.md").read_text(encoding="utf-8").lower()
    for command in ("add", "list", "update", "remove", "adopt"):
        assert f"setup.py {command}" in readme
    assert "seed" in readme and "backup" in readme and "repository root" in readme
    assert "catalog.json" in readme and "unclaimed" in readme and "cli" in readme
