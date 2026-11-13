"""RC-22 criteria 8-14: recording deal-time pricing on listing_matches.

Expected values are literals chosen by hand.  The end-to-end tests follow the RC-16 style:
stubbed Yahoo page, stubbed Frankfurter response through FakeHTTP, stubbed send_email.
"""
import dataclasses
import logging
import math
from datetime import date, datetime, timezone

import pytest
import requests

import db
import yahoo_scrape
from digest import Deal
from dbhelpers import rows, run

DEAL_COLUMNS_SQL = (
    "SELECT deal_kind, deal_price_yen, deal_market_price_usd, deal_yen_to_usd, "
    "deal_rate_is_fallback, deal_discount_pct FROM listing_matches ORDER BY listing_id, item_id"
)
AUCTION = "https://auctions.yahoo.co.jp/jp/auction/"
FRANKFURTER = yahoo_scrape.FRANKFURTER_RATE_URL


@pytest.fixture
def one_item_db(temp_db):
    """A current-version database with one brand and one item (ids 1 and 1)."""
    run(temp_db, "INSERT INTO brands (id, name, yahoo_brand_id) VALUES (1, 'Rick Owens', '103538')")
    run(temp_db, "INSERT INTO items (id, brand_id, name, manual_price_usd) VALUES (1, 1, 'ramones', 800.0)")
    return temp_db


def _deal(n, *, kind="fixed_price", price_yen=50000, market=400.0, discount=37.5, item_id=1):
    return Deal(
        item_id, 1, f"{AUCTION}t{n}", f"Ramones {n}", price_yen, price_yen * 0.005,
        market, discount, None, kind=kind,
    )


def _record(deals, **overrides):
    kwargs = {"yen_to_usd_rate": 0.005, "rate_is_fallback": False}
    kwargs.update(overrides)
    return db.record_listing_matches(deals, **kwargs)


def _forbid_connection(monkeypatch):
    def unexpected():
        raise AssertionError("validation must finish before any database connection is opened")

    monkeypatch.setattr(db, "get_connection", unexpected)


def _row_counts(path):
    return (
        rows(path, "SELECT COUNT(*) FROM listings")[0][0],
        rows(path, "SELECT COUNT(*) FROM listing_matches")[0][0],
    )


# --- Criterion 8 -------------------------------------------------------------


def test_rc22_c8_one_deal_of_each_kind_stores_its_deal_time_values(one_item_db):
    deals = [
        _deal(1, kind="fixed_price", price_yen=50000, market=400.0, discount=37.5),
        _deal(2, kind="auction_buy_now", price_yen=60000, market=800.0, discount=62.5),
        # an unrounded discount, stored exactly as given
        _deal(3, kind="auction_watch", price_yen=1000, market=333.0, discount=98.49849849849849),
    ]
    assert _record(deals, yen_to_usd_rate=0.005, rate_is_fallback=False) == 3

    assert rows(one_item_db, DEAL_COLUMNS_SQL) == [
        ("fixed_price", 50000, 400.0, 0.005, 0, 37.5),
        ("auction_buy_now", 60000, 800.0, 0.005, 0, 62.5),
        ("auction_watch", 1000, 333.0, 0.005, 0, 98.49849849849849),
    ]


def test_rc22_c8_fallback_run_stores_flag_1_and_the_fallback_rate(one_item_db):
    assert _record([_deal(1)], yen_to_usd_rate=0.0062, rate_is_fallback=True) == 1
    assert rows(one_item_db, DEAL_COLUMNS_SQL) == [("fixed_price", 50000, 400.0, 0.0062, 1, 37.5)]


def test_rc22_c8_module_level_wrapper_forwards_the_new_keyword_arguments(one_item_db):
    assert yahoo_scrape.record_listing_matches(
        [_deal(1)], yen_to_usd_rate=0.007, rate_is_fallback=False
    ) == 1
    assert rows(one_item_db, DEAL_COLUMNS_SQL) == [("fixed_price", 50000, 400.0, 0.007, 0, 37.5)]


def test_rc22_c8_module_level_wrapper_requires_the_rate_arguments(one_item_db):
    with pytest.raises(TypeError):
        yahoo_scrape.record_listing_matches([_deal(1)])
    assert _row_counts(one_item_db) == (0, 0)


def test_rc22_single_pair_api_keeps_null_deal_columns(one_item_db):
    assert db.record_listing_match(1, f"{AUCTION}single", "Single", 10000, None, 1)
    assert rows(one_item_db, DEAL_COLUMNS_SQL) == [(None,) * 6]


# --- Criterion 9 -------------------------------------------------------------


def test_rc22_c9_buy_now_deal_keeps_current_bid_on_listing_and_buy_now_price_on_pair(one_item_db):
    source = yahoo_scrape.YahooListing(
        auction_id="t1", url=f"{AUCTION}t1", title="Ramones 1", price_yen=20000,
        image_url=None, is_fixed_price=False, buy_now_price_yen=41200,
        scheduled_end_at=datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc),
    )
    deal = _deal(1, kind="auction_buy_now", price_yen=41200, market=400.0, discount=48.5)

    assert _record([deal], sources_by_url={deal.url: source}) == 1

    assert rows(one_item_db, "SELECT price_yen, buy_now_price_yen FROM listings") == [(20000, 41200)]
    assert rows(one_item_db, DEAL_COLUMNS_SQL) == [("auction_buy_now", 41200, 400.0, 0.005, 0, 48.5)]


# --- Criterion 10 ------------------------------------------------------------


def test_rc22_c10_re_recording_a_pair_keeps_the_original_values_and_matched_at(one_item_db):
    assert _record([_deal(1, price_yen=50000, market=400.0, discount=37.5)]) == 1
    first = rows(one_item_db, "SELECT matched_at, deal_kind, deal_price_yen, deal_market_price_usd, "
                              "deal_yen_to_usd, deal_rate_is_fallback, deal_discount_pct FROM listing_matches")

    changed = _deal(1, kind="auction_watch", price_yen=70000, market=900.0, discount=10.0)
    assert _record([changed], yen_to_usd_rate=0.0062, rate_is_fallback=True) == 0

    assert rows(one_item_db, "SELECT matched_at, deal_kind, deal_price_yen, deal_market_price_usd, "
                             "deal_yen_to_usd, deal_rate_is_fallback, deal_discount_pct FROM listing_matches") == first
    assert first[0][1:] == ("fixed_price", 50000, 400.0, 0.005, 0, 37.5)


# --- Criterion 11 ------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"yen_to_usd_rate": 0.005}, {"rate_is_fallback": False}],
    ids=["neither", "missing_fallback_flag", "missing_rate"],
)
def test_rc22_c11_missing_rate_arguments_raise_type_error_before_any_connection(
    one_item_db, monkeypatch, kwargs
):
    _forbid_connection(monkeypatch)
    with pytest.raises(TypeError):
        db.record_listing_matches([_deal(1)], **kwargs)
    assert _row_counts(one_item_db) == (0, 0)


@pytest.mark.parametrize(
    "rate",
    [0, 0.0, -0.005, math.nan, math.inf, -math.inf, True, False, "0.005", None],
    ids=["zero", "zero_float", "negative", "nan", "inf", "neg_inf", "true", "false", "string", "none"],
)
def test_rc22_c11_invalid_rates_raise_value_error_and_write_nothing(one_item_db, monkeypatch, rate):
    _forbid_connection(monkeypatch)
    with pytest.raises(ValueError):
        _record([_deal(1)], yen_to_usd_rate=rate)
    assert _row_counts(one_item_db) == (0, 0)


@pytest.mark.parametrize("flag", [0, 1, "yes", None, 0.0], ids=["zero", "one", "string", "none", "float"])
def test_rc22_c11_non_bool_fallback_flag_raises_value_error_and_writes_nothing(
    one_item_db, monkeypatch, flag
):
    _forbid_connection(monkeypatch)
    with pytest.raises(ValueError):
        _record([_deal(1)], rate_is_fallback=flag)
    assert _row_counts(one_item_db) == (0, 0)


BAD_DEAL_FIELDS = [
    ("kind", "bogus"),
    ("kind", ""),
    ("kind", None),
    ("price_yen", 0),
    ("price_yen", -1),
    ("price_yen", True),
    ("price_yen", 1.5),
    ("price_yen", "100"),
    ("market_price_usd", 0),
    ("market_price_usd", -1.0),
    ("market_price_usd", math.nan),
    ("market_price_usd", math.inf),
    ("discount_pct", 0),
    ("discount_pct", -5.0),
    ("discount_pct", 100),
    ("discount_pct", 101.0),
    ("discount_pct", math.nan),
    ("discount_pct", math.inf),
]


@pytest.mark.parametrize("field, bad", BAD_DEAL_FIELDS, ids=[f"{f}={b!r}" for f, b in BAD_DEAL_FIELDS])
def test_rc22_c11_invalid_deal_value_raises_value_error_and_writes_nothing(
    one_item_db, monkeypatch, field, bad
):
    _forbid_connection(monkeypatch)
    invalid = dataclasses.replace(_deal(2), **{field: bad})
    # the valid deal comes first: it must not be written either
    with pytest.raises(ValueError):
        _record([_deal(1), invalid])
    assert _row_counts(one_item_db) == (0, 0)


# --- Criterion 12 ------------------------------------------------------------


def test_rc22_c12_yen_rate_is_a_frozen_dataclass_with_rate_and_is_fallback():
    value = yahoo_scrape.YenRate(0.0062, True)
    assert dataclasses.is_dataclass(value)
    assert [f.name for f in dataclasses.fields(value)] == ["rate", "is_fallback"]
    assert value == yahoo_scrape.YenRate(rate=0.0062, is_fallback=True)
    with pytest.raises(dataclasses.FrozenInstanceError):
        value.rate = 1.0


def _rate_body(rate, published="2026-10-27"):
    return {"date": published, "base": "JPY", "quote": "USD", "rate": rate}


def test_rc22_c12_valid_provider_response_returns_the_provider_rate_without_a_warning(fake_http, caplog):
    fake_http.add("GET", FRANKFURTER, json=_rate_body(0.0067))
    with caplog.at_level(logging.WARNING, logger="yahoo_scrape"):
        result = yahoo_scrape.resolve_yen_to_usd_rate(date(2026, 10, 27))
    assert result == yahoo_scrape.YenRate(0.0067, False)
    assert [r for r in caplog.records if r.name == "yahoo_scrape"] == []


@pytest.mark.parametrize(
    "scenario", ["http_500", "network_error", "timeout", "zero_rate", "missing_rate", "stale_date"]
)
def test_rc22_c12_provider_failure_returns_the_fallback_and_warns_once(
    scenario, fake_http, monkeypatch, caplog
):
    if scenario == "http_500":
        fake_http.add("GET", FRANKFURTER, json=_rate_body(0.0067), status=500)
    elif scenario in ("network_error", "timeout"):
        error = requests.ConnectionError("offline") if scenario == "network_error" else requests.Timeout("slow")

        def fail(url, **kwargs):
            raise error

        monkeypatch.setattr(requests, "get", fail)
    elif scenario == "zero_rate":
        fake_http.add("GET", FRANKFURTER, json=_rate_body(0))
    elif scenario == "missing_rate":
        fake_http.add("GET", FRANKFURTER, json={"date": "2026-10-27", "base": "JPY", "quote": "USD"})
    else:
        fake_http.add("GET", FRANKFURTER, json=_rate_body(0.0067, published="2026-10-01"))

    with caplog.at_level(logging.WARNING, logger="yahoo_scrape"):
        result = yahoo_scrape.resolve_yen_to_usd_rate(date(2026, 10, 27))

    assert result == yahoo_scrape.YenRate(0.0062, True)
    warnings = [r for r in caplog.records if r.name == "yahoo_scrape"]
    assert len(warnings) == 1
    assert "0.0062" in warnings[0].getMessage()


# --- Criteria 13 and 14: end to end through yahoo_scrape.main() --------------


def _frozen_clock(monkeypatch, moment):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return moment.replace(tzinfo=None)
            return moment.astimezone(tz)

    monkeypatch.setattr(yahoo_scrape, "datetime", FrozenDateTime)


def _listing(n, price, *, fixed, buy_now=None):
    return yahoo_scrape.YahooListing(
        auction_id=f"t{n}", url=f"{AUCTION}t{n}", title=f"Rick Owens Ramones {n}",
        price_yen=price,
        image_url=f"https://auc-pctr.c.yimg.jp/i/auctions.c.yimg.jp/images.auctions.yahoo.co.jp/image/dr000/auc0210/users/45a6ce28d3cf2f865f425e68b2890ed9a088ceac/i-img100x100-1792990662638j63eeo-t{n}.jpg",
        is_fixed_price=fixed, buy_now_price_yen=buy_now,
        scheduled_end_at=datetime.fromisoformat("2026-11-01T12:00:00+00:00"),
    )


def _stub_yahoo(fake_http, monkeypatch, listings):
    monkeypatch.setattr(yahoo_scrape, "parse_listings", lambda _html: listings)
    fake_http.add("GET", yahoo_scrape.RICK_OWENS_URL, text="unused")
    _frozen_clock(monkeypatch, datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc))


def _two_deals():
    # a fixed-price listing and a buy-now auction whose current bid is lower than the buy-now price
    return [
        _listing(1, 40000, fixed=True, buy_now=40000),
        _listing(2, 50000, fixed=False, buy_now=90000),
    ]


def test_rc22_c13_provider_failure_records_the_fallback_flag_and_rate_on_every_new_pair(
    fake_http, seeded_db, sent_emails, monkeypatch
):
    _stub_yahoo(fake_http, monkeypatch, _two_deals())
    fake_http.add("GET", FRANKFURTER, json=_rate_body(0.0067), status=503)

    assert yahoo_scrape.main() == 0

    assert len(sent_emails) == 1
    stored = rows(seeded_db, DEAL_COLUMNS_SQL)
    assert len(stored) == 2
    assert all(row[4] == 1 and row[3] == 0.0062 for row in stored)
    assert all(value is not None for row in stored for value in row)


def test_rc22_c13_provider_rate_is_recorded_with_flag_0_on_every_new_pair(
    fake_http, seeded_db, sent_emails, monkeypatch
):
    _stub_yahoo(fake_http, monkeypatch, _two_deals())
    fake_http.add("GET", FRANKFURTER, json=_rate_body(0.007))

    assert yahoo_scrape.main() == 0

    stored = rows(seeded_db, DEAL_COLUMNS_SQL)
    assert len(stored) == 2
    assert all(row[4] == 0 and row[3] == 0.007 for row in stored)
    assert all(value is not None for row in stored for value in row)


def test_rc22_c13_end_to_end_pairs_carry_kind_compared_price_and_discount(
    fake_http, seeded_db, sent_emails, monkeypatch
):
    _stub_yahoo(fake_http, monkeypatch, _two_deals())
    fake_http.add("GET", FRANKFURTER, json=_rate_body(0.007))

    assert yahoo_scrape.main() == 0

    by_url = {
        url: values for url, *values in rows(
            seeded_db,
            "SELECT l.url, m.deal_kind, m.deal_price_yen, m.deal_market_price_usd, m.deal_discount_pct, "
            "l.price_yen FROM listing_matches m JOIN listings l ON l.id = m.listing_id",
        )
    }
    # fixed price: 40000 yen * 0.007 = 280 USD against the seeded 800 USD market price -> 65.0 percent
    kind, price_yen, market, discount, listing_price = by_url[f"{AUCTION}t1"]
    assert (kind, price_yen, market, listing_price) == ("fixed_price", 40000, 800.0, 40000)
    assert discount == pytest.approx(65.0)
    # buy-now auction: compared at the buy-now price 90000 yen * 0.007 = 630 USD -> 21.25 percent
    kind, price_yen, market, discount, listing_price = by_url[f"{AUCTION}t2"]
    assert (kind, price_yen, market, listing_price) == ("auction_buy_now", 90000, 800.0, 50000)
    assert discount == pytest.approx(21.25)


def test_rc22_c14_failed_send_records_nothing(fake_http, seeded_db, monkeypatch):
    _stub_yahoo(fake_http, monkeypatch, _two_deals())
    fake_http.add("GET", FRANKFURTER, json=_rate_body(0.007))
    monkeypatch.setattr(yahoo_scrape, "send_email", lambda *args, **kwargs: False)

    with pytest.raises(SystemExit):
        yahoo_scrape.main()

    assert _row_counts(seeded_db) == (0, 0)


def test_rc22_c14_preview_records_nothing(fake_http, seeded_db, sent_emails, monkeypatch, capsys):
    _stub_yahoo(fake_http, monkeypatch, _two_deals())
    fake_http.add("GET", FRANKFURTER, json=_rate_body(0.007))

    assert yahoo_scrape.main(preview=True) == 0

    assert sent_emails == []
    assert _row_counts(seeded_db) == (0, 0)
    capsys.readouterr()
