"""Yahoo pipeline tests.

RC-07 rewrote the RC-01 criteria 14, 45, 46, 47 against parse_listings (they keep
their RC-01 names) and added criteria 20-29.
RC-15 replaced report_item_deals with find_item_deals / build_sections and made main()
send one digest; the superseded tests keep their names and now run main().
"""
import dataclasses
import re
from datetime import date, datetime, timezone

from bs4 import BeautifulSoup

import pytest
import requests

import db
import yahoo_scrape
from dbhelpers import rows as sql_rows, run as sql_run, set_user_version, sha256
from yahoo_scrape import RICK_OWENS_URL

URL_1 = "https://auctions.yahoo.co.jp/jp/auction/t1000000001"
URL_3 = "https://auctions.yahoo.co.jp/jp/auction/t1000000003"
URL_4 = "https://auctions.yahoo.co.jp/jp/auction/t1000000004"
X1 = "https://auctions.yahoo.co.jp/jp/auction/x1000000001"
X3 = "https://auctions.yahoo.co.jp/jp/auction/x1000000003"
Z2 = "https://paypayfleamarket.yahoo.co.jp/item/z1000000002"
FRANKFURTER_URL = "https://api.frankfurter.dev/v2/rate/jpy/usd"


def _add_rate_response(fake_http, rate=0.0062, published="2026-10-27", status=200):
    fake_http.add(
        "GET", FRANKFURTER_URL,
        json={"date": published, "base": "JPY", "quote": "USD", "rate": rate},
        status=status,
    )


def _html(fixtures_dir, name):
    return (fixtures_dir / "yahoo" / name).read_text(encoding="utf-8")


@pytest.fixture
def minimal_listings(fixtures_dir):
    return yahoo_scrape.parse_listings(_html(fixtures_dir, "brand_page_minimal.html"))


@pytest.fixture
def malformed_listings(fixtures_dir):
    return yahoo_scrape.parse_listings(_html(fixtures_dir, "brand_page_malformed.html"))


@pytest.fixture
def yahoo_http(fake_http, fixtures_dir, monkeypatch):
    fake_http.add("GET", RICK_OWENS_URL, text=_html(fixtures_dir, "brand_page_minimal.html"))
    _add_rate_response(fake_http)
    _freeze_rc05_clock(monkeypatch, datetime(2026, 10, 27, tzinfo=timezone.utc))
    return fake_http


def _item(name):
    return [i for i in db.get_tracked_items() if i.name == name][0]


@pytest.fixture
def ramones_item(seeded_db):
    return _item("rick owens ramones")


def _rows(conn):
    return conn.execute("SELECT url, price_yen, image_url FROM listings ORDER BY url").fetchall()


# ---- helpers for the pure tests ----------------------------------------------------

AUCTION = "https://auctions.yahoo.co.jp/jp/auction/"


def inline_item(item_id=1, name="rick owens ramones", keywords=("ramones", "ramon"),
                exclude=(), price=800.0, brand_id=1):
    return db.TrackedItem(
        id=item_id, brand_id=brand_id, brand="Rick Owens", name=name, search_query=None,
        keywords=tuple(keywords), exclude_keywords=tuple(exclude),
        manual_price_usd=price, market_price_usd=None,
    )


def inline_listing(n, title=None, yen=10000, image=None):
    listing = yahoo_scrape.YahooListing(
        auction_id=f"t{n}", url=f"{AUCTION}t{n}", title=title or f"Rick Owens Ramones {n}",
        price_yen=yen, image_url=image,
    )
    # Existing pure tests exercise price ranking and matching with fixed-price examples.
    # State that format explicitly once YahooListing exposes the RC-16 metadata fields.
    if "is_fixed_price" in {field.name for field in dataclasses.fields(listing)}:
        listing = dataclasses.replace(
            listing, is_fixed_price=True, buy_now_price_yen=yen, scheduled_end_at=None,
        )
    return listing


def _fixed_listing(auction_id, url, title, price_yen, image_url=None):
    listing = yahoo_scrape.YahooListing(auction_id, url, title, price_yen, image_url)
    if "is_fixed_price" in {field.name for field in dataclasses.fields(listing)}:
        listing = dataclasses.replace(
            listing, is_fixed_price=True, buy_now_price_yen=price_yen, scheduled_end_at=None,
        )
    return listing


def seven_ramones_listings():
    # 10000..70000 yen against an 800 USD market: all are deals, cheapest has the best discount
    return [inline_listing(n, yen=10000 * n) for n in range(1, 8)]


def _soup(html):
    return BeautifulSoup(html, "html.parser")


def _row_urls(html):
    """The listing URL of each data row, in order, read from the Listing cell's link."""
    urls = []
    for tr in _soup(html).find_all("tr"):
        tds = tr.find_all("td")
        if tds:
            urls.append(tds[1].find("a")["href"])
    return urls


# ---- Finding deals (criteria 1-9) --------------------------------------------------


def test_rc15_c1_find_item_deals_returns_both_deals_best_first_with_full_fields(
    ramones_item, minimal_listings
):
    from digest import Deal

    deals = yahoo_scrape.find_item_deals(ramones_item, minimal_listings, set(), 0.0062)
    assert [d.url for d in deals] == [URL_1, URL_4]
    assert all(isinstance(d, Deal) for d in deals)
    first, second = deals
    assert first.item_id == ramones_item.id
    assert first.brand_id == ramones_item.brand_id
    assert first.title == "Rick Owens Ramones High Top 42"
    assert first.price_yen == 90000
    assert first.price_usd == pytest.approx(558.0)
    assert first.market_price_usd == 800.0
    assert first.discount_pct == pytest.approx(30.25)
    assert first.kind == "auction_buy_now"
    assert first.current_price_yen == 50000
    assert first.potential_discount_pct == pytest.approx(61.25)
    assert first.image_url == "https://auc-pctr.c.yimg.jp/i/test/1.jpg"
    assert second.discount_pct == pytest.approx(22.5)


def test_rc15_c2_skip_urls_are_excluded(ramones_item, minimal_listings):
    deals = yahoo_scrape.find_item_deals(ramones_item, minimal_listings, {URL_1}, 0.0062)
    assert [d.url for d in deals] == [URL_4]


def test_rc15_c3_exclude_keywords_from_the_item_apply(ramones_item, minimal_listings):
    item = dataclasses.replace(ramones_item, exclude_keywords=("high",))
    assert [d.url for d in yahoo_scrape.find_item_deals(item, minimal_listings, set(), 0.0062)] == [URL_4]


def test_rc15_c4_effective_price_drives_the_deal_math(ramones_item, minimal_listings):
    item = dataclasses.replace(ramones_item, market_price_usd=400.0)
    assert item.effective_price_usd == 400.0
    deals = yahoo_scrape.find_item_deals(item, minimal_listings, set(), 0.0062)
    assert [d.url for d in deals] == [URL_1]
    assert deals[0].discount_pct == pytest.approx(22.5)
    assert deals[0].kind == "auction_watch"
    assert deals[0].market_price_usd == 400.0


def test_rc15_c5_no_usable_price_returns_empty_and_prints_nothing(
    ramones_item, minimal_listings, capsys
):
    item = dataclasses.replace(ramones_item, market_price_usd=None, manual_price_usd=None)
    assert yahoo_scrape.find_item_deals(item, minimal_listings, set(), 0.0062) == []
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_rc15_c6_a_listing_exactly_at_market_is_not_a_deal(ramones_item, minimal_listings):
    item = dataclasses.replace(ramones_item, market_price_usd=310.0)
    assert [d.url for d in yahoo_scrape.find_item_deals(item, minimal_listings, set(), 0.0062)] == []
    # strictly above market is not a deal either
    item = dataclasses.replace(ramones_item, market_price_usd=300.0)
    assert yahoo_scrape.find_item_deals(item, minimal_listings, set(), 0.0062) == []


def test_rc15_c7_equal_discounts_keep_input_order():
    listings = [inline_listing(1, "Ramones A", 20000), inline_listing(2, "Ramones B", 20000),
                inline_listing(3, "Ramones C", 20000)]
    deals = yahoo_scrape.find_item_deals(inline_item(), listings, set(), 0.0062)
    assert [d.title for d in deals] == ["Ramones A", "Ramones B", "Ramones C"]


def test_rc15_c8_brand_comes_from_the_item_not_a_name_lookup(ramones_item, minimal_listings):
    item = dataclasses.replace(ramones_item, brand="Not In Database")
    deals = yahoo_scrape.find_item_deals(item, minimal_listings, set(), 0.0062)
    assert [d.url for d in deals] == [URL_1, URL_4]
    assert {d.brand_id for d in deals} == {ramones_item.brand_id}


def test_rc15_c9_pure_functions_need_no_database(tmp_path):
    path = tmp_path / "test.db"
    assert not path.exists()
    items = [
        inline_item(1),
        inline_item(2, "pod shorts", keywords=("pod short", "pod shorts"), price=300.0),
    ]
    listings = [inline_listing(1, yen=20000), inline_listing(2, "Rick Owens Pod Shorts", 10000)]
    deals = yahoo_scrape.find_item_deals(items[0], listings, set(), 0.0062)
    sections = yahoo_scrape.build_sections(items, listings, set(), 0.0062)
    assert [d.url for d in deals] == [f"{AUCTION}t1"]
    assert [s.item_name for s in sections] == ["rick owens ramones", "pod shorts"]
    assert not path.exists()


# ---- Building sections (criteria 10-13) --------------------------------------------


def test_rc15_c10_top_five_per_item_best_discount_first_with_not_shown_count():
    from digest import ItemDeals

    sections = yahoo_scrape.build_sections([inline_item()], seven_ramones_listings(), set(), 0.0062)
    assert len(sections) == 1
    sec = sections[0]
    assert isinstance(sec, ItemDeals)
    assert sec.item_name == "rick owens ramones"
    assert sec.market_price_usd == 800.0
    assert [d.price_yen for d in sec.deals] == [10000, 20000, 30000, 40000, 50000]
    assert sec.not_shown == 2


def test_rc15_c10_default_limit_is_five_and_limit_is_adjustable():
    assert yahoo_scrape.TOP_DEALS_PER_ITEM == 5
    sec = yahoo_scrape.build_sections(
        [inline_item()], seven_ramones_listings(), set(), 0.0062, per_item_limit=2
    )[0]
    assert len(sec.deals) == 2 and sec.not_shown == 5


def test_rc15_c11_a_listing_that_is_a_deal_for_two_items_appears_only_under_the_first():
    first = inline_item(1, "first", keywords=("ramones",))
    second = inline_item(2, "second", keywords=("ramones",))
    listings = [inline_listing(1, yen=20000)]
    sections = yahoo_scrape.build_sections([first, second], listings, set(), 0.0062)
    assert [s.item_name for s in sections] == ["first"]


def test_rc15_c11_claim_holds_even_when_the_first_section_cut_the_listing():
    first = inline_item(1, "first", keywords=("ramones",))
    second = inline_item(2, "second", keywords=("sneakers",))
    listings = [
        inline_listing(1, "Rick Owens Ramones High", 10000),
        inline_listing(2, "Rick Owens Ramones Sneakers", 20000),
    ]
    sections = yahoo_scrape.build_sections(
        [first, second], listings, set(), 0.0062, per_item_limit=1
    )
    assert [s.item_name for s in sections] == ["first"]
    assert [d.url for d in sections[0].deals] == [f"{AUCTION}t1"]
    assert sections[0].not_shown == 1


def test_rc10_c19_healthy_earlier_claim_still_blocks_a_cut_listing():
    # Preserve the approved claim priority for healthy items after item isolation.
    first = inline_item(1, "first", keywords=("ramones",))
    second = inline_item(2, "second", keywords=("sneakers",))
    listings = [
        inline_listing(1, "Rick Owens Ramones High", 10000),
        inline_listing(2, "Rick Owens Ramones Sneakers", 20000),
    ]
    sections = yahoo_scrape.build_sections(
        [first, second], listings, set(), 0.0062, per_item_limit=1
    )
    assert [section.item_name for section in sections] == ["first"]
    assert [deal.url for deal in sections[0].deals] == [f"{AUCTION}t1"]


def test_rc15_c11_second_item_keeps_its_other_deals():
    first = inline_item(1, "first", keywords=("ramones",))
    second = inline_item(2, "second", keywords=("ramones", "geobasket"))
    listings = [inline_listing(1, "Ramones A", 20000), inline_listing(2, "Geobasket B", 30000)]
    sections = yahoo_scrape.build_sections([first, second], listings, set(), 0.0062)
    assert [(s.item_name, [d.title for d in s.deals]) for s in sections] == [
        ("first", ["Ramones A"]), ("second", ["Geobasket B"]),
    ]


def test_rc15_c12_items_without_deals_or_price_make_no_section_and_order_is_input_order():
    items = [
        inline_item(1, "first", keywords=("ramones",)),
        inline_item(2, "no price", keywords=("ramones",), price=None),
        inline_item(3, "no match", keywords=("zzz",)),
        inline_item(4, "last", keywords=("geobasket",)),
    ]
    listings = [inline_listing(1, "Ramones A", 20000), inline_listing(2, "Geobasket B", 30000)]
    sections = yahoo_scrape.build_sections(items, listings, set(), 0.0062)
    assert [s.item_name for s in sections] == ["first", "last"]


def test_rc15_c13_known_urls_are_in_no_section():
    listings = [inline_listing(1, yen=20000), inline_listing(2, yen=30000)]
    sections = yahoo_scrape.build_sections([inline_item()], listings, {f"{AUCTION}t1"}, 0.0062)
    assert [d.url for s in sections for d in s.deals] == [f"{AUCTION}t2"]
    assert yahoo_scrape.build_sections(
        [inline_item()], listings, {f"{AUCTION}t1", f"{AUCTION}t2"}, 0.0062
    ) == []


def test_rc15_c13_known_urls_may_be_any_collection():
    listings = [inline_listing(1, yen=20000)]
    for known in ([f"{AUCTION}t1"], (f"{AUCTION}t1",), frozenset({f"{AUCTION}t1"})):
        assert yahoo_scrape.build_sections([inline_item()], listings, known, 0.0062) == []


# ---- RC-08 exchange-rate resolution and pure conversion ----------------------------


def test_rc08_c1_fetches_one_valid_pair_with_status_check_and_ten_second_timeout(fake_http):
    _add_rate_response(fake_http, rate=0.0067)
    assert yahoo_scrape.fetch_yen_to_usd_rate(date(2026, 10, 27)) == pytest.approx(0.0067)
    assert len(fake_http.calls) == 1
    method, url, kwargs = fake_http.calls[0]
    assert (method, url) == ("GET", FRANKFURTER_URL)
    assert kwargs.get("timeout") == 10


@pytest.mark.parametrize(
    "body",
    [
        {"date": "2026-10-27", "base": "JPY", "quote": "USD", "rate": None},
        {"date": "2026-10-27", "base": "JPY", "quote": "USD", "rate": "0.0067"},
        {"date": "2026-10-27", "base": "JPY", "quote": "USD", "rate": True},
        {"date": "2026-10-27", "base": "JPY", "quote": "USD", "rate": 0},
        {"date": "2026-10-27", "base": "JPY", "quote": "USD", "rate": -0.1},
        {"date": "2026-10-27", "base": "JPY", "quote": "USD", "rate": float("nan")},
        {"date": "2026-10-27", "base": "JPY", "quote": "USD", "rate": float("inf")},
        {"date": "2026-10-27", "base": "JPY", "quote": "USD"},
        {"date": "2026-10-27", "base": "USD", "quote": "JPY", "rate": 0.0067},
        {"date": "2026-10-27", "quote": "USD", "rate": 0.0067},
        {"date": "2026-10-27", "base": "JPY", "rate": 0.0067},
        {"date": "not-a-date", "base": "JPY", "quote": "USD", "rate": 0.0067},
        {"base": "JPY", "quote": "USD", "rate": 0.0067},
        ["not", "an", "object"],
    ],
    ids=["null", "string", "boolean", "zero", "negative", "nan", "infinite", "missing-rate", "wrong-pair", "missing-base", "missing-quote", "bad-date", "missing-date", "non-object"],
)
def test_rc08_c4_c5_bad_exchange_data_uses_fallback_and_warns_once(body, fake_http, caplog):
    import logging

    fake_http.add("GET", FRANKFURTER_URL, json=body)
    with caplog.at_level(logging.WARNING, logger="yahoo_scrape"):
        rate = yahoo_scrape.resolve_yen_to_usd_rate(date(2026, 10, 27))
    assert rate == 0.0062
    warnings = [record for record in caplog.records if record.name == "yahoo_scrape"]
    assert len(warnings) == 1
    message = warnings[0].getMessage().lower()
    assert "jpy" in message and "usd" in message and "0.0062" in message


@pytest.mark.parametrize("published", ["2026-10-20", "2026-10-19", "2026-10-28"])
def test_rc08_c4_freshness_boundary_is_seven_days_inclusive(published, fake_http):
    _add_rate_response(fake_http, rate=0.0067, published=published)
    rate = yahoo_scrape.resolve_yen_to_usd_rate(date(2026, 10, 27))
    assert rate == (0.0067 if published == "2026-10-20" else 0.0062)


@pytest.mark.parametrize("failure", [requests.Timeout("slow"), requests.ConnectionError("offline")])
def test_rc08_c6_provider_transport_failure_falls_back_and_warns(failure, monkeypatch, caplog):
    import logging

    def fail_get(url, **kwargs):
        raise failure

    monkeypatch.setattr(requests, "get", fail_get)
    with caplog.at_level(logging.WARNING, logger="yahoo_scrape"):
        assert yahoo_scrape.resolve_yen_to_usd_rate(date(2026, 10, 27)) == 0.0062
    warnings = [record for record in caplog.records if record.name == "yahoo_scrape"]
    assert len(warnings) == 1
    assert "0.0062" in warnings[0].getMessage()


@pytest.mark.parametrize("rate", [None, True, False, 0, -1, float("nan"), float("inf"), "0.0067"])
def test_rc08_c9_pure_deal_functions_reject_invalid_supplied_rates(rate):
    listings = [inline_listing(1, yen=50000)]
    with pytest.raises(ValueError):
        yahoo_scrape.find_item_deals(inline_item(), listings, set(), rate)
    with pytest.raises(ValueError):
        yahoo_scrape.build_sections([inline_item()], listings, set(), rate)


@pytest.mark.parametrize(
    "rate,price,discount", [(0.0067, 335.0, 58.125), (0.0062, 310.0, 61.25)]
)
def test_rc08_c3_pure_conversion_uses_only_explicit_rate(rate, price, discount, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("pure deal calculation attempted network access")

    monkeypatch.setattr(requests, "get", no_network)
    item = inline_item(price=800.0)
    deals = yahoo_scrape.find_item_deals(item, [inline_listing(1, yen=50000)], set(), rate)
    assert deals[0].price_usd == pytest.approx(price)
    assert deals[0].discount_pct == pytest.approx(discount)
    sections = yahoo_scrape.build_sections(
        [item], [inline_listing(1, yen=50000)], set(), rate
    )
    assert sections[0].deals[0].price_usd == pytest.approx(price)


def test_rc08_c10_rate_change_can_move_listing_across_deal_threshold():
    item = inline_item(price=800.0)
    listing = inline_listing(1, yen=120000)
    assert yahoo_scrape.find_item_deals(item, [listing], set(), 0.0067) == []
    deals_at_fallback = yahoo_scrape.find_item_deals(item, [listing], set(), 0.0062)
    assert len(deals_at_fallback) == 1
    assert deals_at_fallback[0].price_usd == pytest.approx(744.0)


def test_rc08_c10_main_leaves_same_listing_unrecorded_then_sends_it_after_rate_change(
    yahoo_http, seeded_db, sent_emails, monkeypatch, db_conn, caplog
):
    caplog.set_level("INFO", logger="yahoo_scrape")
    rate_values = iter([0.0067, 0.0062])
    rate_calls = []
    real_get = requests.get

    class ExchangeResponse:
        def __init__(self, rate):
            self.rate = rate

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "date": "2026-10-27", "base": "JPY", "quote": "USD", "rate": self.rate
            }

    def fake_get(url, **kwargs):
        if url == FRANKFURTER_URL:
            rate_calls.append((url, kwargs))
            return ExchangeResponse(next(rate_values))
        return real_get(url, **kwargs)

    monkeypatch.setattr(requests, "get", fake_get)
    listing = inline_listing(1, "Rick Owens Ramones High", 120000)
    monkeypatch.setattr(yahoo_scrape, "parse_listings", lambda html: [listing])

    yahoo_scrape.main()
    assert sql_rows(seeded_db, "SELECT url FROM listings") == []
    assert sql_rows(seeded_db, "SELECT listing_id FROM listing_matches") == []
    assert sent_emails == []
    assert any("no new deals" in record.getMessage().lower() for record in caplog.records)

    yahoo_scrape.main()
    assert len(rate_calls) == 2
    assert all(call[1].get("timeout") == 10 for call in rate_calls)
    assert len(sent_emails) == 1
    assert "$744.00" in sent_emails[0][1] and "7.0% off" in sent_emails[0][1]
    assert sql_rows(seeded_db, "SELECT url FROM listings") == [(listing.url,)]
    assert len(sql_rows(seeded_db, "SELECT listing_id FROM listing_matches")) == 1


def test_rc08_c2_one_fetched_rate_drives_all_items_and_digest_values(
    yahoo_http, seeded_db, sent_emails, monkeypatch, db_conn
):
    _add_rate_response(yahoo_http, rate=0.0067, published="2026-10-27")
    monkeypatch.setattr(
        yahoo_scrape,
        "parse_listings",
        lambda html: [
            inline_listing(2, "Rick Owens Ramones Vintage", 70000),
            inline_listing(1, "Rick Owens Ramones High", 50000),
            inline_listing(3, "Rick Owens Pod Shorts", 10000),
        ],
    )
    observed_at = datetime(2026, 10, 27, tzinfo=timezone.utc)
    _freeze_rc05_clock(monkeypatch, observed_at)

    yahoo_scrape.main()

    rate_calls = [call for call in yahoo_http.calls if call[1] == FRANKFURTER_URL]
    assert len(rate_calls) == 1
    assert len(sent_emails) == 1
    html = sent_emails[0][1]
    assert "$335.00" in html and "$469.00" in html and "$67.00" in html
    assert "58.1% off" in html and "41.4% off" in html and "77.7% off" in html
    assert _row_urls(html) == [f"{AUCTION}t1", f"{AUCTION}t2", f"{AUCTION}t3"]
    assert [row[0] for row in db_conn.execute("SELECT url FROM listings ORDER BY url")] == [
        f"{AUCTION}t1", f"{AUCTION}t2", f"{AUCTION}t3"
    ]


def test_rc08_c6_provider_http_failure_warns_but_digest_and_records_still_happen(
    yahoo_http, seeded_db, sent_emails, db_conn, caplog
):
    import logging

    _add_rate_response(yahoo_http, status=503)
    with caplog.at_level(logging.WARNING, logger="yahoo_scrape"):
        yahoo_scrape.main()
    warnings = [record for record in caplog.records if record.name == "yahoo_scrape"]
    assert len(warnings) == 1
    assert "0.0062" in warnings[0].getMessage()
    assert len(sent_emails) == 1
    # The buy-now price is the payable deal price; the lower current bid remains
    # visible as a provisional yen amount on the same auction row.
    assert "$558.00" in sent_emails[0][1]
    assert "¥50,000" in sent_emails[0][1]
    assert [row[0] for row in db_conn.execute("SELECT url FROM listings ORDER BY url")] == [
        URL_1, URL_4
    ]
    assert len([call for call in yahoo_http.calls if call[1] == FRANKFURTER_URL]) == 3


@pytest.mark.parametrize("status", [400, 500])
def test_rc08_c6_http_error_uses_fallback_without_traceback(status, fake_http, caplog):
    import logging

    fake_http.add("GET", FRANKFURTER_URL, status=status)
    with caplog.at_level(logging.WARNING, logger="yahoo_scrape"):
        assert yahoo_scrape.resolve_yen_to_usd_rate(date(2026, 10, 27)) == 0.0062
    warnings = [record for record in caplog.records if record.name == "yahoo_scrape"]
    assert len(warnings) == 1
    assert warnings[0].exc_info is None


def test_rc08_c5_invalid_json_uses_fallback(fake_http, caplog):
    import logging

    fake_http.add("GET", FRANKFURTER_URL, text="not json")
    with caplog.at_level(logging.WARNING, logger="yahoo_scrape"):
        assert yahoo_scrape.resolve_yen_to_usd_rate(date(2026, 10, 27)) == 0.0062
    assert len([record for record in caplog.records if record.name == "yahoo_scrape"]) == 1


# ---- Pipeline through main() (criteria 39-47) --------------------------------------


def test_c45_inserts_only_the_two_matching_cheap_listings(yahoo_http, seeded_db, sent_emails, db_conn):
    yahoo_scrape.main()
    assert [(r["url"], r["price_yen"]) for r in _rows(db_conn)] == [(URL_1, 50000), (URL_4, 100000)]


def test_rc15_c40_recorded_rows_carry_the_item_brand(yahoo_http, seeded_db, sent_emails, db_conn):
    yahoo_scrape.main()
    stored = db_conn.execute("SELECT url, brand_id FROM listings ORDER BY url").fetchall()
    assert [r["url"] for r in stored] == [URL_1, URL_4]
    assert {r["brand_id"] for r in stored} == {_item("rick owens ramones").brand_id}


def test_c46_one_email_sorted_by_discount_with_percentages(yahoo_http, seeded_db, sent_emails):
    yahoo_scrape.main()
    assert len(sent_emails) == 1
    subject, html, text = sent_emails[0]
    assert re.fullmatch(r"RainCheck: 2 new deals - \d{4}-\d{2}-\d{2}", subject)
    assert _row_urls(html) == [URL_1, URL_4]
    rows = [tr for tr in _soup(html).find_all("tr") if tr.find("td")]
    assert [r.find_all("td")[2].find("strong").get_text() for r in rows] == ["30.2% off", "22.5% off"]
    assert "cheaper than market" not in html and "cheaper than market" not in text
    assert text.index(URL_1) < text.index(URL_4)


def test_c47_second_run_inserts_no_new_rows(yahoo_http, seeded_db, sent_emails, db_conn, caplog):
    """RC-15 criterion 41: the second run sends nothing and says so."""
    caplog.set_level("INFO", logger="yahoo_scrape")
    yahoo_scrape.main()
    first = _rows(db_conn)
    caplog.clear()
    yahoo_scrape.main()
    assert _rows(db_conn) == first
    assert len(first) == 2
    assert len(sent_emails) == 1
    assert yahoo_scrape.NO_DEALS_LINE == "No new deals; no email sent."
    assert any("no new deals" in record.getMessage().lower() for record in caplog.records)


def test_rc07_c24_malformed_fixture_pipeline_rows_and_email(
    fake_http, fixtures_dir, seeded_db, sent_emails, db_conn
):
    fake_http.add("GET", RICK_OWENS_URL, text=_html(fixtures_dir, "brand_page_malformed.html"))
    _add_rate_response(fake_http)
    yahoo_scrape.main()
    rows = {r["url"]: r for r in _rows(db_conn)}
    assert set(rows) == {X1, Z2, X3}
    assert rows[Z2]["price_yen"] == 40000
    assert rows[X3]["image_url"] is None
    assert len(sent_emails) == 1
    html = sent_emails[0][1]
    # The fixed-price flea-market row and the auction's buy-now price are
    # immediately purchasable; the bid-only auction is shown after them as a watch.
    assert _row_urls(html) == [Z2, X1, X3]
    soup = _soup(html)
    assert len(soup.find_all("img")) == 2
    first_cells = [tr.find_all("td")[0] for tr in soup.find_all("tr") if tr.find("td")]
    assert first_cells[0].find("img") is not None and first_cells[1].find("img") is not None
    assert first_cells[2].get_text(strip=True) == "No image"
    assert first_cells[2].find("img") is None
    rendered = soup.get_text(" ", strip=True)
    assert "69.0% off" in rendered and "65.1% off" in rendered
    assert "76.8% (not guaranteed; bids may rise)" in rendered
    assert "84.5% (not guaranteed; bids may rise)" in rendered
    assert "Auction watch - current price (provisional)" in rendered
    assert len([call for call in fake_http.calls if call[1] == FRANKFURTER_URL]) == 1


def test_rc07_c25_null_market_price_skips_item(
    yahoo_http, seeded_db, sent_emails, db_conn, caplog
):
    """RC-15 criterion 46."""
    caplog.set_level("INFO", logger="yahoo_scrape")
    db_conn.execute(
        "UPDATE items SET market_price_usd = NULL, manual_price_usd = NULL WHERE name = ?",
        ("rick owens ramones",),
    )
    db_conn.commit()
    yahoo_scrape.main()
    assert _rows(db_conn) == []
    assert sent_emails == []
    out = "\n".join(record.getMessage() for record in caplog.records)
    assert "rick owens ramones" in out and "no usable market price" in out.lower()
    assert "no new deals" in out.lower()


def test_rc15_c42_deals_are_recorded_only_after_the_send(yahoo_http, seeded_db, monkeypatch):
    seen_during_send = []

    def inspecting_send(subject, html_body, text_body):
        seen_during_send.append(sql_rows(seeded_db, "SELECT url FROM listings"))
        return True

    monkeypatch.setattr(yahoo_scrape, "send_email", inspecting_send)
    yahoo_scrape.main()
    assert seen_during_send == [[]]
    assert [r[0] for r in sql_rows(seeded_db, "SELECT url FROM listings ORDER BY url")] == [URL_1, URL_4]


def test_rc04_c1_successful_digest_records_each_displayed_deals_item_association(
    yahoo_http, seeded_db, sent_emails
):
    yahoo_scrape.main()
    item_id = _item("rick owens ramones").id
    matches = sql_rows(
        seeded_db,
        "SELECT l.url, m.item_id FROM listing_matches m "
        "JOIN listings l ON l.id = m.listing_id ORDER BY l.url",
    )
    assert matches == [(URL_1, item_id), (URL_4, item_id)]
    assert len(sent_emails) == 1


def test_rc04_digest_persists_deal_association_for_each_item(
    fake_http, fixtures_dir, seeded_db, sent_emails
):
    html = _html(fixtures_dir, "brand_page_minimal.html").replace(
        "Rick Owens Geobasket 43", "Rick Owens Pod Shorts 43"
    )
    fake_http.add("GET", RICK_OWENS_URL, text=html)
    _add_rate_response(fake_http)

    yahoo_scrape.main()

    item_ids = {
        "rick owens ramones": _item("rick owens ramones").id,
        "pod shorts": _item("pod shorts").id,
    }
    matches = sql_rows(
        seeded_db,
        "SELECT l.url, m.item_id FROM listing_matches m "
        "JOIN listings l ON l.id = m.listing_id ORDER BY l.url",
    )
    assert matches == [
        (URL_1, item_ids["rick owens ramones"]),
        (URL_3, item_ids["pod shorts"]),
        (URL_4, item_ids["rick owens ramones"]),
    ]
    assert len(sent_emails) == 1


def test_rc04_c5_and_c6_non_deal_is_not_stored_and_later_deal_is_sent(
    yahoo_http, seeded_db, sent_emails, db_conn
):
    db_conn.execute(
        "UPDATE items SET market_price_usd = 100.0 WHERE name = ?",
        ("rick owens ramones",),
    )
    db_conn.commit()
    yahoo_scrape.main()
    assert sql_rows(seeded_db, "SELECT url FROM listings") == []
    assert sql_rows(seeded_db, "SELECT listing_id, item_id FROM listing_matches") == []
    assert sent_emails == []

    db_conn.execute(
        "UPDATE items SET market_price_usd = 800.0 WHERE name = ?",
        ("rick owens ramones",),
    )
    db_conn.commit()
    yahoo_scrape.main()
    assert len(sent_emails) == 1
    assert {row[0] for row in sql_rows(seeded_db, "SELECT url FROM listings")} == {URL_1, URL_4}
    assert len(sql_rows(seeded_db, "SELECT listing_id FROM listing_matches")) == 2


# ---- RC-05 listing lifecycle tracking (criteria 1-5, 9, 11) -------------------------


def _freeze_rc05_clock(monkeypatch, observed_at):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return observed_at.replace(tzinfo=None)
            return observed_at.astimezone(tz)

    monkeypatch.setattr(yahoo_scrape, "datetime", FrozenDateTime)


def _make_existing_rc05_listing(db_conn, url=URL_1):
    item = _item("rick owens ramones")
    db.record_listing_match(
        item.brand_id, url, "Previously stored title", 54321, "https://img/old.jpg", item.id
    )
    db_conn.execute(
        "UPDATE listings SET first_seen_at = ?, last_seen_at = ? WHERE url = ?",
        ("2019-01-02T03:04:05+00:00", "2020-02-03T04:05:06+00:00", url),
    )
    db_conn.commit()


def test_rc05_c1_and_c3_refreshes_known_row_before_email_and_records_only_sent_new_deal(
    fake_http, fixtures_dir, seeded_db, db_conn, sent_emails, monkeypatch
):
    fake_http.add(
        "GET", RICK_OWENS_URL, text=_html(fixtures_dir, "brand_page_minimal.html")
    )
    _add_rate_response(fake_http, published="2035-04-05")
    _make_existing_rc05_listing(db_conn)
    existing_before = dict(
        db_conn.execute("SELECT * FROM listings WHERE url = ?", (URL_1,)).fetchone()
    )
    matches_before = [
        tuple(row)
        for row in db_conn.execute(
            "SELECT listing_id, item_id, matched_at FROM listing_matches ORDER BY listing_id, item_id"
        )
    ]
    observed_at = datetime(2035, 4, 5, 6, 7, 8, tzinfo=timezone.utc)
    _freeze_rc05_clock(monkeypatch, observed_at)
    state_during_send = []

    def inspecting_send(subject, html_body, text_body):
        state_during_send.append(
            db_conn.execute(
                "SELECT last_seen_at FROM listings WHERE url = ?", (URL_1,)
            ).fetchone()[0]
        )
        sent_emails.append((subject, html_body, text_body))
        return True

    monkeypatch.setattr(yahoo_scrape, "send_email", inspecting_send)
    yahoo_scrape.main()

    assert state_during_send == [observed_at.isoformat()]
    existing_after = dict(
        db_conn.execute("SELECT * FROM listings WHERE url = ?", (URL_1,)).fetchone()
    )
    assert existing_after == {**existing_before, "last_seen_at": observed_at.isoformat()}
    assert [
        tuple(row)
        for row in db_conn.execute(
            "SELECT listing_id, item_id, matched_at FROM listing_matches ORDER BY listing_id, item_id"
        )
        if row[0] == existing_after["id"]
    ] == [row for row in matches_before if row[0] == existing_after["id"]]
    stored = {
        row["url"]: row
        for row in db_conn.execute(
            "SELECT l.url, l.id, m.item_id FROM listings l "
            "LEFT JOIN listing_matches m ON m.listing_id = l.id"
        )
    }
    assert set(stored) == {URL_1, URL_4}
    assert stored[URL_4]["item_id"] == _item("rick owens ramones").id
    assert len(sent_emails) == 1
    assert _row_urls(sent_emails[0][1]) == [URL_4]


def test_rc05_c2_refreshes_previous_rows_when_there_are_no_new_deals(
    yahoo_http, fixtures_dir, seeded_db, db_conn, sent_emails, monkeypatch
):
    listings = yahoo_scrape.parse_listings(_html(fixtures_dir, "brand_page_minimal.html"))
    brand_id = _item("rick owens ramones").brand_id
    old = "2020-02-03T04:05:06+00:00"
    for listing in listings:
        db.insert_listing(brand_id, listing.url, listing.title, listing.price_yen, listing.image_url)
    for listing in listings:
        db_conn.execute(
            "UPDATE listings SET last_seen_at = ? WHERE url = ?", (old, listing.url)
        )
    db_conn.commit()
    observed_at = datetime(2035, 4, 5, 6, 7, 8, tzinfo=timezone.utc)
    _freeze_rc05_clock(monkeypatch, observed_at)

    yahoo_scrape.main()

    assert sent_emails == []
    assert db_conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == len(listings)
    assert db_conn.execute("SELECT COUNT(*) FROM listing_matches").fetchone()[0] == 0
    assert {
        row[0] for row in db_conn.execute("SELECT last_seen_at FROM listings")
    } == {observed_at.isoformat()}


def test_rc05_c4_failed_send_refreshes_known_rows_but_leaves_new_deals_for_retry(
    yahoo_http, seeded_db, db_conn, sent_emails, monkeypatch
):
    _make_existing_rc05_listing(db_conn)
    observed_at = datetime(2035, 4, 5, 6, 7, 8, tzinfo=timezone.utc)
    _freeze_rc05_clock(monkeypatch, observed_at)
    monkeypatch.setattr(yahoo_scrape, "send_email", lambda subject, html, text: False)

    with pytest.raises(SystemExit) as exc:
        yahoo_scrape.main()

    assert exc.value.code == 1
    assert db_conn.execute(
        "SELECT last_seen_at FROM listings WHERE url = ?", (URL_1,)
    ).fetchone()[0] == observed_at.isoformat()
    assert [
        row[0] for row in db_conn.execute("SELECT url FROM listings ORDER BY url")
    ] == [URL_1]
    assert db_conn.execute("SELECT COUNT(*) FROM listing_matches").fetchone()[0] == 1

    monkeypatch.setattr(yahoo_scrape, "send_email", lambda subject, html, text: True)
    yahoo_scrape.main()
    assert [
        row[0] for row in db_conn.execute("SELECT url FROM listings ORDER BY url")
    ] == [URL_1, URL_4]
    assert db_conn.execute("SELECT COUNT(*) FROM listing_matches").fetchone()[0] == 2


def test_rc05_c5_and_c11_absent_listing_keeps_ended_fields_and_schema_stays_v3(
    yahoo_http, seeded_db, db_conn, monkeypatch, sent_emails
):
    brand_id = _item("rick owens ramones").brand_id
    absent_url = "https://auctions.yahoo.co.jp/jp/auction/not-on-this-page"
    db.insert_listing(brand_id, absent_url, "Absent", 1000, None)
    db_conn.execute(
        "UPDATE listings SET last_seen_at = ?, ended_at = ?, final_price_yen = ? WHERE url = ?",
        ("2020-02-03T04:05:06+00:00", "2020-02-01T00:00:00+00:00", 12345, absent_url),
    )
    db_conn.commit()
    observed_at = datetime(2035, 4, 5, 6, 7, 8, tzinfo=timezone.utc)
    _freeze_rc05_clock(monkeypatch, observed_at)

    yahoo_scrape.main()

    absent = db_conn.execute(
        "SELECT last_seen_at, ended_at, final_price_yen FROM listings WHERE url = ?",
        (absent_url,),
    ).fetchone()
    assert tuple(absent) == (
        "2020-02-03T04:05:06+00:00",
        "2020-02-01T00:00:00+00:00",
        12345,
    )
    assert db_conn.execute("PRAGMA user_version").fetchone()[0] == 4
    assert tuple(db_conn.execute(
        "SELECT ended_at, final_price_yen FROM listings WHERE url = ?", (URL_4,)
    ).fetchone()) == (None, None)


@pytest.mark.parametrize("failure", ["connection", "http", "malformed"])
def test_rc10_c15_fetch_or_parse_failure_exits_cleanly_without_refresh_or_email(
    failure, fake_http, fixtures_dir, seeded_db, db_conn, sent_emails, monkeypatch
):
    _make_existing_rc05_listing(db_conn)
    prior = "2020-02-03T04:05:06+00:00"
    db_conn.execute("UPDATE listings SET last_seen_at = ? WHERE url = ?", (prior, URL_1))
    db_conn.commit()
    if failure == "connection":
        def fail_request(*args, **kwargs):
            raise requests.ConnectionError("injected fetch failure")

        monkeypatch.setattr(yahoo_scrape.requests, "get", fail_request)
    elif failure == "http":
        fake_http.add("GET", RICK_OWENS_URL, status=503)
    else:
        fake_http.add("GET", RICK_OWENS_URL, text="not a Yahoo results page")

    with pytest.raises(SystemExit) as exc:
        yahoo_scrape.main()
    assert exc.value.code == 1

    assert sent_emails == []
    assert [
        row[0] for row in db_conn.execute("SELECT url FROM listings ORDER BY url")
    ] == [URL_1]
    assert db_conn.execute("SELECT COUNT(*) FROM listing_matches").fetchone()[0] == 1
    assert db_conn.execute(
        "SELECT last_seen_at FROM listings WHERE url = ?", (URL_1,)
    ).fetchone()[0] == prior


def test_rc15_c43_failed_send_exits_1_with_one_error_line_and_records_nothing(
    yahoo_http, seeded_db, monkeypatch, caplog
):
    calls = []

    def failing_send(subject, html_body, text_body):
        calls.append(subject)
        return False

    monkeypatch.setattr(yahoo_scrape, "send_email", failing_send)
    with pytest.raises(SystemExit) as exc:
        yahoo_scrape.main()
    assert exc.value.code == 1
    assert len(calls) == 1
    assert any(record.levelname == "ERROR" for record in caplog.records)
    assert sql_rows(seeded_db, "SELECT url FROM listings") == []
    assert sql_rows(seeded_db, "SELECT listing_id, item_id FROM listing_matches") == []

    delivered = []

    def ok_send(subject, html_body, text_body):
        delivered.append((subject, html_body))
        return True

    monkeypatch.setattr(yahoo_scrape, "send_email", ok_send)
    yahoo_scrape.main()
    assert len(delivered) == 1
    assert _row_urls(delivered[0][1]) == [URL_1, URL_4]
    assert len(sql_rows(seeded_db, "SELECT url FROM listings")) == 2
    assert len(sql_rows(seeded_db, "SELECT listing_id, item_id FROM listing_matches")) == 2


def test_rc15_c43_failed_send_message_uses_singular_for_one_deal(
    yahoo_http, seeded_db, monkeypatch, caplog
):
    sql_run(seeded_db, "UPDATE items SET manual_price_usd = 400.0 WHERE name = ?", ("rick owens ramones",))
    monkeypatch.setattr(yahoo_scrape, "send_email", lambda s, h, t: False)
    with pytest.raises(SystemExit) as exc:
        yahoo_scrape.main()
    assert exc.value.code == 1
    assert any(record.levelname == "ERROR" for record in caplog.records)


def test_rc15_c44_a_listing_stored_by_an_earlier_run_is_not_shown(
    yahoo_http, seeded_db, sent_emails
):
    db.insert_listing(_item("rick owens ramones").brand_id, URL_1, "Rick Owens Ramones High Top 42", 50000, None)
    yahoo_scrape.main()
    assert len(sent_emails) == 1
    assert sent_emails[0][0].startswith("RainCheck: 1 new deal - ")
    assert _row_urls(sent_emails[0][1]) == [URL_4]


def test_rc15_c45_top_five_per_item_end_to_end_and_leftovers_next_run(
    yahoo_http, seeded_db, sent_emails, monkeypatch
):
    monkeypatch.setattr(yahoo_scrape, "parse_listings", lambda html: seven_ramones_listings())
    listings = seven_ramones_listings()
    yahoo_scrape.main()
    assert len(sent_emails) == 1
    subject, html, text = sent_emails[0]
    assert subject.startswith("RainCheck: 5 new deals - ")
    assert _row_urls(html) == [l.url for l in listings[:5]]
    assert "and 2 more new deals not shown" in html
    assert "and 2 more new deals not shown" in text
    stored = [r[0] for r in sql_rows(seeded_db, "SELECT url FROM listings ORDER BY url")]
    assert stored == sorted(l.url for l in listings[:5])

    yahoo_scrape.main()
    assert len(sent_emails) == 2
    subject2, html2, text2 = sent_emails[1]
    assert subject2.startswith("RainCheck: 2 new deals - ")
    assert _row_urls(html2) == [l.url for l in listings[5:]]
    assert "not shown" not in html2 and "not shown" not in text2


def test_rc15_c39_pipeline_main_prints_no_per_deal_console_lines(
    yahoo_http, seeded_db, sent_emails, capsys
):
    yahoo_scrape.main()
    out = capsys.readouterr().out
    assert "Title:" not in out and "Product Link:" not in out and "--- Yahoo Auctions Update" not in out


def test_c14_main_works_from_unrelated_cwd_and_emails_once_per_item(
    yahoo_http, test_catalog, seeded_db, sent_emails, monkeypatch, tmp_path, caplog
):
    """RC-15 criterion 39: one digest for all items."""
    monkeypatch.chdir(tmp_path)
    yahoo_scrape.main()
    assert len(sent_emails) == 1
    subject, html, _text = sent_emails[0]
    assert re.fullmatch(r"RainCheck: 2 new deals - \d{4}-\d{2}-\d{2}", subject)
    soup = _soup(html)
    assert [h.get_text() for h in soup.find_all("h2")] == [
        "rick owens ramones (market $800.00)"
    ]
    assert _row_urls(html) == [URL_1, URL_4]
    yahoo_calls = [call for call in yahoo_http.calls if call[1] == RICK_OWENS_URL]
    assert len(yahoo_calls) == 1
    method, url, kwargs = yahoo_calls[0]
    assert (method, url) == ("GET", RICK_OWENS_URL)
    assert kwargs.get("timeout") == 30
    assert len([call for call in yahoo_http.calls if call[1] == FRANKFURTER_URL]) == 1


def test_rc15_c50_dry_run_end_to_end_prints_whole_digest_and_records(
    yahoo_http, seeded_db, monkeypatch, capsys, caplog
):
    import smtplib
    caplog.set_level("INFO", logger="yahoo_scrape")

    def boom(*args, **kwargs):
        raise AssertionError("smtplib.SMTP must not be constructed in dry run")

    monkeypatch.setattr(smtplib, "SMTP", boom)
    monkeypatch.setenv("RAINCHECK_DRY_RUN", "1")
    yahoo_scrape.main()
    out = capsys.readouterr().out
    assert "[DRY RUN] Email not sent" in out
    assert re.search(r"^Subject: RainCheck: 2 new deals - \d{4}-\d{2}-\d{2}$", out, re.MULTILINE)
    assert "--- text/plain ---" in out
    assert "--- text/html ---" in out
    assert URL_1 in out
    assert [r[0] for r in sql_rows(seeded_db, "SELECT url FROM listings ORDER BY url")] == [URL_1, URL_4]

    yahoo_scrape.main()
    assert any("no new deals" in record.getMessage().lower() for record in caplog.records)


def test_rc15_c41_empty_yahoo_page_sends_nothing(
    fake_http, fixtures_dir, seeded_db, sent_emails, monkeypatch, caplog
):
    caplog.set_level("INFO", logger="yahoo_scrape")
    fake_http.add("GET", RICK_OWENS_URL, text=_html(fixtures_dir, "brand_page_minimal.html"))
    monkeypatch.setattr(yahoo_scrape, "parse_listings", lambda html: [])
    yahoo_scrape.main()
    assert sent_emails == []
    assert any("no new deals" in record.getMessage().lower() for record in caplog.records)
    assert sql_rows(seeded_db, "SELECT url FROM listings") == []
    assert all(call[1] != FRANKFURTER_URL for call in fake_http.calls)


# ---- RC-03b item-based pure rules moved to find_item_deals: see criteria 3, 4, 8 ----


def test_rc03b_c9_exclude_keywords_from_the_item_apply(ramones_item, minimal_listings):
    """RC-15 criterion 3."""
    item = dataclasses.replace(ramones_item, exclude_keywords=("high",))
    deals = yahoo_scrape.find_item_deals(item, minimal_listings, set(), 0.0062)
    assert [d.url for d in deals] == [URL_4]


def test_rc03b_c10_brand_comes_from_the_item_not_a_name_lookup(ramones_item, minimal_listings):
    """RC-15 criterion 8."""
    item = dataclasses.replace(ramones_item, brand="Not In Database")
    deals = yahoo_scrape.find_item_deals(item, minimal_listings, set(), 0.0062)
    assert [d.url for d in deals] == [URL_1, URL_4]
    assert {d.brand_id for d in deals} == {ramones_item.brand_id}


def test_rc03b_c11_effective_price_of_the_item_drives_the_deal_math(
    seeded_db, ramones_item, minimal_listings
):
    """RC-15 criterion 4."""
    db.update_market_price(ramones_item.id, 400.0)
    item = _item("rick owens ramones")
    assert item.effective_price_usd == 400.0
    deals = yahoo_scrape.find_item_deals(item, minimal_listings, set(), 0.0062)
    assert [d.url for d in deals] == [URL_1]
    assert deals[0].discount_pct == pytest.approx(22.5)
    assert deals[0].kind == "auction_watch"


def test_rc03b_c12_item_without_usable_price_is_skipped_with_a_line(
    yahoo_http, seeded_db, sent_emails, db_conn, caplog
):
    """RC-15 criterion 46."""
    caplog.set_level("INFO", logger="yahoo_scrape")
    db_conn.execute(
        "UPDATE items SET market_price_usd = NULL, manual_price_usd = NULL WHERE name = ?",
        ("rick owens ramones",),
    )
    db_conn.commit()
    yahoo_scrape.main()
    assert _rows(db_conn) == []
    assert sent_emails == []
    assert any("no usable market price" in record.getMessage().lower() for record in caplog.records)


def test_rc06_c24_yahoo_job_rejects_fake_titles_for_both_seeded_items(
    yahoo_http, seeded_db, sent_emails, monkeypatch, db_conn
):
    fake_titles = [
        inline_listing(81, "Rick Owens Fake Ramones High", yen=10000),
        inline_listing(82, "Rick Owens Fake Pod Shorts", yen=10000),
    ]
    monkeypatch.setattr(yahoo_scrape, "parse_listings", lambda html: fake_titles)

    yahoo_scrape.main()

    assert sent_emails == []
    assert db_conn.execute("SELECT COUNT(*) FROM listing_matches").fetchone()[0] == 0


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
    with pytest.raises(SystemExit) as exc:
        yahoo_scrape.main()
    assert exc.value.code == 1
    assert sent_emails == []
    assert _rows(db_conn) == []
    assert all(call[1] != FRANKFURTER_URL for call in fake_http.calls)


def test_rc07_c29_old_layout_page_raises_with_no_email_and_no_rows(
    fake_http, test_catalog, seeded_db, sent_emails, db_conn
):
    old = '<html><body><h3>Rick Owens Ramones</h3><span class="Product__priceValue u-textRed">1円</span></body></html>'
    fake_http.add("GET", RICK_OWENS_URL, text=old)
    with pytest.raises(SystemExit) as exc:
        yahoo_scrape.main()
    assert exc.value.code == 1
    assert sent_emails == []
    assert _rows(db_conn) == []
    assert all(call[1] != FRANKFURTER_URL for call in fake_http.calls)


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


def test_rc03b_c14_main_skips_inactive_items(yahoo_http, seeded_db, sent_emails, monkeypatch):
    """RC-15 criterion 48: find_item_deals is spied, since pod shorts has no deals anyway."""
    sql_run(seeded_db, "UPDATE items SET active = 0 WHERE name = ?", ("pod shorts",))
    real = yahoo_scrape.find_item_deals
    called_for = []

    def spy(item, listings, skip_urls, yen_to_usd):
        called_for.append(item.name)
        return real(item, listings, skip_urls, yen_to_usd)

    monkeypatch.setattr(yahoo_scrape, "find_item_deals", spy)
    yahoo_scrape.main()
    assert called_for == ["rick owens ramones"]
    assert len(sent_emails) == 1
    assert len([call for call in yahoo_http.calls if call[1] == RICK_OWENS_URL]) == 1
    assert len([call for call in yahoo_http.calls if call[1] == FRANKFURTER_URL]) == 1


def test_rc03b_c15_main_does_not_read_catalog_json(
    yahoo_http, seeded_db, sent_emails, monkeypatch, tmp_path
):
    """RC-15 criterion 49."""
    monkeypatch.setattr(db, "CATALOG_PATH", tmp_path / "does_not_exist.json")
    yahoo_scrape.main()
    assert len(sent_emails) == 1
    assert sent_emails[0][0].startswith("RainCheck: 2 new deals - ")
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


def test_rc10_c26_yahoo_no_active_items_is_a_successful_no_work_run(
    temp_db, fake_http, sent_emails, caplog
):
    caplog.set_level("INFO", logger="yahoo_scrape")
    result = yahoo_scrape.main()
    assert result in (None, 0)
    assert any("no active items" in record.getMessage().lower() for record in caplog.records)
    assert fake_http.calls == []
    assert sent_emails == []


def test_rc03b_c19_main_on_newer_schema_version_is_a_clean_error(
    temp_db, fake_http, sent_emails, capsys
):
    set_user_version(temp_db, 5)
    before = sha256(temp_db)
    with pytest.raises(SystemExit) as exc:
        yahoo_scrape.main()
    _assert_clean_schema_error(capsys, fake_http, sent_emails, exc)
    assert sha256(temp_db) == before


def test_rc03b_c21_yahoo_job_uses_the_shared_matcher():
    import matching

    assert yahoo_scrape.title_matches is matching.title_matches


def test_rc15_c54_report_item_deals_is_gone():
    import inspect

    assert not hasattr(yahoo_scrape, "report_item_deals")
    assert yahoo_scrape.YEN_TO_USD == 0.0062
    source = inspect.getsource(yahoo_scrape)
    assert "Daily Refresh" not in source
    assert '"<br>".join' not in source


# ---- Amendment 1: unencodable text (lone surrogates) and insert failure after send ---


def _surrogate_page(bad_title=None, bad_image=None):
    """A results page with one valid Ramones deal and one record carrying a lone surrogate.

    json.dumps escapes the surrogate as the six characters \\ud800, which is how Yahoo's JSON
    could carry it; json.loads turns that back into a str that cannot be encoded as UTF-8.
    """
    import json

    good = {"auctionId": "t1000000001", "title": "Rick Owens Ramones High Top 42",
            "price": 50000, "isFixedPrice": True, "buyNowPrice": 50000,
            "imageUrl": "https://auc-pctr.c.yimg.jp/i/test/1.jpg"}
    bad = {"auctionId": "t1000000002", "title": bad_title or "Rick Owens Ramones Bad 44",
           "price": 20000, "imageUrl": bad_image or "https://auc-pctr.c.yimg.jp/i/test/2.jpg"}
    data = {"props": {"pageProps": {"initialState": {"search": {"items": {"listing": {
        "items": [good, bad]}}}}}}}
    raw = json.dumps(data)
    assert "\\ud800" in raw
    return (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        + raw + "</script></body></html>"
    )


@pytest.mark.parametrize(
    "kwargs",
    [{"bad_title": "Rick Owens Ramones \ud800 Bad"}, {"bad_image": "https://img.test/\ud800.jpg"}],
    ids=["title", "image_url"],
)
def test_rc15_c57_lone_surrogate_makes_the_record_invalid(kwargs, caplog):
    import logging

    with caplog.at_level(logging.DEBUG):
        listings = yahoo_scrape.parse_listings(_surrogate_page(**kwargs))
    assert [l.url for l in listings] == [URL_1]
    warnings = [r for r in caplog.records if r.name == "yahoo_scrape" and r.levelno >= logging.WARNING]
    assert [w.getMessage() for w in warnings] == [
        "Skipped 1 of 2 Yahoo listing records: 1 invalid, 0 duplicate"
    ]
    for l in listings:
        l.title.encode("utf-8")
        if l.image_url is not None:
            l.image_url.encode("utf-8")


@pytest.mark.parametrize(
    "kwargs",
    [{"bad_title": "Rick Owens Ramones \ud800 Bad"}, {"bad_image": "https://img.test/\ud800.jpg"}],
    ids=["title", "image_url"],
)
def test_rc15_c57_main_sends_one_digest_with_the_valid_deal(kwargs, fake_http, seeded_db, sent_emails):
    fake_http.add("GET", RICK_OWENS_URL, text=_surrogate_page(**kwargs))
    _add_rate_response(fake_http)
    yahoo_scrape.main()
    assert len(sent_emails) == 1
    subject, html, text = sent_emails[0]
    assert subject.startswith("RainCheck: 1 new deal - ")
    assert _row_urls(html) == [URL_1]
    html.encode("utf-8")
    text.encode("utf-8")
    assert [r[0] for r in sql_rows(seeded_db, "SELECT url FROM listings")] == [URL_1]


def test_rc10_c21_c22_batch_failure_rolls_back_then_next_run_resends_all_deals(
    yahoo_http, seeded_db, monkeypatch
):
    delivered = []

    def ok_send(subject, html_body, text_body):
        delivered.append(html_body)
        return True

    monkeypatch.setattr(yahoo_scrape, "send_email", ok_send)
    real_record_matches = db.record_listing_matches
    calls = []

    def fail_first_batch(deals, *, sources_by_url=None):
        calls.append(tuple(deals))
        if len(calls) == 1:
            raise RuntimeError("database is locked")
        if sources_by_url is None:
            return real_record_matches(deals)
        return real_record_matches(deals, sources_by_url=sources_by_url)

    monkeypatch.setattr(db, "record_listing_matches", fail_first_batch)
    with pytest.raises(SystemExit) as exc:
        yahoo_scrape.main()
    assert exc.value.code == 1
    assert len(delivered) == 1
    assert [r[0] for r in sql_rows(seeded_db, "SELECT url FROM listings")] == []

    yahoo_scrape.main()
    assert len(delivered) == 2
    assert _row_urls(delivered[1]) == [URL_1, URL_4]
    assert _row_urls(delivered[0]) == _row_urls(delivered[1])
    assert [r[0] for r in sql_rows(seeded_db, "SELECT url FROM listings ORDER BY url")] == [URL_1, URL_4]


# ---- RC-10 reliability and logging -------------------------------------------------

def test_rc10_c14_yahoo_page_retries_shared_fetch_once_and_records_one_digest(
    fake_http, fixtures_dir, seeded_db, sent_emails, db_conn, monkeypatch
):
    fake_http.add_sequence("GET", RICK_OWENS_URL, [
        {"status": 503},
        {"text": _html(fixtures_dir, "brand_page_minimal.html")},
    ])
    _add_rate_response(fake_http)
    waits = []
    import http_retry
    original = http_retry.request_with_retry
    monkeypatch.setattr(http_retry, "request_with_retry", lambda *a, **kw: original(*a, sleep=waits.append, **kw))
    original_parse = yahoo_scrape.parse_listings
    parsed = []
    def record_parse(html):
        parsed.append(html)
        return original_parse(html)
    monkeypatch.setattr(yahoo_scrape, "parse_listings", record_parse)

    yahoo_scrape.main()

    assert [call[1] for call in fake_http.calls].count(RICK_OWENS_URL) == 2
    assert waits == [0.5]
    assert len(parsed) == 1 and parsed[0]
    assert len(sent_emails) == 1
    assert db_conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 2


@pytest.mark.parametrize("failure", ["final_503", "malformed"])
def test_rc10_c15_yahoo_shared_page_failure_has_no_send_or_listing_writes(
    failure, fake_http, seeded_db, sent_emails, db_conn, monkeypatch, capsys
):
    item = _item("rick owens ramones")
    db.record_listing_match(item.brand_id, URL_1, "Stored listing", 50000, None, item.id)
    old_seen = "2026-10-20T00:00:00+00:00"
    db_conn.execute("UPDATE listings SET last_seen_at = ? WHERE url = ?", (old_seen, URL_1))
    db_conn.commit()
    parsed = []
    original_parse = yahoo_scrape.parse_listings
    monkeypatch.setattr(yahoo_scrape, "parse_listings", lambda html: parsed.append(html) or original_parse(html))
    if failure == "final_503":
        fake_http.add_sequence("GET", RICK_OWENS_URL, [
            {"status": 503}, {"status": 503}, {"status": 503},
        ])
    else:
        fake_http.add("GET", RICK_OWENS_URL, text="<html><body>unexpected page</body></html>")
    with pytest.raises(SystemExit) as exc:
        yahoo_scrape.main()
    assert exc.value.code == 1
    assert sent_emails == []
    assert "Traceback" not in capsys.readouterr().err
    assert db_conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 1
    assert db_conn.execute("SELECT COUNT(*) FROM listing_matches").fetchone()[0] == 1
    assert db_conn.execute("SELECT last_seen_at FROM listings WHERE url = ?", (URL_1,)).fetchone()[0] == old_seen
    if failure == "malformed":
        assert [call[1] for call in fake_http.calls].count(RICK_OWENS_URL) == 1
        assert len(parsed) == 1
    else:
        assert [call[1] for call in fake_http.calls].count(RICK_OWENS_URL) == 3
        assert parsed == []


def test_rc10_c17_malformed_200_page_is_not_retried(
    fake_http, seeded_db, sent_emails
):
    fake_http.add("GET", RICK_OWENS_URL, text="<html><body>unexpected page</body></html>")
    with pytest.raises(SystemExit) as exc:
        yahoo_scrape.main()
    assert exc.value.code == 1
    assert len([call for call in fake_http.calls if call[1] == RICK_OWENS_URL]) == 1
    assert sent_emails == []


def test_rc10_c7_yahoo_transport_failure_sends_nothing_and_does_not_refresh_last_seen(
    seeded_db, sent_emails, monkeypatch, db_conn
):
    item = _item("rick owens ramones")
    db.record_listing_match(item.brand_id, URL_1, "Existing listing", 50000, None, item.id)
    old = "2026-10-20T00:00:00+00:00"
    db_conn.execute("UPDATE listings SET last_seen_at = ? WHERE url = ?", (old, URL_1))
    db_conn.commit()

    def transport_error(*args, **kwargs):
        raise requests.Timeout("offline")

    monkeypatch.setattr(requests, "get", transport_error)
    with pytest.raises(SystemExit) as exc:
        yahoo_scrape.main()
    assert exc.value.code == 1
    assert sent_emails == []
    assert db_conn.execute("SELECT last_seen_at FROM listings WHERE url = ?", (URL_1,)).fetchone()[0] == old


def test_rc10_c16_rate_retry_uses_provider_without_fallback_warning(fake_http, caplog, monkeypatch):
    fake_http.add_sequence("GET", FRANKFURTER_URL, [
        {"status": 500},
        {"json": {"date": "2026-10-27", "base": "JPY", "quote": "USD", "rate": 0.007}},
    ])
    import http_retry
    original = http_retry.request_with_retry
    waits = []
    monkeypatch.setattr(http_retry, "request_with_retry", lambda *a, **kw: original(*a, sleep=waits.append, **kw))
    assert yahoo_scrape.resolve_yen_to_usd_rate(date(2026, 10, 27)) == 0.007
    assert waits == [0.5]
    assert not [record for record in caplog.records if "fallback" in record.getMessage().lower()]


def test_rc10_c16_huge_retry_after_uses_frankfurter_fallback_without_overflow(fake_http, caplog):
    import logging

    fake_http.add_sequence("GET", FRANKFURTER_URL, [
        {"status": 429, "headers": {"Retry-After": "9" * 400}},
    ])
    with caplog.at_level(logging.WARNING, logger="yahoo_scrape"):
        assert yahoo_scrape.resolve_yen_to_usd_rate(date(2026, 10, 27)) == 0.0062
    assert len([call for call in fake_http.calls if call[1] == FRANKFURTER_URL]) == 1
    warnings = [record for record in caplog.records if record.name == "yahoo_scrape"]
    assert len(warnings) == 1
    assert "0.0062" in warnings[0].getMessage()
    assert warnings[0].exc_info is None


def test_rc10_c18_c19_item_calculation_error_does_not_claim_url_and_keeps_one_digest(
    yahoo_http, seeded_db, sent_emails, monkeypatch, db_conn, caplog
):
    brand_id = db_conn.execute(
        "SELECT id FROM brands WHERE name = ?", ("Rick Owens",)
    ).fetchone()[0]
    db_conn.execute(
        "INSERT INTO items (brand_id, name, search_query, manual_price_usd, active) "
        "VALUES (?, ?, ?, ?, 1)", (brand_id, "test boots", "boots", 500.0),
    )
    boots_id = db_conn.execute(
        "SELECT id FROM items WHERE name = ?", ("test boots",)
    ).fetchone()[0]
    db_conn.execute(
        "INSERT INTO item_keywords (item_id, keyword, kind) VALUES (?, ?, ?)",
        (boots_id, "boots", "include"),
    )
    db_conn.commit()

    monkeypatch.setattr(yahoo_scrape, "parse_listings", lambda html: [
        _fixed_listing("ramones", "https://auctions.yahoo.co.jp/jp/auction/ramones",
                       "Rick Owens Ramones High", 50000),
        _fixed_listing("boots", "https://auctions.yahoo.co.jp/jp/auction/boots",
                       "Rick Owens Black Boots", 40000),
    ])
    original = yahoo_scrape.find_item_deals

    def fail_middle(item, *args, **kwargs):
        if item.name == "pod shorts":
            raise ValueError("bad calculation")
        return original(item, *args, **kwargs)

    monkeypatch.setattr(yahoo_scrape, "find_item_deals", fail_middle)
    with pytest.raises(SystemExit) as exc:
        yahoo_scrape.main()
    assert exc.value.code == 1
    assert len(sent_emails) == 1
    assert _row_urls(sent_emails[0][1]) == [
        "https://auctions.yahoo.co.jp/jp/auction/ramones",
        "https://auctions.yahoo.co.jp/jp/auction/boots",
    ]
    assert [tuple(row) for row in db_conn.execute(
        "SELECT item_id FROM listing_matches ORDER BY item_id"
    )] == [
        (_item("rick owens ramones").id,),
        (boots_id,),
    ]
    assert db_conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 2
    assert any(record.levelname == "ERROR" for record in caplog.records)


def test_rc10_c19_failed_earlier_item_does_not_claim_listing_from_later_healthy_item(
    yahoo_http, seeded_db, sent_emails, monkeypatch, db_conn, caplog
):
    shared_url = "https://auctions.yahoo.co.jp/jp/auction/shared"
    monkeypatch.setattr(yahoo_scrape, "parse_listings", lambda html: [
        _fixed_listing("shared", shared_url, "Rick Owens Ramones Pod Shorts", 10000),
    ])
    original = yahoo_scrape.find_item_deals

    def fail_first_item(item, *args, **kwargs):
        if item.name == "rick owens ramones":
            raise ValueError("bad calculation")
        return original(item, *args, **kwargs)

    monkeypatch.setattr(yahoo_scrape, "find_item_deals", fail_first_item)
    with pytest.raises(SystemExit) as exc:
        yahoo_scrape.main()

    assert exc.value.code == 1
    assert len(sent_emails) == 1
    assert _row_urls(sent_emails[0][1]) == [shared_url]
    assert [tuple(row) for row in db_conn.execute(
        "SELECT item_id FROM listing_matches"
    )] == [(_item("pod shorts").id,)]
    assert any(record.levelname == "ERROR" for record in caplog.records)


def test_rc10_c20_unsuccessful_send_records_no_deals_and_exits_failure(
    yahoo_http, seeded_db, monkeypatch, db_conn, caplog
):
    sent = []
    monkeypatch.setattr(yahoo_scrape, "send_email", lambda *args: sent.append(args) or False)
    with pytest.raises(SystemExit) as exc:
        yahoo_scrape.main()
    assert exc.value.code == 1
    assert len(sent) == 1
    assert db_conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 0
    assert any(record.levelname == "ERROR" for record in caplog.records)


def test_rc10_c21_atomic_batch_failure_rolls_back_every_displayed_deal(
    yahoo_http, seeded_db, monkeypatch, db_conn, caplog
):
    calls = []

    def fail_batch(deals, *, sources_by_url=None):
        calls.append(tuple(deals))
        raise RuntimeError("forced batch failure")

    monkeypatch.setattr(db, "record_listing_matches", fail_batch, raising=False)
    with pytest.raises(SystemExit) as exc:
        yahoo_scrape.main()
    assert exc.value.code == 1
    assert len(calls) == 1 and len(calls[0]) == 2
    assert db_conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 0
    assert db_conn.execute("SELECT COUNT(*) FROM listing_matches").fetchone()[0] == 0
    assert any(record.levelname == "ERROR" for record in caplog.records)


def test_rc10_c22_failed_batch_can_be_offered_again_on_next_run(
    yahoo_http, seeded_db, sent_emails, monkeypatch, db_conn
):
    real_batch = getattr(db, "record_listing_matches", None)
    attempts = []

    def fail_once(deals, *, sources_by_url=None):
        attempts.append(tuple(deals))
        if len(attempts) == 1:
            raise RuntimeError("forced batch failure")
        if sources_by_url is None:
            return real_batch(deals)
        return real_batch(deals, sources_by_url=sources_by_url)

    monkeypatch.setattr(db, "record_listing_matches", fail_once, raising=False)
    with pytest.raises(SystemExit):
        yahoo_scrape.main()
    yahoo_scrape.main()
    assert len(sent_emails) == 2
    assert _row_urls(sent_emails[0][1]) == _row_urls(sent_emails[1][1])


def test_rc10_c24_dry_run_keeps_full_output_and_records_displayed_deals(
    yahoo_http, seeded_db, monkeypatch, db_conn, capsys
):
    monkeypatch.setenv("RAINCHECK_DRY_RUN", "1")
    yahoo_scrape.main()
    output = capsys.readouterr().out
    assert "[DRY RUN] Email not sent" in output
    assert "--- text/plain ---" in output and "--- text/html ---" in output
    assert db_conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 2


def test_rc10_c27_yahoo_main_reuses_preinstalled_logging_without_duplicate_handlers(
    temp_db, caplog
):
    import logging

    root = logging.getLogger()
    before_handlers = tuple(root.handlers)
    before_level = root.level
    with caplog.at_level(logging.INFO):
        for _ in range(2):
            try:
                yahoo_scrape.main()
            except SystemExit as exc:
                assert exc.code == 0
    assert tuple(root.handlers) == before_handlers
    assert root.level == before_level
    records = [r for r in caplog.records if r.name == "yahoo_scrape"]
    assert records
    assert len([r for r in records if "job started" in r.getMessage().lower()]) == 2


def test_rc10_c25_yahoo_logs_exclude_seller_titles_urls_and_email_body(
    yahoo_http, seeded_db, sent_emails, caplog
):
    import logging

    with caplog.at_level(logging.INFO, logger="yahoo_scrape"):
        yahoo_scrape.main()
    records = [r for r in caplog.records if r.name == "yahoo_scrape"]
    assert records and all(r.levelno >= logging.INFO for r in records)
    rendered = "\n".join(r.getMessage() for r in records)
    assert "Rick Owens Ramones High" not in rendered
    assert RICK_OWENS_URL not in rendered
    assert sent_emails[0][1] not in rendered
