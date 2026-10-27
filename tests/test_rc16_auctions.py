"""RC-16 auction behavior tests, kept separate from RC-10 regression coverage."""
import json
import sqlite3
from dataclasses import replace
from datetime import date, datetime

import pytest
from bs4 import BeautifulSoup

import db
import digest
import yahoo_scrape


MARKET = 800.0
RATE = 0.0062
RUN_DATE = date(2026, 10, 27)
BRAND_ID = 1
ITEM_ID = 1


def _listing(
    number,
    price,
    *,
    fixed=False,
    buy_now=None,
    end="2026-11-01T12:00:00+00:00",
    title=None,
):
    return yahoo_scrape.YahooListing(
        auction_id=f"t{number}",
        url=f"https://auctions.yahoo.co.jp/jp/auction/t{number}",
        title=title or f"Rick Owens Ramones {number}",
        price_yen=price,
        image_url=None,
        is_fixed_price=fixed,
        buy_now_price_yen=buy_now,
        scheduled_end_at=datetime.fromisoformat(end) if end else None,
    )


def _item():
    return db.TrackedItem(
        id=ITEM_ID,
        brand_id=BRAND_ID,
        brand="Rick Owens",
        name="rick owens ramones",
        search_query=None,
        keywords=("ramones",),
        exclude_keywords=(),
        manual_price_usd=MARKET,
        market_price_usd=None,
    )


def _record(auction_id="t9000000001", **overrides):
    row = {
        "auctionId": auction_id,
        "title": "Rick Owens Ramones High",
        "price": 1000,
        "imageUrl": None,
        "isFixedPrice": False,
        "buyNowPrice": None,
        "endTime": "2026-11-01T21:00:00+09:00",
    }
    row.update(overrides)
    return row


def _page(records):
    data = {
        "props": {"pageProps": {"initialState": {"search": {"items": {
            "listing": {"items": records}
        }}}}}
    }
    return '<script id="__NEXT_DATA__">' + json.dumps(data) + "</script>"


def _version2_listing(db_conn, item_id):
    brand_id = db_conn.execute("SELECT id FROM brands LIMIT 1").fetchone()[0]
    db_conn.execute(
        "INSERT INTO listings (brand_id, url, title, price_yen, image_url, first_seen_at, last_seen_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (brand_id, "https://auctions.yahoo.co.jp/jp/auction/legacy", "Legacy", 12345,
         "https://img.test/old.jpg", "2020-01-01T00:00:00+00:00", "2020-01-02T00:00:00+00:00"),
    )
    listing_id = db_conn.execute("SELECT id FROM listings WHERE url LIKE '%legacy'").fetchone()[0]
    db_conn.execute(
        "INSERT INTO listing_matches (listing_id, item_id, matched_at) VALUES (?, ?, ?)",
        (listing_id, item_id, "2020-01-01T00:00:00+00:00"),
    )
    db_conn.commit()
    return listing_id


def test_rc16_c1_fresh_schema_has_version_three_metadata_and_constraints(temp_db, db_conn):
    db.init_db()
    assert db_conn.execute("PRAGMA user_version").fetchone()[0] == 3
    columns = {row[1]: row for row in db_conn.execute("PRAGMA table_info(listings)")}
    assert {"is_fixed_price", "buy_now_price_yen", "scheduled_end_at"} <= set(columns)
    assert columns["is_fixed_price"][3] == 0
    assert columns["buy_now_price_yen"][3] == 0
    assert columns["scheduled_end_at"][3] == 0
    assert db_conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    indexes = {row[0] for row in db_conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL"
    )}
    assert {"idx_listings_brand_seen", "idx_matches_item", "idx_ebay_listing_prices_item_seen"} <= indexes
    db_conn.execute(
        "INSERT INTO brands (name, yahoo_brand_id) VALUES (?, ?)", ("Constraint test", "constraint")
    )
    brand_id = db_conn.execute(
        "SELECT id FROM brands WHERE yahoo_brand_id = ?", ("constraint",)
    ).fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            "INSERT INTO listings (brand_id, url, title, price_yen, first_seen_at, last_seen_at, "
            "is_fixed_price) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (brand_id, "https://x/invalid-flag", "Invalid", 1,
             "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00", 2),
        )
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            "INSERT INTO listings (brand_id, url, title, price_yen, first_seen_at, last_seen_at, "
            "buy_now_price_yen) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (brand_id, "https://x/invalid-buy-now", "Invalid", 1,
             "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00", 0),
        )


def test_rc16_c2_c3_v2_migration_preserves_legacy_rows_and_is_idempotent(seeded_db, db_conn):
    item_id = db_conn.execute("SELECT id FROM items ORDER BY id LIMIT 1").fetchone()[0]
    _version2_listing(db_conn, item_id)
    legacy_columns = (
        "id, brand_id, url, title, price_yen, image_url, first_seen_at, last_seen_at, ended_at, final_price_yen"
    )
    before_listing = tuple(db_conn.execute(f"SELECT {legacy_columns} FROM listings").fetchone())
    before_match = tuple(db_conn.execute("SELECT * FROM listing_matches").fetchone())
    assert db_conn.execute("PRAGMA user_version").fetchone()[0] == 2

    db.init_db()
    columns = "is_fixed_price, buy_now_price_yen, scheduled_end_at"
    after = db_conn.execute(
        f"SELECT {columns} FROM listings WHERE url LIKE '%legacy'"
    ).fetchone()
    assert tuple(after) == (None, None, None)
    assert tuple(db_conn.execute(f"SELECT {legacy_columns} FROM listings").fetchone()) == before_listing
    assert tuple(db_conn.execute("SELECT * FROM listing_matches").fetchone()) == before_match
    assert db_conn.execute("PRAGMA user_version").fetchone()[0] == 3

    db.init_db()
    assert tuple(db_conn.execute(f"SELECT {legacy_columns} FROM listings").fetchone()) == before_listing
    assert tuple(db_conn.execute("SELECT * FROM listing_matches").fetchone()) == before_match
    assert db_conn.execute("PRAGMA user_version").fetchone()[0] == 3


def test_rc16_c4_failed_migration_rolls_back_columns_and_version(seeded_db, db_conn, monkeypatch):
    original = db.MIGRATIONS.get(3, ())
    monkeypatch.setitem(
        db.MIGRATIONS,
        3,
        (
            "ALTER TABLE listings ADD COLUMN migration_probe TEXT",
            "ALTER TABLE table_that_does_not_exist ADD COLUMN failure TEXT",
        ),
    )
    monkeypatch.setattr(db, "SCHEMA_VERSION", 3)
    with pytest.raises(sqlite3.OperationalError):
        db.init_db()
    names = {row[1] for row in db_conn.execute("PRAGMA table_info(listings)")}
    assert "migration_probe" not in names
    assert "is_fixed_price" not in names
    assert db_conn.execute("PRAGMA user_version").fetchone()[0] == 2
    monkeypatch.setitem(db.MIGRATIONS, 3, original)


def test_rc16_c5_saved_live_fixture_exposes_valid_flags_prices_and_utc_ends(fixtures_dir):
    html = (fixtures_dir / "yahoo" / "brand_page_live.html").read_text(encoding="utf-8")
    listings = yahoo_scrape.parse_listings(html)
    assert len(listings) == 100
    assert all(type(row.is_fixed_price) is bool for row in listings)
    assert all(row.buy_now_price_yen is None or row.buy_now_price_yen > 0 for row in listings)
    assert all(row.scheduled_end_at.utcoffset().total_seconds() == 0 for row in listings)
    assert listings[0].is_fixed_price is True
    assert listings[0].buy_now_price_yen == listings[0].price_yen == 148000
    assert listings[0].scheduled_end_at.isoformat() == "2026-10-30T11:21:21+00:00"


@pytest.mark.parametrize(
    "changes, expected_fixed, expected_buy_now, expected_end",
    [
        ({"isFixedPrice": "true"}, None, None, "2026-11-01T12:00:00+00:00"),
        ({"isFixedPrice": None}, None, None, "2026-11-01T12:00:00+00:00"),
        ({"buyNowPrice": None}, False, None, "2026-11-01T12:00:00+00:00"),
        ({"endTime": "2026-11-01T21:00:00"}, False, None, None),
        ({"endTime": "bad"}, False, None, None),
    ],
)
def test_rc16_c12_c13_parser_keeps_untrusted_metadata_unknown(
    changes, expected_fixed, expected_buy_now, expected_end
):
    rows = yahoo_scrape.parse_listings(_page([_record(**changes)]))
    assert len(rows) == 1
    row = rows[0]
    assert row.is_fixed_price is expected_fixed
    assert row.buy_now_price_yen is expected_buy_now
    assert (row.scheduled_end_at.isoformat() if row.scheduled_end_at else None) == expected_end


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "90000"])
def test_rc16_c12_parser_skips_invalid_non_null_buy_now_amount(bad):
    assert yahoo_scrape.parse_listings(_page([_record(buyNowPrice=bad)])) == []


def test_rc16_c6_c7_fixed_price_uses_validated_purchase_amount_or_current_price():
    item = _item()
    equal = yahoo_scrape.find_item_deals(
        item, [_listing(1, 50000, fixed=True, buy_now=50000)], set(), RATE
    )
    absent = yahoo_scrape.find_item_deals(
        item, [_listing(2, 50000, fixed=True)], set(), RATE
    )
    assert [(deal.price_yen, deal.kind, deal.discount_pct) for deal in equal] == [
        (50000, "fixed_price", pytest.approx(61.25))
    ]
    assert [(deal.price_yen, deal.kind) for deal in absent] == [(50000, "fixed_price")]


def test_rc16_c8_c9_buy_now_auction_is_one_row_and_watch_when_only_bid_is_below_market():
    item = _item()
    buy_now = yahoo_scrape.find_item_deals(
        item, [_listing(1, 1000, fixed=False, buy_now=90000)], set(), RATE
    )
    assert len(buy_now) == 1
    assert (buy_now[0].kind, buy_now[0].price_yen, buy_now[0].current_price_yen) == (
        "auction_buy_now", 90000, 1000,
    )
    assert buy_now[0].discount_pct == pytest.approx(30.25)
    assert buy_now[0].potential_discount_pct == pytest.approx(99.225)

    watch = yahoo_scrape.find_item_deals(
        item, [_listing(2, 1000, fixed=False, buy_now=150000)], set(), RATE
    )
    assert len(watch) == 1
    assert (watch[0].kind, watch[0].price_yen, watch[0].discount_pct) == (
        "auction_watch", 1000, pytest.approx(99.225),
    )
    assert watch[0].current_price_yen is None


def test_rc16_c10_c11_penny_bid_is_potential_not_a_confirmed_discount():
    item = replace(_item(), manual_price_usd=6.2)
    deal = yahoo_scrape.find_item_deals(
        item, [_listing(1, 1, fixed=False)], set(), RATE
    )[0]
    email = digest.build_digest(
        [digest.ItemDeals("rick owens ramones", MARKET, (deal,))], RUN_DATE
    )
    assert "Auction watch" in email.html
    assert "current price (provisional)" in email.html
    assert "Potential discount at current price: 99.9% (not guaranteed; bids may rise)" in email.html
    assert "99.9% off" not in email.html
    assert "not guaranteed; bids may rise" in email.text
    assert "99.9% below market" not in email.text


def test_rc16_c12_missing_format_or_conflicting_fixed_price_is_not_actionable():
    item = _item()
    unknown = _listing(1, 1000, fixed=None, buy_now=None)
    mismatch = _listing(2, 50000, fixed=True, buy_now=90000)
    assert yahoo_scrape.find_item_deals(item, [unknown, mismatch], set(), RATE) == []


def test_rc16_c13_c14_end_time_is_utc_for_storage_and_jst_in_digest():
    parsed = yahoo_scrape.parse_listings(_page([_record(endTime="2026-11-01T21:00:00+09:00")]))[0]
    assert parsed.scheduled_end_at.isoformat() == "2026-11-01T12:00:00+00:00"
    deal = yahoo_scrape.find_item_deals(_item(), [parsed], set(), RATE)[0]
    email = digest.build_digest([digest.ItemDeals("Ramones", MARKET, (deal,))], RUN_DATE)
    assert "Scheduled end 2026-11-01 21:00 JST (may change)" in email.html
    assert "Scheduled end unavailable" not in email.html

    no_end = yahoo_scrape.parse_listings(_page([_record(endTime="not-a-time")]))[0]
    no_end_deal = yahoo_scrape.find_item_deals(_item(), [no_end], set(), RATE)[0]
    unavailable = digest.build_digest(
        [digest.ItemDeals("Ramones", MARKET, (no_end_deal,))], RUN_DATE
    )
    assert "Scheduled end unavailable" in unavailable.html


def test_rc16_c15_fleamarket_url_prefix_does_not_replace_explicit_sale_format():
    record = _record("z9000000001", isFleamarketItem=True, isFixedPrice=None, buyNowPrice=50000)
    parsed = yahoo_scrape.parse_listings(_page([record]))[0]
    assert parsed.url.startswith("https://paypayfleamarket.yahoo.co.jp/item/")
    assert parsed.is_fixed_price is None
    assert yahoo_scrape.find_item_deals(_item(), [parsed], set(), RATE) == []


def test_rc16_c16_watches_sort_by_end_time_not_potential_percentage():
    listings = [
        _listing(1, 1, end="2026-11-05T00:00:00+00:00"),
        _listing(2, 10000, end="2026-11-02T00:00:00+00:00"),
        _listing(3, 20000, end="2026-11-02T00:00:00+00:00"),
        _listing(4, 30000, end=None),
    ]
    deals = yahoo_scrape.find_item_deals(_item(), listings, set(), RATE)
    assert [deal.url.rsplit("/", 1)[-1] for deal in deals] == ["t2", "t3", "t1", "t4"]


def test_rc16_c17_immediate_and_watch_caps_are_independent_and_overflow_is_neutral():
    immediate = [_listing(i, 10000 * i, fixed=True) for i in range(1, 7)]
    watches = [_listing(100 + i, 1000 * i, end=f"2026-11-{i + 1:02d}T00:00:00+00:00") for i in range(1, 7)]
    section = yahoo_scrape.build_sections([_item()], immediate + watches, set(), RATE)[0]
    kinds = [deal.kind for deal in section.deals]
    assert kinds.count("fixed_price") == 5
    assert kinds.count("auction_watch") == 5
    email = digest.build_digest([section], RUN_DATE)
    assert "more listings not shown" in email.html
    assert "more listings not shown" in email.text
    assert "more new deal" not in email.text


def test_rc16_c18_c20_mixed_digest_labels_prices_escapes_and_counts_rows():
    buy = yahoo_scrape.find_item_deals(
        _item(), [_listing(1, 50000, fixed=False, buy_now=90000, title="Ramones <script> & shoes")], set(), RATE
    )[0]
    watch = yahoo_scrape.find_item_deals(
        _item(), [_listing(2, 1, fixed=False, title="Ramones bid")], set(), RATE
    )[0]
    fixed = yahoo_scrape.find_item_deals(
        _item(), [_listing(3, 40000, fixed=True, title="Ramones fixed")], set(), RATE
    )[0]
    email = digest.build_digest(
        [digest.ItemDeals("Ramones", MARKET, (buy, fixed, watch))], RUN_DATE
    )
    assert email.subject == "RainCheck: 3 listings to review - 2026-10-27"
    soup = BeautifulSoup(email.html, "html.parser")
    rows = soup.find("table").find_all("tr")[1:]
    assert len(rows) == 3
    assert "Buy now" in rows[0].get_text(" ", strip=True)
    assert "¥90,000" in rows[0].get_text(" ", strip=True)
    assert "¥50,000" in rows[0].get_text(" ", strip=True)
    assert "provisional" in rows[0].get_text(" ", strip=True)
    assert "Auction watch - current price (provisional)" in rows[2].get_text(" ", strip=True)
    assert "&lt;script&gt;" in email.html and soup.find("script") is None
    assert "not guaranteed; bids may rise" in email.html
    assert "Auction watch - current price (provisional)" in email.text
    assert "Potential discount at current price" in email.text


def test_rc16_c20_auction_title_and_unsafe_url_follow_existing_escape_rules():
    deal = digest.Deal(
        ITEM_ID, BRAND_ID, "javascript:alert(1)", "<b>Ramones</b> & shoes", 1, 0.0062,
        MARKET, 99.9, None, kind="auction_watch", potential_discount_pct=99.9,
    )
    email = digest.build_digest([digest.ItemDeals("Ramones", MARKET, (deal,))], RUN_DATE)
    soup = BeautifulSoup(email.html, "html.parser")
    assert soup.find("b") is None
    assert "&lt;b&gt;Ramones&lt;/b&gt; &amp; shoes" in email.html
    assert "javascript:" not in email.html and "javascript:" not in email.text
    assert "<b>Ramones</b> & shoes" in email.text


def test_rc16_c19_empty_page_and_known_non_deal_do_not_create_digest():
    assert yahoo_scrape.parse_listings(_page([])) == []
    assert yahoo_scrape.find_item_deals(
        _item(), [_listing(1, 200000, fixed=False)], set(), RATE
    ) == []


def test_rc16_c18_main_sends_mixed_digest_and_records_source_metadata(fake_http, seeded_db, sent_emails, monkeypatch):
    listings = [
        _listing(1, 40000, fixed=True),
        _listing(2, 50000, fixed=False, buy_now=90000),
        _listing(3, 1000, fixed=False, end="2026-10-28T12:00:00+00:00"),
    ]
    monkeypatch.setattr(yahoo_scrape, "parse_listings", lambda _html: listings)
    fake_http.add("GET", yahoo_scrape.RICK_OWENS_URL, text="unused")
    fake_http.add(
        "GET", yahoo_scrape.FRANKFURTER_RATE_URL,
        json={"date": "2026-10-27", "base": "JPY", "quote": "USD", "rate": RATE},
    )

    assert yahoo_scrape.main() == 0
    assert len(sent_emails) == 1
    subject, html, text = sent_emails[0]
    assert subject.startswith("RainCheck: 3 listings to review - ")
    assert "Buy now" in html and "Auction watch" in html
    assert "¥90,000" in html and "¥50,000" in html and "¥1,000" in html
    assert "not guaranteed; bids may rise" in text
    conn = sqlite3.connect(seeded_db)
    try:
        rows = conn.execute(
            "SELECT url, price_yen, is_fixed_price, buy_now_price_yen, scheduled_end_at "
            "FROM listings ORDER BY url"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 3
    by_url = {row[0]: row[1:] for row in rows}
    assert by_url[listings[0].url][:3] == (40000, 1, None)
    assert by_url[listings[1].url][:3] == (50000, 0, 90000)
    assert by_url[listings[2].url][:3] == (1000, 0, None)
    conn = sqlite3.connect(seeded_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM listing_matches").fetchone()[0] == 3
    finally:
        conn.close()


def test_rc16_c19_watch_only_digest_sends_once_and_records_the_watch(fake_http, seeded_db, sent_emails, monkeypatch):
    watch = _listing(1, 1000, fixed=False, end="2026-11-05T12:00:00+00:00")
    monkeypatch.setattr(yahoo_scrape, "parse_listings", lambda _html: [watch])
    fake_http.add("GET", yahoo_scrape.RICK_OWENS_URL, text="unused")
    fake_http.add(
        "GET", yahoo_scrape.FRANKFURTER_RATE_URL,
        json={"date": "2026-10-27", "base": "JPY", "quote": "USD", "rate": RATE},
    )
    assert yahoo_scrape.main() == 0
    assert len(sent_emails) == 1
    assert sent_emails[0][0].startswith("RainCheck: 1 listings to review - ")
    assert "Auction watch" in sent_emails[0][1]
    assert "Scheduled end 2026-11-05 21:00 JST (may change)" in sent_emails[0][1]
    conn = sqlite3.connect(seeded_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM listing_matches").fetchone()[0] == 1
    finally:
        conn.close()


def test_rc16_c23_above_market_listing_can_qualify_on_a_later_observation(fake_http, seeded_db, sent_emails, monkeypatch):
    current = [_listing(1, 200000, fixed=False)]
    monkeypatch.setattr(yahoo_scrape, "parse_listings", lambda _html: current)
    fake_http.add("GET", yahoo_scrape.RICK_OWENS_URL, text="unused")
    fake_http.add(
        "GET", yahoo_scrape.FRANKFURTER_RATE_URL,
        json={"date": "2026-10-27", "base": "JPY", "quote": "USD", "rate": RATE},
    )
    assert yahoo_scrape.main() == 0
    assert sent_emails == []
    current[:] = [_listing(1, 1000, fixed=False)]
    assert yahoo_scrape.main() == 0
    assert len(sent_emails) == 1
    assert "Auction watch" in sent_emails[0][1]


def test_rc16_c24_duplicate_records_keep_first_sale_metadata():
    rows = yahoo_scrape.parse_listings(_page([
        _record("t9000000001", price=50000, isFixedPrice=False, buyNowPrice=90000),
        _record("t9000000001", price=1000, isFixedPrice=True, buyNowPrice=1000),
    ]))
    assert len(rows) == 1
    assert rows[0].price_yen == 50000
    assert rows[0].is_fixed_price is False
    assert rows[0].buy_now_price_yen == 90000


def test_rc16_c21_failed_send_records_no_auction_metadata(fake_http, seeded_db, sent_emails, monkeypatch):
    listing = _listing(1, 1000, fixed=False)
    monkeypatch.setattr(yahoo_scrape, "parse_listings", lambda _html: [listing])
    monkeypatch.setattr(yahoo_scrape, "send_email", lambda *_args: False)
    fake_http.add("GET", yahoo_scrape.RICK_OWENS_URL, text="unused")
    fake_http.add(
        "GET", yahoo_scrape.FRANKFURTER_RATE_URL,
        json={"date": "2026-10-27", "base": "JPY", "quote": "USD", "rate": RATE},
    )
    with pytest.raises(SystemExit):
        yahoo_scrape.main()
    assert sent_emails == []
    conn = sqlite3.connect(seeded_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 0
    finally:
        conn.close()


def test_rc16_c28_yahoo_rejects_missing_source_before_sending(fake_http, seeded_db, sent_emails, monkeypatch):
    source = _listing(1, 1000, fixed=False)
    item_id = db.get_tracked_items()[0].id
    brand_id = db.get_tracked_items()[0].brand_id
    missing = digest.Deal(item_id, brand_id, "https://auctions.yahoo.co.jp/jp/auction/not-on-page",
                          "Rick Owens Ramones", 1000, 6.2, MARKET, 99.0, None,
                          kind="auction_watch")
    monkeypatch.setattr(yahoo_scrape, "parse_listings", lambda _html: [source])
    monkeypatch.setattr(
        yahoo_scrape, "find_item_deals",
        lambda item, *_args, **_kwargs: [missing] if item.name == "rick owens ramones" else [],
    )
    fake_http.add("GET", yahoo_scrape.RICK_OWENS_URL, text="unused")
    fake_http.add(
        "GET", yahoo_scrape.FRANKFURTER_RATE_URL,
        json={"date": "2026-10-27", "base": "JPY", "quote": "USD", "rate": RATE},
    )
    with pytest.raises(SystemExit):
        yahoo_scrape.main()
    assert sent_emails == []
    conn = sqlite3.connect(seeded_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 0
    finally:
        conn.close()


def test_rc16_c22_c23_metadata_snapshot_is_preserved_for_existing_url(seeded_db, db_conn):
    item_id = db_conn.execute("SELECT id FROM items ORDER BY id LIMIT 1").fetchone()[0]
    brand_id = db_conn.execute("SELECT id FROM brands LIMIT 1").fetchone()[0]
    source = _listing(1, 50000, fixed=False, buy_now=90000)
    deal = digest.Deal(item_id, brand_id, source.url, "Auction", 90000, 558.0,
                       MARKET, 30.25, None, kind="auction_buy_now", current_price_yen=50000)
    assert db.record_listing_matches([deal], sources_by_url={deal.url: source}) == 1
    before = tuple(db_conn.execute(
        "SELECT price_yen, is_fixed_price, buy_now_price_yen, scheduled_end_at FROM listings WHERE url = ?",
        (deal.url,),
    ).fetchone())
    changed = _listing(1, 80000, fixed=False, buy_now=120000, end="2026-12-01T00:00:00+00:00")
    assert db.record_listing_matches([deal], sources_by_url={deal.url: changed}) == 0
    after = tuple(db_conn.execute(
        "SELECT price_yen, is_fixed_price, buy_now_price_yen, scheduled_end_at FROM listings WHERE url = ?",
        (deal.url,),
    ).fetchone())
    assert after == before == (50000, 0, 90000, "2026-11-01T12:00:00+00:00")


def test_rc16_c26_metadata_batch_failure_rolls_back_all_rows(seeded_db, db_conn):
    item_id = db_conn.execute("SELECT id FROM items ORDER BY id LIMIT 1").fetchone()[0]
    brand_id = db_conn.execute("SELECT id FROM brands LIMIT 1").fetchone()[0]
    first = _listing(1, 1000, fixed=False)
    second = _listing(2, 2000, fixed=False)
    deals = [
        digest.Deal(item_id, brand_id, row.url, row.title, row.price_yen, row.price_yen * RATE,
                    MARKET, 99.0, None, kind="auction_watch")
        for row in (first, second)
    ]
    db_conn.execute(
        "CREATE TRIGGER reject_rc16_second BEFORE INSERT ON listings "
        "WHEN NEW.url = 'https://auctions.yahoo.co.jp/jp/auction/t2' "
        "BEGIN SELECT RAISE(ABORT, 'second row rejected'); END"
    )
    db_conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="second row rejected"):
        db.record_listing_matches(deals, sources_by_url={first.url: first, second.url: second})
    assert db_conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 0
    assert db_conn.execute("SELECT COUNT(*) FROM listing_matches").fetchone()[0] == 0


@pytest.mark.parametrize("source_case", ["missing", "mismatched", "boolean_price", "bad_flag", "boolean_buy_now", "non_utc_end"])
def test_rc16_c28_invalid_source_mapping_fails_before_database_access(monkeypatch, source_case):
    source = _listing(1, 50000, fixed=False, buy_now=90000)
    deal = digest.Deal(1, 1, source.url, "Auction", 90000, 558.0,
                       MARKET, 30.25, None, kind="auction_buy_now")
    mapping = {deal.url: source}
    if source_case == "missing":
        mapping = {}
    elif source_case == "mismatched":
        mapping[deal.url] = _listing(2, 50000, fixed=False, buy_now=90000)
    elif source_case == "boolean_price":
        mapping[deal.url] = yahoo_scrape.YahooListing(
            source.auction_id, source.url, source.title, True, None, False, 90000, source.scheduled_end_at
        )
    elif source_case == "bad_flag":
        mapping[deal.url] = yahoo_scrape.YahooListing(
            source.auction_id, source.url, source.title, 50000, None, 1, 90000, source.scheduled_end_at
        )
    elif source_case == "boolean_buy_now":
        mapping[deal.url] = yahoo_scrape.YahooListing(
            source.auction_id, source.url, source.title, 50000, None, False, True, source.scheduled_end_at
        )
    elif source_case == "non_utc_end":
        mapping[deal.url] = yahoo_scrape.YahooListing(
            source.auction_id, source.url, source.title, 50000, None, False, 90000,
            datetime.fromisoformat("2026-11-01T21:00:00+09:00"),
        )

    def unexpected_connection():
        raise AssertionError("invalid sources must be rejected before database access")

    monkeypatch.setattr(db, "get_connection", unexpected_connection)
    with pytest.raises(ValueError):
        db.record_listing_matches([deal], sources_by_url=mapping)


def test_rc16_c29_legacy_batch_call_and_single_match_keep_null_metadata(seeded_db, db_conn):
    item_id = db_conn.execute("SELECT id FROM items ORDER BY id LIMIT 1").fetchone()[0]
    brand_id = db_conn.execute("SELECT id FROM brands LIMIT 1").fetchone()[0]
    deal = digest.Deal(item_id, brand_id, "https://x/legacy", "Legacy", 12345,
                       76.539, MARKET, 90.4, None)
    assert db.record_listing_matches([deal]) == 1
    assert db.record_listing_match(brand_id, "https://x/single", "Single", 10000, None, item_id)
    for url in (deal.url, "https://x/single"):
        row = db_conn.execute(
            "SELECT is_fixed_price, buy_now_price_yen, scheduled_end_at FROM listings WHERE url = ?",
            (url,),
        ).fetchone()
        assert tuple(row) == (None, None, None)


def test_rc16_c30_fixed_only_digest_keeps_legacy_subject_and_price_markup():
    deal = digest.Deal(ITEM_ID, BRAND_ID, "https://x/fixed", "Fixed", 50000, 310.0,
                       MARKET, 61.25000000000001, None)
    email = digest.build_digest([digest.ItemDeals("Ramones", MARKET, (deal,))], RUN_DATE)
    assert email.subject == "RainCheck: 1 new deal - 2026-10-27"
    price_cell = BeautifulSoup(email.html, "html.parser").find("table").find_all("tr")[1].find_all("td")[2]
    assert price_cell.get_text(" ", strip=True) == "¥50,000 $310.00 61.3% off"
    assert "Buy now" not in email.html
