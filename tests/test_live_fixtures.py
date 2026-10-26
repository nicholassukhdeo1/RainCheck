import json

import pytest
from bs4 import BeautifulSoup

HINT = "Live fixture missing: {path}. Run `python3 scripts/capture_fixtures.py` from the repository root."


def _require(path):
    if not path.exists():
        pytest.fail(HINT.format(path=path), pytrace=False)
    return path


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="RC-07: Yahoo's Next.js redesign removed the Product__* classes, so the scraper selectors match nothing",
)
def test_c48_live_yahoo_page_has_all_four_selectors(fixtures_dir):
    path = _require(fixtures_dir / "yahoo" / "brand_page_live.html")
    soup = BeautifulSoup(path.read_bytes(), "html.parser")
    assert soup.find_all("h3")
    assert soup.find_all("span", class_="Product__priceValue u-textRed")
    assert soup.find_all("a", class_="Product__titleLink js-browseHistory-add js-rapid-override")
    assert soup.find_all("img", class_="Product__imageData")


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
