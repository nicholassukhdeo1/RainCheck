import importlib
import json
import math
import pytest
import requests

import db
from conftest import FakeResponse
from dbhelpers import run as sql_run, sha256

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
EXCLUDES = ["drkshdw", "canvas", "low"]
RAMONES_KEYWORDS = ["ramones", "ramon"]


@pytest.fixture
def ebay_pricing():
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


def _listing(item_id, title="Rick Owens Ramones High", value="100", currency="USD"):
    return {"itemId": item_id, "title": title, "price": {"value": value, "currency": currency}}


def _summaries(*listings):
    return {"itemSummaries": list(listings)}


def _valid_prices(values, prefix="item"):
    return [_listing(f"{prefix}-{i}", value=value) for i, value in enumerate(values)]


def _price(ebay_pricing, fake_http, response, keywords=RAMONES_KEYWORDS, excludes=EXCLUDES):
    fake_http.add("GET", SEARCH_URL, json=response)
    return ebay_pricing.ebay_market_price("rick owens ramones", keywords, excludes, "tok")


def _stored(db_conn):
    return {
        row["name"]: row["market_price_usd"]
        for row in db_conn.execute("SELECT name, market_price_usd FROM items")
    }


class QueryHTTP:
    """Offline eBay fake which selects its response by the requested search query."""

    def __init__(self, token):
        self.token = token
        self.responses = {}
        self.calls = []

    def add(self, query, json_data, status=200, error=None):
        self.responses[query] = (json_data, status, error)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return FakeResponse(json_data=self.token)

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        query = kwargs["params"]["q"]
        if query not in self.responses:
            raise AssertionError(f"Unexpected eBay query: {query!r}")
        data, status, error = self.responses[query]
        if error:
            raise error
        return FakeResponse(json_data=data, status=status)

    def queries(self):
        return [call[2]["params"]["q"] for call in self.calls if call[0] == "GET"]


@pytest.fixture
def query_http(monkeypatch, fixtures_dir):
    fake = QueryHTTP(_load(fixtures_dir, "token.json"))
    monkeypatch.setattr(requests, "post", fake.post)
    monkeypatch.setattr(requests, "get", fake.get)
    return fake


def _five_pod_listings():
    return _summaries(
        _listing("pod-1", "Rick Owens Pod Shorts Black", "200"),
        _listing("pod-2", "Rick Owens Pod Shorts Dust", "250"),
        _listing("pod-3", "Rick Owens Pod Shorts Grey", "300"),
        _listing("pod-4", "Rick Owens Pod Shorts Blue", "350"),
        _listing("pod-5", "Rick Owens Pod Shorts Brown", "400"),
        _listing("pod-excluded", "Rick Owens Pod Short Pants Hybrid", "900"),
    )


# --- Credentials and request contract --------------------------------------


def test_c33_get_ebay_token_sends_basic_auth_and_returns_token(ebay_pricing, creds, fake_http, fixtures_dir):
    fake_http.add("POST", TOKEN_URL, json=_load(fixtures_dir, "token.json"))
    assert ebay_pricing.get_ebay_token() == "test-token-not-real"
    method, url, kwargs = fake_http.calls[0]
    assert (method, url) == ("POST", TOKEN_URL)
    assert kwargs["headers"]["Authorization"] == "Basic aWQ6c2VjcmV0"
    assert kwargs["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert kwargs["data"] == {
        "grant_type": "client_credentials",
        "scope": "https://api.ebay.com/oauth/api_scope",
    }
    assert kwargs["timeout"] == 30


def test_rc09_c12_search_keeps_one_page_request_shape_and_timeout(ebay_pricing, fake_http):
    result = _price(ebay_pricing, fake_http, _summaries(*_valid_prices([100, 110, 120, 130, 140])))
    calls = [call for call in fake_http.calls if call[0] == "GET"]
    assert len(calls) == 1
    _, url, kwargs = calls[0]
    assert url == SEARCH_URL
    assert kwargs["params"] == {"q": "rick owens ramones", "limit": 50}
    assert kwargs["headers"]["Authorization"] == "Bearer tok"
    assert kwargs["headers"]["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_US"
    assert kwargs["timeout"] == 30
    assert result.price_usd == 120.0


def test_c34_get_ebay_token_raises_http_error_on_401(ebay_pricing, creds, fake_http):
    fake_http.add("POST", TOKEN_URL, json={"error": "invalid_client"}, status=401)
    with pytest.raises(requests.HTTPError):
        ebay_pricing.get_ebay_token()


# --- Existing include/exclude and empty-search behavior --------------------


def _fixture_price(ebay_pricing, fake_http, fixtures_dir, keywords, excludes=EXCLUDES, fixture="search_ramones.json"):
    return _price(ebay_pricing, fake_http, _load(fixtures_dir, fixture), keywords, excludes)


def test_c35_median_with_includes_and_excludes(ebay_pricing, ebay_http, fixtures_dir):
    estimate = _fixture_price(ebay_pricing, ebay_http, fixtures_dir, ["ramones", "ramon"])
    assert estimate.price_usd == 900.0
    assert (estimate.candidate_count, estimate.outlier_count, estimate.retained_count) == (6, 0, 6)


def test_c36_search_request_shape(ebay_pricing, ebay_http, fixtures_dir):
    _fixture_price(ebay_pricing, ebay_http, fixtures_dir, ["ramones", "ramon"])
    gets = [call for call in ebay_http.calls if call[0] == "GET"]
    assert len(gets) == 1
    assert gets[0][1] == SEARCH_URL
    kwargs = gets[0][2]
    assert kwargs["params"] == {"q": "rick owens ramones", "limit": 50}
    assert kwargs["headers"]["Authorization"] == "Bearer tok"
    assert kwargs["headers"]["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_US"


def test_c37_case_insensitive_title_matching(ebay_pricing, ebay_http, fixtures_dir):
    estimate = _fixture_price(ebay_pricing, ebay_http, fixtures_dir, ["ramones"])
    assert estimate.price_usd == 900.0
    assert (estimate.candidate_count, estimate.outlier_count, estimate.retained_count) == (5, 0, 5)


def test_c38_without_excludes_low_and_canvas_rows_are_candidates(ebay_pricing, ebay_http, fixtures_dir):
    estimate = _fixture_price(ebay_pricing, ebay_http, fixtures_dir, ["ramon"], excludes=[])
    assert estimate.price_usd == 900.0
    assert (estimate.candidate_count, estimate.outlier_count, estimate.retained_count) == (8, 2, 6)


def test_c39_listing_include_and_exclude_behavior_is_preserved(ebay_pricing, ebay_http, fixtures_dir):
    estimate = _fixture_price(
        ebay_pricing, ebay_http, fixtures_dir, ["ramones"], excludes=["low", "drkshdw"]
    )
    assert estimate.price_usd == 900.0
    assert (estimate.candidate_count, estimate.outlier_count, estimate.retained_count) == (5, 0, 5)


def test_c40_everything_excluded_returns_empty_estimate(ebay_pricing, ebay_http, fixtures_dir):
    estimate = _fixture_price(ebay_pricing, ebay_http, fixtures_dir, ["geobasket"], excludes=["geobasket"])
    assert estimate == ebay_pricing.PriceEstimate(None, 0, 0, 0)


@pytest.mark.parametrize("fixture", ["search_empty.json"])
def test_c41_missing_item_summaries_key_returns_empty_estimate(ebay_pricing, fake_http, fixtures_dir, fixture):
    estimate = _fixture_price(ebay_pricing, fake_http, fixtures_dir, ["ramones"], fixture=fixture)
    assert estimate == ebay_pricing.PriceEstimate(None, 0, 0, 0)


def test_c42_empty_item_summaries_list_returns_empty_estimate(ebay_pricing, fake_http):
    estimate = _price(ebay_pricing, fake_http, {"total": 0, "itemSummaries": []}, ["ramones"])
    assert estimate == ebay_pricing.PriceEstimate(None, 0, 0, 0)


# --- Estimation and summary validation -------------------------------------


def test_rc09_c1_five_distinct_usd_candidates_return_frozen_median_estimate(
    ebay_pricing, query_http
):
    response = _summaries(*_valid_prices([100, 110, 120, 130, 140]))
    query_http.add("rick owens ramones", response)
    estimate = ebay_pricing.ebay_market_price("rick owens ramones", RAMONES_KEYWORDS, EXCLUDES, "tok")
    assert estimate == ebay_pricing.PriceEstimate(120.0, 5, 0, 5)
    with pytest.raises((AttributeError, TypeError)):
        estimate.price_usd = 999.0


def test_rc09_c1_refresh_stores_five_candidate_median(
    ebay_pricing, query_http, creds, seeded_db, db_conn
):
    query_http.add("rick owens ramones", _summaries(*_valid_prices([100, 110, 120, 130, 140])))
    query_http.add("pod shorts", _five_pod_listings())
    ebay_pricing.refresh_market_prices()
    assert _stored(db_conn)["rick owens ramones"] == 120.0


def test_rc09_c2_four_candidates_are_insufficient_and_do_not_update(
    ebay_pricing, fake_http
):
    estimate = _price(ebay_pricing, fake_http, _summaries(*_valid_prices([100, 110, 120, 130])))
    assert estimate == ebay_pricing.PriceEstimate(None, 4, 0, 4)


def test_rc09_c3_mad_filter_removes_extreme_outlier(ebay_pricing, fake_http):
    estimate = _price(
        ebay_pricing, fake_http,
        _summaries(*_valid_prices([100, 105, 110, 115, 120, 1000])),
    )
    assert estimate == ebay_pricing.PriceEstimate(110.0, 6, 1, 5)


def test_rc09_mad_boundary_value_is_retained(ebay_pricing, fake_http):
    boundary = 100 + (3.5 * 1.4826 * 10)
    just_above = math.nextafter(boundary, math.inf)
    prices = [90, 90, 100, 100, 100, 110, boundary, just_above]
    estimate = _price(ebay_pricing, fake_http, _summaries(*_valid_prices(prices)))
    # nextafter creates the closest representable float above the boundary; no
    # arbitrary epsilon is used, so exact-boundary equality is tested directly.
    assert estimate == ebay_pricing.PriceEstimate(100.0, 8, 1, 7)


def test_rc09_prices_accept_json_numbers_as_well_as_numeric_strings(ebay_pricing, fake_http):
    response = _summaries(
        _listing("number-1", value=100),
        _listing("number-2", value="110"),
        _listing("number-3", value=120),
        _listing("number-4", value="130"),
        _listing("number-5", value=140),
    )
    estimate = _price(ebay_pricing, fake_http, response)
    assert estimate == ebay_pricing.PriceEstimate(120.0, 5, 0, 5)


def test_rc09_c4_zero_mad_keeps_only_values_equal_to_median(
    ebay_pricing, fake_http
):
    response = _summaries(*_valid_prices([100, 100, 100, 100, 100, 1000]))
    estimate = _price(ebay_pricing, fake_http, response)
    assert estimate == ebay_pricing.PriceEstimate(100.0, 6, 1, 5)


def test_rc09_c4_zero_mad_result_is_stored_from_five_retained_listings(
    ebay_pricing, query_http, creds, seeded_db, db_conn
):
    response = _summaries(*_valid_prices([100, 100, 100, 100, 100, 1000]))
    query_http.add("rick owens ramones", response)
    query_http.add("pod shorts", _five_pod_listings())
    ebay_pricing.refresh_market_prices()
    assert _stored(db_conn)["rick owens ramones"] == 100.0


def test_rc09_c5_trimmed_sample_below_five_preserves_existing_price(
    ebay_pricing, fake_http, creds, fixtures_dir, seeded_db, db_conn, capsys
):
    # The median/MAD pass retains only the five identical candidates, so this case
    # also checks the stored result. Four candidates plus a single extreme is tested
    # separately below through an estimate when no database refresh is needed.
    response = _summaries(*_valid_prices([100, 100, 100, 100, 100, 1000]))
    # Make one of the identical rows fail matching, leaving four retained rows.
    response["itemSummaries"][0]["title"] = "Rick Owens Geobasket"
    fake_http.add("POST", TOKEN_URL, json=_load(fixtures_dir, "token.json"))
    fake_http.add("GET", SEARCH_URL, json=response)
    db_conn.execute("UPDATE items SET market_price_usd = 765 WHERE name = 'rick owens ramones'")
    db_conn.commit()
    ebay_pricing.refresh_market_prices()
    assert _stored(db_conn)["rick owens ramones"] == 765.0
    output = capsys.readouterr().out
    assert "insufficient" in output.lower()


def test_rc09_c6_deduplicates_valid_ids_and_allows_valid_copy_after_malformed_copy(
    ebay_pricing, fake_http
):
    estimate = _price(
        ebay_pricing,
        fake_http,
        _summaries(
            _listing("same", value="100"),
            _listing("same", value="999"),
            {"itemId": "later-valid", "title": None, "price": {"value": "50", "currency": "USD"}},
            _listing("later-valid", value="110"),
            _listing("item-3", value="120"),
            _listing("item-4", value="130"),
            _listing("item-5", value="140"),
        ),
    )
    assert estimate == ebay_pricing.PriceEstimate(120.0, 5, 0, 5)


@pytest.mark.parametrize(
    "bad_row",
    [
        {"itemId": "x", "price": {"value": "100", "currency": "USD"}},
        _listing("x", title=None),
        _listing("x", title=7),
        _listing("", title="Rick Owens Ramones"),
        _listing("   ", title="Rick Owens Ramones"),
        {"itemId": "x", "title": "Rick Owens Ramones", "price": None},
        {"itemId": "x", "title": "Rick Owens Ramones", "price": []},
        {"itemId": "x", "title": "Rick Owens Ramones"},
        _listing("x", currency="JPY"),
        {"itemId": "x", "title": "Rick Owens Ramones", "price": {"value": 100}},
        {"itemId": "x", "title": "Rick Owens Ramones", "price": {"currency": "USD"}},
        _listing("x", value="0"),
        _listing("x", value="-1"),
        _listing("x", value="not-a-price"),
        _listing("x", value=True),
        _listing("x", value="NaN"),
        _listing("x", value="Infinity"),
        _listing("x", value=None),
        _listing("x", value=[]),
        None,
        [],
    ],
)
def test_rc09_c7_invalid_summary_is_skipped_without_counting_or_raising(
    ebay_pricing, fake_http, bad_row
):
    estimate = _price(ebay_pricing, fake_http, _summaries(bad_row))
    assert estimate == ebay_pricing.PriceEstimate(None, 0, 0, 0)


@pytest.mark.parametrize("response", [{"itemSummaries": []}, {"total": 0}])
def test_rc09_c8_empty_or_missing_summaries_returns_zero_counts_and_refresh_preserves_price(
    ebay_pricing, fake_http, response
):
    estimate = _price(ebay_pricing, fake_http, response)
    assert estimate == ebay_pricing.PriceEstimate(None, 0, 0, 0)


@pytest.mark.parametrize("response", [None, [], "bad", {"itemSummaries": None}, {"itemSummaries": {}}])
def test_rc09_c9_malformed_response_container_raises_value_error(
    ebay_pricing, fake_http, response
):
    if response is None:
        fake_http.add("GET", SEARCH_URL, text="null")
        with pytest.raises(ValueError):
            ebay_pricing.ebay_market_price("rick owens ramones", RAMONES_KEYWORDS, EXCLUDES, "tok")
        return
    with pytest.raises(ValueError):
        _price(ebay_pricing, fake_http, response)


def test_rc09_c9_refresh_malformed_container_does_not_update_or_search_later_items(
    ebay_pricing, creds, query_http, seeded_db, db_conn
):
    query_http.add("rick owens ramones", [])
    query_http.add("pod shorts", _five_pod_listings())
    db_conn.execute("UPDATE items SET market_price_usd = 765 WHERE name = 'rick owens ramones'")
    db_conn.commit()
    with pytest.raises(ValueError):
        ebay_pricing.refresh_market_prices()
    assert _stored(db_conn)["rick owens ramones"] == 765.0
    assert query_http.queries() == ["rick owens ramones"]


# --- Refresh behavior and database preservation ----------------------------


def test_rc09_c2_refresh_skips_update_for_four_candidate_estimate(
    ebay_pricing, creds, query_http, seeded_db, db_conn, capsys, monkeypatch
):
    calls = []
    update = ebay_pricing.update_market_price

    def recording_update(*args):
        calls.append(args)
        return update(*args)

    monkeypatch.setattr(ebay_pricing, "update_market_price", recording_update)
    query_http.add("rick owens ramones", _summaries(*_valid_prices([100, 110, 120, 130])))
    query_http.add("pod shorts", _five_pod_listings())
    ebay_pricing.refresh_market_prices()
    assert _stored(db_conn) == {"rick owens ramones": None, "pod shorts": 300.0}
    assert len(calls) == 1
    assert "insufficient" in capsys.readouterr().out.lower()


@pytest.mark.parametrize("response_name", ["saved_empty", "missing_summaries"])
def test_rc09_c8_empty_search_prints_skip_reason_and_keeps_stored_price(
    ebay_pricing, creds, query_http, fixtures_dir, seeded_db, db_conn, capsys,
    response_name
):
    response = (
        _load(fixtures_dir, "search_empty.json")
        if response_name == "saved_empty"
        else {"total": 0}
    )
    query_http.add("rick owens ramones", response)
    query_http.add("pod shorts", _five_pod_listings())
    db_conn.execute("UPDATE items SET market_price_usd = 765 WHERE name = 'rick owens ramones'")
    db_conn.commit()
    ebay_pricing.refresh_market_prices()
    assert _stored(db_conn)["rick owens ramones"] == 765.0
    output = capsys.readouterr().out
    assert "rick owens ramones" in output
    assert "no" in output.lower() or "insufficient" in output.lower()


def test_rc09_c10_refresh_prints_counts_and_estimate_without_raw_listing_data(
    ebay_pricing, creds, query_http, fixtures_dir, seeded_db, capsys
):
    titles = [f"Rick Owens Ramones Item {i}" for i in range(5)]
    response = _summaries(*[
        _listing(f"ramones-{i}", title=titles[i], value=str(100 + i * 10))
        for i in range(5)
    ])
    query_http.add("rick owens ramones", response)
    query_http.add("pod shorts", _five_pod_listings())
    ebay_pricing.refresh_market_prices()
    output = capsys.readouterr().out
    assert "rick owens ramones" in output
    lowered = output.lower()
    for label in ("candidate", "outlier", "retained"):
        assert label in lowered
    for item_section in output.split("Pricing ")[1:]:
        item_output = item_section.lower()
        assert all(label in item_output for label in ("candidate", "outlier", "retained"))
    assert "5" in output and "0" in output and "120.0" in output
    assert all(title not in output for title in titles)
    assert all(f"{price} - " not in output for price in (100, 110, 120, 130, 140))


def test_rc09_c11_insufficient_first_item_does_not_prevent_later_item_pricing(
    ebay_pricing, creds, query_http, seeded_db, db_conn
):
    query_http.add("rick owens ramones", _summaries(*_valid_prices([100, 110, 120, 130])))
    query_http.add("pod shorts", _five_pod_listings())
    ebay_pricing.refresh_market_prices()
    assert _stored(db_conn) == {"rick owens ramones": None, "pod shorts": 300.0}
    assert query_http.queries() == ["rick owens ramones", "pod shorts"]


def test_rc09_c11_http_500_stops_before_later_item_and_preserves_failing_price(
    ebay_pricing, creds, query_http, seeded_db, db_conn, capsys
):
    query_http.add("rick owens ramones", _summaries(*_valid_prices([100, 110, 120, 130, 140])))
    query_http.add("pod shorts", {"errors": []}, status=500)
    db_conn.execute("UPDATE items SET market_price_usd = 765 WHERE name = 'pod shorts'")
    db_conn.commit()
    with pytest.raises(requests.HTTPError):
        ebay_pricing.refresh_market_prices()
    assert _stored(db_conn) == {"rick owens ramones": 120.0, "pod shorts": 765.0}
    assert query_http.queries() == ["rick owens ramones", "pod shorts"]
    output = capsys.readouterr().out
    failing_item_output = output.split('Pricing pod shorts with eBay query "pod shorts"', 1)[1]
    assert not any(label in failing_item_output.lower() for label in ("candidate", "outlier", "retained"))


def test_rc09_c11_timeout_stops_run_before_later_item(
    ebay_pricing, creds, query_http, seeded_db, db_conn, capsys
):
    query_http.add("rick owens ramones", _summaries(*_valid_prices([100, 110, 120, 130, 140])))
    query_http.add("pod shorts", None, error=requests.Timeout("offline timeout"))
    db_conn.execute("UPDATE items SET market_price_usd = 765 WHERE name = 'pod shorts'")
    db_conn.commit()
    with pytest.raises(requests.Timeout):
        ebay_pricing.refresh_market_prices()
    assert _stored(db_conn)["rick owens ramones"] == 120.0
    assert _stored(db_conn)["pod shorts"] == 765.0
    assert query_http.queries() == ["rick owens ramones", "pod shorts"]
    output = capsys.readouterr().out
    failing_item_output = output.split('Pricing pod shorts with eBay query "pod shorts"', 1)[1]
    assert not any(label in failing_item_output.lower() for label in ("candidate", "outlier", "retained"))


def test_rc09_c13_skipped_updates_preserve_ebay_and_manual_prices_and_effective_fallback(
    ebay_pricing, creds, query_http, seeded_db, db_conn
):
    query_http.add("rick owens ramones", _summaries(*_valid_prices([100, 110, 120, 130])))
    query_http.add("pod shorts", {"itemSummaries": []})
    db_conn.execute(
        "UPDATE items SET market_price_usd = 765, manual_price_usd = 800 WHERE name = 'rick owens ramones'"
    )
    db_conn.execute(
        "UPDATE items SET manual_price_usd = 333 WHERE name = 'pod shorts'"
    )
    db_conn.commit()
    ebay_pricing.refresh_market_prices()
    rows = {row["name"]: row for row in db_conn.execute(
        "SELECT name, market_price_usd, manual_price_usd FROM items"
    )}
    assert rows["rick owens ramones"]["market_price_usd"] == 765.0
    assert rows["rick owens ramones"]["manual_price_usd"] == 800.0
    assert rows["pod shorts"]["market_price_usd"] is None
    assert rows["pod shorts"]["manual_price_usd"] == 333.0
    tracked = {item.name: item for item in db.get_tracked_items()}
    assert tracked["rick owens ramones"].effective_price_usd == 765.0
    assert tracked["pod shorts"].effective_price_usd == 333.0


def test_rc09_c14_readme_describes_sample_threshold_trimming_and_counts():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8").lower()
    assert "first 50" in text
    assert "five" in text or "5 " in text
    assert "distinct" in text
    assert "retained" in text
    assert "median absolute deviation" in text or "mad" in text
    assert "3.5" in text
    assert "outlier" in text
    assert "count" in text
    assert "not stored" in text or "aren't stored" in text


# ---- Preserved RC-03b job/database regression coverage --------------------


NO_ITEMS_LINE = "No active items in the database. Add items to catalog.json, then run python3 src/db.py --seed."


def _ramones_and_pods(query_http, fixtures_dir, ramones=None, pods=None, pods_status=200):
    query_http.add("rick owens ramones", ramones or _load(fixtures_dir, "search_ramones.json"))
    query_http.add("pod shorts", pods if pods is not None else _five_pod_listings(), status=pods_status)


def test_c13_refresh_works_from_unrelated_cwd(
    ebay_pricing, creds, ebay_http, seeded_db, db_conn, monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    ebay_pricing.refresh_market_prices()
    row = db_conn.execute(
        "SELECT market_price_usd FROM items WHERE name='rick owens ramones'"
    ).fetchone()
    assert row["market_price_usd"] == 900.0


def test_c43_refresh_one_token_searches_each_item_and_updates_supported_prices(
    ebay_pricing, creds, ebay_http, seeded_db, db_conn
):
    ebay_pricing.refresh_market_prices()
    kinds = [(method, url) for method, url, _ in ebay_http.calls]
    assert kinds == [("POST", TOKEN_URL), ("GET", SEARCH_URL), ("GET", SEARCH_URL)]
    prices = {
        row["name"]: row["market_price_usd"]
        for row in db_conn.execute("SELECT name, market_price_usd FROM items")
    }
    assert prices == {"rick owens ramones": 900.0, "pod shorts": None}
    assert db.get_market_price("Rick Owens", "pod shorts") == 300.0


def test_c44_main_requests_exactly_one_token(ebay_pricing, creds, ebay_http, seeded_db):
    ebay_pricing.main()
    assert len([call for call in ebay_http.calls if call[0] == "POST" and call[1] == TOKEN_URL]) == 1


def test_rc03b_c22_ebay_market_price_keeps_four_argument_signature(ebay_pricing):
    import inspect

    assert list(inspect.signature(ebay_pricing.ebay_market_price).parameters) == [
        "query", "keywords", "exclude_keywords", "token",
    ]


def test_rc03b_c23_search_uses_each_items_search_query_in_order(
    ebay_pricing, creds, ebay_http, seeded_db
):
    ebay_pricing.refresh_market_prices()
    assert [call[2]["params"]["q"] for call in ebay_http.calls if call[0] == "GET"] == [
        "rick owens ramones", "pod shorts",
    ]


def test_rc03b_c24_null_search_query_falls_back_to_item_name(
    ebay_pricing, creds, ebay_http, seeded_db, capsys
):
    sql_run(seeded_db, "UPDATE items SET search_query = 'ramones high' WHERE name = 'rick owens ramones'")
    sql_run(seeded_db, "UPDATE items SET search_query = NULL WHERE name = 'pod shorts'")
    ebay_pricing.refresh_market_prices()
    assert [call[2]["params"]["q"] for call in ebay_http.calls if call[0] == "GET"] == [
        "ramones high", "pod shorts",
    ]
    output = capsys.readouterr().out
    assert 'Pricing rick owens ramones with eBay query "ramones high"' in output
    assert 'Pricing pod shorts with eBay query "pod shorts"' in output


def test_rc03b_c25_database_excludes_apply_per_item(
    ebay_pricing, creds, query_http, fixtures_dir, seeded_db, db_conn
):
    _ramones_and_pods(query_http, fixtures_dir)
    ebay_pricing.refresh_market_prices()
    assert _stored(db_conn) == {"rick owens ramones": 900.0, "pod shorts": 300.0}


def test_rc03b_c26_inactive_items_are_not_searched(
    ebay_pricing, creds, ebay_http, seeded_db, db_conn
):
    sql_run(seeded_db, "UPDATE items SET active = 0 WHERE name = ?", ("pod shorts",))
    ebay_pricing.refresh_market_prices()
    assert len([call for call in ebay_http.calls if call[0] == "POST" and call[1] == TOKEN_URL]) == 1
    assert len([call for call in ebay_http.calls if call[0] == "GET"]) == 1
    assert _stored(db_conn)["pod shorts"] is None


@pytest.mark.parametrize("bad", ["0.00", "-1.00"])
def test_rc03b_c27_unusable_candidates_are_skipped_and_run_continues(
    ebay_pricing, creds, query_http, fixtures_dir, seeded_db, db_conn, capsys, bad
):
    ramones = _load(fixtures_dir, "search_ramones.json")
    for summary in ramones["itemSummaries"]:
        summary["price"]["value"] = bad
    _ramones_and_pods(query_http, fixtures_dir, ramones=ramones)
    ebay_pricing.refresh_market_prices()
    assert _stored(db_conn) == {"rick owens ramones": None, "pod shorts": 300.0}
    output = capsys.readouterr().out.lower()
    assert "rick owens ramones" in output and "insufficient" in output


def test_rc03b_c28_no_matching_listings_prints_skip_reason_and_keeps_price(
    ebay_pricing, creds, query_http, fixtures_dir, seeded_db, db_conn, capsys
):
    _ramones_and_pods(
        query_http, fixtures_dir,
        pods=_summaries(_listing("geobasket", "Rick Owens Geobasket", "500")),
    )
    ebay_pricing.refresh_market_prices()
    output = capsys.readouterr().out.lower()
    assert "pod shorts" in output
    assert "no" in output or "insufficient" in output
    assert _stored(db_conn)["pod shorts"] is None


def test_rc03b_c29_http_error_stops_run_but_keeps_earlier_prices(
    ebay_pricing, creds, query_http, fixtures_dir, seeded_db, db_conn
):
    _ramones_and_pods(query_http, fixtures_dir, pods={"errors": []}, pods_status=500)
    with pytest.raises(requests.HTTPError):
        ebay_pricing.refresh_market_prices()
    assert _stored(db_conn) == {"rick owens ramones": 900.0, "pod shorts": None}


def _assert_clean_schema_error(capsys, fake_http, exc, needle="python3 src/db.py"):
    assert exc.value.code == 1
    error = capsys.readouterr().err
    assert len(error.strip().splitlines()) == 1
    assert error.startswith("Error: ")
    assert needle in error
    assert "Traceback" not in error
    assert fake_http.calls == []


def test_rc03b_c30_main_on_legacy_db_is_a_clean_error_before_any_network(
    ebay_pricing, legacy_db, fake_http, capsys
):
    before = sha256(legacy_db)
    with pytest.raises(SystemExit) as exc:
        ebay_pricing.main()
    _assert_clean_schema_error(capsys, fake_http, exc)
    assert sha256(legacy_db) == before


def test_rc03b_c31_main_with_no_database_file_is_clean_and_creates_nothing(
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
