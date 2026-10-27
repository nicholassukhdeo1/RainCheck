"""RC-03a criteria 13-29: validated, transactional upsert seeding."""
import json
from pathlib import Path

import pytest

import db
from dbhelpers import (
    assert_legacy_data_preserved,
    dump_all,
    dump_table,
    keyword_sets,
    rows,
    run,
    user_version,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_CATALOG = REPO_ROOT / "tests" / "fixtures" / "catalog.json"
RAMONES = ("Rick Owens", "rick owens ramones")
PODS = ("Rick Owens", "pod shorts")
ITEM_COLUMNS_SQL = (
    "SELECT id, brand_id, name, market_price_usd, search_query, manual_price_usd, active "
    "FROM items WHERE id = ?"
)


def base_catalog():
    return json.loads(FIXTURE_CATALOG.read_text(encoding="utf-8"))


def write_catalog(tmp_path, data, name="catalog_variant.json"):
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def item_row(path, name):
    return rows(
        path,
        "SELECT id, market_price_usd, manual_price_usd, search_query, active "
        "FROM items WHERE name = ?",
        (name,),
    )[0]


# --- Fresh seed (13-14) ------------------------------------------------------


def test_rc03a_c13_seed_writes_expected_rows(seeded_db):
    assert rows(seeded_db, "SELECT name, yahoo_brand_id FROM brands") == [("Rick Owens", "103538")]
    assert rows(
        seeded_db,
        "SELECT name, manual_price_usd, market_price_usd, search_query, active "
        "FROM items ORDER BY id",
    ) == [
        ("rick owens ramones", 800.0, None, "rick owens ramones", 1),
        ("pod shorts", 300.0, None, "pod shorts", 1),
    ]
    assert keyword_sets(seeded_db) == {
        "rick owens ramones": {
            "include": {"ramones", "ramon"},
            "exclude": {"drkshdw", "canvas", "low"},
        },
        "pod shorts": {"include": {"pod short"}, "exclude": {"pants"}},
    }


def test_rc03a_c14_reseed_is_idempotent_and_reports_updated(temp_db, test_catalog):
    first = db.seed_from_catalog()
    assert first == db.SeedResult(added=(RAMONES, PODS), updated=(), deactivated=())
    before = {t: dump_table(temp_db, t) for t in ("brands", "items", "item_keywords")}

    second = db.seed_from_catalog()

    assert second == db.SeedResult(added=(), updated=(RAMONES, PODS), deactivated=())
    assert {t: dump_table(temp_db, t) for t in before} == before


# --- Edits propagate (15-18) -------------------------------------------------


def test_rc03a_c15_catalog_edits_reach_the_database(seeded_db, tmp_path):
    ramones_id, _, _, _, _ = item_row(seeded_db, "rick owens ramones")
    pods_before = item_row(seeded_db, "pod shorts")
    pod_keywords_before = keyword_sets(seeded_db)["pod shorts"]
    data = base_catalog()
    data[0].update(
        {
            "market_price": 650,
            "search_query": "ramones high",
            "keywords": ["ramones", "geobasket"],
            "exclude_keywords": ["canvas"],
        }
    )

    db.seed_from_catalog(write_catalog(tmp_path, data))

    row = item_row(seeded_db, "rick owens ramones")
    assert row[0] == ramones_id
    assert row[2] == 650.0
    assert row[3] == "ramones high"
    assert keyword_sets(seeded_db)["rick owens ramones"] == {
        "include": {"ramones", "geobasket"},
        "exclude": {"canvas"},
    }
    assert item_row(seeded_db, "pod shorts") == pods_before
    assert keyword_sets(seeded_db)["pod shorts"] == pod_keywords_before


def test_rc03a_c16_reseed_never_overwrites_the_ebay_price(seeded_db, tmp_path):
    item_id = db.get_item_id("Rick Owens", "rick owens ramones")
    db.update_market_price(item_id, 912.5)
    data = base_catalog()
    data[0]["market_price"] = 650

    db.seed_from_catalog(write_catalog(tmp_path, data))

    row = item_row(seeded_db, "rick owens ramones")
    assert row[1] == 912.5
    assert row[2] == 650.0
    assert db.get_market_price("Rick Owens", "rick owens ramones") == 912.5


def _insert_match_for(path, item_id):
    brand_id = rows(path, "SELECT id FROM brands")[0][0]
    assert db.insert_listing(brand_id, "https://example.test/m1", "t", 1000, None) is True
    listing_id = rows(path, "SELECT id FROM listings")[0][0]
    run(
        path,
        "INSERT INTO listing_matches (listing_id, item_id, matched_at) VALUES (?, ?, 'x')",
        (listing_id, item_id),
    )


def test_rc03a_c17_item_removed_from_catalog_is_deactivated_not_deleted(
    seeded_db, tmp_path, capsys
):
    pods_id = db.get_item_id(*PODS)
    db.update_market_price(pods_id, 280.0)
    _insert_match_for(seeded_db, pods_id)
    before_item = rows(seeded_db, ITEM_COLUMNS_SQL, (pods_id,))[0]
    before_keywords = rows(
        seeded_db, "SELECT * FROM item_keywords WHERE item_id = ? ORDER BY keyword", (pods_id,)
    )
    before_matches = dump_table(seeded_db, "listing_matches")
    assert before_matches and before_keywords
    data = [base_catalog()[0]]
    capsys.readouterr()

    result = db.seed_from_catalog(write_catalog(tmp_path, data))

    after_item = rows(seeded_db, ITEM_COLUMNS_SQL, (pods_id,))[0]
    assert after_item[-1] == 0  # the last selected column is active
    assert after_item[:-1] == before_item[:-1]
    assert rows(
        seeded_db, "SELECT * FROM item_keywords WHERE item_id = ? ORDER BY keyword", (pods_id,)
    ) == before_keywords
    assert dump_table(seeded_db, "listing_matches") == before_matches
    assert result.deactivated == (PODS,)
    assert (
        "Deactivated item no longer in catalog.json: Rick Owens / pod shorts"
        in capsys.readouterr().out
    )


def test_rc03a_c18_readding_an_item_reactivates_the_same_row(seeded_db, tmp_path):
    pods_id = db.get_item_id(*PODS)
    db.update_market_price(pods_id, 280.0)
    db.seed_from_catalog(write_catalog(tmp_path, [base_catalog()[0]], "only_ramones.json"))
    assert item_row(seeded_db, "pod shorts")[4] == 0

    result = db.seed_from_catalog(write_catalog(tmp_path, base_catalog(), "full.json"))

    row = item_row(seeded_db, "pod shorts")
    assert row[0] == pods_id
    assert row[4] == 1
    assert row[1] == 280.0
    assert PODS in result.updated
    assert PODS not in result.added


# --- Normalization and optional fields (19-21) -------------------------------


def test_rc03a_c19_keywords_are_stripped_lowercased_and_deduplicated(temp_db, tmp_path):
    data = base_catalog()[:1]
    data[0]["keywords"] = ["  RAMONES ", "ramones", "Ramon"]

    db.seed_from_catalog(write_catalog(tmp_path, data))

    assert keyword_sets(temp_db)["rick owens ramones"]["include"] == {"ramones", "ramon"}
    assert len(rows(temp_db, "SELECT * FROM item_keywords WHERE kind = 'include'")) == 2


@pytest.mark.parametrize("mode", ["missing", "null"])
def test_rc03a_c20_optional_fields_may_be_missing_or_null(temp_db, tmp_path, mode):
    entry = {"brand": "Rick Owens", "item name": "bare item", "keywords": ["bare"]}
    if mode == "null":
        entry.update({"market_price": None, "search_query": None})

    db.seed_from_catalog(write_catalog(tmp_path, [entry]))

    assert rows(temp_db, "SELECT manual_price_usd, search_query FROM items") == [(None, None)]
    assert rows(temp_db, "SELECT kind, keyword FROM item_keywords") == [("include", "bare")]


def test_rc03a_c21_size_is_accepted_and_not_stored(seeded_db):
    # the fixture catalog carries "size": 42 on both entries
    assert rows(seeded_db, "SELECT COUNT(*) FROM items WHERE manual_price_usd IS NOT NULL") == [(2,)]
    for table, table_rows in dump_all(seeded_db).items():
        for row in table_rows:
            assert 42 not in row and "42" not in row, (table, row)


# --- Validation (22-25, 28-29) -----------------------------------------------


def _with(index, **changes):
    data = base_catalog()
    data[index].update(changes)
    return data


def _without(index, key):
    data = base_catalog()
    del data[index][key]
    return data


INVALID_CATALOGS = {
    "top_level_object": {},
    "empty_list": [],
    "entry_is_string": ["rick owens ramones"],
    "missing_brand": _without(0, "brand"),
    "missing_item_name": _without(0, "item name"),
    "missing_keywords": _without(0, "keywords"),
    "blank_brand": _with(0, brand="   "),
    "empty_keywords": _with(0, keywords=[]),
    "keywords_is_string": _with(0, keywords="ramones"),
    "empty_keyword": _with(0, keywords=[""]),
    "int_keyword": _with(0, keywords=[5]),
    "exclude_keywords_is_string": _with(0, exclude_keywords="low"),
    "price_zero": _with(0, market_price=0),
    "price_negative": _with(0, market_price=-5),
    "price_bool": _with(0, market_price=True),
    "price_string": _with(0, market_price="800"),
    "price_nan": _with(0, market_price=float("nan")),
    "price_infinity": _with(0, market_price=float("inf")),
    "query_int": _with(0, search_query=5),
    "query_blank": _with(0, search_query="  "),
    "query_101_chars": _with(0, search_query="a" * 101),
    "query_plus": _with(0, search_query="rick+owens"),
    "query_star": _with(0, search_query="rick*"),
    "unknown_key": _with(0, exclude_keyword=["low"]),
    "duplicate_entries": base_catalog() + [dict(base_catalog()[1], **{"item name": " pod shorts "})],
    "keyword_in_both_lists": _with(0, keywords=["Low"], exclude_keywords=["low"]),
}


@pytest.mark.parametrize("name", list(INVALID_CATALOGS))
def test_rc03a_c22_invalid_catalog_raises_and_changes_nothing(seeded_db, tmp_path, name):
    before = dump_all(seeded_db)
    # json.dumps writes NaN and Infinity literally, as json.load accepts them
    path = write_catalog(tmp_path, INVALID_CATALOGS[name])

    with pytest.raises(db.CatalogError):
        db.seed_from_catalog(path)

    assert dump_all(seeded_db) == before


def test_rc03a_c22_search_query_of_exactly_100_chars_is_accepted(temp_db, tmp_path):
    data = base_catalog()[:1]
    data[0]["search_query"] = "a" * 100

    db.seed_from_catalog(write_catalog(tmp_path, data))

    assert rows(temp_db, "SELECT search_query, manual_price_usd FROM items") == [("a" * 100, 800.0)]


def test_rc03a_c23_all_problems_are_reported_in_one_error(seeded_db, tmp_path):
    data = base_catalog()
    data[0]["market_price"] = 0
    data[1]["colour"] = "red"

    with pytest.raises(db.CatalogError) as exc:
        db.seed_from_catalog(write_catalog(tmp_path, data))

    message = str(exc.value)
    for needle in ("Catalog is invalid:", "entry 1", "market_price", "entry 2", "colour"):
        assert needle in message


def test_rc03a_c24_valid_entry_before_invalid_entry_writes_nothing(seeded_db, tmp_path):
    before = dump_all(seeded_db)
    data = base_catalog()
    data[0]["market_price"] = 650
    data[1]["keywords"] = []

    with pytest.raises(db.CatalogError):
        db.seed_from_catalog(write_catalog(tmp_path, data))

    assert dump_all(seeded_db) == before


def test_rc03a_c25_second_brand_is_rejected_and_rolled_back(seeded_db, tmp_path):
    before = dump_all(seeded_db)
    data = base_catalog()
    data[0]["market_price"] = 650
    data.append({"brand": "Other Brand", "item name": "thing", "keywords": ["thing"]})

    with pytest.raises(db.CatalogError) as exc:
        db.seed_from_catalog(write_catalog(tmp_path, data))

    assert "Other Brand" in str(exc.value)
    assert "RC-13" in str(exc.value)
    assert dump_all(seeded_db) == before


def test_rc03a_c28_real_catalog_is_valid_and_pod_shorts_is_fixed():
    parsed = json.loads((REPO_ROOT / "src" / "catalog.json").read_text(encoding="utf-8"))

    entries = db.validate_catalog(parsed)

    pods = [e for e in entries if e.name == "pod shorts"][0]
    for stale in ("ramones", "ramon", "high top"):
        assert stale not in pods.keywords
    for entry in entries:
        assert "+" not in (entry.search_query or "")


def test_rc03a_c28_real_catalog_has_the_resolved_values():
    parsed = json.loads((REPO_ROOT / "src" / "catalog.json").read_text(encoding="utf-8"))

    by_name = {e.name: e for e in db.validate_catalog(parsed)}

    assert by_name["pod shorts"].keywords == ("pod short", "ポッドショーツ", "pod shorts")
    assert by_name["pod shorts"].exclude_keywords == ("pants",)
    assert by_name["pod shorts"].search_query == "rick owens pod shorts"
    assert by_name["rick owens ramones"].search_query == "rick owens ramones"


def test_rc17_c15_seeded_database_has_three_pod_includes_and_existing_exclude(
    temp_db, monkeypatch
):
    monkeypatch.setattr(db, "CATALOG_PATH", REPO_ROOT / "src" / "catalog.json")
    db.seed_from_catalog()

    assert keyword_sets(temp_db)["pod shorts"] == {
        "include": {"pod short", "ポッドショーツ", "pod shorts"},
        "exclude": {"pants"},
    }


def test_rc03a_c29_validate_catalog_returns_normalized_entries():
    entries = db.validate_catalog(base_catalog())

    assert entries == [
        db.CatalogEntry(
            brand="Rick Owens",
            name="rick owens ramones",
            search_query="rick owens ramones",
            manual_price_usd=800.0,
            keywords=("ramones", "ramon"),
            exclude_keywords=("drkshdw", "canvas", "low"),
        ),
        db.CatalogEntry(
            brand="Rick Owens",
            name="pod shorts",
            search_query="pod shorts",
            manual_price_usd=300.0,
            keywords=("pod short",),
            exclude_keywords=("pants",),
        ),
    ]


# --- Legacy databases (26-27) ------------------------------------------------


def test_rc03a_c26_seeding_a_legacy_database_is_refused(legacy_db):
    before = dump_all(legacy_db)

    with pytest.raises(db.SchemaVersionError):
        db.seed_from_catalog(FIXTURE_CATALOG)

    assert user_version(legacy_db) == 0
    assert dump_all(legacy_db) == before


def test_rc03a_c27_seeding_a_migrated_legacy_database_keeps_ids_and_prices(legacy_db):
    legacy_before = dump_all(legacy_db)
    db.init_db()
    assert_legacy_data_preserved(legacy_db, legacy_before)

    db.seed_from_catalog(FIXTURE_CATALOG)

    assert rows(
        legacy_db,
        "SELECT id, name, market_price_usd, manual_price_usd FROM items ORDER BY id",
    ) == [
        (1, "rick owens ramones", 420.0, 800.0),
        (2, "pod shorts", 300.0, 300.0),
    ]
    assert keyword_sets(legacy_db) == {
        "rick owens ramones": {
            "include": {"ramones", "ramon"},
            "exclude": {"drkshdw", "canvas", "low"},
        },
        "pod shorts": {"include": {"pod short"}, "exclude": {"pants"}},
    }
    assert "high top" not in {r[0] for r in rows(legacy_db, "SELECT keyword FROM item_keywords")}
    assert dump_table(legacy_db, "listings") == legacy_before["listings"]
    assert dump_table(legacy_db, "listing_matches") == legacy_before["listing_matches"]
