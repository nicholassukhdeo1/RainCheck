import json

import pytest
from bs4 import BeautifulSoup

HINT = "Live fixture missing: {path}. Run `python3 scripts/capture_fixtures.py` from the repository root."


def _require(path):
    if not path.exists():
        pytest.fail(HINT.format(path=path), pytrace=False)
    return path


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


def test_c50_missing_live_fixture_fails_with_capture_instructions(tmp_path):
    with pytest.raises(pytest.fail.Exception) as excinfo:
        _require(tmp_path / "nope.json")
    assert "python3 scripts/capture_fixtures.py" in str(excinfo.value)
