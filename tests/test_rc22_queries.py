"""RC-22 criteria 24-44: the user's report, trends, and doctor queries, run through reports.main.

These tests load the real files in src/sql/ (nothing is stubbed) and compare against rows that were
worked out BY HAND from the literal fixture values in tests/rc22_fixtures.py and from the spec's
test plan.  No expected value is produced by running SQL or Python aggregation over a fixture.
Until the user writes the query files they fail with the harness's "no query yet" error; that
is intended.

Hand arithmetic behind the less obvious numbers (all from fixture F1):
- Rick Owens deals are the pairs (L1,1) unpriced, (L2,1) 26.0, (L3,1) 48.5, (L4,2) 20.0, (L4,1) 40.0,
  (L7,4) 19.0; the watch (L5,1) is excluded.  Average of the five priced: (26.0 + 48.5 + 20.0 + 40.0
  + 19.0) / 5 = 153.5 / 5 = 30.7.
- With --since 2026-10-20 the pairs of L1 and L7 drop out: (26.0 + 48.5 + 20.0 + 40.0) / 4 = 134.5 / 4
  = 33.625, which rounds to 33.6.
- With --since 2026-11-01 only the two L4 pairs and the watch L5 remain: (20.0 + 40.0) / 2 = 30.0.
- Item 1 observation order is a(110), b(100), c(130), d(90), e(120).  Window 5 rolling averages:
  110; (110 + 100) / 2 = 105; (110 + 100 + 130) / 3 = 113.33; (110 + 100 + 130 + 90) / 4 = 107.5;
  550 / 5 = 110.  Window 3: c = 340 / 3 = 113.33, d = (100 + 130 + 90) / 3 = 320 / 3 = 106.67,
  e = (130 + 90 + 120) / 3 = 113.33.
"""
import json
from collections import Counter

import pytest

import rc22_fixtures
from dbhelpers import rows as sql_rows

REPORT_COLUMNS = [
    "brand", "deals", "watches", "priced_deals",
    "avg_discount_pct", "best_discount_pct", "last_deal_date",
]
TRENDS_COLUMNS = [
    "brand", "item_id", "item", "recency_rank", "first_seen_at",
    "ebay_listing_id", "price_usd", "change_usd", "rolling_avg_usd", "window_rows",
]
CHECK_IDS = [
    "dangling_reference", "listing_brand_mismatch", "noncanonical_timestamp", "deal_discount_mismatch",
    "no_include_keywords", "no_usable_price", "thin_ebay_pool",
]
CHECK_COLUMNS = {
    "dangling_reference": ["child_table", "child_key", "parent_table", "missing_id"],
    "listing_brand_mismatch": ["listing_id", "item_id", "listing_brand", "item_brand"],
    "noncanonical_timestamp": ["table_name", "column_name", "row_key", "value"],
    "deal_discount_mismatch": ["listing_id", "item_id", "stored_pct", "computed_pct"],
    "no_include_keywords": ["item_id", "brand", "item"],
    "no_usable_price": ["item_id", "brand", "item", "market_price_usd", "manual_price_usd"],
    "thin_ebay_pool": ["item_id", "brand", "item", "observations"],
}


@pytest.fixture
def reports():
    import reports as module
    return module


def run_cli(reports, *argv):
    try:
        code = reports.main(list(argv))
    except SystemExit as exc:
        code = exc.code
    return 0 if code is None else code


def query_rows(reports, capsys, *argv, expect_exit=0):
    """Run a report or trends command with --json and return its rows as lists, in column order."""
    code = run_cli(reports, *argv, "--json")
    captured = capsys.readouterr()
    assert code == expect_exit, captured.err
    data = json.loads(captured.out)
    return [list(row.values()) for row in data["rows"]], data


def doctor(reports, capsys, expect_exit):
    code = run_cli(reports, "doctor", "--json")
    captured = capsys.readouterr()
    assert code == expect_exit, captured.err
    data = json.loads(captured.out)
    assert [c["id"] for c in data["checks"]] == CHECK_IDS
    return {c["id"]: c for c in data["checks"]}, data


def check_rows(check):
    return [list(row.values()) for row in check["rows"]]


# --- report on fixture F1 ----------------------------------------------------

RICK = ["Rick Owens", 6, 1, 5, 30.7, 48.5, "2026-11-01"]
UNDERCOVER = ["Undercover", 1, 0, 1, 38.0, 38.0, "2026-10-31"]
KAPITAL = ["Kapital", 0, 0, 0, None, None, None]


@pytest.fixture
def f1(tmp_path):
    return rc22_fixtures.build_f1(tmp_path / "test.db")


def test_rc22_c24_report_defaults_return_rick_owens_then_undercover(reports, f1, capsys):
    rows, data = query_rows(reports, capsys, "report")
    assert rows == [RICK, UNDERCOVER]
    assert list(data["rows"][0]) == REPORT_COLUMNS


def test_rc22_c25_min_deals_0_adds_kapital_as_the_third_row(reports, f1, capsys):
    rows, _ = query_rows(reports, capsys, "report", "--min-deals", "0")
    assert rows == [RICK, UNDERCOVER, KAPITAL]


def test_rc22_c25_min_deals_2_returns_only_rick_owens(reports, f1, capsys):
    rows, _ = query_rows(reports, capsys, "report", "--min-deals", "2")
    assert rows == [RICK]


def test_rc22_c26_since_2026_10_20_includes_the_pair_exactly_at_midnight(reports, f1, capsys):
    rows, _ = query_rows(reports, capsys, "report", "--since", "2026-10-20")
    # (L2,1) was matched at exactly 2026-10-20T00:00:00+00:00 and counts: 4 deals, not 3
    assert rows == [["Rick Owens", 4, 1, 4, 33.6, 48.5, "2026-11-01"], UNDERCOVER]


def test_rc22_c27_since_with_min_deals_0_still_includes_the_zero_deal_brand(reports, f1, capsys):
    rows, _ = query_rows(reports, capsys, "report", "--since", "2026-10-20", "--min-deals", "0")
    assert rows == [["Rick Owens", 4, 1, 4, 33.6, 48.5, "2026-11-01"], UNDERCOVER, KAPITAL]


def test_rc22_since_compares_fractional_second_timestamps_on_the_right_side_of_midnight(reports, f1, capsys):
    # (L6,5) at 2026-10-31T23:59:59.999999 is before 2026-11-01; the two L4 pairs at 08:30:00.000001 are after
    rows, _ = query_rows(reports, capsys, "report", "--since", "2026-11-01")
    assert rows == [["Rick Owens", 2, 1, 2, 30.0, 40.0, "2026-11-01"]]


def test_rc22_since_after_every_deal_keeps_a_watch_only_brand_with_no_statistics(reports, f1, capsys):
    # only the watch (L5,1) at 2026-11-05 is on or after 2026-11-02, so every brand has 0 deals
    rows, _ = query_rows(reports, capsys, "report", "--since", "2026-11-02", "--min-deals", "0")
    assert rows == [
        ["Kapital", 0, 0, 0, None, None, None],
        ["Rick Owens", 0, 1, 0, None, None, None],
        ["Undercover", 0, 0, 0, None, None, None],
    ]


def test_rc22_report_text_output_on_f1_ends_with_the_legacy_note(reports, f1, capsys):
    assert run_cli(reports, "report") == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    # (L1,1) is the one Rick Owens deal recorded without a discount
    assert lines[-1] == (
        "Note: 1 deal(s) were recorded before discounts were stored and are excluded from "
        "avg_discount_pct and best_discount_pct."
    )


# --- report: empty, single-row, legacy-only, and watch-only fixtures ---------


def test_rc22_c28_report_on_an_empty_database_says_no_brands_and_exits_0(reports, tmp_path, capsys):
    rc22_fixtures.build_f0(tmp_path / "test.db")
    assert run_cli(reports, "report") == 0
    assert "No brands with at least 1 deal." in capsys.readouterr().out.splitlines()


def test_rc22_c28_report_min_deals_0_on_an_empty_database_returns_no_rows(reports, tmp_path, capsys):
    rc22_fixtures.build_f0(tmp_path / "test.db")
    rows, _ = query_rows(reports, capsys, "report", "--min-deals", "0")
    assert rows == []


def test_rc22_c29_single_priced_deal_row(reports, tmp_path, capsys):
    rc22_fixtures.build_single_row(tmp_path / "test.db")
    rows, _ = query_rows(reports, capsys, "report")
    assert rows == [["Rick Owens", 1, 0, 1, 12.3, 12.3, "2026-10-12"]]


def test_rc22_c30_brand_with_only_a_pre_v5_deal_has_no_statistics_and_prints_the_note(
    reports, tmp_path, capsys
):
    rc22_fixtures.build_pre_v5_only(tmp_path / "test.db")
    rows, _ = query_rows(reports, capsys, "report")
    assert rows == [["Kapital", 1, 0, 0, None, None, "2026-09-06"]]

    assert run_cli(reports, "report") == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines[-1] == (
        "Note: 1 deal(s) were recorded before discounts were stored and are excluded from "
        "avg_discount_pct and best_discount_pct."
    )


def test_rc22_c31_watch_only_brand_appears_only_with_min_deals_0(reports, tmp_path, capsys):
    rc22_fixtures.build_watch_only(tmp_path / "test.db")
    rows, _ = query_rows(reports, capsys, "report")
    assert rows == []
    rows, _ = query_rows(reports, capsys, "report", "--min-deals", "0")
    assert rows == [["Undercover", 0, 1, 0, None, None, None]]


# --- trends on fixture F1 ----------------------------------------------------

SEEN = {
    "legacy:a": "2026-10-01T00:00:00+00:00",
    "rest:b": "2026-10-01T00:00:00+00:00",
    "legacy:c": "2026-10-02T00:00:00.250000+00:00",
    "legacy:d": "2026-10-03T00:00:00+00:00",
    "legacy:e": "2026-10-03T00:00:00+00:00",
}


def ramones(listing, price, change, rolling, window_rows, rank):
    return ["Rick Owens", 1, "Ramones", rank, SEEN[listing], listing, price, change, rolling, window_rows]


POD = ["Rick Owens", 2, "Pod Shorts", 1, "2026-10-05T00:00:00+00:00", "legacy:z", 250.0, None, 250.0, 1]
GEOBASKET = ["Rick Owens", 4, "Geobasket", 1, "2026-10-06T00:00:00+00:00", "legacy:g", 400.0, None, 400.0, 1]


def test_rc22_c32_window_3_latest_5_orders_ties_by_listing_id_not_insertion_order(reports, f1, capsys):
    rows, data = query_rows(reports, capsys, "trends", "--window", "3", "--latest", "5")
    # rest:b was inserted before legacy:a but they share first_seen_at, so legacy:a is first
    assert rows == [
        POD,
        ramones("legacy:a", 110.0, None, 110.0, 1, 5),
        ramones("rest:b", 100.0, -10.0, 105.0, 2, 4),
        ramones("legacy:c", 130.0, 30.0, 113.33, 3, 3),
        ramones("legacy:d", 90.0, -40.0, 106.67, 3, 2),
        ramones("legacy:e", 120.0, 30.0, 113.33, 3, 1),
    ]
    assert list(data["rows"][0]) == TRENDS_COLUMNS


def test_rc22_c33_latest_2_still_uses_undisplayed_rows_for_change_and_window(reports, f1, capsys):
    rows, _ = query_rows(reports, capsys, "trends", "--window", "3", "--latest", "2", "--item", "1")
    assert rows == [
        ramones("legacy:d", 90.0, -40.0, 106.67, 3, 2),
        ramones("legacy:e", 120.0, 30.0, 113.33, 3, 1),
    ]


def test_rc22_c33_latest_2_for_all_active_items_keeps_the_single_observation_item(reports, f1, capsys):
    rows, _ = query_rows(reports, capsys, "trends", "--window", "3", "--latest", "2")
    assert rows == [
        POD,
        ramones("legacy:d", 90.0, -40.0, 106.67, 3, 2),
        ramones("legacy:e", 120.0, 30.0, 113.33, 3, 1),
    ]


def test_rc22_c34_defaults_latest_10_window_5(reports, f1, capsys):
    rows, data = query_rows(reports, capsys, "trends")
    assert rows == [
        POD,
        ramones("legacy:a", 110.0, None, 110.0, 1, 5),
        ramones("rest:b", 100.0, -10.0, 105.0, 2, 4),
        ramones("legacy:c", 130.0, 30.0, 113.33, 3, 3),
        ramones("legacy:d", 90.0, -40.0, 107.5, 4, 2),
        ramones("legacy:e", 120.0, 30.0, 110.0, 5, 1),
    ]
    assert data["parameters"]["latest"] == 10 and data["parameters"]["window"] == 5


def test_rc22_c35_single_observation_item_has_no_change_and_a_window_of_one(reports, f1, capsys):
    rows, _ = query_rows(reports, capsys, "trends", "--item", "2")
    assert rows == [["Rick Owens", 2, "Pod Shorts", 1, "2026-10-05T00:00:00+00:00", "legacy:z", 250.0, None, 250.0, 1]]


def test_rc22_c36_inactive_item_is_hidden_by_default_and_shown_with_include_inactive(reports, f1, capsys):
    rows, _ = query_rows(reports, capsys, "trends")
    assert 4 not in [row[1] for row in rows]

    rows, _ = query_rows(reports, capsys, "trends", "--include-inactive")
    # brand, then item name ascending: Geobasket, Pod Shorts, Ramones
    assert [row[1] for row in rows] == [4, 2, 1, 1, 1, 1, 1]
    assert rows[0] == GEOBASKET


def test_rc22_c36_item_filter_selects_an_inactive_item_without_include_inactive(reports, f1, capsys):
    rows, _ = query_rows(reports, capsys, "trends", "--item", "4")
    assert rows == [GEOBASKET]


@pytest.mark.parametrize("item_id", ["5", "999"], ids=["active_without_observations", "unknown_item"])
def test_rc22_c36_item_without_observations_or_unknown_returns_no_rows_and_exits_0(
    reports, f1, capsys, item_id
):
    rows, _ = query_rows(reports, capsys, "trends", "--item", item_id)
    assert rows == []


def test_rc22_c37_trends_on_an_empty_database_says_no_observations(reports, tmp_path, capsys):
    rc22_fixtures.build_f0(tmp_path / "test.db")
    assert run_cli(reports, "trends") == 0
    assert "No eBay price observations." in capsys.readouterr().out.splitlines()


def test_rc22_c38_window_1_makes_the_rolling_average_equal_the_price_on_every_row(reports, f1, capsys):
    rows, _ = query_rows(reports, capsys, "trends", "--window", "1")
    assert rows == [
        POD,
        ramones("legacy:a", 110.0, None, 110.0, 1, 5),
        ramones("rest:b", 100.0, -10.0, 100.0, 1, 4),
        ramones("legacy:c", 130.0, 30.0, 130.0, 1, 3),
        ramones("legacy:d", 90.0, -40.0, 90.0, 1, 2),
        ramones("legacy:e", 120.0, 30.0, 120.0, 1, 1),
    ]


# --- doctor ------------------------------------------------------------------


def test_rc22_c39_doctor_on_an_empty_database_is_all_ok(reports, tmp_path, capsys):
    rc22_fixtures.build_f0(tmp_path / "test.db")
    assert run_cli(reports, "doctor") == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines == [f"[ok] {check_id}" for check_id in CHECK_IDS] + [
        "doctor: 0 error findings, 0 warning findings, 7 of 7 checks ok."
    ]


def test_rc22_c40_doctor_on_f1_reports_the_configuration_findings_and_exits_3(reports, f1, capsys):
    checks, data = doctor(reports, capsys, expect_exit=3)

    assert check_rows(checks["no_include_keywords"]) == [
        [3, "Undercover", "Scab Jacket"],
        [6, "Undercover", "Hoodie"],
    ]
    assert check_rows(checks["no_usable_price"]) == [
        [3, "Undercover", "Scab Jacket", None, None],
        [6, "Undercover", "Hoodie", 0.0, None],
    ]
    assert check_rows(checks["thin_ebay_pool"]) == [
        [2, "Rick Owens", "Pod Shorts", 1],
        [3, "Undercover", "Scab Jacket", 0],
        [5, "Undercover", "Bones Tee", 0],
        [6, "Undercover", "Hoodie", 0],
    ]
    for quiet in ("dangling_reference", "listing_brand_mismatch", "noncanonical_timestamp", "deal_discount_mismatch"):
        assert checks[quiet]["rows"] == [], quiet
    assert data["error_findings"] == 2
    assert data["warning_findings"] == 6
    assert [checks[c]["count"] for c in CHECK_IDS] == [0, 0, 0, 0, 2, 2, 4]


def test_rc22_c40_doctor_text_on_f1_status_lines_and_summary(reports, f1, capsys):
    assert run_cli(reports, "doctor") == 3
    lines = capsys.readouterr().out.splitlines()
    assert any(line.startswith("[error] no_include_keywords: 2 findings - ") for line in lines)
    assert any(line.startswith("[warning] no_usable_price: 2 findings - ") for line in lines)
    assert any(line.startswith("[warning] thin_ebay_pool: 4 findings - ") for line in lines)
    assert "[ok] dangling_reference" in lines
    assert lines[-1] == "doctor: 2 error findings, 6 warning findings, 4 of 7 checks ok."


@pytest.fixture
def f2(tmp_path):
    return rc22_fixtures.build_f2(tmp_path / "test.db")


# Sorted by child_table, parent_table, missing_id, child_key (all ascending, binary collation).
F2_DANGLING = [
    ["ebay_listing_prices", "item_id=996 ebay_listing_id=legacy:x", "items", 996],
    ["item_keywords", "item_id=997 keyword=ghost", "items", 997],
    ["items", "id=3", "brands", 94],
    ["listing_matches", "listing_id=98 item_id=998", "items", 998],
    ["listing_matches", "listing_id=10 item_id=999", "items", 999],
    ["listing_matches", "listing_id=98 item_id=998", "listings", 98],
    ["listing_matches", "listing_id=99 item_id=1", "listings", 99],
    ["listings", "id=11", "brands", 95],
]


def test_rc22_c41_dangling_reference_returns_exactly_the_eight_findings_in_contract_order(reports, f2, capsys):
    checks, _ = doctor(reports, capsys, expect_exit=3)
    assert check_rows(checks["dangling_reference"]) == F2_DANGLING
    assert checks["dangling_reference"]["count"] == 8


def test_rc22_c41_dangling_reference_agrees_with_sqlite_foreign_key_check(reports, f2, capsys):
    # PRAGMA foreign_key_check is SQLite's built-in check, not one of the user's queries.
    # Each result row is (child table, rowid, parent table, foreign key index).
    builtin = Counter((child, parent) for child, _rowid, parent, _fk in sql_rows(f2, "PRAGMA foreign_key_check"))
    checks, _ = doctor(reports, capsys, expect_exit=3)
    users = Counter((row[0], row[2]) for row in check_rows(checks["dangling_reference"]))
    assert users == builtin
    assert sum(users.values()) == 8


def test_rc22_c42_listing_brand_mismatch_on_f2(reports, f2, capsys):
    checks, _ = doctor(reports, capsys, expect_exit=3)
    assert check_rows(checks["listing_brand_mismatch"]) == [[12, 1, "Undercover", "Rick Owens"]]


def test_rc22_c42_noncanonical_timestamp_on_f2(reports, f2, capsys):
    checks, _ = doctor(reports, capsys, expect_exit=3)
    assert check_rows(checks["noncanonical_timestamp"]) == [
        ["ebay_listing_prices", "first_seen_at", "item_id=2 ebay_listing_id=legacy:short",
         "2026-10-01T12:00:00.25+00:00"],
        ["listing_matches", "matched_at", "listing_id=12 item_id=1", "2026-10-01T12:00:00Z"],
        ["listings", "first_seen_at", "id=12", "2026-10-01 12:00:00"],
    ]


def test_rc22_c42_deal_discount_mismatch_on_f2_reports_only_the_pair_outside_tolerance(reports, f2, capsys):
    checks, _ = doctor(reports, capsys, expect_exit=3)
    # stored 50.0 against 100 * (400 - 60000 * 0.005) / 400 = 100 * 100 / 400 = 25.0.
    # Listing 13 stores 25.0005, within the 0.001 tolerance, and is not reported.
    found = check_rows(checks["deal_discount_mismatch"])
    assert len(found) == 1
    listing_id, item_id, stored, computed = found[0]
    assert (listing_id, item_id) == (10, 1)
    assert stored == pytest.approx(50.0)
    assert computed == pytest.approx(25.0)


def test_rc22_c42_the_remaining_checks_on_f2_find_nothing(reports, f2, capsys):
    checks, data = doctor(reports, capsys, expect_exit=3)
    for quiet in ("no_include_keywords", "no_usable_price", "thin_ebay_pool"):
        assert checks[quiet]["rows"] == [], quiet
    assert data["error_findings"] == 8 + 1 + 3 + 1
    assert data["warning_findings"] == 0


def test_rc22_c43_a_database_with_only_warning_findings_exits_0(reports, tmp_path, capsys):
    rc22_fixtures.build_warning_only(tmp_path / "test.db")
    checks, data = doctor(reports, capsys, expect_exit=0)
    assert check_rows(checks["thin_ebay_pool"]) == [[1, "Rick Owens", "Ramones", 0]]
    assert data["error_findings"] == 0
    assert data["warning_findings"] == 1

    assert run_cli(reports, "doctor") == 0
    lines = capsys.readouterr().out.splitlines()
    assert any(line.startswith("[warning] thin_ebay_pool: 1 finding - ") for line in lines)


def test_rc22_c44_doctor_json_lists_all_seven_checks_in_order_with_counts_and_totals(reports, f1, capsys):
    checks, data = doctor(reports, capsys, expect_exit=3)
    assert data["command"] == "doctor"
    assert [c["severity"] for c in data["checks"]] == ["error"] * 5 + ["warning"] * 2
    assert [c["count"] for c in data["checks"]] == [0, 0, 0, 0, 2, 2, 4]
    for check_id in CHECK_IDS:
        for row in checks[check_id]["rows"]:
            assert list(row) == CHECK_COLUMNS[check_id]
    assert data["error_findings"] == 2
    assert data["warning_findings"] == 6
