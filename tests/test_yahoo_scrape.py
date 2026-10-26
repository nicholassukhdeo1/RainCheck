import json

import pytest

import yahoo_scrape
from yahoo_scrape import RICK_OWENS_URL, yahoo_jp_scrape

URL_1 = "https://auctions.yahoo.co.jp/jp/auction/t1000000001"
URL_4 = "https://auctions.yahoo.co.jp/jp/auction/t1000000004"


@pytest.fixture
def yahoo_http(fake_http, fixtures_dir):
    html = (fixtures_dir / "yahoo" / "brand_page_minimal.html").read_text(encoding="utf-8")
    fake_http.add("GET", RICK_OWENS_URL, text=html)
    return fake_http


@pytest.fixture
def ramones_entry(fixtures_dir):
    return json.loads((fixtures_dir / "catalog.json").read_text())[0]


def _listings(conn):
    return conn.execute("SELECT url, price_yen FROM listings ORDER BY url").fetchall()


def test_c45_inserts_only_the_two_matching_cheap_listings(
    yahoo_http, seeded_db, sent_emails, db_conn, ramones_entry
):
    yahoo_jp_scrape(ramones_entry, RICK_OWENS_URL)
    assert [(r["url"], r["price_yen"]) for r in _listings(db_conn)] == [
        (URL_1, 50000),
        (URL_4, 100000),
    ]


def test_c46_one_email_sorted_by_discount_with_percentages(
    yahoo_http, seeded_db, sent_emails, ramones_entry
):
    yahoo_jp_scrape(ramones_entry, RICK_OWENS_URL)
    assert len(sent_emails) == 1
    subject, body = sent_emails[0]
    assert subject == "Daily Refresh on rick owens ramones"
    assert URL_1 in body and URL_4 in body
    assert body.index(URL_1) < body.index(URL_4)
    assert "61.3% cheaper than market" in body
    assert "22.5% cheaper than market" in body


def test_c47_second_run_inserts_no_new_rows(
    yahoo_http, seeded_db, sent_emails, db_conn, ramones_entry
):
    yahoo_jp_scrape(ramones_entry, RICK_OWENS_URL)
    first = _listings(db_conn)
    yahoo_jp_scrape(ramones_entry, RICK_OWENS_URL)
    assert _listings(db_conn) == first
    assert len(first) == 2


def test_c14_main_works_from_unrelated_cwd_and_emails_once_per_item(
    yahoo_http, seeded_db, sent_emails, monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    yahoo_scrape.main()
    assert [s for s, _ in sent_emails] == [
        "Daily Refresh on rick owens ramones",
        "Daily Refresh on pod shorts",
    ]
