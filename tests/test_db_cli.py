"""Tests for db.main (RC-02 criteria 5-13).

db.main is called in-process so conftest's no-op load_dotenv and the temporary
RAINCHECK_DB_PATH apply.  Tests that need the database file to not exist
beforehand do not use the temp_db fixture.
"""
import sqlite3
from pathlib import Path

import pytest

from conftest import assert_db_is_temp
from dbhelpers import assert_legacy_data_preserved, dump_all, set_user_version, user_version

TABLES = {"brands", "items", "item_keywords", "listings", "listing_matches"}


def _rows(path, sql):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _count(path, table):
    return _rows(path, f"SELECT COUNT(*) FROM {table}")[0][0]


@pytest.fixture
def fresh_path(tmp_path):
    """Temp database path that does not exist yet (after the safety check)."""
    assert_db_is_temp(tmp_path)
    path = tmp_path / "test.db"
    assert not path.exists()
    return path


def test_rc02_c5_main_no_args_creates_five_empty_tables(fresh_path, capsys):
    import db

    db.main([])

    assert fresh_path.exists()
    names = {r[0] for r in _rows(fresh_path, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert TABLES <= names
    for table in TABLES:
        assert _count(fresh_path, table) == 0
    assert f"Database initialized at {fresh_path}." in capsys.readouterr().out


def test_rc02_c6_main_seed_produces_catalog_rows(fresh_path, test_catalog):
    import db

    db.main(["--seed"])

    assert _rows(fresh_path, "SELECT name, yahoo_brand_id FROM brands") == [("Rick Owens", "103538")]
    items = _rows(
        fresh_path,
        "SELECT name, manual_price_usd, market_price_usd, search_query FROM items ORDER BY name",
    )
    assert items == [
        ("pod shorts", 300.0, None, "pod shorts"),
        ("rick owens ramones", 800.0, None, "rick owens ramones"),
    ]
    kws = _rows(
        fresh_path,
        "SELECT i.name, k.kind, k.keyword FROM item_keywords k JOIN items i ON i.id = k.item_id "
        "ORDER BY i.name, k.kind, k.keyword",
    )
    assert kws == [
        ("pod shorts", "exclude", "fake"),
        ("pod shorts", "exclude", "pants"),
        ("pod shorts", "include", "pod short"),
        ("pod shorts", "include", "pod shorts"),
        ("rick owens ramones", "exclude", "canvas"),
        ("rick owens ramones", "exclude", "drkshdw"),
        ("rick owens ramones", "exclude", "fake"),
        ("rick owens ramones", "exclude", "low"),
        ("rick owens ramones", "include", "ramon"),
        ("rick owens ramones", "include", "ramones"),
    ]


def test_rc02_c7_main_seed_prints_init_then_seeded(fresh_path, test_catalog, capsys):
    import db

    db.main(["--seed"])

    out = capsys.readouterr().out
    init_line = f"Database initialized at {fresh_path}."
    assert init_line in out
    assert "Catalog seeded." in out
    assert out.index(init_line) < out.index("Catalog seeded.")


def test_rc02_c8_main_seed_twice_keeps_row_counts(fresh_path, test_catalog):
    import db

    db.main(["--seed"])
    first = {t: _count(fresh_path, t) for t in ("brands", "items", "item_keywords")}
    db.main(["--seed"])
    second = {t: _count(fresh_path, t) for t in ("brands", "items", "item_keywords")}

    assert first == {"brands": 1, "items": 2, "item_keywords": 10}
    assert second == first


def test_rc02_c9_main_seed_leaves_existing_listing_unchanged(fresh_path, test_catalog):
    import db

    db.main(["--seed"])
    conn = sqlite3.connect(fresh_path)
    conn.execute(
        "INSERT INTO listings (brand_id, url, title, price_yen, image_url, first_seen_at, last_seen_at) "
        "VALUES (1, 'https://example.test/a', 'Some title', 12345, NULL, '2026-01-01', '2026-01-02')"
    )
    conn.commit()
    conn.close()
    before = _rows(fresh_path, "SELECT * FROM listings")

    db.main(["--seed"])

    assert _rows(fresh_path, "SELECT * FROM listings") == before
    assert len(before) == 1


def test_rc02_c10_main_unknown_arg_exits_2_without_creating_db(fresh_path, capsys):
    import db

    with pytest.raises(SystemExit) as exc:
        db.main(["--bogus"])

    assert exc.value.code == 2
    assert not fresh_path.exists()


def test_rc02_c11_main_help_exits_0_prints_seed_without_creating_db(fresh_path, capsys):
    import db

    with pytest.raises(SystemExit) as exc:
        db.main(["--help"])

    assert exc.value.code == 0
    assert "--seed" in capsys.readouterr().out
    assert not fresh_path.exists()


def test_rc02_c12_main_seed_missing_catalog_raises_filenotfound(fresh_path, tmp_path, monkeypatch):
    import db

    monkeypatch.setattr(db, "CATALOG_PATH", tmp_path / "no_such_catalog.json")

    with pytest.raises(FileNotFoundError):
        db.main(["--seed"])


def test_rc02_c13_main_calls_load_dotenv_before_opening_db(tmp_path, monkeypatch):
    import db

    assert_db_is_temp(tmp_path)
    target = tmp_path / "from_dotenv.db"
    assert not target.exists()

    def fake_load_dotenv(*args, **kwargs):
        monkeypatch.setenv("RAINCHECK_DB_PATH", str(target))
        return True

    monkeypatch.setattr(db, "load_dotenv", fake_load_dotenv)

    db.main([])

    assert target.exists()
    names = {r[0] for r in _rows(target, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert TABLES <= names
    assert not (tmp_path / "test.db").exists()


# --- RC-03a: migration and error reporting (criteria 38-40) ------------------


def test_rc03a_c38_main_migrates_a_legacy_database(legacy_db, capsys):
    import db

    before = dump_all(legacy_db)

    db.main([])

    assert user_version(legacy_db) == 4
    assert_legacy_data_preserved(legacy_db, before)
    assert f"Migrated database at {legacy_db} from schema version 0 to 4." in capsys.readouterr().out


def test_rc03a_c39_main_seed_with_invalid_catalog_exits_1_and_changes_no_items(
    seeded_db, tmp_path, monkeypatch, capsys
):
    import db

    before = _rows(seeded_db, "SELECT * FROM items ORDER BY id")
    bad = tmp_path / "bad_catalog.json"
    bad.write_text('[{"brand": "Rick Owens", "item name": "x", "market_price": 0}]')
    monkeypatch.setattr(db, "CATALOG_PATH", bad)
    capsys.readouterr()

    with pytest.raises(SystemExit) as exc:
        db.main(["--seed"])

    assert exc.value.code == 1
    assert capsys.readouterr().err.startswith("Error: Catalog is invalid:")
    assert _rows(seeded_db, "SELECT * FROM items ORDER BY id") == before


def test_rc03a_c40_main_on_a_newer_database_exits_1(temp_db, capsys):
    import db

    set_user_version(temp_db, 5)
    capsys.readouterr()

    with pytest.raises(SystemExit) as exc:
        db.main([])

    assert exc.value.code == 1
    assert "Error:" in capsys.readouterr().err
