"""RC-04 listing to tracked-item persistence tests."""
import gc
import sqlite3
import warnings

import pytest

import db
from dbhelpers import user_version


def _ids(conn):
    return {
        row["name"]: row["id"]
        for row in conn.execute("SELECT id, name FROM items").fetchall()
    }


def _brand_id(conn):
    return conn.execute("SELECT id FROM brands WHERE name = ?", ("Rick Owens",)).fetchone()[0]


def test_rc04_c2_one_listing_can_match_two_items(seeded_db, db_conn):
    items = _ids(db_conn)
    brand_id = _brand_id(db_conn)

    assert db.record_listing_match(brand_id, "https://x/shared", "Shared", 50000, None,
                                   items["rick owens ramones"]) is True
    assert db.record_listing_match(brand_id, "https://x/shared", "Shared", 50000, None,
                                   items["pod shorts"]) is False

    assert db_conn.execute("SELECT COUNT(*) FROM listings WHERE url = ?", ("https://x/shared",)).fetchone()[0] == 1
    assert [tuple(row) for row in db_conn.execute(
        "SELECT item_id FROM listing_matches ORDER BY item_id"
    ).fetchall()] == [(items["rick owens ramones"],), (items["pod shorts"],)]


def test_rc04_c3_repeat_is_idempotent_for_listing_and_match(seeded_db, db_conn):
    item_id = _ids(db_conn)["rick owens ramones"]
    args = (_brand_id(db_conn), "https://x/repeat", "Repeat", 42000, None, item_id)

    assert db.record_listing_match(*args) is True
    assert db.record_listing_match(*args) is False
    assert db_conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 1
    assert db_conn.execute("SELECT COUNT(*) FROM listing_matches").fetchone()[0] == 1


def test_rc04_c4_conflicting_url_keeps_metadata_and_records_item(seeded_db, db_conn):
    item_id = _ids(db_conn)["rick owens ramones"]
    brand_id = _brand_id(db_conn)
    db.insert_listing(brand_id, "https://x/conflict", "Original", 12345, "https://x/original.jpg")
    before = dict(db_conn.execute("SELECT * FROM listings WHERE url = ?", ("https://x/conflict",)).fetchone())

    assert db.record_listing_match(brand_id, "https://x/conflict", "Changed", 99999,
                                   None, item_id) is False
    after = dict(db_conn.execute("SELECT * FROM listings WHERE url = ?", ("https://x/conflict",)).fetchone())
    assert after == before
    assert [tuple(row) for row in db_conn.execute("SELECT item_id FROM listing_matches").fetchall()] == [(item_id,)]


@pytest.mark.parametrize("bad_foreign_key", ["brand", "item"])
def test_rc04_c7_invalid_foreign_key_rolls_back_listing_and_match(
    seeded_db, db_conn, bad_foreign_key
):
    item_id = _ids(db_conn)["rick owens ramones"]
    brand_id = _brand_id(db_conn)
    if bad_foreign_key == "brand":
        brand_id = 999999
    else:
        item_id = 999999

    with pytest.raises(sqlite3.IntegrityError):
        db.record_listing_match(brand_id, f"https://x/invalid-{bad_foreign_key}",
                                "Invalid", 10000, None, item_id)

    assert db_conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 0
    assert db_conn.execute("SELECT COUNT(*) FROM listing_matches").fetchone()[0] == 0


def test_rc04_c7_integrity_error_closes_database_connection(seeded_db):
    def attempt():
        with pytest.raises(sqlite3.IntegrityError):
            db.record_listing_match(999999, "https://x/orphan", "Orphan", 10000, None, 1)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        attempt()
        gc.collect()
    assert [w for w in caught if issubclass(w.category, ResourceWarning)] == []


def test_rc04_required_title_none_raises_integrity_error_without_partial_writes(seeded_db):
    from dbhelpers import rows as sql_rows

    brand_id = sql_rows(seeded_db, "SELECT id FROM brands WHERE name = ?", ("Rick Owens",))[0][0]
    item_id = sql_rows(
        seeded_db, "SELECT id FROM items WHERE name = ?", ("rick owens ramones",)
    )[0][0]

    def attempt():
        with pytest.raises(sqlite3.IntegrityError):
            db.record_listing_match(brand_id, "https://x/missing-title", None, 10000, None, item_id)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        attempt()
        gc.collect()

    assert [w for w in caught if issubclass(w.category, ResourceWarning)] == []
    assert sql_rows(seeded_db, "SELECT url FROM listings") == []
    assert sql_rows(seeded_db, "SELECT listing_id, item_id FROM listing_matches") == []


def test_rc04_c8_schema_v1_and_existing_listing_match_rows_are_preserved(seeded_db, db_conn):
    item_id = _ids(db_conn)["rick owens ramones"]
    brand_id = _brand_id(db_conn)
    db.insert_listing(brand_id, "https://x/existing", "Existing", 20000, None)
    db_conn.execute(
        "INSERT INTO listing_matches (listing_id, item_id, matched_at) "
        "SELECT id, ?, '2026-01-01T00:00:00+00:00' FROM listings WHERE url = ?",
        (item_id, "https://x/existing"),
    )
    db_conn.commit()
    before = db_conn.execute("SELECT listing_id, item_id, matched_at FROM listing_matches").fetchall()

    assert db.record_listing_match(brand_id, "https://x/new", "New", 30000, None, item_id) is True

    assert user_version(seeded_db) == 1
    assert [tuple(row) for row in db_conn.execute("SELECT url FROM listings ORDER BY url").fetchall()] == [
        ("https://x/existing",), ("https://x/new",),
    ]
    assert db_conn.execute(
        "SELECT listing_id, item_id, matched_at FROM listing_matches WHERE listing_id = 1"
    ).fetchall() == before
