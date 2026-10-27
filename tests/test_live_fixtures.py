import json
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

HINT = "Live fixture missing: {path}. Run `python3 scripts/capture_fixtures.py` from the repository root."


def _require(path):
    if not path.exists():
        pytest.fail(HINT.format(path=path), pytrace=False)
    return path


def _live_html(fixtures_dir):
    return _require(fixtures_dir / "yahoo" / "brand_page_live.html").read_text(encoding="utf-8")


def _raw_live_records(html):
    soup = BeautifulSoup(html, "html.parser")
    node = json.loads(soup.find("script", id="__NEXT_DATA__").string)
    for key in ("props", "pageProps", "initialState", "search", "items", "listing", "items"):
        node = node[key]
    return node


def test_rc07_c30_live_page_parses_every_record_without_warning(fixtures_dir, caplog):
    import logging

    import yahoo_scrape

    html = _live_html(fixtures_dir)
    raw = _raw_live_records(html)
    assert raw, "live fixture has no records (test would be vacuous)"
    with caplog.at_level(logging.DEBUG):
        listings = yahoo_scrape.parse_listings(html)
    assert len(listings) == len(raw)
    assert len({l.auction_id for l in listings}) == len(listings)
    assert [r for r in caplog.records if r.name == "yahoo_scrape" and r.levelno >= logging.WARNING] == []


def test_rc07_c31_live_urls_match_json_ld_item_list_in_order(fixtures_dir):
    import yahoo_scrape

    html = _live_html(fixtures_dir)
    soup = BeautifulSoup(html, "html.parser")
    item_list = None
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string)
        except (TypeError, ValueError):
            continue
        if isinstance(data, dict) and data.get("@type") == "ItemList":
            item_list = data
            break
    assert item_list is not None, "no JSON-LD ItemList in the live fixture"
    expected = [e["url"] for e in item_list["itemListElement"]]
    assert expected, "JSON-LD list is empty (test would be vacuous)"
    assert [l.url for l in yahoo_scrape.parse_listings(html)] == expected


def test_rc07_c32_live_listings_have_sane_fields(fixtures_dir):
    import yahoo_scrape

    listings = yahoo_scrape.parse_listings(_live_html(fixtures_dir))
    assert listings
    for l in listings:
        assert l.title and l.title.strip()
        assert isinstance(l.price_yen, int) and not isinstance(l.price_yen, bool)
        assert l.price_yen >= 1
        assert l.image_url is None or l.image_url.startswith("https://")


def test_c49_live_ebay_search_has_expected_shape_and_no_seller(fixtures_dir):
    path = _require(fixtures_dir / "ebay" / "search_live.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    summaries = data["itemSummaries"]
    assert isinstance(summaries, list)
    for entry in summaries:
        assert isinstance(entry["title"], str)
        if "price" in entry:
            float(entry["price"]["value"])
            assert entry["price"]["currency"] == "USD"
        assert "seller" not in entry
        assert "itemLocation" not in entry


def test_c50_missing_live_fixture_fails_with_capture_instructions(tmp_path):
    with pytest.raises(pytest.fail.Exception) as excinfo:
        _require(tmp_path / "nope.json")
    assert "python3 scripts/capture_fixtures.py" in str(excinfo.value)


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def test_c53_live_yahoo_next_data_parses_and_has_no_seller_or_cookie_data(fixtures_dir):
    path = _require(fixtures_dir / "yahoo" / "brand_page_live.html")
    soup = BeautifulSoup(path.read_bytes(), "html.parser")
    script = soup.find("script", id="__NEXT_DATA__")
    assert script is not None, "no <script id=__NEXT_DATA__> block in the live Yahoo fixture"
    data = json.loads(script.string)
    nodes = list(_walk(data))
    assert [d for d in nodes if "auctionId" in d], "no object with an auctionId key found (test would be vacuous)"
    for d in nodes:
        assert "seller" not in d
        assert "prefectureCode" not in d
        if "setCookies" in d:
            assert not d["setCookies"]


def test_rc15_c52_digest_from_live_page_round_trips_real_image_urls(fixtures_dir):
    from datetime import date

    import yahoo_scrape
    from db import TrackedItem
    from digest import build_digest

    catalog_path = Path(__file__).resolve().parent.parent / "src" / "catalog.json"
    entry = [e for e in json.loads(catalog_path.read_text(encoding="utf-8"))
             if e["item name"] == "rick owens ramones"][0]
    item = TrackedItem(
        id=1, brand_id=1, brand=entry["brand"], name=entry["item name"],
        search_query=entry["search_query"],
        keywords=tuple(sorted(entry["keywords"])),
        exclude_keywords=tuple(sorted(entry["exclude_keywords"])),
        manual_price_usd=100000.0, market_price_usd=None,
    )
    listings = yahoo_scrape.parse_listings(_live_html(fixtures_dir))
    sections = yahoo_scrape.build_sections([item], listings, set(), 0.0062)
    digest = build_digest(sections, date(2026, 10, 26))
    assert digest is not None and digest.deals, "live fixture produced no deals (test would be vacuous)"
    soup = BeautifulSoup(digest.html, "html.parser")
    srcs = [img["src"] for img in soup.find_all("img")]
    assert srcs == [d.image_url for d in digest.deals]
    assert all("&" in s for s in srcs)
    hrefs = [a["href"] for a in soup.find_all("a")]
    assert hrefs and all(h.startswith("https://") for h in hrefs)
