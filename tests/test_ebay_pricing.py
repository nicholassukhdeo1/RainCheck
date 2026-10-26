import importlib
import json

import pytest
import requests

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
EXCLUDES = ["drkshdw", "canvas", "low"]


@pytest.fixture
def ebay_pricing():
    # Imported lazily so an import-time KeyError fails each test individually
    # instead of aborting collection of the whole suite.
    return importlib.import_module("ebay_pricing")


def _load(fixtures_dir, name):
    return json.loads((fixtures_dir / "ebay" / name).read_text(encoding="utf-8"))


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("EBAY_CLIENT_ID", "id")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "secret")


@pytest.fixture
def ebay_http(fake_http, fixtures_dir):
    fake_http.add("POST", TOKEN_URL, json=_load(fixtures_dir, "token.json"))
    fake_http.add("GET", SEARCH_URL, json=_load(fixtures_dir, "search_ramones.json"))
    return fake_http


def _price(ebay_pricing, keywords, excludes=EXCLUDES):
    return ebay_pricing.ebay_market_price("rick owens ramones", keywords, excludes, "tok")


def _token_posts(fake_http):
    return [c for c in fake_http.calls if c[0] == "POST" and c[1] == TOKEN_URL]


# --- Token ------------------------------------------------------------------


def test_c33_get_ebay_token_sends_basic_auth_and_returns_token(ebay_pricing, creds, ebay_http):
    assert ebay_pricing.get_ebay_token() == "test-token-not-real"
    posts = _token_posts(ebay_http)
    assert len(posts) == 1
    kwargs = posts[0][2]
    assert kwargs["headers"]["Authorization"] == "Basic aWQ6c2VjcmV0"
    assert kwargs["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert kwargs["data"] == {
        "grant_type": "client_credentials",
        "scope": "https://api.ebay.com/oauth/api_scope",
    }


def test_c34_get_ebay_token_raises_http_error_on_401(ebay_pricing, creds, fake_http):
    fake_http.add("POST", TOKEN_URL, json={"error": "invalid_client"}, status=401)
    with pytest.raises(requests.HTTPError):
        ebay_pricing.get_ebay_token()


# --- Median -----------------------------------------------------------------


def test_c35_median_with_includes_and_excludes(ebay_pricing, ebay_http):
    assert _price(ebay_pricing, ["ramones", "ramon"]) == 900.0


def test_c36_search_request_shape(ebay_pricing, ebay_http):
    _price(ebay_pricing, ["ramones", "ramon"])
    gets = [c for c in ebay_http.calls if c[0] == "GET"]
    assert len(gets) == 1
    assert gets[0][1] == SEARCH_URL
    kwargs = gets[0][2]
    assert kwargs["params"] == {"q": "rick owens ramones", "limit": 50}
    assert kwargs["headers"]["Authorization"] == "Bearer tok"
    assert kwargs["headers"]["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_US"


def test_c37_even_count_median_and_case_insensitive_title(ebay_pricing, ebay_http):
    assert _price(ebay_pricing, ["ramones"]) == 800.0


def test_c38_without_excludes_low_and_canvas_rows_are_kept(ebay_pricing, ebay_http):
    assert _price(ebay_pricing, ["ramon"], excludes=[]) == 700.0


def test_c39_listing_matching_include_and_exclude_is_excluded(ebay_pricing, ebay_http):
    # Row 3 ("Ramones Low") contains an include (ramones) and an exclude (low).
    # Keeping it would give median(700, 900, 300) = 700 instead of 800.
    # "drkshdw" is also excluded so the only variable is row 3.
    assert _price(ebay_pricing, ["ramones"], excludes=["low", "drkshdw"]) == 800.0


def test_c40_everything_excluded_returns_none(ebay_pricing, ebay_http):
    assert _price(ebay_pricing, ["geobasket"], excludes=["geobasket"]) is None


def test_c41_missing_item_summaries_key_returns_none(ebay_pricing, fake_http, fixtures_dir):
    fake_http.add("GET", SEARCH_URL, json=_load(fixtures_dir, "search_empty.json"))
    assert _price(ebay_pricing, ["ramones"]) is None


def test_c42_empty_item_summaries_list_returns_none(ebay_pricing, fake_http):
    fake_http.add("GET", SEARCH_URL, json={"total": 0, "itemSummaries": []})
    assert _price(ebay_pricing, ["ramones"]) is None


# --- Refresh and main -------------------------------------------------------


def test_c13_refresh_works_from_unrelated_cwd(
    ebay_pricing, creds, ebay_http, seeded_db, db_conn, monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    ebay_pricing.refresh_market_prices()
    row = db_conn.execute("SELECT market_price_usd FROM items WHERE name='rick owens ramones'").fetchone()
    assert row["market_price_usd"] == 900.0


def test_c43_refresh_one_token_one_search_per_item_and_updates_prices(
    ebay_pricing, creds, ebay_http, seeded_db, db_conn
):
    ebay_pricing.refresh_market_prices()
    kinds = [(m, u) for m, u, _ in ebay_http.calls]
    assert kinds == [("POST", TOKEN_URL), ("GET", SEARCH_URL), ("GET", SEARCH_URL)]
    prices = {
        r["name"]: r["market_price_usd"]
        for r in db_conn.execute("SELECT name, market_price_usd FROM items")
    }
    assert prices == {"rick owens ramones": 900.0, "pod shorts": 300.0}


def test_c44_main_requests_exactly_one_token(ebay_pricing, creds, ebay_http, seeded_db):
    ebay_pricing.main()
    assert len(_token_posts(ebay_http)) == 1
