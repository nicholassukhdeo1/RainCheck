"""RC-06 acceptance tests for the permanent, distinct eBay listing pool."""
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import textwrap

import pytest

import db
import ebay_pricing
from dbhelpers import columns, rows, run, set_user_version, user_version


OBSERVED = "2026-10-27T12:30:00+00:00"


def listing(listing_id, price):
    return ebay_pricing.ListingPrice(listing_id, price)


def ramones_id(seeded_db):
    return db.get_item_id("Rick Owens", "rick owens ramones")


def stored(path, item_id):
    return rows(
        path,
        "SELECT ebay_listing_id, first_price_usd, first_seen_at "
        "FROM ebay_listing_prices WHERE item_id = ? "
        "ORDER BY first_seen_at, ebay_listing_id",
        (item_id,),
    )


def _upgrade_to_v1(path):
    """Construct the shipped v1 state from the v0 fixture without current code."""
    conn = sqlite3.connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for statement in db.MIGRATIONS[1]:
            conn.execute(statement)
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
    finally:
        conn.close()


def test_rc06_c1_fresh_schema_v3_has_listing_price_table_key_fk_and_index(temp_db):
    assert user_version(temp_db) == 4
    assert columns(temp_db, "ebay_listing_prices") == [
        "item_id", "ebay_listing_id", "first_price_usd", "first_seen_at"
    ]
    assert rows(temp_db, "PRAGMA table_info(ebay_listing_prices)")[0][5] == 1
    assert rows(temp_db, "PRAGMA table_info(ebay_listing_prices)")[1][5] == 2
    assert rows(temp_db, "PRAGMA foreign_key_list(ebay_listing_prices)") == [
        (0, 0, "items", "item_id", "id", "NO ACTION", "NO ACTION", "NONE")
    ]
    indexes = {row[1] for row in rows(temp_db, "PRAGMA index_list(ebay_listing_prices)")}
    assert "idx_ebay_listing_prices_item_seen" in indexes
    assert rows(
        temp_db,
        "SELECT item_id, ebay_listing_id, first_price_usd, first_seen_at "
        "FROM ebay_listing_prices",
    ) == []


def test_rc06_c1_database_rejects_empty_ebay_listing_id(seeded_db):
    item_id = ramones_id(seeded_db)

    with pytest.raises(sqlite3.IntegrityError):
        run(
            seeded_db,
            "INSERT INTO ebay_listing_prices "
            "(item_id, ebay_listing_id, first_price_usd, first_seen_at) "
            "VALUES (?, ?, ?, ?)",
            (item_id, "", 100.0, OBSERVED),
        )

    assert rows(seeded_db, "SELECT COUNT(*) FROM ebay_listing_prices") == [(0,)]


def test_rc06_c2_version_one_migration_preserves_existing_rows(legacy_db):
    _upgrade_to_v1(legacy_db)
    run(legacy_db, "INSERT INTO item_keywords (item_id, keyword, kind) VALUES (1, 'old', 'exclude')")
    run(legacy_db, "UPDATE items SET market_price_usd = 456.75 WHERE id = 1")
    before = {
        table: rows(legacy_db, f"SELECT * FROM {table} ORDER BY rowid")
        for table in ("brands", "items", "item_keywords", "listings", "listing_matches")
    }

    db.init_db()

    assert user_version(legacy_db) == 4
    for table, expected in before.items():
        actual = rows(legacy_db, f"SELECT * FROM {table} ORDER BY rowid")
        if table == "listings":
            # RC-16 appends nullable metadata; the legacy columns and values remain intact.
            assert [row[:len(expected[0])] for row in actual] == expected
            assert all(row[len(expected[0]):] == (None, None, None) for row in actual)
        else:
            assert actual == expected
    assert rows(legacy_db, "SELECT COUNT(*) FROM ebay_listing_prices") == [(0,)]


def test_rc06_c3_version_zero_migrates_through_one_to_three_and_is_idempotent(legacy_db):
    db.init_db()
    assert user_version(legacy_db) == 4
    snapshot = {
        table: rows(legacy_db, f"SELECT * FROM {table} ORDER BY rowid")
        for table in ("brands", "items", "item_keywords", "listings", "listing_matches", "ebay_listing_prices")
    }
    db.init_db()
    assert user_version(legacy_db) == 4
    assert {
        table: rows(legacy_db, f"SELECT * FROM {table} ORDER BY rowid")
        for table in snapshot
    } == snapshot


def test_rc06_c4_migration_two_error_rolls_back_table_and_version(legacy_db, monkeypatch):
    _upgrade_to_v1(legacy_db)
    before = {table: rows(legacy_db, f"SELECT * FROM {table} ORDER BY rowid") for table in (
        "brands", "items", "item_keywords", "listings", "listing_matches"
    )}
    monkeypatch.setitem(
        db.MIGRATIONS, 2, db.MIGRATIONS[2] + ("INSERT INTO no_such_rc06_table VALUES (1)",)
    )

    with pytest.raises(sqlite3.OperationalError):
        db.init_db()

    assert user_version(legacy_db) == 1
    names = {r[0] for r in rows(legacy_db, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "ebay_listing_prices" not in names
    for table, expected in before.items():
        assert rows(legacy_db, f"SELECT * FROM {table} ORDER BY rowid") == expected


def test_rc06_c5_five_new_rows_set_arithmetic_mean(seeded_db, db_conn):
    item_id = ramones_id(seeded_db)
    result = db.record_ebay_listing_prices(
        item_id, [listing(f"legacy:{n}", p) for n, p in enumerate((100, 110, 120, 130, 140))],
        observed_at=OBSERVED,
    )
    assert result.new_count == 5
    assert result.candidate_count == 5
    assert result.outlier_count == 0
    assert result.retained_count == 5
    assert result.mean_usd == 120.0
    assert result.price_updated is True
    assert len(stored(seeded_db, item_id)) == 5
    assert db_conn.execute("SELECT market_price_usd FROM items WHERE id = ?", (item_id,)).fetchone()[0] == 120.0


def test_rc06_c6_second_search_adds_only_new_ids_and_averages_all_rows(seeded_db, db_conn):
    item_id = ramones_id(seeded_db)
    first = [listing(f"legacy:{n}", p) for n, p in enumerate((100, 110, 120, 130, 140))]
    db.record_ebay_listing_prices(item_id, first, observed_at=OBSERVED)
    result = db.record_ebay_listing_prices(
        item_id, first + [listing("legacy:5", 150), listing("legacy:6", 160)],
        observed_at="2026-10-28T12:30:00+00:00",
    )
    assert result.new_count == 2
    assert result.duplicate_count == 5
    assert result.candidate_count == result.retained_count == 7
    assert result.mean_usd == pytest.approx(130.0)
    assert len(stored(seeded_db, item_id)) == 7
    assert db_conn.execute("SELECT market_price_usd FROM items WHERE id = ?", (item_id,)).fetchone()[0] == pytest.approx(130.0)


def test_rc06_c7_price_waits_for_five_retained_and_preserves_manual_price(seeded_db, db_conn):
    item_id = ramones_id(seeded_db)
    db_conn.execute("UPDATE items SET market_price_usd = 765 WHERE id = ?", (item_id,))
    db_conn.execute("UPDATE items SET manual_price_usd = 810 WHERE id = ?", (item_id,))
    db_conn.commit()
    first = db.record_ebay_listing_prices(
        item_id, [listing(f"legacy:{n}", p) for n, p in enumerate((100, 110, 120, 130))],
        observed_at=OBSERVED,
    )
    assert first.retained_count == 4 and first.mean_usd is None
    assert tuple(db_conn.execute("SELECT market_price_usd, manual_price_usd FROM items WHERE id = ?", (item_id,)).fetchone()) == (765, 810)
    later = db.record_ebay_listing_prices(item_id, [listing("legacy:4", 140)], observed_at=OBSERVED)
    assert later.retained_count == 5 and later.mean_usd == 120.0
    assert tuple(db_conn.execute("SELECT market_price_usd, manual_price_usd FROM items WHERE id = ?", (item_id,)).fetchone()) == (120, 810)


def test_rc06_c8_outlier_is_stored_but_excluded_from_current_mean(seeded_db):
    item_id = ramones_id(seeded_db)
    result = db.record_ebay_listing_prices(
        item_id, [listing(f"legacy:{n}", p) for n, p in enumerate((100, 105, 110, 115, 120, 1000))],
        observed_at=OBSERVED,
    )
    assert result.new_count == 6
    assert result.candidate_count == 6 and result.outlier_count == 1 and result.retained_count == 5
    assert result.mean_usd == 110.0
    assert len(stored(seeded_db, item_id)) == 6


def test_rc06_c9_previous_outlier_can_become_inlier_as_pool_changes(seeded_db):
    item_id = ramones_id(seeded_db)
    db.record_ebay_listing_prices(
        item_id, [listing(f"legacy:{n}", p) for n, p in enumerate((100, 100, 100, 100, 1000))],
        observed_at=OBSERVED,
    )
    result = db.record_ebay_listing_prices(
        item_id, [listing(f"legacy:{n}", 1000) for n in range(5, 10)],
        observed_at="2026-10-28T12:30:00+00:00",
    )
    assert result.new_count == 5
    assert len(stored(seeded_db, item_id)) == 10
    assert result.retained_count == 6
    assert result.mean_usd == 1000.0


def test_rc06_c10_zero_mad_retains_only_equal_prices(seeded_db):
    item_id = ramones_id(seeded_db)
    result = db.record_ebay_listing_prices(
        item_id, [listing(f"legacy:{n}", p) for n, p in enumerate((100, 100, 100, 100, 100, 1000))],
        observed_at=OBSERVED,
    )
    assert result.candidate_count == 6 and result.outlier_count == 1
    assert result.retained_count == 5 and result.mean_usd == 100.0
    assert len(stored(seeded_db, item_id)) == 6


def test_rc06_c8_mad_cutoff_value_is_retained_and_next_float_is_removed(seeded_db):
    item_id = ramones_id(seeded_db)
    boundary = 100 + (3.5 * 1.4826 * 10)
    just_above = math.nextafter(boundary, math.inf)
    prices = [90, 90, 100, 100, 100, 110, boundary, just_above]
    result = db.record_ebay_listing_prices(
        item_id, [listing(f"legacy:{n}", price) for n, price in enumerate(prices)],
        observed_at=OBSERVED,
    )
    assert result.candidate_count == 8
    assert result.outlier_count == 1
    assert result.retained_count == 7
    assert result.mean_usd == pytest.approx(sum(prices[:-1]) / 7)
    assert len(stored(seeded_db, item_id)) == 8


def test_rc06_c11_repeat_only_keeps_first_price_and_timestamp_and_does_not_rewrite_price(seeded_db, db_conn):
    item_id = ramones_id(seeded_db)
    initial = [listing(f"legacy:{n}", p) for n, p in enumerate((100, 110, 120, 130, 140))]
    db.record_ebay_listing_prices(item_id, initial, observed_at=OBSERVED)
    before = stored(seeded_db, item_id)
    db_conn.execute("UPDATE items SET market_price_usd = 999 WHERE id = ?", (item_id,))
    db_conn.commit()
    result = db.record_ebay_listing_prices(
        item_id, [listing(f"legacy:{n}", p + 500) for n, p in enumerate((100, 110, 120, 130, 140))],
        observed_at="2026-10-28T12:30:00+00:00",
    )
    assert result.new_count == 0 and result.duplicate_count == 5
    assert result.price_updated is False
    assert stored(seeded_db, item_id) == before
    assert rows(seeded_db, "SELECT market_price_usd FROM items WHERE id = ?", (item_id,)) == [(999,)]


def test_rc06_c12_empty_search_preserves_price_and_reports_accumulated_counts(seeded_db, db_conn):
    item_id = ramones_id(seeded_db)
    db.record_ebay_listing_prices(item_id, [listing(f"legacy:{n}", p) for n, p in enumerate((100, 110, 120))], observed_at=OBSERVED)
    db_conn.execute("UPDATE items SET market_price_usd = 765 WHERE id = ?", (item_id,))
    db_conn.commit()
    result = db.record_ebay_listing_prices(item_id, [], observed_at="2026-10-28T12:30:00+00:00")
    assert result.new_count == 0 and result.candidate_count == 3
    assert result.price_updated is False
    assert rows(seeded_db, "SELECT market_price_usd FROM items WHERE id = ?", (item_id,)) == [(765,)]


def test_rc06_c13_canonical_variations_deduplicate_per_item_but_not_across_items(seeded_db):
    ramones = db.get_item_id("Rick Owens", "rick owens ramones")
    pods = db.get_item_id("Rick Owens", "pod shorts")
    db.record_ebay_listing_prices(ramones, [listing("legacy:123", 100), listing("legacy:124", 110)], observed_at=OBSERVED)
    db.record_ebay_listing_prices(pods, [listing("legacy:123", 100)], observed_at=OBSERVED)
    assert len(stored(seeded_db, ramones)) == 2
    assert len(stored(seeded_db, pods)) == 1


@pytest.mark.parametrize("failure", ["insert", "price_update", "commit"])
def test_rc06_c16_writer_rolls_back_and_closes_after_transaction_failure(
    seeded_db, db_conn, monkeypatch, failure
):
    item_id = ramones_id(seeded_db)
    db_conn.execute("UPDATE items SET market_price_usd = 765 WHERE id = ?", (item_id,))
    db_conn.commit()
    if failure == "insert":
        db_conn.execute(
            "CREATE TRIGGER reject_listing_price BEFORE INSERT ON ebay_listing_prices "
            "BEGIN SELECT RAISE(ABORT, 'injected listing insert failure'); END"
        )
    elif failure == "price_update":
        db_conn.execute(
            "CREATE TRIGGER reject_market_update BEFORE UPDATE OF market_price_usd ON items "
            "BEGIN SELECT RAISE(ABORT, 'injected market update failure'); END"
        )
    db_conn.commit()
    real_get_connection = db.get_connection
    closed = []

    class TrackedConnection:
        def __init__(self, connection, fail_commit=False):
            self.connection = connection
            self.fail_commit = fail_commit

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, sql, *args, **kwargs):
            if self.fail_commit and sql.strip().upper() == "COMMIT":
                raise sqlite3.OperationalError("injected commit failure")
            return self.connection.execute(sql, *args, **kwargs)

        def commit(self):
            if self.fail_commit:
                raise sqlite3.OperationalError("injected commit failure")
            return self.connection.commit()

        def close(self):
            closed.append(True)
            self.connection.close()

    monkeypatch.setattr(
        db, "get_connection",
        lambda: TrackedConnection(real_get_connection(), fail_commit=failure == "commit"),
    )
    error = sqlite3.OperationalError if failure == "commit" else sqlite3.IntegrityError
    with pytest.raises(error):
        db.record_ebay_listing_prices(
            item_id, [listing(f"legacy:{n}", p) for n, p in enumerate((100, 110, 120, 130, 140))],
            observed_at=OBSERVED,
        )
    assert closed
    assert stored(seeded_db, item_id) == []
    assert rows(seeded_db, "SELECT market_price_usd FROM items WHERE id = ?", (item_id,)) == [(765,)]


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), True, None])
def test_rc06_c17_invalid_prices_are_rejected_before_opening_database(seeded_db, monkeypatch, bad):
    item_id = ramones_id(seeded_db)
    monkeypatch.setattr(db, "get_connection", lambda: pytest.fail("database opened before validation"))
    with pytest.raises(ValueError):
        db.record_ebay_listing_prices(item_id, [listing("legacy:1", bad)], observed_at=OBSERVED)


def test_rc06_c17_invalid_utc_timestamp_is_rejected_before_database(seeded_db, monkeypatch):
    item_id = ramones_id(seeded_db)
    monkeypatch.setattr(db, "get_connection", lambda: pytest.fail("database opened before validation"))
    for bad in ("2026-10-27T12:00:00", "not-a-time", datetime(2026, 10, 27)):
        with pytest.raises(ValueError):
            db.record_ebay_listing_prices(item_id, [], observed_at=bad)


@pytest.mark.parametrize("bad_id", [None, "", "   ", 17])
def test_rc06_c17_invalid_listing_ids_are_rejected_before_database(seeded_db, monkeypatch, bad_id):
    item_id = ramones_id(seeded_db)
    monkeypatch.setattr(db, "get_connection", lambda: pytest.fail("database opened before validation"))
    with pytest.raises(ValueError):
        db.record_ebay_listing_prices(item_id, [listing(bad_id, 100)], observed_at=OBSERVED)


@pytest.mark.parametrize("bad_item_id", [0, -1, True, "1", None])
def test_rc06_c17_invalid_item_ids_are_rejected_before_database(seeded_db, monkeypatch, bad_item_id):
    monkeypatch.setattr(db, "get_connection", lambda: pytest.fail("database opened before validation"))
    with pytest.raises(ValueError):
        db.record_ebay_listing_prices(bad_item_id, [], observed_at=OBSERVED)


def test_rc06_c17_unknown_item_does_not_write(seeded_db):
    result = db.record_ebay_listing_prices(999999, [listing("legacy:1", 100)], observed_at=OBSERVED)
    assert result.item_found is False
    assert result.new_count == 0
    assert rows(seeded_db, "SELECT COUNT(*) FROM ebay_listing_prices") == [(0,)]


def test_rc06_c19_seeding_preserves_listing_history_and_inactive_item_observations(seeded_db, test_catalog, tmp_path):
    item_id = ramones_id(seeded_db)
    db.record_ebay_listing_prices(item_id, [listing("legacy:keep", 100)], observed_at=OBSERVED)
    pods_id = db.get_item_id("Rick Owens", "pod shorts")
    db.record_ebay_listing_prices(pods_id, [listing("legacy:pods", 200)], observed_at=OBSERVED)
    db.seed_from_catalog()
    assert stored(seeded_db, item_id) == [("legacy:keep", 100.0, OBSERVED)]
    assert stored(seeded_db, pods_id) == [("legacy:pods", 200.0, OBSERVED)]
    data = __import__("json").loads(test_catalog.read_text(encoding="utf-8"))
    # Use a temporary one-item catalog to deactivate the item without deleting it.
    path = tmp_path / "catalog.json"
    path.write_text(__import__("json").dumps(data[:1]), encoding="utf-8")
    db.seed_from_catalog(path)
    assert rows(seeded_db, "SELECT active FROM items WHERE id = ?", (db.get_item_id("Rick Owens", "pod shorts"),)) == [(0,)]
    assert stored(seeded_db, pods_id) == [("legacy:pods", 200.0, OBSERVED)]


def _spec_python_script_after(marker):
    spec = Path(__file__).resolve().parents[1] / "docs/specs/RC-06-market-price-history.md"
    text = spec.read_text(encoding="utf-8")
    section = text[text.index(marker):]
    match = re.search(r"```sh\s*\n(.*?)\n\s*```", section, re.DOTALL)
    assert match, f"approved spec must keep the documented script after {marker!r}"
    command = match.group(1)
    start = command.index("python3 - <<'PY'") + len("python3 - <<'PY'\n")
    end_match = re.search(r"\n\s*PY\s*$", command)
    assert end_match, "documented Python heredoc must have a closing delimiter"
    end = end_match.start()
    return textwrap.dedent(command[start:end])


def _targeted_update_script():
    return _spec_python_script_after("5. Add only the `fake` exclude")


def _rc16_online_backup_script():
    spec = Path(__file__).resolve().parents[1] / "docs/specs/RC-16-auction-aware-deals.md"
    text = spec.read_text(encoding="utf-8")
    marker = "For the version 3 step, this command creates and verifies the required version 2 online backup"
    section = text[text.index(marker):]
    match = re.search(r"```sh\s*\n(.*?)\n\s*```", section, re.DOTALL)
    assert match, "approved RC-16 spec must retain its online-backup command"
    command = match.group(1)
    start = command.index("<<'PY'\n") + len("<<'PY'\n")
    end = command.rfind("\nPY")
    assert end > start, "RC-16 online-backup heredoc must close"
    return textwrap.dedent(command[start:end])


def test_rc16_c2_online_backup_includes_committed_uncheckpointed_wal_row(tmp_path):
    source_root = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "wal-v2.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            (source_root / "tests/fixtures/db/schema_v2.sql").read_text(encoding="utf-8")
        )
        conn.execute("PRAGMA user_version = 2")
        conn.execute("INSERT INTO brands (id, name, yahoo_brand_id) VALUES (1, 'Rick Owens', ?)",
                     (db.DEFAULT_YAHOO_BRAND_ID,))
        conn.execute(
            "INSERT INTO items (id, brand_id, name, manual_price_usd) VALUES (1, 1, ?, 800)",
            ("rick owens ramones",),
        )
        conn.execute(
            "INSERT INTO items (id, brand_id, name, manual_price_usd) VALUES (2, 1, ?, 300)",
            ("pod shorts",),
        )
        conn.executemany(
            "INSERT INTO item_keywords (item_id, keyword, kind) VALUES (?, 'fake', 'exclude')",
            [(1,), (2,)],
        )
        conn.commit()
        assert conn.execute("PRAGMA journal_mode = WAL").fetchone()[0].lower() == "wal"
        conn.execute("PRAGMA wal_autocheckpoint = 0")
        conn.execute(
            "INSERT INTO listings (id, brand_id, url, title, price_yen, first_seen_at, last_seen_at) "
            "VALUES (1, 1, ?, ?, 50000, '2026-10-27T00:00:00+00:00', '2026-10-27T00:00:00+00:00')",
            ("https://auctions.yahoo.co.jp/jp/auction/waltest", "WAL listing"),
        )
        conn.execute(
            "INSERT INTO listing_matches (listing_id, item_id, matched_at) "
            "VALUES (1, 1, '2026-10-27T00:00:00+00:00')"
        )
        conn.commit()
        wal_path = Path(str(db_path) + "-wal")
        assert wal_path.is_file() and wal_path.stat().st_size > 0

        raw_copy = tmp_path / "raw-file-copy.db"
        shutil.copy2(db_path, raw_copy)
        raw = sqlite3.connect(raw_copy)
        try:
            assert raw.execute("SELECT COUNT(*) FROM listings").fetchone() == (0,)
        finally:
            raw.close()

        home = tmp_path / "home"
        home.mkdir()
        env = os.environ.copy()
        env["HOME"] = str(home)
        env["RC_UPGRADE_DB_PATH"] = str(db_path)
        result = subprocess.run(
            [sys.executable, "-c", _rc16_online_backup_script()],
            cwd=tmp_path, env=env, capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        backup_path = Path(result.stdout.strip().removeprefix("Verified RC-16 backup: "))
        assert rows(backup_path, "PRAGMA user_version") == [(2,)]
        assert rows(backup_path, "PRAGMA quick_check") == [("ok",)]
        assert rows(backup_path, "SELECT url FROM listings") == [
            ("https://auctions.yahoo.co.jp/jp/auction/waltest",)
        ]
        assert rows(backup_path, "SELECT listing_id, item_id FROM listing_matches") == [(1, 1)]
    finally:
        conn.close()


def _prepare_update_repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    src_dir = repo / "src"
    src_dir.mkdir(parents=True)
    source_root = Path(__file__).resolve().parents[1]
    (src_dir / "matching.py").write_text(
        (source_root / "src/matching.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    catalog = json.loads((source_root / "tests/fixtures/catalog.json").read_text(encoding="utf-8"))
    for entry in catalog:
        entry["exclude_keywords"] = [word for word in entry["exclude_keywords"] if word != "fake"]
    db_path = src_dir / "raincheck.db"
    monkeypatch.setenv("RAINCHECK_DB_PATH", str(db_path))
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            (source_root / "tests/fixtures/db/schema_v2.sql").read_text(encoding="utf-8")
        )
        conn.execute("PRAGMA user_version = 2")
        conn.execute(
            "INSERT INTO brands (id, name, yahoo_brand_id) VALUES (?, ?, ?)",
            (1, "Rick Owens", db.DEFAULT_YAHOO_BRAND_ID),
        )
        for item_id, entry in enumerate(catalog, start=1):
            conn.execute(
                "INSERT INTO items (id, brand_id, name, manual_price_usd, search_query) "
                "VALUES (?, 1, ?, ?, ?)",
                (item_id, entry["item name"], entry["market_price"], entry["search_query"]),
            )
            for keyword in entry["keywords"]:
                conn.execute(
                    "INSERT INTO item_keywords (item_id, keyword, kind) VALUES (?, ?, ?)",
                    (item_id, keyword, "include"),
                )
            for keyword in entry["exclude_keywords"]:
                conn.execute(
                    "INSERT INTO item_keywords (item_id, keyword, kind) VALUES (?, ?, ?)",
                    (item_id, keyword, "exclude"),
                )
        conn.execute(
            "INSERT INTO item_keywords (item_id, keyword, kind) VALUES (?, ?, ?)",
            (2, "database only phrase", "include"),
        )
        conn.commit()
    finally:
        conn.close()
    home = tmp_path / "home"
    home.mkdir()
    env = os.environ.copy()
    env["HOME"] = str(home)
    return repo, db_path, home, env


def test_rc06_c25_targeted_update_script_preserves_keywords_and_is_idempotent(
    tmp_path, monkeypatch
):
    repo, db_path, home, env = _prepare_update_repo(tmp_path, monkeypatch)
    before = set(rows(db_path, "SELECT item_id, keyword, kind FROM item_keywords"))
    script = _targeted_update_script()

    first = subprocess.run([sys.executable, "-c", script], cwd=repo, env=env, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    after_first = set(rows(db_path, "SELECT item_id, keyword, kind FROM item_keywords"))
    assert after_first == before | {
        (1, "fake", "exclude"),
        (2, "fake", "exclude"),
    }
    custom_keyword = (2, "database only phrase", "include")
    assert custom_keyword in after_first
    assert len(list((home / "RainCheck-backups").glob("raincheck-before-rc06-fake-*.db"))) == 1

    second = subprocess.run([sys.executable, "-c", script], cwd=repo, env=env, capture_output=True, text=True)
    assert second.returncode == 0, second.stderr
    after_second = set(rows(db_path, "SELECT item_id, keyword, kind FROM item_keywords"))
    assert after_second == after_first
    assert custom_keyword in after_second
    assert len(list((home / "RainCheck-backups").glob("raincheck-before-rc06-fake-*.db"))) == 2


def test_rc06_c26_targeted_update_refuses_normalized_fake_include_without_writing(
    tmp_path, monkeypatch
):
    repo, db_path, home, env = _prepare_update_repo(tmp_path, monkeypatch)
    pods_id = 2
    run(db_path, "INSERT INTO item_keywords (item_id, keyword, kind) VALUES (?, ?, ?)",
        (pods_id, "ｆａｋｅ", "include"))
    before = set(rows(db_path, "SELECT item_id, keyword, kind FROM item_keywords"))

    result = subprocess.run(
        [sys.executable, "-c", _targeted_update_script()], cwd=repo, env=env,
        capture_output=True, text=True,
    )

    assert result.returncode != 0
    assert set(rows(db_path, "SELECT item_id, keyword, kind FROM item_keywords")) == before
    assert not list((home / "RainCheck-backups").glob("raincheck-before-rc06-fake-*.db"))


def test_rc16_c2_v1_through_v2_to_v3_upgrade_keeps_backup_integrity_and_keywords(
    legacy_db, tmp_path, monkeypatch
):
    _upgrade_to_v1(legacy_db)
    # Recreate the installed RC-06 schema-v2 checkpoint before exercising its
    # targeted keyword procedure and the subsequent RC-16 v2 -> v3 migration.
    conn = sqlite3.connect(legacy_db)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for statement in db.MIGRATIONS[2]:
            conn.execute(statement)
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
    finally:
        conn.close()

    repo = tmp_path / "upgrade-repo"
    src_dir = repo / "src"
    src_dir.mkdir(parents=True)
    source_root = Path(db.__file__).resolve().parents[1]
    upgrade_db = src_dir / "raincheck.db"
    upgrade_db.write_bytes(Path(legacy_db).read_bytes())
    for filename in ("db.py", "schema.sql", "matching.py"):
        (src_dir / filename).write_bytes((source_root / "src" / filename).read_bytes())
    home = tmp_path / "upgrade-home"
    home.mkdir()
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["RAINCHECK_DB_PATH"] = str(upgrade_db)

    # Apply the approved RC-06 data-only update while the database is still v2.
    update_run = subprocess.run(
        [sys.executable, "-c", _targeted_update_script()],
        cwd=repo, env=env, capture_output=True, text=True,
    )
    assert update_run.returncode == 0, update_run.stderr
    assert len(list((home / "RainCheck-backups").glob("raincheck-before-rc06-fake-*.db"))) == 1

    before_columns = columns(upgrade_db, "listings")
    tables = ("brands", "items", "item_keywords", "listings", "listing_matches", "ebay_listing_prices")
    before = {
        table: rows(upgrade_db, f"SELECT * FROM {table} ORDER BY rowid")
        for table in tables
    }
    backup_dir = home / "RainCheck-backups"
    backup_dir.mkdir(mode=0o700, exist_ok=True)
    backup_path = backup_dir / "raincheck-before-rc16-test.db"
    live = sqlite3.connect(upgrade_db)
    backup = sqlite3.connect(backup_path)
    try:
        live.backup(backup)
        assert backup.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert backup.execute("PRAGMA user_version").fetchone() == (2,)
    finally:
        backup.close()
        live.close()
    assert backup_path.is_file() and backup_path.stat().st_size > 0
    assert rows(backup_path, "PRAGMA foreign_key_check") == []
    assert {
        (1, "fake", "exclude"),
        (2, "fake", "exclude"),
    } <= set(rows(backup_path, "SELECT item_id, keyword, kind FROM item_keywords"))
    for table, expected in before.items():
        assert rows(backup_path, f"SELECT * FROM {table} ORDER BY rowid") == expected

    migration_run = subprocess.run(
        [sys.executable, "src/db.py"], cwd=repo, env=env, capture_output=True, text=True,
    )
    assert migration_run.returncode == 0, migration_run.stderr
    assert user_version(upgrade_db) == 4
    assert rows(upgrade_db, "PRAGMA integrity_check") == [("ok",)]
    assert rows(upgrade_db, "PRAGMA foreign_key_check") == []
    assert {"is_fixed_price", "buy_now_price_yen", "scheduled_end_at"} <= set(
        columns(upgrade_db, "listings")
    )
    for table, expected in before.items():
        actual = rows(upgrade_db, f"SELECT * FROM {table} ORDER BY rowid")
        if table == "listings":
            assert columns(upgrade_db, table)[:len(before_columns)] == before_columns
            assert [row[:len(before_columns)] for row in actual] == expected
            assert all(row[len(before_columns):] == (None, None, None) for row in actual)
        else:
            assert actual == expected
    keyword_rows = set(rows(upgrade_db, "SELECT item_id, keyword, kind FROM item_keywords"))
    assert {(1, "fake", "exclude"), (2, "fake", "exclude")} <= keyword_rows


def test_rc06_c28_backlog_describes_distinct_all_time_filtered_arithmetic_mean():
    backlog = Path(__file__).resolve().parents[1] / "docs/BACKLOG.md"
    text = backlog.read_text(encoding="utf-8").lower()
    rc06 = text[text.index("### rc-06"):]
    rc06 = rc06.split("\n### ", 1)[0]
    assert "distinct" in rc06 and "listing" in rc06
    assert "all-time" in rc06
    assert "arithmetic mean" in rc06
    assert "mad" in rc06 or "median absolute deviation" in rc06
    assert "median per run" not in rc06
