"""Read-only reports on the RainCheck database: report, trends, and doctor.

This file is only the harness: it parses arguments, opens the database read-only,
runs the query files in src/sql/, checks their columns, and prints the rows.
The queries themselves live in src/sql/, one statement per file. The agents wrote them and the
user reviews them (RC-22 spec, Amendment 1); this file contains no query text.
"""

import argparse
import json
import re
import sqlite3
import sys
import unicodedata
from contextlib import closing
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

import db

SRC_DIR = Path(__file__).resolve().parent
REPO_ROOT = SRC_DIR.parent
# looked up at call time (not copied) so a test can point it at a temporary directory
SQL_DIR = SRC_DIR / "sql"

REPORT_COLUMNS = (
    "brand", "deals", "watches", "priced_deals",
    "avg_discount_pct", "best_discount_pct", "last_deal_date",
)
TRENDS_COLUMNS = (
    "brand", "item_id", "item", "recency_rank", "first_seen_at",
    "ebay_listing_id", "price_usd", "change_usd", "rolling_avg_usd", "window_rows",
)

# (check_id, severity, one-sentence description, bound parameters, required columns), in run order.
# integrity checks come first, configuration checks last.
DOCTOR_CHECKS = (
    (
        "dangling_reference", "error",
        "A row points at a parent row that does not exist.",
        {},
        ("child_table", "child_key", "parent_table", "missing_id"),
    ),
    (
        "listing_brand_mismatch", "error",
        "A recorded pair's listing and item belong to different brands, so brand reports are ambiguous.",
        {},
        ("listing_id", "item_id", "listing_brand", "item_brand"),
    ),
    (
        "noncanonical_timestamp", "error",
        "A timestamp is not canonical UTC text, so comparing timestamps as text can give wrong answers.",
        {},
        ("table_name", "column_name", "row_key", "value"),
    ),
    (
        "deal_discount_mismatch", "error",
        "A stored discount differs from the one recomputed from the stored prices and rate.",
        {"tolerance_pct": 0.001},
        ("listing_id", "item_id", "stored_pct", "computed_pct"),
    ),
    (
        "no_include_keywords", "error",
        "Active items with no include keyword can never match a listing.",
        {},
        ("item_id", "brand", "item"),
    ),
    (
        "no_usable_price", "warning",
        "Active items with no usable market or manual price are skipped by the Yahoo job.",
        {},
        ("item_id", "brand", "item", "market_price_usd", "manual_price_usd"),
    ),
    (
        "thin_ebay_pool", "warning",
        "Active items with too few stored eBay prices cannot get an eBay market price.",
        {"min_observations": db.MIN_POOL_PRICES},
        ("item_id", "brand", "item", "observations"),
    ),
)

TRENDS_FOOTER = (
    "Prices are first-seen eBay asking prices of newly discovered listings, not past market prices. "
    "Rows sharing first_seen_at came from one pricing run and are ordered by ebay_listing_id."
)

# how each column is printed. numeric columns are right-aligned, everything else left-aligned.
NUMERIC_COLUMNS = {
    "deals", "watches", "priced_deals", "avg_discount_pct", "best_discount_pct",
    "item_id", "recency_rank", "price_usd", "change_usd", "rolling_avg_usd", "window_rows",
    "listing_id", "missing_id", "stored_pct", "computed_pct",
    "market_price_usd", "manual_price_usd", "observations",
}
PERCENT_COLUMNS = {"avg_discount_pct", "best_discount_pct"}
USD_COLUMNS = {"price_usd", "rolling_avg_usd", "market_price_usd", "manual_price_usd"}
SIGNED_USD_COLUMNS = {"change_usd"}
# stored_pct and computed_pct are printed unrounded on purpose: a rounded value
# would hide the small difference the doctor check is reporting


class QueryFileError(Exception):
    """A query file is missing, has no query yet, or does not match its contract."""


# --- command line ------------------------------------------------------------


def _int_in_range(name, low, high=None):
    """An argparse type: an integer from low up to high (or with no upper limit)."""
    def parse(value):
        try:
            number = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{name} must be an integer") from None
        if number < low or (high is not None and number > high):
            limit = f"{low} or more" if high is None else f"{low} to {high}"
            raise argparse.ArgumentTypeError(f"{name} must be {limit}")
        return number
    return parse


def _utc_date(value):
    """Exactly YYYY-MM-DD and a real calendar date (strptime alone would accept 2026-1-5)."""
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError("date must be a real calendar date") from None
    return value


def _parser():
    parser = argparse.ArgumentParser(
        description="Read-only RainCheck reports. The queries live in src/sql/."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    report = commands.add_parser("report", help="deal statistics per brand")
    report.add_argument("--min-deals", type=_int_in_range("--min-deals", 0), default=1,
                        help="only brands with at least this many deals (default 1)")
    report.add_argument("--since", type=_utc_date, default=None, metavar="YYYY-MM-DD",
                        help="only pairs matched on or after this UTC date")
    report.add_argument("--json", action="store_true", help="print JSON instead of a table")

    trends = commands.add_parser("trends", help="recent eBay asking prices per item")
    trends.add_argument("--latest", type=_int_in_range("--latest", 1, 1000), default=10,
                        help="observations to show per item (default 10)")
    trends.add_argument("--window", type=_int_in_range("--window", 1, 1000), default=5,
                        help="rolling average window in observations (default 5)")
    trends.add_argument("--item", type=_int_in_range("--item", 1), default=None, metavar="ID",
                        help="only this item, active or not")
    trends.add_argument("--include-inactive", action="store_true", help="also show inactive items")
    trends.add_argument("--json", action="store_true", help="print JSON instead of a table")

    doctor = commands.add_parser("doctor", help="data-quality checks")
    doctor.add_argument("--json", action="store_true", help="print JSON instead of text")
    return parser


# --- running a query file ----------------------------------------------------


def _display_path(path):
    """The path relative to the repository for messages, or as given when it is elsewhere."""
    path = Path(path)
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _strip_comments(text):
    """
    Removes -- and /* */ comments, leaving quoted text alone.
    Used only to tell a header-only file (nothing left) from a file with a statement.
    """
    out = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in "'\"`":
            end = text.find(ch, i + 1)
            while end != -1 and text[end + 1:end + 2] == ch:  # a doubled quote is an escaped quote
                end = text.find(ch, end + 2)
            end = n - 1 if end == -1 else end
            out.append(text[i:end + 1])
            i = end + 1
        elif text.startswith("--", i):
            end = text.find("\n", i)
            i = n if end == -1 else end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def run_query(conn, path, params, expected_columns):
    """
    Runs the one statement in a query file and returns (column names, rows as tuples).
    Every value is bound from `params`, never formatted into the text.
    The connection is read-only, so a file that tries to change data fails with a SQLite error.
    """
    name = _display_path(path)
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        raise QueryFileError(f"{name} was not found.") from None

    # a file that is only a comment header has not been written yet
    if not _strip_comments(text).strip(" \t\r\n;"):
        raise QueryFileError(f"{name} has no query yet.")

    # sqlite3 refuses a second statement in one execute() call, which enforces one statement per file
    with closing(conn.execute(text, params)) as cursor:
        columns = [column[0] for column in (cursor.description or ())]
        if columns != list(expected_columns):
            raise QueryFileError(
                f"{name} returned columns [{', '.join(columns)}] "
                f"but the contract is [{', '.join(expected_columns)}]."
            )
        return columns, [tuple(row) for row in cursor.fetchall()]


# --- text output -------------------------------------------------------------


def _display_width(text):
    """Terminal width of text: East Asian Wide and Fullwidth characters take two cells."""
    width = 0
    for character in text:
        if unicodedata.combining(character):
            continue
        width += 2 if unicodedata.east_asian_width(character) in ("W", "F") else 1
    return width


def _pad(text, width, right):
    padding = " " * (width - _display_width(text))
    return padding + text if right else text + padding


def _format_cell(column, value):
    if value is None:
        return "-"
    if column in PERCENT_COLUMNS:
        return f"{value:.1f}"
    if column in USD_COLUMNS:
        return f"{value:.2f}"
    if column in SIGNED_USD_COLUMNS:
        # an explicit sign for nonzero changes; zero (and -0.0) prints as a plain 0.00
        return f"{value:+.2f}" if value else "0.00"
    # runs of whitespace collapse to one space, like the digest's titles
    return " ".join(str(value).split())


def format_table(columns, rows):
    """Two-space separated, dash-ruled table, each column as wide as its widest cell."""
    cells = [[_format_cell(c, v) for c, v in zip(columns, row)] for row in rows]
    widths = [
        max([_display_width(column)] + [_display_width(row[i]) for row in cells])
        for i, column in enumerate(columns)
    ]
    right = [column in NUMERIC_COLUMNS for column in columns]

    def line(values):
        return "  ".join(_pad(v, w, r) for v, w, r in zip(values, widths, right)).rstrip()

    return [line(columns), "  ".join("-" * w for w in widths)] + [line(row) for row in cells]


def _plural(count, word):
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


# --- commands ----------------------------------------------------------------


def _rows_as_dicts(columns, rows):
    return [dict(zip(columns, row)) for row in rows]


def _print_json(payload):
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def _run_report(conn, args):
    params = {"min_deals": args.min_deals, "since": args.since}
    columns, rows = run_query(conn, SQL_DIR / "report.sql", params, REPORT_COLUMNS)
    if args.json:
        _print_json({"command": "report", "parameters": params, "rows": _rows_as_dicts(columns, rows)})
        return 0

    print("Recorded deals per brand" + (f" since {args.since}" if args.since else ""))
    if not rows:
        print(f"No brands with at least {_plural(args.min_deals, 'deal')}.")
        return 0
    print("\n".join(format_table(columns, rows)))
    deals_at = columns.index("deals")
    priced_at = columns.index("priced_deals")
    unpriced = sum(row[deals_at] - row[priced_at] for row in rows)
    if unpriced > 0:
        print(
            f"Note: {unpriced} deal(s) were recorded before discounts were stored "
            "and are excluded from avg_discount_pct and best_discount_pct."
        )
    return 0


def _run_trends(conn, args):
    params = {
        "latest": args.latest,
        "window": args.window,
        "item_id": args.item,
        "include_inactive": 1 if args.include_inactive else 0,
    }
    columns, rows = run_query(conn, SQL_DIR / "trends.sql", params, TRENDS_COLUMNS)
    if args.json:
        _print_json({"command": "trends", "parameters": params, "rows": _rows_as_dicts(columns, rows)})
        return 0

    if not rows:
        print("No eBay price observations.")
        return 0
    print("\n".join(format_table(columns, rows)))
    print(TRENDS_FOOTER)
    return 0


def _run_doctor(conn, args):
    # run every check before printing anything, so a failing check never leaves a partial report
    results = []
    for check_id, severity, description, params, expected in DOCTOR_CHECKS:
        columns, rows = run_query(conn, SQL_DIR / "doctor" / f"{check_id}.sql", dict(params), expected)
        results.append((check_id, severity, description, columns, rows))

    findings = {"error": 0, "warning": 0}
    for _check_id, severity, _description, _columns, rows in results:
        findings[severity] += len(rows)
    ok_count = sum(1 for result in results if not result[4])

    if args.json:
        _print_json({
            "command": "doctor",
            "checks": [
                {
                    "id": check_id, "severity": severity, "description": description,
                    "count": len(rows), "rows": _rows_as_dicts(columns, rows),
                }
                for check_id, severity, description, columns, rows in results
            ],
            "error_findings": findings["error"],
            "warning_findings": findings["warning"],
        })
    else:
        for check_id, severity, description, columns, rows in results:
            if rows:
                print(f"[{severity}] {check_id}: {_plural(len(rows), 'finding')} - {description}")
                print("\n".join(format_table(columns, rows)))
            else:
                print(f"[ok] {check_id}")
            print()
        print(
            f"doctor: {findings['error']} error findings, {findings['warning']} warning findings, "
            f"{ok_count} of {len(results)} checks ok."
        )
    # 3 = problems found, so a script can tell it from 1 = the check could not run
    return 3 if findings["error"] else 0


COMMANDS = {"report": _run_report, "trends": _run_trends, "doctor": _run_doctor}


def main(argv=None):
    # argparse errors and --help exit here, before any database is opened
    args = _parser().parse_args(argv)
    load_dotenv()
    try:
        # only the read-only opener: a report must never be able to change the database
        with closing(db.get_read_only_connection()) as conn:
            return COMMANDS[args.command](conn, args)
    except (db.SchemaVersionError, QueryFileError, sqlite3.Error, OSError) as exc:
        # one line on stderr, even when the message has several
        print(f"Error: {' '.join(str(exc).split())}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
