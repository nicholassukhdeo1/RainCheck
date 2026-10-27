"""RC-13 approved behavior for per-brand Yahoo searches and staged seeding."""
import json
import shutil
import sqlite3
from pathlib import Path

import pytest
import requests

import db
import yahoo_scrape
from dbhelpers import dump_all, rows, run, user_version
from yahoo_scrape import RICK_OWENS_URL

FIXTURE_CATALOG = Path(__file__).parent / "fixtures" / "catalog.json"
RATE_URL = yahoo_scrape.FRANKFURTER_RATE_URL
SECOND_BRAND_ID = "123456"
SECOND_BRAND_URL = (
    "https://auctions.yahoo.co.jp/category/list/23000?auccat=23000&s1=new&o1=d&"
    f"brand_id={SECOND_BRAND_ID}&n=100"
)


def catalog():
    return json.loads(FIXTURE_CATALOG.read_text(encoding="utf-8"))


def write_catalog(tmp_path, entries, name="catalog.json"):
    path = tmp_path / name
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


def two_brand_catalog():
    entries = catalog()
    entries.append({
        "brand": "Brand B", "yahoo_brand_id": SECOND_BRAND_ID,
        "item name": "rick owens ramones", "search_query": "brand b ramones",
        "market_price": 800.0, "keywords": ["ramones"],
    })
    return entries


def page(*records):
    data = {
        "props": {"pageProps": {"initialState": {"search": {"items": {
            "listing": {"items": list(records), "totalResultsAvailable": len(records)}
        }}}}},
        "page": "/category/list/[categoryId]",
    }
    return '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(data) + "</script>"


def listing(auction_id, title, price=10000):
    return {
        "auctionId": auction_id, "title": title, "price": price,
        "isFixedPrice": True, "buyNowPrice": price, "isFleamarketItem": False,
    }


def add_rate(fake_http):
    fake_http.add("GET", RATE_URL, json={
        "date": "2026-10-27", "base": "JPY", "quote": "USD", "rate": 0.0062,
    })


def prepare_database(path, include_brand_b=False):
    """Set up predictable rows directly so scraper tests isolate the job pipeline."""
    conn = sqlite3.connect(path)
    try:
        conn.execute("INSERT INTO brands (id, name, yahoo_brand_id) VALUES (1, 'Rick Owens', '103538')")
        conn.execute(
            "INSERT INTO items (id, brand_id, name, manual_price_usd, search_query, active) "
            "VALUES (1, 1, 'rick owens ramones', 800, 'rick owens ramones', 1)"
        )
        conn.execute(
            "INSERT INTO items (id, brand_id, name, manual_price_usd, search_query, active) "
            "VALUES (2, 1, 'pod shorts', 300, 'pod shorts', 1)"
        )
        conn.executemany(
            "INSERT INTO item_keywords (item_id, keyword, kind) VALUES (?, ?, ?)",
            [(1, "ramones", "include"), (1, "ramon", "include"),
             (1, "drkshdw", "exclude"), (1, "canvas", "exclude"),
             (1, "low", "exclude"), (1, "fake", "exclude"),
             (2, "pod short", "include"), (2, "pod shorts", "include"),
             (2, "pants", "exclude"), (2, "fake", "exclude")],
        )
        if include_brand_b:
            conn.execute("INSERT INTO brands (id, name, yahoo_brand_id) VALUES (2, 'Brand B', ?)", (SECOND_BRAND_ID,))
            conn.execute(
                "INSERT INTO items (id, brand_id, name, manual_price_usd, search_query, active) "
                "VALUES (3, 2, 'rick owens ramones', 800, 'brand b ramones', 1)"
            )
            conn.execute(
                "INSERT INTO item_keywords (item_id, keyword, kind) VALUES (3, 'ramones', 'include')"
            )
        conn.commit()
    finally:
        conn.close()


def test_rc13_c1_fresh_schema_keeps_version_three_and_unique_yahoo_brand_id(temp_db):
    assert db.SCHEMA_VERSION == 3
    assert user_version(temp_db) == 3
    indexes = rows(temp_db, "PRAGMA index_list(brands)")
    unique_columns = [
        rows(temp_db, f"PRAGMA index_info('{index[1]}')")
        for index in indexes if index[2]
    ]
    assert any([column[2] for column in columns] == ["yahoo_brand_id"] for columns in unique_columns)
    assert 4 not in db.MIGRATIONS


def test_rc13_c2_real_and_fixture_catalogs_declare_rick_owens_id_in_order():
    real_entries = json.loads((Path(db.SRC_DIR) / "catalog.json").read_text(encoding="utf-8"))
    fixture_entries = json.loads(FIXTURE_CATALOG.read_text(encoding="utf-8"))
    for entries in (real_entries, fixture_entries):
        assert [entry.get("yahoo_brand_id") for entry in entries] == ["103538", "103538"]

    real_without_ids = [
        {key: value for key, value in entry.items() if key != "yahoo_brand_id"}
        for entry in real_entries
    ]
    fixture_without_ids = [
        {key: value for key, value in entry.items() if key != "yahoo_brand_id"}
        for entry in fixture_entries
    ]
    assert real_without_ids == [
        {
            "item name": "rick owens ramones", "brand": "Rick Owens", "size": 42,
            "search_query": "rick owens ramones", "market_price": 800.0,
            "exclude_keywords": ["drkshdw", "canvas", "low", "ローカット", "fake"],
            "keywords": ["ramones", "ramon", "ラモーンズ"],
        },
        {
            "item name": "pod shorts", "brand": "Rick Owens", "size": 42,
            "search_query": "rick owens pod shorts", "market_price": 300.0,
            "exclude_keywords": ["pants", "fake"],
            "keywords": ["pod short", "ポッドショーツ", "pod shorts"],
        },
    ]
    assert fixture_without_ids == [
        {
            "item name": "rick owens ramones", "brand": "Rick Owens", "size": 42,
            "search_query": "rick owens ramones", "market_price": 800.0,
            "exclude_keywords": ["drkshdw", "canvas", "low", "fake"],
            "keywords": ["ramones", "ramon"],
        },
        {
            "item name": "pod shorts", "brand": "Rick Owens", "size": 42,
            "search_query": "pod shorts", "market_price": 300.0,
            "exclude_keywords": ["pants", "fake"],
            "keywords": ["pod short", "pod shorts"],
        },
    ]


@pytest.mark.parametrize("bad", [
    pytest.param("missing", id="missing"), pytest.param(None, id="null"),
    pytest.param("   ", id="blank"), pytest.param(103538, id="integer"),
    pytest.param(True, id="boolean"), pytest.param("１０３５３８", id="non-ascii-digits"),
    pytest.param("+103538", id="sign"), pytest.param("10 3538", id="embedded-whitespace"),
    pytest.param("103538&foo=bar", id="query-injection"),
])
def test_rc13_c3_catalog_yahoo_id_validation_identifies_entry_and_field(bad):
    entry = catalog()[0]
    if bad == "missing":
        entry.pop("yahoo_brand_id")
    else:
        entry["yahoo_brand_id"] = bad
    with pytest.raises(db.CatalogError) as exc:
        db.validate_catalog([entry])
    message = str(exc.value)
    assert "entry 1" in message
    assert "yahoo_brand_id" in message
    assert "unknown key" not in message


def test_rc13_c3_catalog_yahoo_id_validation_trims_whitespace():
    entries = catalog()[:1]
    entries[0]["yahoo_brand_id"] = " 103538 "
    assert db.validate_catalog(entries)[0].yahoo_brand_id == "103538"


@pytest.mark.parametrize("entries", [
    pytest.param(
        [dict(catalog()[0], yahoo_brand_id="103538"), dict(catalog()[1], yahoo_brand_id="999999")],
        id="same-brand-two-ids",
    ),
    pytest.param(
        [dict(catalog()[0], brand="Brand A", yahoo_brand_id="123456"),
         dict(catalog()[1], brand="Brand B", yahoo_brand_id="123456")],
        id="two-brands-same-id",
    ),
])
def test_rc13_c4_conflicting_brand_mapping_is_aggregated_before_database_open(entries, monkeypatch, tmp_path):
    def forbidden_connection():
        raise AssertionError("validation must happen before opening a database")
    monkeypatch.setattr(db, "get_connection", forbidden_connection)
    with pytest.raises(db.CatalogError) as exc:
        db.seed_from_catalog(write_catalog(tmp_path, entries, "rc13_conflict.json"))
    assert "unknown key" not in str(exc.value)


def test_rc13_c5_seed_and_reseed_two_brands_is_idempotent_and_preserves_prices(
    temp_db, tmp_path
):
    prepare_database(temp_db)
    data = two_brand_catalog()
    path = write_catalog(tmp_path, data)
    db.seed_from_catalog(path)
    item_id = db.get_item_id("Rick Owens", "rick owens ramones")
    db.update_market_price(item_id, 901.25)
    before_ids = rows(temp_db, "SELECT id, brand_id, name FROM items ORDER BY id")

    db.seed_from_catalog(path)

    assert rows(temp_db, "SELECT name, yahoo_brand_id FROM brands ORDER BY id") == [
        ("Rick Owens", "103538"), ("Brand B", SECOND_BRAND_ID)
    ]
    assert rows(temp_db, "SELECT id, brand_id, name FROM items ORDER BY id") == before_ids
    assert db.get_market_price("Rick Owens", "rick owens ramones") == 901.25
    assert rows(temp_db, "SELECT COUNT(*) FROM item_keywords") == [(11,)]


def test_rc13_c6_invalid_existing_brand_id_change_rolls_back_new_brand_and_all_rows(
    temp_db, tmp_path
):
    prepare_database(temp_db)
    before = dump_all(temp_db)
    data = [{
        "brand": "Brand B", "yahoo_brand_id": SECOND_BRAND_ID,
        "item name": "brand b thing", "keywords": ["thing"],
    }] + catalog()
    for entry in data[1:]:
        entry["yahoo_brand_id"] = "999999"

    with pytest.raises(db.CatalogError) as exc:
        db.seed_from_catalog(write_catalog(tmp_path, data))
    assert "yahoo_brand_id" in str(exc.value)
    assert "unknown key" not in str(exc.value)
    assert dump_all(temp_db) == before


def test_rc13_c7_deactivated_brand_keeps_brand_and_listing_history_but_is_not_active(
    temp_db, tmp_path
):
    prepare_database(temp_db, include_brand_b=True)
    brand_id = rows(temp_db, "SELECT id FROM brands WHERE name='Brand B'")[0][0]
    db.insert_listing(brand_id, "https://example.test/old", "old listing", 1000, None)
    db.seed_from_catalog(write_catalog(tmp_path, catalog()))
    assert rows(temp_db, "SELECT name FROM brands ORDER BY id") == [("Rick Owens",), ("Brand B",)]
    assert rows(temp_db, "SELECT url FROM listings") == [("https://example.test/old",)]
    assert rows(temp_db, "SELECT active FROM items WHERE id=3") == [(0,)]
    assert [brand.name for brand in db.get_active_brands()] == ["Rick Owens"]


def test_rc13_c8_active_brands_are_unique_ordered_by_first_active_item_and_can_be_empty(
    temp_db, tmp_path
):
    data = two_brand_catalog()
    data.append({
        "brand": "Brand C", "yahoo_brand_id": "456789",
        "item name": "brand c thing", "keywords": ["thing"],
    })
    db.seed_from_catalog(write_catalog(tmp_path, data))
    expected = [brand.name for brand in db.get_active_brands()]
    assert expected == ["Rick Owens", "Brand B", "Brand C"]
    run(temp_db, "UPDATE items SET active=0 WHERE brand_id=(SELECT id FROM brands WHERE name='Brand B')")
    active = db.get_active_brands()
    assert [brand.name for brand in active] == ["Rick Owens", "Brand C"]
    assert len({brand.id for brand in active}) == len(active)
    run(temp_db, "UPDATE items SET active=0 WHERE active=1")
    assert db.get_active_brands() == []


def test_rc13_c8_active_brand_reader_closes_connection_on_success_and_failure(
    temp_db, monkeypatch
):
    prepare_database(temp_db)
    real_get_connection = db.get_connection
    opened = []

    class TrackedConnection:
        def __init__(self, fail=False):
            self.connection = real_get_connection()
            self.closed = False
            self.fail = fail

        def execute(self, *args, **kwargs):
            if self.fail:
                raise sqlite3.OperationalError("query failed")
            return self.connection.execute(*args, **kwargs)

        def close(self):
            self.closed = True
            self.connection.close()

    def tracked_connection():
        connection = TrackedConnection()
        opened.append(connection)
        return connection

    monkeypatch.setattr(db, "get_connection", tracked_connection)
    db.get_active_brands()
    assert opened[-1].closed is True

    def failing_connection():
        connection = TrackedConnection(fail=True)
        opened.append(connection)
        return connection

    monkeypatch.setattr(db, "get_connection", failing_connection)
    with pytest.raises(sqlite3.OperationalError):
        db.get_active_brands()
    assert opened[-1].closed is True


def test_rc13_c9_brand_url_matches_legacy_and_validates_numeric_id():
    assert yahoo_scrape.build_brand_url("103538") == RICK_OWENS_URL
    assert yahoo_scrape.build_brand_url(SECOND_BRAND_ID) == SECOND_BRAND_URL
    for invalid in ("12&x=3", "12/abc", "１２３", " 123"):
        with pytest.raises(ValueError):
            yahoo_scrape.build_brand_url(invalid)


def test_rc13_c10_two_brand_run_fetches_each_page_once_and_sends_one_ordered_digest(
    temp_db, fake_http, sent_emails
):
    prepare_database(temp_db, include_brand_b=True)
    fake_http.add("GET", RICK_OWENS_URL, text=page(listing("t7000000001", "Rick Owens Ramones", 10000)))
    fake_http.add("GET", SECOND_BRAND_URL, text=page(listing("t7000000002", "Brand B Ramones", 12000)))
    add_rate(fake_http)

    assert yahoo_scrape.main() == 0

    yahoo_calls = [call for call in fake_http.calls if call[1] in {RICK_OWENS_URL, SECOND_BRAND_URL}]
    assert [call[1] for call in yahoo_calls] == [RICK_OWENS_URL, SECOND_BRAND_URL]
    assert all(call[2]["timeout"] == 30 for call in yahoo_calls)
    assert len([call for call in fake_http.calls if call[1] == RATE_URL]) == 1
    assert len(sent_emails) == 1
    assert sent_emails[0][1].index("Rick Owens / rick owens ramones") < sent_emails[0][1].index(
        "Brand B / rick owens ramones"
    )


def test_rc13_c11_brand_page_listings_are_not_matched_against_another_brands_items(
    temp_db, fake_http, sent_emails
):
    prepare_database(temp_db, include_brand_b=True)
    run(temp_db, "DELETE FROM item_keywords WHERE item_id=3")
    run(temp_db, "INSERT INTO item_keywords (item_id, keyword, kind) VALUES (3, 'geobasket', 'include')")
    fake_http.add("GET", RICK_OWENS_URL, text=page())
    fake_http.add("GET", SECOND_BRAND_URL, text=page(listing("t7000000003", "Rick Owens Ramones", 10000)))
    add_rate(fake_http)

    yahoo_scrape.main()

    assert [call[1] for call in fake_http.calls if call[1] in {RICK_OWENS_URL, SECOND_BRAND_URL}] == [
        RICK_OWENS_URL, SECOND_BRAND_URL
    ]
    item_a = db.get_item_id("Rick Owens", "rick owens ramones")
    matches = rows(temp_db, "SELECT item_id FROM listing_matches")
    assert item_a not in [row[0] for row in matches]
    assert rows(temp_db, "SELECT url FROM listings") == []
    assert sent_emails == []


def test_rc13_c12_multi_brand_headings_are_disambiguated_and_single_brand_stays_compatible(
    temp_db, fake_http, sent_emails
):
    prepare_database(temp_db, include_brand_b=True)
    fake_http.add("GET", RICK_OWENS_URL, text=page(listing("t7000000004", "Rick Owens Ramones", 10000)))
    fake_http.add("GET", SECOND_BRAND_URL, text=page(listing("t7000000005", "Brand B Ramones", 10000)))
    add_rate(fake_http)
    yahoo_scrape.main()
    html = sent_emails[-1][1]
    assert "Rick Owens / rick owens ramones" in html
    assert "Brand B / rick owens ramones" in html

    sent_emails.clear()
    run(temp_db, "UPDATE items SET active=0 WHERE brand_id=(SELECT id FROM brands WHERE name='Brand B')")
    fake_http.calls.clear()
    fake_http.add("GET", RICK_OWENS_URL, text=page(listing("t7000000006", "Rick Owens Ramones", 10000)))
    yahoo_scrape.main()
    assert [call[1] for call in fake_http.calls if call[1].startswith("https://auctions.yahoo.co.jp/category/list/")] == [
        RICK_OWENS_URL
    ]
    assert "rick owens ramones" in sent_emails[-1][1]
    assert "Rick Owens / rick owens ramones" not in sent_emails[-1][1]


def test_rc13_c13_valid_empty_brand_page_does_not_block_other_brands_and_all_empty_skips_rate(
    temp_db, fake_http, sent_emails
):
    prepare_database(temp_db, include_brand_b=True)
    fake_http.add("GET", RICK_OWENS_URL, text=page())
    fake_http.add("GET", SECOND_BRAND_URL, text=page(listing("t7000000007", "Brand B Ramones", 10000)))
    add_rate(fake_http)
    yahoo_scrape.main()
    assert len(sent_emails) == 1

    sent_emails.clear()
    rate_calls_before = len([call for call in fake_http.calls if call[1] == RATE_URL])
    fake_http.add("GET", RICK_OWENS_URL, text=page())
    fake_http.add("GET", SECOND_BRAND_URL, text=page())
    yahoo_scrape.main()
    assert sent_emails == []
    assert len([call for call in fake_http.calls if call[1] == RATE_URL]) == rate_calls_before


@pytest.mark.parametrize("failure", ["http", "timeout", "malformed"])
def test_rc13_c14_any_brand_page_failure_prevents_email_and_all_database_writes(
    failure, temp_db, fake_http, sent_emails, monkeypatch
):
    prepare_database(temp_db, include_brand_b=True)
    fake_http.add("GET", RICK_OWENS_URL, text=page(listing("t7000000008", "Rick Owens Ramones", 10000)))
    add_rate(fake_http)
    if failure == "http":
        fake_http.add("GET", SECOND_BRAND_URL, status=500)
    elif failure == "malformed":
        fake_http.add("GET", SECOND_BRAND_URL, text="<html>not Yahoo results</html>")
    else:
        original = requests.get
        def timeout_second(url, **kwargs):
            if url == SECOND_BRAND_URL:
                raise requests.Timeout("offline timeout")
            return original(url, **kwargs)
        monkeypatch.setattr(requests, "get", timeout_second)
    before = dump_all(temp_db)

    with pytest.raises(SystemExit):
        yahoo_scrape.main()

    assert sent_emails == []
    assert dump_all(temp_db) == before


def test_rc13_c15_send_failure_records_no_listings_or_matches(temp_db, fake_http, monkeypatch):
    prepare_database(temp_db, include_brand_b=True)
    fake_http.add("GET", RICK_OWENS_URL, text=page(listing("t7000000009", "Rick Owens Ramones", 10000)))
    fake_http.add("GET", SECOND_BRAND_URL, text=page(listing("t7000000010", "Brand B Ramones", 10000)))
    add_rate(fake_http)
    shown = []
    monkeypatch.setattr(yahoo_scrape, "send_email", lambda subject, html, text: shown.append(html) or False)

    with pytest.raises(SystemExit):
        yahoo_scrape.main()

    assert len(shown) == 1
    assert "Rick Owens / rick owens ramones" in shown[0]
    assert "Brand B / rick owens ramones" in shown[0]
    assert rows(temp_db, "SELECT * FROM listings") == []
    assert rows(temp_db, "SELECT * FROM listing_matches") == []

    monkeypatch.setattr(yahoo_scrape, "send_email", lambda subject, html, text: True)
    yahoo_scrape.main()
    assert rows(temp_db, "SELECT url FROM listings ORDER BY url") == [
        ("https://auctions.yahoo.co.jp/jp/auction/t7000000009",),
        ("https://auctions.yahoo.co.jp/jp/auction/t7000000010",),
    ]


def test_rc13_c16_duplicate_listing_url_belongs_to_first_brand_only(temp_db, fake_http, sent_emails):
    prepare_database(temp_db, include_brand_b=True)
    duplicate = listing("t7000000011", "Rick Owens Ramones", 10000)
    fake_http.add("GET", RICK_OWENS_URL, text=page(duplicate))
    fake_http.add("GET", SECOND_BRAND_URL, text=page(dict(duplicate, title="Brand B Ramones", price=9000)))
    add_rate(fake_http)

    yahoo_scrape.main()

    assert [call[1] for call in fake_http.calls if call[1] in {RICK_OWENS_URL, SECOND_BRAND_URL}] == [
        RICK_OWENS_URL, SECOND_BRAND_URL
    ]
    url = "https://auctions.yahoo.co.jp/jp/auction/t7000000011"
    assert rows(temp_db, "SELECT url, brand_id FROM listings") == [
        (url, rows(temp_db, "SELECT id FROM brands WHERE name='Rick Owens'")[0][0])
    ]
    assert sum(url in html for _, html, _ in sent_emails) == 1


def test_rc13_c17_version_three_database_requires_no_seed_and_keeps_all_rows(temp_db):
    prepare_database(temp_db)
    before = dump_all(temp_db)
    assert user_version(temp_db) == 3
    db.init_db()
    assert user_version(temp_db) == 3
    assert dump_all(temp_db) == before


def test_rc13_c18_staged_seed_comparison_detects_database_only_settings_and_live_stays_unchanged(
    temp_db, tmp_path, monkeypatch
):
    prepare_database(temp_db)
    conn = sqlite3.connect(temp_db)
    conn.execute("INSERT INTO item_keywords (item_id, keyword, kind) VALUES (1, 'database-only', 'exclude')")
    conn.execute("INSERT INTO items (brand_id, name, manual_price_usd, active) VALUES (1, 'database-only item', 44, 1)")
    conn.execute("INSERT INTO item_keywords (item_id, keyword) VALUES (3, 'special')")
    conn.commit()
    conn.close()
    before = dump_all(temp_db)
    staged = tmp_path / "staged.db"
    shutil.copy2(temp_db, staged)
    monkeypatch.setenv("RAINCHECK_DB_PATH", str(staged))
    data = two_brand_catalog()

    db.seed_from_catalog(write_catalog(tmp_path, data))
    staged_after = dump_all(staged)

    assert staged_after != before
    assert staged_after["items"] != before["items"]  # database-only active item deactivated
    assert staged_after["item_keywords"] != before["item_keywords"]  # include/exclude replaced
    monkeypatch.setenv("RAINCHECK_DB_PATH", str(temp_db))
    assert dump_all(temp_db) == before


def test_rc13_c19_full_staged_configuration_preserves_old_rows_and_addition_is_idempotent(
    temp_db, tmp_path, monkeypatch
):
    prepare_database(temp_db)
    conn = sqlite3.connect(temp_db)
    conn.execute("INSERT INTO item_keywords (item_id, keyword, kind) VALUES (1, 'database-only', 'exclude')")
    conn.execute("INSERT INTO items (brand_id, name, manual_price_usd, active) VALUES (1, 'database-only item', 44, 1)")
    conn.execute("INSERT INTO item_keywords (item_id, keyword) VALUES (3, 'special')")
    conn.commit()
    conn.close()
    before = dump_all(temp_db)
    staged = tmp_path / "staged-full.db"
    shutil.copy2(temp_db, staged)
    monkeypatch.setenv("RAINCHECK_DB_PATH", str(staged))
    data = two_brand_catalog()
    data[0]["exclude_keywords"].append("database-only")
    data.append({
        "brand": "Rick Owens", "yahoo_brand_id": "103538",
        "item name": "database-only item", "market_price": 44,
        "keywords": ["special"],
    })
    path = write_catalog(tmp_path, data)

    db.seed_from_catalog(path)
    after_first = dump_all(staged)
    for table, old_rows in before.items():
        assert all(row in after_first[table] for row in old_rows), table
    assert rows(staged, "SELECT name FROM brands ORDER BY id") == [("Rick Owens",), ("Brand B",)]
    db.seed_from_catalog(path)
    assert dump_all(staged) == after_first
