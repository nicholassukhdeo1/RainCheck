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
    return {
        "itemId": item_id, "title": title,
        "price": {"value": value, "currency": currency},
        "buyingOptions": ["FIXED_PRICE"],
    }


def _summaries(*listings):
    return {"itemSummaries": list(listings)}


def _valid_prices(values, prefix="item"):
    return [_listing(f"{prefix}-{i}", value=value) for i, value in enumerate(values)]


def _price(ebay_pricing, fake_http, response, keywords=RAMONES_KEYWORDS, excludes=EXCLUDES):
    fake_http.add("GET", SEARCH_URL, json=response)
    return ebay_pricing.ebay_listing_prices("rick owens ramones", keywords, excludes, "tok")


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


def test_rc06_c20_search_keeps_one_page_request_shape_and_timeout(ebay_pricing, fake_http):
    result = _price(ebay_pricing, fake_http, _summaries(*_valid_prices([100, 110, 120, 130, 140])))
    calls = [call for call in fake_http.calls if call[0] == "GET"]
    assert len(calls) == 1
    _, url, kwargs = calls[0]
    assert url == SEARCH_URL
    assert kwargs["params"] == {"q": "rick owens ramones", "limit": 50, "sort": "newlyListed"}
    assert kwargs["headers"]["Authorization"] == "Bearer tok"
    assert kwargs["headers"]["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_US"
    assert kwargs["timeout"] == 30
    assert len(result) == 5


def test_c34_get_ebay_token_raises_http_error_on_401(ebay_pricing, creds, fake_http):
    fake_http.add("POST", TOKEN_URL, json={"error": "invalid_client"}, status=401)
    with pytest.raises(requests.HTTPError):
        ebay_pricing.get_ebay_token()


def test_rc10_c2_token_request_retries_statuses_then_returns_token(
    ebay_pricing, creds, fake_http, fixtures_dir
):
    fake_http.add_sequence("POST", TOKEN_URL, [
        {"status": 503}, {"status": 503}, {"json": _load(fixtures_dir, "token.json")},
    ])
    assert ebay_pricing.get_ebay_token() == "test-token-not-real"
    assert len(fake_http.calls) == 3
    assert [call[2]["timeout"] for call in fake_http.calls] == [30, 30, 30]


def test_rc10_c2_browse_search_retries_then_parser_receives_only_successful_response(
    ebay_pricing, fake_http
):
    expected = _summaries(*_valid_prices([100, 110, 120, 130, 140]))
    fake_http.add_sequence("GET", SEARCH_URL, [
        {"status": 503}, {"status": 503}, {"json": expected},
    ])
    parsed = ebay_pricing.ebay_listing_prices(
        "rick owens ramones", RAMONES_KEYWORDS, EXCLUDES, "tok"
    )
    assert len(fake_http.calls) == 3
    assert len(parsed) == 5
    assert all(item.price_usd in {100.0, 110.0, 120.0, 130.0, 140.0} for item in parsed)


# --- Raw listing validation and eBay search contract -----------------------


def test_rc06_c14_and_c27_search_returns_observations_and_requests_first_page(
    ebay_pricing, fake_http
):
    response = _summaries(*_valid_prices([100, 110, 120, 130, 140]))
    results = _price(ebay_pricing, fake_http, response)
    assert [(r.ebay_listing_id, r.price_usd) for r in results] == [
        (f"rest:item-{n}", p) for n, p in enumerate((100, 110, 120, 130, 140))
    ]
    call = next(call for call in fake_http.calls if call[0] == "GET")
    assert call[2]["params"] == {
        "q": "rick owens ramones", "limit": 50, "sort": "newlyListed"
    }
    assert call[2]["headers"]["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_US"
    assert call[2]["timeout"] == 30


def test_rc06_c14_legacy_item_id_canonicalizes_variations_and_rejects_disagreement(
    ebay_pricing, fake_http
):
    first = _listing("v1|12345|0", value="100")
    first["legacyItemId"] = "12345"
    variation = _listing("v1|12345|987", value="999")
    variation["legacyItemId"] = "12345"
    disagree = _listing("v1|54321|0", value="500")
    disagree["legacyItemId"] = "12345"
    results = _price(ebay_pricing, fake_http, _summaries(first, variation, disagree))
    assert [(r.ebay_listing_id, r.price_usd) for r in results] == [("legacy:12345", 100.0)]


def test_rc06_c14_legacy_fallback_uses_rest_listing_component_then_opaque_id(
    ebay_pricing, fake_http
):
    valid_rest = _listing("v1|12345|987", value="100")
    opaque = _listing("old-format-opaque", value="110")
    results = _price(ebay_pricing, fake_http, _summaries(valid_rest, opaque))
    assert [(r.ebay_listing_id, r.price_usd) for r in results] == [
        ("legacy:12345", 100.0), ("rest:old-format-opaque", 110.0)
    ]


def test_rc06_c14_missing_and_invalid_ids_are_skipped(ebay_pricing, fake_http):
    no_rest_id = _listing("", value="100")
    no_item_id = {"title": "Rick Owens Ramones High", "price": {"value": "100", "currency": "USD"},
                  "buyingOptions": ["FIXED_PRICE"]}
    invalid_legacy = _listing("v1|12345|0", value="110")
    invalid_legacy["legacyItemId"] = "not-decimal"
    results = _price(ebay_pricing, fake_http, _summaries(no_rest_id, no_item_id, invalid_legacy))
    assert [(r.ebay_listing_id, r.price_usd) for r in results] == [("legacy:12345", 110.0)]


def test_rc06_c14_first_valid_duplicate_wins_after_malformed_copy(ebay_pricing, fake_http):
    malformed = _listing("v1|12345|0", value="bad")
    valid = _listing("v1|12345|1", value="100")
    repeated = _listing("v1|12345|2", value="999")
    for row in (malformed, valid, repeated):
        row["legacyItemId"] = "12345"
    results = _price(ebay_pricing, fake_http, _summaries(malformed, valid, repeated))
    assert [(r.ebay_listing_id, r.price_usd) for r in results] == [("legacy:12345", 100.0)]


@pytest.mark.parametrize("buying_options", [None, [], ["AUCTION"], "FIXED_PRICE"])
def test_rc06_c27_auction_only_or_missing_buying_options_are_skipped(
    ebay_pricing, fake_http, buying_options
):
    row = _listing("auction", value="100")
    row["buyingOptions"] = buying_options
    assert _price(ebay_pricing, fake_http, _summaries(row)) == ()


@pytest.mark.parametrize(
    "buying_options", [["FIXED_PRICE"], ["FIXED_PRICE", "BEST_OFFER"]]
)
def test_rc06_c27_fixed_price_with_or_without_best_offer_is_eligible(
    ebay_pricing, fake_http, buying_options
):
    row = _listing("fixed", value="100")
    row["buyingOptions"] = buying_options
    assert len(_price(ebay_pricing, fake_http, _summaries(row))) == 1


@pytest.mark.parametrize("bad_row", [
    {"price": {"value": "100", "currency": "USD"}, "title": "Rick Owens Ramones", "buyingOptions": ["FIXED_PRICE"]},
    _listing("x", title=None), _listing("x", title=7), _listing("", title="Rick Owens Ramones"),
    _listing("   ", title="Rick Owens Ramones"),
    {"itemId": "x", "title": "Rick Owens Ramones", "price": None, "buyingOptions": ["FIXED_PRICE"]},
    {"itemId": "x", "title": "Rick Owens Ramones", "price": [], "buyingOptions": ["FIXED_PRICE"]},
    {"itemId": "x", "title": "Rick Owens Ramones", "price": {"value": "100"}, "buyingOptions": ["FIXED_PRICE"]},
    _listing("x", currency="JPY"), _listing("x", value="0"), _listing("x", value="-1"),
    _listing("x", value="bad"), _listing("x", value=True), _listing("x", value="NaN"),
    _listing("x", value="Infinity"), _listing("x", value=None), _listing("x", value=[]),
    None, [],
])
def test_rc06_c15_malformed_listing_summary_is_skipped(ebay_pricing, fake_http, bad_row):
    assert _price(ebay_pricing, fake_http, _summaries(bad_row)) == ()


@pytest.mark.parametrize("response", [{"itemSummaries": []}, {"total": 0}])
def test_rc06_empty_or_missing_search_results_return_no_observations(
    ebay_pricing, fake_http, response
):
    assert _price(ebay_pricing, fake_http, response) == ()


@pytest.mark.parametrize(
    "response", [None, [], "bad", {"itemSummaries": None}, {"itemSummaries": {}}]
)
def test_rc06_c15_malformed_response_container_raises_value_error(
    ebay_pricing, fake_http, response
):
    if response is None:
        fake_http.add("GET", SEARCH_URL, text="null")
        with pytest.raises(ValueError):
            ebay_pricing.ebay_listing_prices(
                "rick owens ramones", RAMONES_KEYWORDS, EXCLUDES, "tok"
            )
    else:
        with pytest.raises(ValueError):
            _price(ebay_pricing, fake_http, response)


# --- Refresh behavior and database preservation ----------------------------


def test_rc06_c7_refresh_stores_four_observations_but_preserves_prior_price(
    ebay_pricing, creds, query_http, seeded_db, db_conn, caplog
):
    caplog.set_level("INFO", logger="ebay_pricing")
    db_conn.execute("UPDATE items SET market_price_usd = 765 WHERE name = 'rick owens ramones'")
    db_conn.commit()
    query_http.add("rick owens ramones", _summaries(*_valid_prices([100, 110, 120, 130])))
    query_http.add("pod shorts", _five_pod_listings())
    ebay_pricing.refresh_market_prices()
    assert _stored(db_conn) == {"rick owens ramones": 765.0, "pod shorts": 300.0}
    assert db_conn.execute(
        "SELECT COUNT(*) FROM ebay_listing_prices WHERE item_id = ?",
        (db.get_item_id("Rick Owens", "rick owens ramones"),),
    ).fetchone()[0] == 4
    assert any("insufficient" in record.getMessage().lower() for record in caplog.records)


@pytest.mark.parametrize("response_name", ["saved_empty", "missing_summaries"])
def test_rc09_c8_empty_search_prints_skip_reason_and_keeps_stored_price(
    ebay_pricing, creds, query_http, fixtures_dir, seeded_db, db_conn, caplog,
    response_name
):
    caplog.set_level("INFO", logger="ebay_pricing")
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
    output = "\n".join(record.getMessage() for record in caplog.records).lower()
    assert "rick owens ramones" in output
    assert "no" in output or "insufficient" in output


def test_rc09_c10_refresh_prints_counts_and_estimate_without_raw_listing_data(
    ebay_pricing, creds, query_http, fixtures_dir, seeded_db, caplog
):
    caplog.set_level("INFO", logger="ebay_pricing")
    titles = [f"Rick Owens Ramones Item {i}" for i in range(5)]
    response = _summaries(*[
        _listing(f"ramones-{i}", title=titles[i], value=str(100 + i * 10))
        for i in range(5)
    ])
    query_http.add("rick owens ramones", response)
    query_http.add("pod shorts", _five_pod_listings())
    ebay_pricing.refresh_market_prices()
    output = "\n".join(record.getMessage() for record in caplog.records)
    assert "rick owens ramones" in output
    lowered = output.lower()
    for label in ("candidate", "outlier", "retained"):
        assert label in lowered
    lowered = output.lower()
    assert all(label in lowered for label in ("candidate", "outlier", "retained"))
    assert "5" in output and "0" in output and "120.0" in output
    assert all(title not in output for title in titles)
    assert all(f"{price} - " not in output for price in (100, 110, 120, 130, 140))


def test_rc10_c11_malformed_completed_search_writes_nothing_and_continues_later_items(
    ebay_pricing, creds, query_http, seeded_db, db_conn
):
    query_http.add("rick owens ramones", [])
    query_http.add("pod shorts", _five_pod_listings())
    result = ebay_pricing.refresh_market_prices()
    assert result.failed_item_ids == (db.get_item_id("Rick Owens", "rick owens ramones"),)
    assert query_http.queries() == ["rick owens ramones", "pod shorts"]
    assert db_conn.execute("SELECT COUNT(*) FROM ebay_listing_prices").fetchone()[0] == 5


def test_rc09_c11_insufficient_first_item_does_not_prevent_later_item_pricing(
    ebay_pricing, creds, query_http, seeded_db, db_conn
):
    query_http.add("rick owens ramones", _summaries(*_valid_prices([100, 110, 120, 130])))
    query_http.add("pod shorts", _five_pod_listings())
    ebay_pricing.refresh_market_prices()
    assert _stored(db_conn) == {"rick owens ramones": None, "pod shorts": 300.0}
    assert query_http.queries() == ["rick owens ramones", "pod shorts"]


def test_rc10_c9_c10_http_500_isolates_item_and_continues_later_items(
    ebay_pricing, creds, query_http, seeded_db, db_conn, caplog
):
    query_http.add("rick owens ramones", _summaries(*_valid_prices([100, 110, 120, 130, 140])))
    query_http.add("pod shorts", {"errors": []}, status=500)
    db_conn.execute("UPDATE items SET market_price_usd = 765 WHERE name = 'pod shorts'")
    db_conn.commit()
    result = ebay_pricing.refresh_market_prices()
    assert _stored(db_conn) == {"rick owens ramones": 120.0, "pod shorts": 765.0}
    assert query_http.queries() == ["rick owens ramones", "pod shorts"]
    assert db_conn.execute(
        "SELECT COUNT(*) FROM ebay_listing_prices WHERE item_id = ?",
        (db.get_item_id("Rick Owens", "rick owens ramones"),),
    ).fetchone()[0] == 5
    assert db_conn.execute(
        "SELECT COUNT(*) FROM ebay_listing_prices WHERE item_id = ?",
        (db.get_item_id("Rick Owens", "pod shorts"),),
    ).fetchone()[0] == 0
    assert result.had_failure is True
    assert result.succeeded_item_ids == (db.get_item_id("Rick Owens", "rick owens ramones"),)
    assert result.failed_item_ids == (db.get_item_id("Rick Owens", "pod shorts"),)
    assert any(record.levelname == "WARNING" and str(db.get_item_id("Rick Owens", "pod shorts")) in record.getMessage() for record in caplog.records)


def test_rc10_c9_partial_ebay_cli_run_exits_one_after_committing_earlier_item(
    ebay_pricing, creds, fake_http, fixtures_dir, seeded_db, db_conn
):
    fake_http.add("POST", TOKEN_URL, json=_load(fixtures_dir, "token.json"))
    first = _summaries(*_valid_prices([100, 110, 120, 130, 140]))
    fake_http.add_sequence("GET", SEARCH_URL, [
        {"json": first}, {"status": 500}, {"status": 500}, {"status": 500},
    ])
    with pytest.raises(SystemExit) as exc:
        ebay_pricing.main()
    assert exc.value.code == 1
    assert _stored(db_conn)["rick owens ramones"] == 120.0
    assert db_conn.execute(
        "SELECT COUNT(*) FROM ebay_listing_prices WHERE item_id = ?",
        (db.get_item_id("Rick Owens", "rick owens ramones"),),
    ).fetchone()[0] == 5
    assert db_conn.execute(
        "SELECT COUNT(*) FROM ebay_listing_prices WHERE item_id = ?",
        (db.get_item_id("Rick Owens", "pod shorts"),),
    ).fetchone()[0] == 0


def test_rc10_c10_third_item_after_failure_is_searched_and_committed(
    ebay_pricing, creds, query_http, seeded_db, db_conn, caplog
):
    brand_id = db_conn.execute("SELECT id FROM brands WHERE name = ?", ("Rick Owens",)).fetchone()[0]
    db_conn.execute(
        "INSERT INTO items (brand_id, name, search_query, manual_price_usd, active) "
        "VALUES (?, ?, ?, ?, 1)", (brand_id, "test boots", "boots", 500.0),
    )
    boots_id = db_conn.execute("SELECT id FROM items WHERE name = ?", ("test boots",)).fetchone()[0]
    db_conn.execute(
        "INSERT INTO item_keywords (item_id, keyword, kind) VALUES (?, ?, ?)",
        (boots_id, "boots", "include"),
    )
    db_conn.commit()
    query_http.add("rick owens ramones", _summaries(*_valid_prices([100, 110, 120, 130, 140])))
    query_http.add("pod shorts", {"errors": []}, status=500)
    query_http.add("boots", _summaries(*[
        _listing(f"boot-{n}", f"Rick Owens Boots {n}", str(100 + 10 * n)) for n in range(5)
    ]))

    result = ebay_pricing.refresh_market_prices()

    assert query_http.queries() == ["rick owens ramones", "pod shorts", "boots"]
    assert result.succeeded_item_ids == (db.get_item_id("Rick Owens", "rick owens ramones"), boots_id)
    assert result.failed_item_ids == (db.get_item_id("Rick Owens", "pod shorts"),)
    assert result.had_failure is True
    assert db_conn.execute(
        "SELECT COUNT(*) FROM ebay_listing_prices WHERE item_id = ?", (boots_id,)
    ).fetchone()[0] == 5
    assert any(record.levelname == "WARNING" and str(db.get_item_id("Rick Owens", "pod shorts")) in record.getMessage() for record in caplog.records)


def test_rc10_c11_item_writer_failure_preserves_prior_item_and_continues(
    ebay_pricing, creds, query_http, seeded_db, db_conn, monkeypatch
):
    first_id = db.get_item_id("Rick Owens", "rick owens ramones")
    failing_id = db.get_item_id("Rick Owens", "pod shorts")
    query_http.add("rick owens ramones", _summaries(*_valid_prices([100, 110, 120, 130, 140])))
    query_http.add("pod shorts", _five_pod_listings())
    db_conn.execute("UPDATE items SET market_price_usd = 765 WHERE id = ?", (failing_id,))
    db_conn.commit()
    original_writer = ebay_pricing.record_ebay_listing_prices

    def fail_pod_writer(item_id, observations):
        if item_id == failing_id:
            raise RuntimeError("injected writer failure")
        return original_writer(item_id, observations)

    # The writer is a local job boundary so the failure is isolated to this item.
    monkeypatch.setattr(ebay_pricing, "record_ebay_listing_prices", fail_pod_writer)
    result = ebay_pricing.refresh_market_prices()
    assert result.failed_item_ids == (failing_id,)
    assert result.succeeded_item_ids == (first_id,)
    assert db_conn.execute(
        "SELECT COUNT(*) FROM ebay_listing_prices WHERE item_id = ?", (first_id,)
    ).fetchone()[0] == 5
    assert db_conn.execute(
        "SELECT COUNT(*) FROM ebay_listing_prices WHERE item_id = ?", (failing_id,)
    ).fetchone()[0] == 0
    assert _stored(db_conn)["pod shorts"] == 765.0


def test_rc10_c7_transport_failure_isolated_and_later_items_continue(
    ebay_pricing, creds, query_http, seeded_db, db_conn
):
    query_http.add("rick owens ramones", _summaries(*_valid_prices([100, 110, 120, 130, 140])))
    query_http.add("pod shorts", None, error=requests.Timeout("offline timeout"))
    db_conn.execute("UPDATE items SET market_price_usd = 765 WHERE name = 'pod shorts'")
    db_conn.commit()
    result = ebay_pricing.refresh_market_prices()
    assert _stored(db_conn)["rick owens ramones"] == 120.0
    assert _stored(db_conn)["pod shorts"] == 765.0
    assert query_http.queries() == ["rick owens ramones", "pod shorts"]
    assert db_conn.execute(
        "SELECT COUNT(*) FROM ebay_listing_prices WHERE item_id = ?",
        (db.get_item_id("Rick Owens", "rick owens ramones"),),
    ).fetchone()[0] == 5
    assert db_conn.execute(
        "SELECT COUNT(*) FROM ebay_listing_prices WHERE item_id = ?",
        (db.get_item_id("Rick Owens", "pod shorts"),),
    ).fetchone()[0] == 0
    assert result.had_failure is True
    assert result.failed_item_ids == (db.get_item_id("Rick Owens", "pod shorts"),)


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


def test_rc06_c20_readme_describes_permanent_listing_pool_and_coverage_limit():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8").lower()
    assert "first 50" in text
    assert "five" in text or "5 " in text
    assert "distinct" in text
    assert "all-time" in text
    assert "arithmetic mean" in text
    assert "median absolute deviation" in text or "mad" in text
    assert "3.5" in text
    assert "outlier" in text
    assert "count" in text
    assert "listing history" in text


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
    assert row["market_price_usd"] == pytest.approx(5300 / 6)


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
    assert prices == {"rick owens ramones": pytest.approx(5300 / 6), "pod shorts": None}
    assert db.get_market_price("Rick Owens", "pod shorts") == 300.0


def test_c44_main_requests_exactly_one_token(ebay_pricing, creds, ebay_http, seeded_db):
    ebay_pricing.main()
    assert len([call for call in ebay_http.calls if call[0] == "POST" and call[1] == TOKEN_URL]) == 1


def test_rc10_c8_token_failure_is_run_level_clean_cli_error(
    ebay_pricing, creds, fake_http, seeded_db, db_conn, caplog
):
    import logging

    fake_http.add("POST", TOKEN_URL, json={"error": "invalid_client"}, status=401)
    before = db_conn.execute("SELECT COUNT(*) FROM ebay_listing_prices").fetchone()[0]
    with caplog.at_level(logging.INFO):
        with pytest.raises(SystemExit) as exc:
            ebay_pricing.main()
    assert exc.value.code == 1
    assert not [call for call in fake_http.calls if call[0] == "GET"]
    assert db_conn.execute("SELECT COUNT(*) FROM ebay_listing_prices").fetchone()[0] == before
    assert any(record.levelname == "ERROR" for record in caplog.records)
    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert "secret" not in rendered and "aWQ6" not in rendered


def test_rc10_c8_missing_credentials_is_run_level_failure_without_request(
    ebay_pricing, fake_http, seeded_db, caplog
):
    with pytest.raises(SystemExit) as exc:
        ebay_pricing.main()
    assert exc.value.code == 1
    assert fake_http.calls == []
    assert any(record.levelname == "ERROR" for record in caplog.records)


def test_rc10_c12_empty_search_is_success_and_preserves_current_price(
    ebay_pricing, creds, query_http, seeded_db, db_conn
):
    query_http.add("rick owens ramones", {"itemSummaries": []})
    query_http.add("pod shorts", _five_pod_listings())
    db_conn.execute("UPDATE items SET market_price_usd = 765 WHERE name = 'rick owens ramones'")
    db_conn.commit()
    result = ebay_pricing.refresh_market_prices()
    assert result.failed_item_ids == ()
    assert result.succeeded_item_ids == tuple(row[0] for row in db_conn.execute("SELECT id FROM items ORDER BY id"))
    assert _stored(db_conn)["rick owens ramones"] == 765.0


def test_rc10_c13_long_rate_limit_stops_later_searches_and_preserves_prior_result(
    ebay_pricing, creds, fake_http, seeded_db, db_conn, fixtures_dir
):
    fake_http.add("POST", TOKEN_URL, json=_load(fixtures_dir, "token.json"))
    fake_http.add_sequence("GET", SEARCH_URL, [
        {"json": _summaries(*_valid_prices([100, 110, 120, 130, 140]))},
        {"status": 429, "headers": {"Retry-After": "6"}},
    ])
    result = ebay_pricing.refresh_market_prices()
    assert result.had_failure is True
    searches = [call for call in fake_http.calls if call[0] == "GET"]
    assert len(searches) == 2
    assert _stored(db_conn)["rick owens ramones"] == 120.0
    assert result.failed_item_ids == (db.get_item_id("Rick Owens", "pod shorts"),)


def test_rc10_c27_ebay_main_reuses_preinstalled_logging_without_duplicate_handlers(
    ebay_pricing, temp_db, caplog
):
    import logging

    root = logging.getLogger()
    before_handlers = tuple(root.handlers)
    before_level = root.level
    with caplog.at_level(logging.INFO):
        for _ in range(2):
            try:
                ebay_pricing.main()
            except SystemExit as exc:
                assert exc.code == 0
    assert tuple(root.handlers) == before_handlers
    assert root.level == before_level
    records = [r for r in caplog.records if r.name == "ebay_pricing"]
    assert records
    assert len([r for r in records if "job started" in r.getMessage().lower()]) == 2


def test_rc10_c25_ebay_operational_logs_exclude_titles_tokens_and_raw_urls(
    ebay_pricing, creds, ebay_http, seeded_db, caplog
):
    import logging

    with caplog.at_level(logging.INFO, logger="ebay_pricing"):
        ebay_pricing.refresh_market_prices()
    records = [r for r in caplog.records if r.name == "ebay_pricing"]
    assert records and all(r.levelno >= logging.INFO for r in records)
    rendered = "\n".join(r.getMessage() for r in records)
    for private in (
        "test-token-not-real", "Rick Owens Ramones High",
        "https://api.ebay.com/buy/browse/v1/item_summary/search",
        "Authorization", "client_secret",
    ):
        assert private not in rendered


def test_rc06_ebay_listing_prices_keeps_four_argument_signature(ebay_pricing):
    import inspect

    assert list(inspect.signature(ebay_pricing.ebay_listing_prices).parameters) == [
        "query", "keywords", "exclude_keywords", "token",
    ]


def test_rc03b_c23_search_uses_each_items_search_query_in_order(
    ebay_pricing, creds, ebay_http, seeded_db
):
    ebay_pricing.refresh_market_prices()
    assert [call[2]["params"]["q"] for call in ebay_http.calls if call[0] == "GET"] == [
        "rick owens ramones", "pod shorts",
    ]
    assert all(call[2]["params"].get("sort") == "newlyListed" for call in ebay_http.calls if call[0] == "GET")


def test_rc03b_c24_null_search_query_falls_back_to_item_name(
    ebay_pricing, creds, ebay_http, seeded_db, caplog
):
    caplog.set_level("INFO", logger="ebay_pricing")
    sql_run(seeded_db, "UPDATE items SET search_query = 'ramones high' WHERE name = 'rick owens ramones'")
    sql_run(seeded_db, "UPDATE items SET search_query = NULL WHERE name = 'pod shorts'")
    ebay_pricing.refresh_market_prices()
    assert [call[2]["params"]["q"] for call in ebay_http.calls if call[0] == "GET"] == [
        "ramones high", "pod shorts",
    ]
    output = "\n".join(record.getMessage() for record in caplog.records)
    assert "rick owens ramones" in output
    assert "pod shorts" in output


def test_rc03b_c25_database_excludes_apply_per_item(
    ebay_pricing, creds, query_http, fixtures_dir, seeded_db, db_conn
):
    _ramones_and_pods(query_http, fixtures_dir)
    ebay_pricing.refresh_market_prices()
    assert _stored(db_conn) == {"rick owens ramones": pytest.approx(5300 / 6), "pod shorts": 300.0}


def test_rc06_c24_ebay_job_does_not_store_fake_titles_for_either_item(
    ebay_pricing, creds, query_http, seeded_db, db_conn
):
    query_http.add(
        "rick owens ramones",
        _summaries(_listing("v1|8001|0", "Rick Owens Fake Ramones High", "100")),
    )
    query_http.add(
        "pod shorts",
        _summaries(_listing("v1|8002|0", "Rick Owens Fake Pod Shorts", "100")),
    )

    ebay_pricing.refresh_market_prices()

    assert db_conn.execute("SELECT COUNT(*) FROM ebay_listing_prices").fetchone()[0] == 0


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
    ebay_pricing, creds, query_http, fixtures_dir, seeded_db, db_conn, caplog, bad
):
    caplog.set_level("INFO", logger="ebay_pricing")
    ramones = _load(fixtures_dir, "search_ramones.json")
    for summary in ramones["itemSummaries"]:
        summary["price"]["value"] = bad
    _ramones_and_pods(query_http, fixtures_dir, ramones=ramones)
    ebay_pricing.refresh_market_prices()
    assert _stored(db_conn) == {"rick owens ramones": None, "pod shorts": 300.0}
    output = "\n".join(record.getMessage() for record in caplog.records).lower()
    assert "rick owens ramones" in output and "insufficient" in output


def test_rc03b_c28_no_matching_listings_prints_skip_reason_and_keeps_price(
    ebay_pricing, creds, query_http, fixtures_dir, seeded_db, db_conn, caplog
):
    caplog.set_level("INFO", logger="ebay_pricing")
    _ramones_and_pods(
        query_http, fixtures_dir,
        pods=_summaries(_listing("geobasket", "Rick Owens Geobasket", "500")),
    )
    ebay_pricing.refresh_market_prices()
    output = "\n".join(record.getMessage() for record in caplog.records).lower()
    assert "pod shorts" in output
    assert "no" in output or "insufficient" in output
    assert _stored(db_conn)["pod shorts"] is None


def test_rc10_c9_http_error_isolates_item_and_keeps_earlier_prices(
    ebay_pricing, creds, query_http, fixtures_dir, seeded_db, db_conn
):
    _ramones_and_pods(query_http, fixtures_dir, pods={"errors": []}, pods_status=500)
    result = ebay_pricing.refresh_market_prices()
    assert _stored(db_conn) == {"rick owens ramones": pytest.approx(5300 / 6), "pod shorts": None}
    assert result.failed_item_ids == (db.get_item_id("Rick Owens", "pod shorts"),)
    assert result.succeeded_item_ids == (db.get_item_id("Rick Owens", "rick owens ramones"),)


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


def test_rc10_c26_ebay_no_active_items_is_a_successful_no_work_run(
    ebay_pricing, creds, temp_db, fake_http, caplog
):
    caplog.set_level("INFO", logger="ebay_pricing")
    result = ebay_pricing.main()
    assert result in (None, 0)
    assert any("no active items" in record.getMessage().lower() for record in caplog.records)
    assert fake_http.calls == []


def test_rc03b_c33_refresh_does_not_read_catalog_json(
    ebay_pricing, creds, ebay_http, seeded_db, db_conn, monkeypatch, tmp_path
):
    monkeypatch.setattr(db, "CATALOG_PATH", tmp_path / "does_not_exist.json")
    ebay_pricing.refresh_market_prices()
    assert _stored(db_conn)["rick owens ramones"] == pytest.approx(5300 / 6)
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
