"""RC-22 criteria 15-23: the read-only reports.py harness, independent of the user's SQL.

Formatting, JSON, and exit-code tests stub `reports.run_query` with canned columns and rows.
The read-only and bad-file tests repoint `reports.SQL_DIR` at a temporary directory that holds
deliberately bad files.  Those files are harness error inputs (a write, two statements, wrong
column names, an unknown parameter), not report, trends, or doctor queries.

ASSUMPTIONS the spec does not pin down (flagged in the hand-off report):
- reports.main(argv) either returns an exit code or raises SystemExit; `run_cli` accepts both.
- reports.run_query is replaced by a stub that returns `(columns, rows)`, where rows are tuples,
  and that is called with something identifying the query: a query name or file path whose
  basename (minus .sql) is "report", "trends", or a doctor check id.
- `parameters` in --json output use the bound parameter names (min_deals, since, latest, window,
  item_id, include_inactive).
"""
import hashlib
import json
import os
import sqlite3

import pytest

import db
import rc22_fixtures
from dbhelpers import sha256

REPORT_COLUMNS = [
    "brand", "deals", "watches", "priced_deals",
    "avg_discount_pct", "best_discount_pct", "last_deal_date",
]
TRENDS_COLUMNS = [
    "brand", "item_id", "item", "recency_rank", "first_seen_at",
    "ebay_listing_id", "price_usd", "change_usd", "rolling_avg_usd", "window_rows",
]
DOCTOR_CHECKS = [
    # (check_id, severity, columns)
    ("dangling_reference", "error", ["child_table", "child_key", "parent_table", "missing_id"]),
    ("listing_brand_mismatch", "error", ["listing_id", "item_id", "listing_brand", "item_brand"]),
    ("noncanonical_timestamp", "error", ["table_name", "column_name", "row_key", "value"]),
    ("deal_discount_mismatch", "error", ["listing_id", "item_id", "stored_pct", "computed_pct"]),
    ("no_include_keywords", "error", ["item_id", "brand", "item"]),
    ("no_usable_price", "warning", ["item_id", "brand", "item", "market_price_usd", "manual_price_usd"]),
    ("thin_ebay_pool", "warning", ["item_id", "brand", "item", "observations"]),
]
CHECK_IDS = [c[0] for c in DOCTOR_CHECKS]
COMMANDS = ["report", "trends", "doctor"]
HEADER = "-- Question: which deals, how many; ordered by deals.\n-- Write your query below.\n"


@pytest.fixture
def reports():
    import reports as module
    return module


def run_cli(reports, *argv):
    """Exit code of one CLI call, whether main() returns it or raises SystemExit."""
    try:
        code = reports.main(list(argv))
    except SystemExit as exc:
        code = exc.code
    return 0 if code is None else code


def stderr_lines(capsys_result):
    return [line for line in capsys_result.err.splitlines() if line.strip()]


def assert_one_error_line(captured):
    lines = stderr_lines(captured)
    assert len(lines) == 1, captured.err
    assert lines[0].startswith("Error:"), lines[0]
    return lines[0]


def install_stub(reports, monkeypatch, data=None, *, raises=None, seen=None):
    """Replace reports.run_query.  `data` maps query key -> (columns, rows); `raises` key -> exception."""
    data = data or {}
    raises = raises or {}
    keys = {"report", "trends", *CHECK_IDS}

    def identify(args, kwargs):
        for value in (*args, *kwargs.values()):
            if isinstance(value, (str, os.PathLike)):
                base = os.path.basename(os.fspath(value))
                stem = base[:-4] if base.endswith(".sql") else base
                if stem in keys:
                    return stem
        raise AssertionError("stubbed run_query could not tell which query was requested")

    def fake(*args, **kwargs):
        key = identify(args, kwargs)
        if seen is not None:
            seen.append((key, args, kwargs))
        if key in raises:
            raise raises[key]
        columns, rows = data.get(key, (default_columns(key), []))
        return list(columns), [tuple(r) for r in rows]

    monkeypatch.setattr(reports, "run_query", fake)


def default_columns(key):
    if key == "report":
        return REPORT_COLUMNS
    if key == "trends":
        return TRENDS_COLUMNS
    return next(cols for cid, _sev, cols in DOCTOR_CHECKS if cid == key)


def forbid_database(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("no database may be opened")

    monkeypatch.setattr(db, "get_read_only_connection", unexpected)
    monkeypatch.setattr(db, "get_connection", unexpected)


def write_sql_dir(tmp_path, files):
    """Create a temporary SQL directory; `files` maps relative path -> text."""
    root = tmp_path / "sql_dir"
    (root / "doctor").mkdir(parents=True)
    for name, text in files.items():
        (root / name).write_text(text, encoding="utf-8")
    return root


# --- Criterion 15 ------------------------------------------------------------


@pytest.mark.parametrize(
    "argv", [(), ("report",), ("trends",), ("doctor",)], ids=["main", "report", "trends", "doctor"]
)
def test_rc22_c15_help_exits_0_without_opening_or_creating_a_database(
    reports, monkeypatch, tmp_path, capsys, argv
):
    forbid_database(monkeypatch)
    assert run_cli(reports, *argv, "--help") == 0
    out = capsys.readouterr().out
    assert "usage" in out.lower()
    assert not (tmp_path / "test.db").exists()


# --- Criteria 16 and 17 ------------------------------------------------------


@pytest.mark.parametrize("command", COMMANDS)
def test_rc22_c16_missing_database_exits_1_with_one_error_line_and_creates_nothing(
    reports, tmp_path, capsys, command
):
    path = tmp_path / "test.db"
    assert not path.exists()
    assert run_cli(reports, command) == 1
    line = assert_one_error_line(capsys.readouterr())
    assert "No database found" in line
    assert not path.exists()
    assert list(tmp_path.glob("test.db*")) == []


@pytest.fixture(params=["version_4", "empty_file", "not_sqlite"])
def bad_database(request, tmp_path):
    path = tmp_path / "test.db"
    if request.param == "version_4":
        rc22_fixtures.build_v4_database(path)
        return path, ["schema version 4", "python3 src/db.py"]
    if request.param == "empty_file":
        path.write_bytes(b"")
        return path, ["is empty"]
    path.write_text("definitely not a database\n", encoding="utf-8")
    return path, ["not a SQLite database"]


@pytest.mark.parametrize("command", COMMANDS)
def test_rc22_c17_unusable_database_exits_1_with_the_schema_message_and_is_left_byte_identical(
    reports, bad_database, capsys, command
):
    path, expected_fragments = bad_database
    before = sha256(path)
    assert run_cli(reports, command) == 1
    line = assert_one_error_line(capsys.readouterr())
    for fragment in expected_fragments:
        assert fragment in line
    assert sha256(path) == before


# --- Criterion 18 ------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE brands SET name = 'tampered' WHERE id = 1;\n",
        "DELETE FROM listing_matches WHERE listing_id = 1;\n",
        "DROP TABLE brands;\n",
    ],
    ids=["update", "delete", "drop"],
)
@pytest.mark.parametrize("command, filename", [("report", "report.sql"), ("trends", "trends.sql")])
def test_rc22_c18_a_data_modifying_query_file_fails_and_never_changes_the_database(
    reports, monkeypatch, tmp_path, capsys, statement, command, filename
):
    path = rc22_fixtures.build_f1(tmp_path / "test.db")
    monkeypatch.setattr(reports, "SQL_DIR", write_sql_dir(tmp_path, {filename: HEADER + statement}))
    before = sha256(path)

    assert run_cli(reports, command) == 1

    assert_one_error_line(capsys.readouterr())
    assert sha256(path) == before


# --- Criterion 19 ------------------------------------------------------------


@pytest.mark.parametrize(
    "command, filename",
    [("report", "report.sql"), ("trends", "trends.sql"), ("doctor", "doctor/dangling_reference.sql")],
)
def test_rc22_c19_header_only_file_reports_no_query_yet(
    reports, monkeypatch, tmp_path, capsys, command, filename
):
    path = rc22_fixtures.build_f0(tmp_path / "test.db")
    monkeypatch.setattr(reports, "SQL_DIR", write_sql_dir(tmp_path, {filename: HEADER}))
    before = sha256(path)

    assert run_cli(reports, command) == 1

    line = assert_one_error_line(capsys.readouterr())
    assert line.endswith(f"{os.path.basename(filename)} has no query yet.")
    assert sha256(path) == before


@pytest.mark.parametrize("command", COMMANDS)
def test_rc22_c19_missing_query_file_exits_1_with_one_error_line(
    reports, monkeypatch, tmp_path, capsys, command
):
    rc22_fixtures.build_f0(tmp_path / "test.db")
    monkeypatch.setattr(reports, "SQL_DIR", write_sql_dir(tmp_path, {}))
    assert run_cli(reports, command) == 1
    assert_one_error_line(capsys.readouterr())


def test_rc22_c19_two_statements_in_one_file_exit_1(reports, monkeypatch, tmp_path, capsys):
    rc22_fixtures.build_f0(tmp_path / "test.db")
    monkeypatch.setattr(
        reports, "SQL_DIR", write_sql_dir(tmp_path, {"report.sql": HEADER + "SELECT 1;\nSELECT 2;\n"})
    )
    assert run_cli(reports, "report") == 1
    assert_one_error_line(capsys.readouterr())


def test_rc22_c19_wrong_column_names_exit_1_and_name_both_lists(reports, monkeypatch, tmp_path, capsys):
    rc22_fixtures.build_f0(tmp_path / "test.db")
    monkeypatch.setattr(
        reports, "SQL_DIR", write_sql_dir(tmp_path, {"report.sql": HEADER + "SELECT 1 AS wrong_name;\n"})
    )
    assert run_cli(reports, "report") == 1
    line = assert_one_error_line(capsys.readouterr())
    assert "wrong_name" in line
    for column in REPORT_COLUMNS:
        assert column in line


def test_rc22_c19_unknown_parameter_name_exits_1(reports, monkeypatch, tmp_path, capsys):
    rc22_fixtures.build_f0(tmp_path / "test.db")
    monkeypatch.setattr(
        reports, "SQL_DIR", write_sql_dir(tmp_path, {"report.sql": HEADER + "SELECT :bogus;\n"})
    )
    assert run_cli(reports, "report") == 1
    assert_one_error_line(capsys.readouterr())


# --- Criterion 20 ------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ("report", "--min-deals", "-1"),
        ("report", "--min-deals", "abc"),
        ("report", "--min-deals", "1.5"),
        ("report", "--since", "2026-13-01"),
        ("report", "--since", "2026-02-30"),
        ("report", "--since", "2026-1-5"),
        ("report", "--since", "20261101"),
        ("report", "--since", "2026-11-01T00:00:00"),
        ("trends", "--latest", "0"),
        ("trends", "--latest", "1001"),
        ("trends", "--latest", "abc"),
        ("trends", "--window", "0"),
        ("trends", "--window", "1001"),
        ("trends", "--item", "0"),
        ("trends", "--item", "-3"),
        ("trends", "--item", "abc"),
        ("doctor", "--bogus"),
        ("bogus",),
    ],
)
def test_rc22_c20_argument_errors_exit_2_before_any_database_access(reports, monkeypatch, tmp_path, argv):
    forbid_database(monkeypatch)
    assert run_cli(reports, *argv) == 2
    assert not (tmp_path / "test.db").exists()


@pytest.mark.parametrize(
    "argv",
    [
        ("report", "--min-deals", "0"),
        ("report", "--since", "2026-02-28"),
        ("trends", "--latest", "1000", "--window", "1000"),
        ("trends", "--latest", "1", "--window", "1"),
        ("trends", "--item", "1"),
        ("trends", "--include-inactive"),
    ],
)
def test_rc22_c20_boundary_values_are_accepted(reports, monkeypatch, tmp_path, argv):
    rc22_fixtures.build_f0(tmp_path / "test.db")
    install_stub(reports, monkeypatch)
    assert run_cli(reports, *argv) == 0


# --- Criterion 21: text output, query runner stubbed -------------------------

REPORT_ROWS = [
    ("Rick   Owens", 6, 1, 5, 30.7, 48.5, "2026-11-01"),
    ("Undercover", 1, 0, 1, 38.0, 38.0, "2026-10-31"),
    ("カプセル", 0, 0, 0, None, None, None),
]
# Column widths: brand 10, deals 5, watches 7, priced_deals 12, avg_discount_pct 16,
# best_discount_pct 17, last_deal_date 14.  "カプセル" is 4 wide characters = display width 8.
# The rows are rstripped before comparison, so trailing padding of the last cell is not asserted.
REPORT_TABLE = [
    "brand       deals  watches  priced_deals  avg_discount_pct  best_discount_pct  last_deal_date",
    "----------  -----  -------  ------------  ----------------  -----------------  --------------",
    "Rick Owens      6        1             5              30.7               48.5  2026-11-01",
    "Undercover      1        0             1              38.0               38.0  2026-10-31",
    "カプセル        0        0             0                 -                  -  -",
]
NOTE_LINE = (
    "Note: 1 deal(s) were recorded before discounts were stored and are excluded from "
    "avg_discount_pct and best_discount_pct."
)


def _lines(captured_out):
    return [line.rstrip() for line in captured_out.splitlines()]


def test_rc22_c21_report_text_table_alignment_decimals_nulls_and_whitespace(
    reports, monkeypatch, tmp_path, capsys
):
    rc22_fixtures.build_f0(tmp_path / "test.db")
    install_stub(reports, monkeypatch, {"report": (REPORT_COLUMNS, REPORT_ROWS)})

    assert run_cli(reports, "report") == 0

    lines = _lines(capsys.readouterr().out)
    non_empty = [line for line in lines if line]
    assert non_empty[0] == "Recorded deals per brand"
    start = lines.index(REPORT_TABLE[0])
    assert lines[start:start + len(REPORT_TABLE)] == REPORT_TABLE


def test_rc22_c21_report_text_adds_the_since_date_to_the_title(reports, monkeypatch, tmp_path, capsys):
    rc22_fixtures.build_f0(tmp_path / "test.db")
    install_stub(reports, monkeypatch, {"report": (REPORT_COLUMNS, REPORT_ROWS)})
    assert run_cli(reports, "report", "--since", "2026-10-20") == 0
    non_empty = [line for line in _lines(capsys.readouterr().out) if line]
    assert non_empty[0] == "Recorded deals per brand since 2026-10-20"


def test_rc22_c21_report_text_ends_with_the_legacy_note_when_deals_lack_discounts(
    reports, monkeypatch, tmp_path, capsys
):
    rc22_fixtures.build_f0(tmp_path / "test.db")
    # deals - priced_deals is (6 - 5) + (1 - 1) + (0 - 0) = 1
    install_stub(reports, monkeypatch, {"report": (REPORT_COLUMNS, REPORT_ROWS)})
    assert run_cli(reports, "report") == 0
    non_empty = [line for line in _lines(capsys.readouterr().out) if line]
    assert non_empty[-1] == NOTE_LINE


def test_rc22_c21_report_text_has_no_note_when_every_deal_is_priced(reports, monkeypatch, tmp_path, capsys):
    rc22_fixtures.build_f0(tmp_path / "test.db")
    rows = [("Undercover", 1, 0, 1, 38.0, 38.0, "2026-10-31")]
    install_stub(reports, monkeypatch, {"report": (REPORT_COLUMNS, rows)})
    assert run_cli(reports, "report") == 0
    assert "Note:" not in capsys.readouterr().out


def test_rc22_c21_report_with_no_rows_prints_the_empty_message_and_exits_0(
    reports, monkeypatch, tmp_path, capsys
):
    rc22_fixtures.build_f0(tmp_path / "test.db")
    install_stub(reports, monkeypatch)
    assert run_cli(reports, "report") == 0
    assert "No brands with at least 1 deal." in _lines(capsys.readouterr().out)


# Column widths: brand 10, item_id 7, item 16 ("ジャケットパンツ" is 8 wide characters), recency_rank 12,
# first_seen_at 32, ebay_listing_id 15, price_usd 9, change_usd 10, rolling_avg_usd 15, window_rows 11.
TRENDS_ROWS = [
    ("Rick Owens", 1, "Ramones   High", 3, "2026-10-01T00:00:00+00:00", "legacy:a", 110.0, None, 110.0, 1),
    ("Rick Owens", 1, "Ramones High", 2, "2026-10-01T00:00:00+00:00", "rest:b", 100.0, -10.0, 105.0, 2),
    ("Rick Owens", 1, "Ramones\tHigh", 1, "2026-10-02T00:00:00.250000+00:00", "legacy:c", 130.0, 30.0, 113.33, 3),
    ("Rick Owens", 2, "ジャケットパンツ", 1, "2026-10-05T00:00:00+00:00", "legacy:z", 250.0, 0.0, 250.0, 1),
]
TRENDS_TABLE = [
    "brand       item_id  item              recency_rank  first_seen_at                     ebay_listing_id  price_usd  change_usd  rolling_avg_usd  window_rows",
    "----------  -------  ----------------  ------------  --------------------------------  ---------------  ---------  ----------  ---------------  -----------",
    "Rick Owens        1  Ramones High                 3  2026-10-01T00:00:00+00:00         legacy:a            110.00           -           110.00            1",
    "Rick Owens        1  Ramones High                 2  2026-10-01T00:00:00+00:00         rest:b              100.00      -10.00           105.00            2",
    "Rick Owens        1  Ramones High                 1  2026-10-02T00:00:00.250000+00:00  legacy:c            130.00      +30.00           113.33            3",
    "Rick Owens        2  ジャケットパンツ             1  2026-10-05T00:00:00+00:00         legacy:z            250.00        0.00           250.00            1",
]
TRENDS_FOOTER = (
    "Prices are first-seen eBay asking prices of newly discovered listings, not past market prices. "
    "Rows sharing first_seen_at came from one pricing run and are ordered by ebay_listing_id."
)


def test_rc22_c21_trends_text_table_signed_change_two_decimals_and_double_width(
    reports, monkeypatch, tmp_path, capsys
):
    rc22_fixtures.build_f0(tmp_path / "test.db")
    install_stub(reports, monkeypatch, {"trends": (TRENDS_COLUMNS, TRENDS_ROWS)})

    assert run_cli(reports, "trends") == 0

    lines = _lines(capsys.readouterr().out)
    start = lines.index(TRENDS_TABLE[0])
    assert lines[start:start + len(TRENDS_TABLE)] == TRENDS_TABLE
    non_empty = [line for line in lines if line]
    assert non_empty[-1] == TRENDS_FOOTER


def test_rc22_c21_trends_with_no_rows_prints_the_empty_message_and_exits_0(
    reports, monkeypatch, tmp_path, capsys
):
    rc22_fixtures.build_f0(tmp_path / "test.db")
    install_stub(reports, monkeypatch)
    assert run_cli(reports, "trends") == 0
    assert "No eBay price observations." in _lines(capsys.readouterr().out)


def _doctor_data():
    return {
        "no_include_keywords": (
            ["item_id", "brand", "item"],
            [(3, "Undercover", "Scab Jacket"), (6, "Undercover", "Hoodie")],
        ),
        "no_usable_price": (
            ["item_id", "brand", "item", "market_price_usd", "manual_price_usd"],
            [(6, "Undercover", "Hoodie", 0.0, None)],
        ),
        "thin_ebay_pool": (
            ["item_id", "brand", "item", "observations"],
            [(2, "Rick Owens", "Pod Shorts", 1), (3, "Undercover", "Scab Jacket", 0)],
        ),
    }


def test_rc22_c21_doctor_text_status_lines_tables_blank_separators_and_summary(
    reports, monkeypatch, tmp_path, capsys
):
    rc22_fixtures.build_f0(tmp_path / "test.db")
    install_stub(reports, monkeypatch, _doctor_data())

    exit_code = run_cli(reports, "doctor")

    lines = _lines(capsys.readouterr().out)
    assert lines[-1] == "doctor: 2 error findings, 3 warning findings, 4 of 7 checks ok."
    blocks, current = [], []
    for line in lines[:-1]:
        if line == "":
            if current:
                blocks.append(current)
            current = []
        else:
            current.append(line)
    if current:
        blocks.append(current)
    assert len(blocks) == 7

    assert blocks[0] == ["[ok] dangling_reference"]
    assert blocks[1] == ["[ok] listing_brand_mismatch"]
    assert blocks[2] == ["[ok] noncanonical_timestamp"]
    assert blocks[3] == ["[ok] deal_discount_mismatch"]
    # the example from the spec, with a one-sentence description between "-" and the table
    assert blocks[4][0].startswith("[error] no_include_keywords: 2 findings - ")
    assert len(blocks[4][0]) > len("[error] no_include_keywords: 2 findings - ")
    assert blocks[4][1:] == [
        "item_id  brand       item",
        "-------  ----------  -----------",
        "      3  Undercover  Scab Jacket",
        "      6  Undercover  Hoodie",
    ]
    assert blocks[5][0].startswith("[warning] no_usable_price: 1 finding - ")
    assert blocks[5][1:] == [
        "item_id  brand       item    market_price_usd  manual_price_usd",
        "-------  ----------  ------  ----------------  ----------------",
        "      6  Undercover  Hoodie              0.00                 -",
    ]
    assert blocks[6][0].startswith("[warning] thin_ebay_pool: 2 findings - ")
    assert blocks[6][1:] == [
        "item_id  brand       item         observations",
        "-------  ----------  -----------  ------------",
        "      2  Rick Owens  Pod Shorts              1",
        "      3  Undercover  Scab Jacket             0",
    ]
    assert exit_code == 3


# --- Criterion 22: JSON output, query runner stubbed -------------------------


def _parse(capsys):
    return json.loads(capsys.readouterr().out)


def test_rc22_c22_report_json_object_column_order_nulls_unescaped_text_and_parameters(
    reports, monkeypatch, tmp_path, capsys
):
    rc22_fixtures.build_f0(tmp_path / "test.db")
    rows = [("Rick Owens", 6, 1, 5, 30.7, 48.5, "2026-11-01"), ("カプセル", 0, 0, 0, None, None, None)]
    install_stub(reports, monkeypatch, {"report": (REPORT_COLUMNS, rows)})

    assert run_cli(reports, "report", "--min-deals", "2", "--since", "2026-10-20", "--json") == 0

    raw = capsys.readouterr().out
    data = json.loads(raw)
    assert list(data) == ["command", "parameters", "rows"]
    assert data["command"] == "report"
    assert data["parameters"] == {"min_deals": 2, "since": "2026-10-20"}
    assert [list(row) for row in data["rows"]] == [REPORT_COLUMNS, REPORT_COLUMNS]
    assert list(data["rows"][0].values()) == ["Rick Owens", 6, 1, 5, 30.7, 48.5, "2026-11-01"]
    assert list(data["rows"][1].values()) == ["カプセル", 0, 0, 0, None, None, None]
    assert "カプセル" in raw  # ensure_ascii=False
    assert "\\u" not in raw
    assert raw.startswith('{\n  "command": "report"')  # indent=2


def test_rc22_c22_report_json_echoes_default_parameters_with_null_for_omitted_since(
    reports, monkeypatch, tmp_path, capsys
):
    rc22_fixtures.build_f0(tmp_path / "test.db")
    install_stub(reports, monkeypatch)
    assert run_cli(reports, "report", "--json") == 0
    data = _parse(capsys)
    assert data["parameters"] == {"min_deals": 1, "since": None}
    assert data["rows"] == []


def test_rc22_c22_trends_json_echoes_effective_parameters_including_defaults(
    reports, monkeypatch, tmp_path, capsys
):
    rc22_fixtures.build_f0(tmp_path / "test.db")
    install_stub(reports, monkeypatch, {"trends": (TRENDS_COLUMNS, TRENDS_ROWS[:1])})

    assert run_cli(reports, "trends", "--json") == 0
    data = _parse(capsys)
    assert list(data) == ["command", "parameters", "rows"]
    assert data["command"] == "trends"
    assert data["parameters"] == {"latest": 10, "window": 5, "item_id": None, "include_inactive": 0}
    assert list(data["rows"][0]) == TRENDS_COLUMNS
    assert data["rows"][0]["change_usd"] is None

    assert run_cli(reports, "trends", "--latest", "3", "--window", "2", "--item", "7",
                   "--include-inactive", "--json") == 0
    assert _parse(capsys)["parameters"] == {"latest": 3, "window": 2, "item_id": 7, "include_inactive": 1}


def test_rc22_c22_doctor_json_lists_seven_checks_in_fixed_order_with_counts_and_totals(
    reports, monkeypatch, tmp_path, capsys
):
    rc22_fixtures.build_f0(tmp_path / "test.db")
    install_stub(reports, monkeypatch, _doctor_data())

    exit_code = run_cli(reports, "doctor", "--json")

    data = json.loads(capsys.readouterr().out)
    assert list(data) == ["command", "checks", "error_findings", "warning_findings"]
    assert data["command"] == "doctor"
    assert [c["id"] for c in data["checks"]] == CHECK_IDS
    assert [c["severity"] for c in data["checks"]] == [sev for _cid, sev, _cols in DOCTOR_CHECKS]
    assert [c["count"] for c in data["checks"]] == [0, 0, 0, 0, 2, 1, 2]
    assert all(list(c) == ["id", "severity", "description", "count", "rows"] for c in data["checks"])
    assert all(isinstance(c["description"], str) and c["description"] for c in data["checks"])
    assert [len(c["rows"]) for c in data["checks"]] == [0, 0, 0, 0, 2, 1, 2]
    assert data["checks"][4]["rows"][0] == {"item_id": 3, "brand": "Undercover", "item": "Scab Jacket"}
    assert data["checks"][5]["rows"][0]["manual_price_usd"] is None
    assert data["error_findings"] == 2
    assert data["warning_findings"] == 3
    assert exit_code == 3


# --- Criterion 23 and parameter binding --------------------------------------


@pytest.mark.parametrize("command", COMMANDS)
def test_rc22_c23_every_command_opens_the_database_only_through_the_read_only_helper(
    reports, monkeypatch, tmp_path, command
):
    rc22_fixtures.build_f1(tmp_path / "test.db")
    calls = []
    real = db.get_read_only_connection

    def spy():
        calls.append("read_only")
        return real()

    def forbidden():
        raise AssertionError("get_connection must not be used by the reports tool")

    monkeypatch.setattr(db, "get_read_only_connection", spy)
    monkeypatch.setattr(db, "get_connection", forbidden)
    install_stub(reports, monkeypatch)

    assert run_cli(reports, command) == 0
    assert calls and set(calls) == {"read_only"}


def _bound(seen, key):
    dicts = [v for k, args, kwargs in seen if k == key for v in (*args, *kwargs.values()) if isinstance(v, dict)]
    assert len(dicts) == 1, f"expected exactly one parameter dict for {key}"
    return dicts[0]


def test_rc22_report_binds_effective_parameters_with_null_for_an_omitted_since(
    reports, monkeypatch, tmp_path
):
    rc22_fixtures.build_f0(tmp_path / "test.db")
    seen = []
    install_stub(reports, monkeypatch, seen=seen)
    assert run_cli(reports, "report") == 0
    assert _bound(seen, "report") == {"min_deals": 1, "since": None}


def test_rc22_trends_binds_effective_parameters_with_null_for_an_omitted_item(reports, monkeypatch, tmp_path):
    rc22_fixtures.build_f0(tmp_path / "test.db")
    seen = []
    install_stub(reports, monkeypatch, seen=seen)
    assert run_cli(reports, "trends") == 0
    assert _bound(seen, "trends") == {"latest": 10, "window": 5, "item_id": None, "include_inactive": 0}


def test_rc22_doctor_binds_only_the_documented_parameters(reports, monkeypatch, tmp_path):
    rc22_fixtures.build_f0(tmp_path / "test.db")
    seen = []
    install_stub(reports, monkeypatch, seen=seen)
    assert run_cli(reports, "doctor") == 0
    assert [key for key, _args, _kwargs in seen] == CHECK_IDS
    assert _bound(seen, "deal_discount_mismatch") == {"tolerance_pct": 0.001}
    assert _bound(seen, "thin_ebay_pool") == {"min_observations": db.MIN_POOL_PRICES}


# --- Criterion 43 and doctor failure handling --------------------------------


def test_rc22_c43_only_warning_findings_exit_0(reports, monkeypatch, tmp_path, capsys):
    rc22_fixtures.build_f0(tmp_path / "test.db")
    data = _doctor_data()
    del data["no_include_keywords"]
    install_stub(reports, monkeypatch, data)
    assert run_cli(reports, "doctor") == 0
    assert capsys.readouterr().out.splitlines()[-1] == (
        "doctor: 0 error findings, 3 warning findings, 5 of 7 checks ok."
    )


def test_rc22_c43_any_error_finding_exits_3(reports, monkeypatch, tmp_path, capsys):
    rc22_fixtures.build_f0(tmp_path / "test.db")
    install_stub(reports, monkeypatch, {
        "listing_brand_mismatch": (
            default_columns("listing_brand_mismatch"),
            [(12, 1, "Undercover", "Rick Owens"), (14, 2, "Rick Owens", "Undercover")],
        ),
    })
    assert run_cli(reports, "doctor") == 3
    assert capsys.readouterr().out.splitlines()[-1] == (
        "doctor: 2 error findings, 0 warning findings, 6 of 7 checks ok."
    )


def test_rc22_doctor_with_no_findings_exits_0(reports, monkeypatch, tmp_path, capsys):
    rc22_fixtures.build_f0(tmp_path / "test.db")
    install_stub(reports, monkeypatch)
    assert run_cli(reports, "doctor") == 0
    assert capsys.readouterr().out.splitlines()[-1] == (
        "doctor: 0 error findings, 0 warning findings, 7 of 7 checks ok."
    )


def test_rc22_doctor_failing_check_query_stops_with_exit_1_and_no_summary(
    reports, monkeypatch, tmp_path, capsys
):
    rc22_fixtures.build_f0(tmp_path / "test.db")
    install_stub(reports, monkeypatch, raises={"noncanonical_timestamp": sqlite3.OperationalError("boom")})
    assert run_cli(reports, "doctor") == 1
    captured = capsys.readouterr()
    assert_one_error_line(captured)
    assert "doctor:" not in captured.out


@pytest.mark.parametrize("command", ["report", "trends"])
def test_rc22_sqlite_error_in_a_query_exits_1_with_one_error_line(
    reports, monkeypatch, tmp_path, capsys, command
):
    rc22_fixtures.build_f0(tmp_path / "test.db")
    install_stub(reports, monkeypatch, raises={command: sqlite3.OperationalError("database is locked")})
    assert run_cli(reports, command) == 1
    assert_one_error_line(capsys.readouterr())


def test_rc22_harness_tests_never_modify_the_database_file(reports, monkeypatch, tmp_path):
    """Guard for the stubbed paths: running a command leaves the fixture file byte-identical."""
    path = rc22_fixtures.build_f1(tmp_path / "test.db")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    install_stub(reports, monkeypatch)
    for command in COMMANDS:
        assert run_cli(reports, command) == 0
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
