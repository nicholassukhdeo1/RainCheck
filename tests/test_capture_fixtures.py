import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "capture_fixtures.py"


@pytest.fixture(scope="module")
def capture():
    # scripts/ is not a package, so load the script by file path.
    spec = importlib.util.spec_from_file_location("capture_fixtures_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _page(data, before="<html><body><p>hi</p>", after="</body></html>"):
    blob = json.dumps(data, ensure_ascii=False)
    return f'{before}<script id="__NEXT_DATA__" type="application/json">{blob}</script>{after}'


def _extract(html):
    start = html.index('<script id="__NEXT_DATA__"')
    start = html.index(">", start) + 1
    end = html.index("</script>", start)
    return html[start:end]


def test_c54_missing_next_data_block_raises(capture):
    with pytest.raises(RuntimeError):
        capture.strip_yahoo_seller_data("<html><body>nothing here</body></html>")


def test_c54_invalid_json_block_raises(capture):
    html = '<html><script id="__NEXT_DATA__" type="application/json">{not json</script></html>'
    with pytest.raises(RuntimeError):
        capture.strip_yahoo_seller_data(html)


def test_c54_script_closing_tag_in_title_stays_escaped(capture):
    title = "Rick </script><b>Owens"
    # Next.js escapes "<" as \u003c inside the JSON; build the page that way.
    raw_json = json.dumps({"props": {"items": [{"auctionId": "a1", "title": title}]}}).replace(
        "<", "\\u003c"
    )
    assert "</script>" not in raw_json
    html = (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        + raw_json
        + "</script></body></html>"
    )
    out = capture.strip_yahoo_seller_data(html)
    assert "\\u003c/script>" in out
    assert out.count("</script>") == 1
    parsed = json.loads(_extract(out))
    assert parsed["props"]["items"][0]["title"] == title


def test_c54_seller_and_prefecture_removed_only_from_auction_objects(capture):
    data = {
        "props": {
            "items": [
                {"auctionId": "a1", "title": "t", "seller": {"userId": "u1", "displayName": "d1"},
                 "prefectureCode": "13", "price": 100},
            ],
            "viewer": {"seller": {"note": "keep"}, "prefectureCode": "27"},
        }
    }
    out = capture.strip_yahoo_seller_data(_page(data))
    parsed = json.loads(_extract(out))
    item = parsed["props"]["items"][0]
    assert "seller" not in item and "prefectureCode" not in item
    assert item["auctionId"] == "a1" and item["price"] == 100
    assert parsed["props"]["viewer"] == {"seller": {"note": "keep"}, "prefectureCode": "27"}


def test_c54_set_cookies_values_are_emptied(capture):
    data = {
        "props": {"setCookies": ["bid=secret"], "pageProps": {"nested": {"setCookies": ["x=y", "z=w"]}}},
        "items": [{"auctionId": "a1"}],
    }
    out = capture.strip_yahoo_seller_data(_page(data))
    assert "secret" not in out
    parsed = json.loads(_extract(out))
    assert parsed["props"]["setCookies"] == []
    assert parsed["props"]["pageProps"]["nested"]["setCookies"] == []


@pytest.mark.parametrize("field,value", [("userId", "seller-user-123"), ("displayName", "Taro Seller")])
def test_c54_seller_value_leaking_outside_json_raises(capture, field, value):
    data = {"items": [{"auctionId": "a1", "seller": {"userId": "seller-user-123", "displayName": "Taro Seller"}}]}
    html = _page(data, before=f"<html><body><a href='/user/{value}'>{value}</a>")
    with pytest.raises(RuntimeError):
        capture.strip_yahoo_seller_data(html)


def test_c54_clean_page_is_preserved_outside_json(capture):
    data = {"items": [{"auctionId": "a1", "seller": {"userId": "u1", "displayName": "d1"}}]}
    html = _page(data, before="<html><body><p>keep me</p>", after="<footer>end</footer></body></html>")
    out = capture.strip_yahoo_seller_data(html)
    assert out.startswith("<html><body><p>keep me</p>")
    assert out.endswith("<footer>end</footer></body></html>")
