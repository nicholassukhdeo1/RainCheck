import importlib
import json

import pytest
import requests

import db
from conftest import FakeResponse
from dbhelpers import run as sql_run, sha256

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
    assert prices == {"rick owens ramones": 900.0, "pod shorts": None}
    assert db.get_market_price("Rick Owens", "pod shorts") == 300.0


def test_c44_main_requests_exactly_one_token(ebay_pricing, creds, ebay_http, seeded_db):
    ebay_pricing.main()
    assert len(_token_posts(ebay_http)) == 1


# ---- RC-03b: the job reads the database (criteria 23-35) --------------------


NO_ITEMS_LINE = "No active items in the database. Add items to catalog.json, then run python3 src/db.py --seed."


class QueryHTTP:
    """Per-query eBay fake: token POST, and a GET answered by params["q"]."""

    def __init__(self, token):
        self.token = token
        self.responses = {}
        self.calls = []

    def add(self, query, json_data, status=200):
        self.responses[query] = (json_data, status)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return FakeResponse(json_data=self.token)

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        query = kwargs["params"]["q"]
        if query not in self.responses:
            raise AssertionError(f"Unexpected eBay query: {query!r}")
        data, status = self.responses[query]
        return FakeResponse(json_data=data, status=status)

    def queries(self):
        return [c[2]["params"]["q"] for c in self.calls if c[0] == "GET"]


@pytest.fixture
def query_http(monkeypatch, fixtures_dir):
    fake = QueryHTTP(_load(fixtures_dir, "token.json"))
    monkeypatch.setattr(requests, "post", fake.post)
    monkeypatch.setattr(requests, "get", fake.get)
    return fake


def _summaries(*pairs):
    return {"itemSummaries": [{"title": t, "price": {"value": v}} for t, v in pairs]}


POD_SHORTS = _summaries(
    ("Rick Owens Pod Shorts Black", "200.00"),
    ("Rick Owens Pod Shorts Dust", "300.00"),
    ("Rick Owens Pod Short Pants Hybrid", "900.00"),
)


def _stored(db_conn):
    return {
        r["name"]: r["market_price_usd"]
        for r in db_conn.execute("SELECT name, market_price_usd FROM items")
    }


def test_rc03b_c22_ebay_market_price_keeps_four_argument_signature(ebay_pricing):
    import inspect

    assert list(inspect.signature(ebay_pricing.ebay_market_price).parameters) == [
        "query", "keywords", "exclude_keywords", "token",
    ]


def test_rc03b_c23_search_uses_each_items_search_query_in_order(
    ebay_pricing, creds, ebay_http, seeded_db
):
    ebay_pricing.refresh_market_prices()
    qs = [c[2]["params"]["q"] for c in ebay_http.calls if c[0] == "GET"]
    assert qs == ["rick owens ramones", "pod shorts"]


def test_rc03b_c24_null_search_query_falls_back_to_item_name(
    ebay_pricing, creds, ebay_http, seeded_db, capsys
):
    sql_run(seeded_db, "UPDATE items SET search_query = 'ramones high' WHERE name = 'rick owens ramones'")
    sql_run(seeded_db, "UPDATE items SET search_query = NULL WHERE name = 'pod shorts'")
    ebay_pricing.refresh_market_prices()
    qs = [c[2]["params"]["q"] for c in ebay_http.calls if c[0] == "GET"]
    assert qs == ["ramones high", "pod shorts"]
    out = capsys.readouterr().out
    assert 'Pricing rick owens ramones with eBay query "ramones high"' in out
    assert 'Pricing pod shorts with eBay query "pod shorts"' in out


def _ramones_and_pods(query_http, fixtures_dir, ramones=None, pods=POD_SHORTS, pods_status=200):
    query_http.add("rick owens ramones", ramones or _load(fixtures_dir, "search_ramones.json"))
    query_http.add("pod shorts", pods, status=pods_status)


def test_rc03b_c25_database_excludes_apply_per_item(
    ebay_pricing, creds, query_http, fixtures_dir, seeded_db, db_conn
):
    _ramones_and_pods(query_http, fixtures_dir)
    ebay_pricing.refresh_market_prices()
    assert _stored(db_conn) == {"rick owens ramones": 900.0, "pod shorts": 250.0}


def test_rc03b_c26_inactive_items_are_not_searched(
    ebay_pricing, creds, ebay_http, seeded_db, db_conn
):
    sql_run(seeded_db, "UPDATE items SET active = 0 WHERE name = ?", ("pod shorts",))
    ebay_pricing.refresh_market_prices()
    assert len(_token_posts(ebay_http)) == 1
    assert len([c for c in ebay_http.calls if c[0] == "GET"]) == 1
    assert _stored(db_conn)["pod shorts"] is None


@pytest.mark.parametrize("bad", ["0.00", "-1.00"])
def test_rc03b_c27_unusable_median_is_skipped_and_run_continues(
    ebay_pricing, creds, query_http, fixtures_dir, seeded_db, db_conn, capsys, bad
):
    ramones = _load(fixtures_dir, "search_ramones.json")
    for s in ramones["itemSummaries"]:
        s["price"]["value"] = bad
    _ramones_and_pods(query_http, fixtures_dir, ramones=ramones)
    ebay_pricing.refresh_market_prices()
    stored = _stored(db_conn)
    assert stored["rick owens ramones"] is None
    assert stored["pod shorts"] == 250.0
    assert "eBay median for rick owens ramones is not a usable price" in capsys.readouterr().out


def test_rc03b_c28_no_matching_listings_prints_skip_line_and_keeps_price(
    ebay_pricing, creds, query_http, fixtures_dir, seeded_db, db_conn, capsys
):
    _ramones_and_pods(query_http, fixtures_dir, pods=_summaries(("Rick Owens Geobasket", "500.00")))
    ebay_pricing.refresh_market_prices()
    assert "No matching eBay listings for pod shorts; market price not updated." in capsys.readouterr().out
    assert _stored(db_conn)["pod shorts"] is None


def test_rc03b_c29_http_error_stops_run_but_keeps_earlier_prices(
    ebay_pricing, creds, query_http, fixtures_dir, seeded_db, db_conn
):
    _ramones_and_pods(query_http, fixtures_dir, pods={"errors": []}, pods_status=500)
    with pytest.raises(requests.HTTPError):
        ebay_pricing.refresh_market_prices()
    assert _stored(db_conn)["rick owens ramones"] == 900.0


def _assert_clean_schema_error(capsys, fake_http, exc, needle="python3 src/db.py"):
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert len(err.strip().splitlines()) == 1
    assert err.startswith("Error: ")
    assert needle in err
    assert "Traceback" not in err
    assert fake_http.calls == []


def test_rc03b_c30_main_on_legacy_db_is_a_clean_error_before_any_network(
    ebay_pricing, legacy_db, fake_http, capsys
):
    before = sha256(legacy_db)
    with pytest.raises(SystemExit) as exc:
        ebay_pricing.main()
    _assert_clean_schema_error(capsys, fake_http, exc)
    assert sha256(legacy_db) == before


def test_rc03b_c31_main_with_no_database_file_is_a_clean_error_and_creates_nothing(
    ebay_pricing, fake_http, capsys, tmp_path
):
    path = tmp_path / "test.db"
    assert not path.exists()
    with pytest.raises(SystemExit) as exc:
        ebay_pricing.main()
    _assert_clean_schema_error(capsys, fake_http, exc, "python3 src/db.py --seed")
    assert not path.exists()


def test_rc03b_c32_no_items_prints_one_line_and_makes_no_http_call(
    ebay_pricing, creds, temp_db, fake_http, capsys
):
    ebay_pricing.refresh_market_prices()
    assert NO_ITEMS_LINE in capsys.readouterr().out
    assert fake_http.calls == []


def test_rc03b_c33_refresh_does_not_read_catalog_json(
    ebay_pricing, creds, ebay_http, seeded_db, db_conn, monkeypatch, tmp_path
):
    monkeypatch.setattr(db, "CATALOG_PATH", tmp_path / "does_not_exist.json")
    ebay_pricing.refresh_market_prices()
    assert _stored(db_conn)["rick owens ramones"] == 900.0
    assert not hasattr(ebay_pricing, "load_catalog")
    assert not hasattr(ebay_pricing, "get_item_id")


def test_rc03b_c34_every_ebay_request_has_a_30_second_timeout(
    ebay_pricing, creds, ebay_http, seeded_db
):
    ebay_pricing.refresh_market_prices()
    assert len(ebay_http.calls) == 3
    for method, url, kwargs in ebay_http.calls:
        assert kwargs.get("timeout") == 30, (method, url)


def test_rc03b_c35_ebay_job_uses_the_shared_matcher(ebay_pricing):
    import matching

    assert ebay_pricing.title_matches is matching.title_matches
