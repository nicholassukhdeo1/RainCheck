"""RC-07 unit tests for parse_listings and its page-level errors (criteria 1-19)."""
import json
import logging

import pytest

import yahoo_scrape

LOGGER = "yahoo_scrape"
LISTING_PATH_DOTTED = "props.pageProps.initialState.search.items.listing.items"
AUCTION = "https://auctions.yahoo.co.jp/jp/auction/"
FLEA = "https://paypayfleamarket.yahoo.co.jp/item/"


def _read(fixtures_dir, name):
    return (fixtures_dir / "yahoo" / name).read_text(encoding="utf-8")


def _shell(script_body):
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>'
        f'<script id="__NEXT_DATA__" type="application/json">{script_body}</script>'
        "</body></html>"
    )


def _page_with_items(items):
    data = {"props": {"pageProps": {"initialState": {"search": {"items": {"listing": {"items": items}}}}}}}
    return _shell(json.dumps(data))


def _yahoo_warnings(caplog):
    return [r for r in caplog.records if r.name == LOGGER and r.levelno >= logging.WARNING]


@pytest.fixture
def minimal_html(fixtures_dir):
    return _read(fixtures_dir, "brand_page_minimal.html")


@pytest.fixture
def malformed_html(fixtures_dir):
    return _read(fixtures_dir, "brand_page_malformed.html")


# ---- Parsing: the minimal fixture -------------------------------------------------


def test_rc07_c1_minimal_fixture_returns_four_listings_in_order(minimal_html):
    listings = yahoo_scrape.parse_listings(minimal_html)
    assert len(listings) == 4
    got = [(l.auction_id, l.url, l.title, l.price_yen, l.image_url) for l in listings]
    assert got == [
        ("t1000000001", AUCTION + "t1000000001", "Rick Owens Ramones High Top 42", 50000,
         "https://auc-pctr.c.yimg.jp/i/auctions.c.yimg.jp/images.auctions.yahoo.co.jp/image/dr000/auc0210/users/45a6ce28d3cf2f865f425e68b2890ed9a088ceac/i-img100x100-1792990662638j63eeo-1.jpg"),
        ("t1000000002", AUCTION + "t1000000002", "Rick Owens Ramones Leather 44", 200000,
         "https://auc-pctr.c.yimg.jp/i/auctions.c.yimg.jp/images.auctions.yahoo.co.jp/image/dr000/auc0210/users/45a6ce28d3cf2f865f425e68b2890ed9a088ceac/i-img100x100-1792990662638j63eeo-2.jpg"),
        ("t1000000003", AUCTION + "t1000000003", "Rick Owens Geobasket 43", 10000,
         "https://auc-pctr.c.yimg.jp/i/auctions.c.yimg.jp/images.auctions.yahoo.co.jp/image/dr000/auc0210/users/45a6ce28d3cf2f865f425e68b2890ed9a088ceac/i-img100x100-1792990662638j63eeo-3.jpg"),
        ("t1000000004", AUCTION + "t1000000004", "Rick Owens Ramon Sneakers", 100000,
         "https://auc-pctr.c.yimg.jp/i/auctions.c.yimg.jp/images.auctions.yahoo.co.jp/image/dr000/auc0210/users/45a6ce28d3cf2f865f425e68b2890ed9a088ceac/i-img100x100-1792990662638j63eeo-4.jpg"),
    ]


def test_rc07_c1_listing_is_a_yahoolisting_and_frozen(minimal_html):
    listing = yahoo_scrape.parse_listings(minimal_html)[0]
    assert isinstance(listing, yahoo_scrape.YahooListing)
    with pytest.raises(Exception):
        listing.price_yen = 1


def test_rc07_c2_price_not_buy_now_price_is_used(minimal_html):
    assert yahoo_scrape.parse_listings(minimal_html)[0].price_yen == 50000


def test_rc07_c3_minimal_fixture_logs_no_warning(minimal_html, caplog):
    with caplog.at_level(logging.DEBUG):
        yahoo_scrape.parse_listings(minimal_html)
    assert _yahoo_warnings(caplog) == []


# ---- Parsing: record-level problems (malformed fixture) ---------------------------


def test_rc07_c4_malformed_fixture_returns_five_listings_in_order(malformed_html):
    ids = [l.auction_id for l in yahoo_scrape.parse_listings(malformed_html)]
    assert ids == ["x1000000001", "z1000000002", "x1000000003", "x1000000004", "2000000018"]


def test_rc07_c5_flea_market_item_gets_flea_market_url(malformed_html):
    urls = {l.auction_id: l.url for l in yahoo_scrape.parse_listings(malformed_html)}
    assert urls["z1000000002"] == FLEA + "z1000000002"
    for auction_id in ("x1000000001", "x1000000003", "x1000000004", "2000000018"):
        assert urls[auction_id] == AUCTION + auction_id


def test_rc07_c6_title_stripped_and_first_duplicate_wins(malformed_html):
    by_id = {l.auction_id: l for l in yahoo_scrape.parse_listings(malformed_html)}
    first = by_id["x1000000001"]
    assert first.title == "Rick Owens Ramones Mixed 42"
    assert first.price_yen == 30000


def test_rc07_c7_missing_or_null_image_becomes_none(malformed_html):
    by_id = {l.auction_id: l for l in yahoo_scrape.parse_listings(malformed_html)}
    assert by_id["x1000000003"].image_url is None
    assert by_id["x1000000004"].image_url is None
    assert by_id["x1000000001"].image_url == "https://auc-pctr.c.yimg.jp/i/auctions.c.yimg.jp/images.auctions.yahoo.co.jp/image/dr000/auc0210/users/45a6ce28d3cf2f865f425e68b2890ed9a088ceac/i-img100x100-1792990662638j63eeo-m1.jpg"


def test_rc07_c8_escaped_script_tag_and_japanese_decode(malformed_html):
    by_id = {l.auction_id: l for l in yahoo_scrape.parse_listings(malformed_html)}
    assert by_id["x1000000004"].title == "Rick Owens </script> テスト"
    assert by_id["z1000000002"].title == "Rick Owens Ramones フリマ 43"


def test_rc07_c9_exactly_one_summary_warning(malformed_html, caplog):
    with caplog.at_level(logging.DEBUG):
        yahoo_scrape.parse_listings(malformed_html)
    warnings = _yahoo_warnings(caplog)
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING
    assert warnings[0].getMessage() == "Skipped 13 of 18 Yahoo listing records: 12 invalid, 1 duplicate"


def test_rc07_c10_no_invalid_record_in_result(malformed_html):
    listings = yahoo_scrape.parse_listings(malformed_html)
    ids = {l.auction_id for l in listings}
    for bad in ["", "../evil"] + [f"x10000000{n:02d}" for n in range(8, 15)]:
        assert bad not in ids
    assert len(listings) == 5
    for l in listings:
        assert l.price_yen >= 1
        assert l.title.strip() == l.title and l.title
        assert "/" not in l.auction_id


# ---- Parsing: page-level problems -------------------------------------------------


def test_rc07_c11_old_layout_without_next_data_raises(fixtures_dir):
    old = '<html><body><ul><li class="Product"><h3>x</h3><span class="Product__priceValue">1円</span></li></ul></body></html>'
    with pytest.raises(yahoo_scrape.YahooPageError, match="__NEXT_DATA__"):
        yahoo_scrape.parse_listings(old)


def test_rc07_c12_empty_string_raises():
    with pytest.raises(yahoo_scrape.YahooPageError):
        yahoo_scrape.parse_listings("")


@pytest.mark.parametrize("body", ["", "   ", "\n\t  \n"])
def test_rc07_c13_blank_next_data_raises(body):
    with pytest.raises(yahoo_scrape.YahooPageError):
        yahoo_scrape.parse_listings(_shell(body))


def test_rc07_c14_truncated_json_raises_with_decode_error_cause():
    with pytest.raises(yahoo_scrape.YahooPageError) as excinfo:
        yahoo_scrape.parse_listings(_shell('{"props":'))
    assert isinstance(excinfo.value.__cause__, json.JSONDecodeError)


def test_rc07_c15_missing_items_key_names_the_listing_path():
    data = {"props": {"pageProps": {"initialState": {"search": {}}}}}
    with pytest.raises(yahoo_scrape.YahooPageError) as excinfo:
        yahoo_scrape.parse_listings(_shell(json.dumps(data)))
    assert LISTING_PATH_DOTTED in str(excinfo.value)


def test_rc07_c15_only_the_pageprops_path_is_used():
    # An identical copy exists at props.initialState; the parser must not fall back to it.
    data = {"props": {"initialState": {"search": {"items": {"listing": {"items": [
        {"auctionId": "a1", "title": "t", "price": 5}]}}}}}}
    with pytest.raises(yahoo_scrape.YahooPageError):
        yahoo_scrape.parse_listings(_shell(json.dumps(data)))


def test_rc07_c16_top_level_list_raises():
    with pytest.raises(yahoo_scrape.YahooPageError):
        yahoo_scrape.parse_listings(_shell("[]"))


def test_rc07_c17_object_instead_of_list_raises():
    with pytest.raises(yahoo_scrape.YahooPageError):
        yahoo_scrape.parse_listings(_page_with_items({"not": "a list"}))


def test_rc07_c18_empty_array_returns_empty_list_and_no_warning(caplog):
    with caplog.at_level(logging.DEBUG):
        result = yahoo_scrape.parse_listings(_page_with_items([]))
    assert result == []
    assert _yahoo_warnings(caplog) == []


def test_rc07_c19_all_invalid_records_raise_with_counts():
    items = [{"auctionId": f"a{n}", "title": "Rick Owens Ramones"} for n in range(3)]
    with pytest.raises(yahoo_scrape.YahooPageError, match="0 of 3"):
        yahoo_scrape.parse_listings(_page_with_items(items))


# ---- Amendment 1: empty array must agree with the page's own result count ----------


def _page_with_listing_parent(items, **parent_fields):
    listing = {"items": items, **parent_fields}
    data = {"props": {"pageProps": {"initialState": {"search": {"items": {"listing": listing}}}}}}
    return _shell(json.dumps(data))


@pytest.mark.parametrize(
    "parent_fields",
    [{"totalResultsAvailable": 5}, {"isFetching": True}, {"totalResultsAvailable": 5, "isFetching": True}],
)
def test_rc07_c36_empty_array_but_page_reports_results_raises(parent_fields):
    with pytest.raises(yahoo_scrape.YahooPageError):
        yahoo_scrape.parse_listings(_page_with_listing_parent([], **parent_fields))


@pytest.mark.parametrize(
    "parent_fields",
    [{"totalResultsAvailable": 0}, {"totalResultsAvailable": 0, "isFetching": False}, {"isFetching": False}],
)
def test_rc07_c36_empty_array_and_page_agrees_no_results_returns_empty(parent_fields, caplog):
    with caplog.at_level(logging.DEBUG):
        result = yahoo_scrape.parse_listings(_page_with_listing_parent([], **parent_fields))
    assert result == []
    assert _yahoo_warnings(caplog) == []


def test_rc07_c37_full_width_auction_id_is_skipped_as_invalid():
    items = [
        {"auctionId": "ａ１２", "title": "Rick Owens Ramones", "price": 100},
        {"auctionId": "a12", "title": "Rick Owens Ramones", "price": 100},
    ]
    result = yahoo_scrape.parse_listings(_page_with_items(items))
    assert [l.auction_id for l in result] == ["a12"]


@pytest.mark.parametrize("flag", ["true", 1])
def test_rc07_c37_non_boolean_flea_market_flag_gets_auction_url(flag):
    items = [{"auctionId": "z123", "title": "Rick Owens Ramones", "price": 100, "isFleamarketItem": flag}]
    result = yahoo_scrape.parse_listings(_page_with_items(items))
    assert [l.url for l in result] == [AUCTION + "z123"]
