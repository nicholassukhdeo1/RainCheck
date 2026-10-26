"""Yahoo pipeline tests.

RC-07 rewrote the RC-01 criteria 14, 45, 46, 47 against parse_listings /
report_item_deals (they keep their RC-01 names) and added criteria 20-29.
"""
import dataclasses

import pytest
import requests

import db
import yahoo_scrape
from dbhelpers import run as sql_run, set_user_version, sha256
from yahoo_scrape import RICK_OWENS_URL

URL_1 = "https://auctions.yahoo.co.jp/jp/auction/t1000000001"
URL_4 = "https://auctions.yahoo.co.jp/jp/auction/t1000000004"
X1 = "https://auctions.yahoo.co.jp/jp/auction/x1000000001"
X3 = "https://auctions.yahoo.co.jp/jp/auction/x1000000003"
Z2 = "https://paypayfleamarket.yahoo.co.jp/item/z1000000002"


def _html(fixtures_dir, name):
    return (fixtures_dir / "yahoo" / name).read_text(encoding="utf-8")


@pytest.fixture
def minimal_listings(fixtures_dir):
    return yahoo_scrape.parse_listings(_html(fixtures_dir, "brand_page_minimal.html"))


@pytest.fixture
def malformed_listings(fixtures_dir):
    return yahoo_scrape.parse_listings(_html(fixtures_dir, "brand_page_malformed.html"))


@pytest.fixture
def yahoo_http(fake_http, fixtures_dir):
    fake_http.add("GET", RICK_OWENS_URL, text=_html(fixtures_dir, "brand_page_minimal.html"))
    return fake_http


def _item(name):
    return [i for i in db.get_tracked_items() if i.name == name][0]


@pytest.fixture
def ramones_item(seeded_db):
    return _item("rick owens ramones")


def _rows(conn):
    return conn.execute("SELECT url, price_yen, image_url FROM listings ORDER BY url").fetchall()


# ---- Pipeline (criteria 20-25) -----------------------------------------------------


def test_c45_inserts_only_the_two_matching_cheap_listings(
    seeded_db, sent_emails, db_conn, ramones_item, minimal_listings
):
    yahoo_scrape.report_item_deals(ramones_item, minimal_listings)
    assert [(r["url"], r["price_yen"]) for r in _rows(db_conn)] == [(URL_1, 50000), (URL_4, 100000)]


def test_c46_one_email_sorted_by_discount_with_percentages(
    seeded_db, sent_emails, ramones_item, minimal_listings
):
    yahoo_scrape.report_item_deals(ramones_item, minimal_listings)
    assert len(sent_emails) == 1
    subject, body = sent_emails[0]
    assert subject == "Daily Refresh on rick owens ramones"
    assert URL_1 in body and URL_4 in body
    assert body.index(URL_1) < body.index(URL_4)
    assert "61.3% cheaper than market" in body
    assert "22.5% cheaper than market" in body


def test_c47_second_run_inserts_no_new_rows(
    seeded_db, sent_emails, db_conn, ramones_item, minimal_listings
):
    yahoo_scrape.report_item_deals(ramones_item, minimal_listings)
    first = _rows(db_conn)
    yahoo_scrape.report_item_deals(ramones_item, minimal_listings)
    assert _rows(db_conn) == first
    assert len(first) == 2


def test_rc07_c24_malformed_fixture_pipeline_rows_and_email(
    seeded_db, sent_emails, db_conn, ramones_item, malformed_listings
):
    yahoo_scrape.report_item_deals(ramones_item, malformed_listings)
    rows = {r["url"]: r for r in _rows(db_conn)}
    assert set(rows) == {X1, Z2, X3}
    assert rows[Z2]["price_yen"] == 40000
    assert rows[X3]["image_url"] is None
    assert len(sent_emails) == 1
    body = sent_emails[0][1]
    assert body.index(X3) < body.index(X1) < body.index(Z2)
    assert "84.5% cheaper than market" in body
    # 76.75 is exact in binary, and format() rounds the tie to even, so this is always 76.8
    assert "76.8% cheaper than market" in body
    assert "69.0% cheaper than market" in body
    assert body.count("Image: ") == 2
    assert "Image: None" not in body


def test_rc07_c25_null_market_price_skips_item(
    seeded_db, sent_emails, db_conn, minimal_listings, capsys
):
    db_conn.execute(
        "UPDATE items SET market_price_usd = NULL, manual_price_usd = NULL WHERE name = ?",
        ("rick owens ramones",),
    )
    db_conn.commit()
    item = _item("rick owens ramones")
    yahoo_scrape.report_item_deals(item, minimal_listings)
    assert _rows(db_conn) == []
    assert sent_emails == []
    assert "Skipping rick owens ramones: no usable market price." in capsys.readouterr().out


# ---- RC-03b item-based report_item_deals (criteria 9-12) ---------------------------


def test_rc03b_c9_exclude_keywords_from_the_item_apply(
    seeded_db, sent_emails, db_conn, ramones_item, minimal_listings
):
    item = dataclasses.replace(ramones_item, exclude_keywords=("high",))
    yahoo_scrape.report_item_deals(item, minimal_listings)
    assert [r["url"] for r in _rows(db_conn)] == [URL_4]
    assert len(sent_emails) == 1
    body = sent_emails[0][1]
    assert URL_4 in body and URL_1 not in body


def test_rc03b_c10_brand_comes_from_the_item_not_a_name_lookup(
    seeded_db, sent_emails, db_conn, ramones_item, minimal_listings
):
    item = dataclasses.replace(ramones_item, brand="Not In Database")
    yahoo_scrape.report_item_deals(item, minimal_listings)
    stored = db_conn.execute("SELECT url, brand_id FROM listings ORDER BY url").fetchall()
    assert [r["url"] for r in stored] == [URL_1, URL_4]
    assert {r["brand_id"] for r in stored} == {ramones_item.brand_id}


def test_rc03b_c11_effective_price_of_the_item_drives_the_deal_math(
    seeded_db, sent_emails, db_conn, ramones_item, minimal_listings
):
    db.update_market_price(ramones_item.id, 400.0)
    item = _item("rick owens ramones")
    assert item.effective_price_usd == 400.0
    yahoo_scrape.report_item_deals(item, minimal_listings)
    assert [r["url"] for r in _rows(db_conn)] == [URL_1]
    assert len(sent_emails) == 1
    assert "22.5% cheaper than market" in sent_emails[0][1]


def test_rc03b_c12_item_without_usable_price_is_skipped_with_a_line(
    seeded_db, sent_emails, db_conn, minimal_listings, capsys
):
    db_conn.execute(
        "UPDATE items SET market_price_usd = NULL, manual_price_usd = NULL WHERE name = ?",
        ("rick owens ramones",),
    )
    db_conn.commit()
    yahoo_scrape.report_item_deals(_item("rick owens ramones"), minimal_listings)
    assert _rows(db_conn) == []
    assert sent_emails == []
    assert "Skipping rick owens ramones: no usable market price." in capsys.readouterr().out


# ---- main() ------------------------------------------------------------------------


def test_c14_main_works_from_unrelated_cwd_and_emails_once_per_item(
    yahoo_http, test_catalog, seeded_db, sent_emails, monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    yahoo_scrape.main()
    assert [s for s, _ in sent_emails] == [
        "Daily Refresh on rick owens ramones",
        "Daily Refresh on pod shorts",
    ]
    assert len(yahoo_http.calls) == 1
    method, url, kwargs = yahoo_http.calls[0]
    assert (method, url) == ("GET", RICK_OWENS_URL)
    assert kwargs.get("timeout") == 30


# ---- Fetching and failure behavior (criteria 26-29) --------------------------------


def test_rc07_c26_rick_owens_url_is_canonical():
    assert RICK_OWENS_URL == (
        "https://auctions.yahoo.co.jp/category/list/23000"
        "?auccat=23000&s1=new&o1=d&brand_id=103538&n=100"
    )


def test_rc07_c27_fetch_returns_text_and_passes_timeout(fake_http):
    fake_http.add("GET", "https://example.test/page", text="<p>ok</p>")
    assert yahoo_scrape.fetch_yahoo_page("https://example.test/page") == "<p>ok</p>"
    assert len(fake_http.calls) == 1
    assert fake_http.calls[0][0:2] == ("GET", "https://example.test/page")
    assert fake_http.calls[0][2].get("timeout") == 30


def test_rc07_c27_fetch_decodes_japanese_as_utf8(fake_http):
    fake_http.add("GET", "https://example.test/jp", text="<p>テスト フリマ</p>")
    assert yahoo_scrape.fetch_yahoo_page("https://example.test/jp") == "<p>テスト フリマ</p>"


def test_rc07_c27_fetch_raises_on_http_error(fake_http):
    fake_http.add("GET", "https://example.test/err", text="oops", status=500)
    with pytest.raises(requests.HTTPError):
        yahoo_scrape.fetch_yahoo_page("https://example.test/err")


def test_rc07_c27_fetch_propagates_timeout(monkeypatch):
    def boom(url, **kwargs):
        raise requests.Timeout("slow")

    monkeypatch.setattr(requests, "get", boom)
    with pytest.raises(requests.Timeout):
        yahoo_scrape.fetch_yahoo_page("https://example.test/slow")


def test_rc07_c28_http_503_raises_with_no_email_and_no_rows(
    fake_http, test_catalog, seeded_db, sent_emails, db_conn
):
    fake_http.add("GET", RICK_OWENS_URL, text="Service Unavailable", status=503)
    with pytest.raises(requests.HTTPError):
        yahoo_scrape.main()
    assert sent_emails == []
    assert _rows(db_conn) == []


def test_rc07_c29_old_layout_page_raises_with_no_email_and_no_rows(
    fake_http, test_catalog, seeded_db, sent_emails, db_conn
):
    old = '<html><body><h3>Rick Owens Ramones</h3><span class="Product__priceValue u-textRed">1円</span></body></html>'
    fake_http.add("GET", RICK_OWENS_URL, text=old)
    with pytest.raises(yahoo_scrape.YahooPageError):
        yahoo_scrape.main()
    assert sent_emails == []
    assert _rows(db_conn) == []


# ---- RC-03b main() reads the database (criteria 13-21) ------------------------------

NO_ITEMS_LINE = "No active items in the database. Add items to catalog.json, then run python3 src/db.py --seed."


def test_rc03b_c13_main_reads_exclude_keywords_from_the_database(
    yahoo_http, seeded_db, sent_emails, db_conn
):
    item_id = _item("rick owens ramones").id
    sql_run(
        seeded_db,
        "INSERT INTO item_keywords (item_id, keyword, kind) VALUES (?, 'sneakers', 'exclude')",
        (item_id,),
    )
    yahoo_scrape.main()
    assert [r["url"] for r in _rows(db_conn)] == [URL_1]


def test_rc03b_c14_main_skips_inactive_items(yahoo_http, seeded_db, sent_emails):
    sql_run(seeded_db, "UPDATE items SET active = 0 WHERE name = ?", ("pod shorts",))
    yahoo_scrape.main()
    assert [s for s, _ in sent_emails] == ["Daily Refresh on rick owens ramones"]
    assert len(yahoo_http.calls) == 1


def test_rc03b_c15_main_does_not_read_catalog_json(
    yahoo_http, seeded_db, sent_emails, monkeypatch, tmp_path
):
    monkeypatch.setattr(db, "CATALOG_PATH", tmp_path / "does_not_exist.json")
    yahoo_scrape.main()
    assert [s for s, _ in sent_emails] == [
        "Daily Refresh on rick owens ramones",
        "Daily Refresh on pod shorts",
    ]
    assert not hasattr(yahoo_scrape, "load_catalog")


def _assert_clean_schema_error(capsys, fake_http, sent_emails, exc, needle="python3 src/db.py"):
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert len(err.strip().splitlines()) == 1
    assert err.startswith("Error: ")
    assert needle in err
    assert "Traceback" not in err
    assert fake_http.calls == []
    assert sent_emails == []


def test_rc03b_c16_main_on_legacy_db_is_a_clean_error_before_any_network(
    legacy_db, fake_http, sent_emails, capsys
):
    before = sha256(legacy_db)
    with pytest.raises(SystemExit) as exc:
        yahoo_scrape.main()
    _assert_clean_schema_error(capsys, fake_http, sent_emails, exc)
    assert sha256(legacy_db) == before


def test_rc03b_c17_main_with_no_database_file_is_a_clean_error_and_creates_nothing(
    fake_http, sent_emails, capsys, tmp_path
):
    path = tmp_path / "test.db"
    assert not path.exists()
    with pytest.raises(SystemExit) as exc:
        yahoo_scrape.main()
    _assert_clean_schema_error(capsys, fake_http, sent_emails, exc, "python3 src/db.py --seed")
    assert not path.exists()


def test_rc03b_c18_main_with_no_items_prints_one_line_and_returns(
    temp_db, fake_http, sent_emails, capsys
):
    yahoo_scrape.main()
    assert NO_ITEMS_LINE in capsys.readouterr().out
    assert fake_http.calls == []
    assert sent_emails == []


def test_rc03b_c19_main_on_newer_schema_version_is_a_clean_error(
    temp_db, fake_http, sent_emails, capsys
):
    set_user_version(temp_db, 2)
    before = sha256(temp_db)
    with pytest.raises(SystemExit) as exc:
        yahoo_scrape.main()
    _assert_clean_schema_error(capsys, fake_http, sent_emails, exc)
    assert sha256(temp_db) == before


def test_rc03b_c21_yahoo_job_uses_the_shared_matcher():
    import matching

    assert yahoo_scrape.title_matches is matching.title_matches
