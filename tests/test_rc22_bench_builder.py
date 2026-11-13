"""RC-22 criterion 47: the deterministic benchmark database builder for the index decision.

The builder is a script, not a module: tests/tools/build_bench_db.py <path>.  It is run in a
subprocess against paths under tmp_path only.  Building 500,000 eBay rows takes a while, so the
determinism test builds twice and is the slowest test in the RC-22 suite.
"""
import hashlib
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BUILDER = ROOT / "tests" / "tools" / "build_bench_db.py"
TABLE_ORDER = {
    "brands": "id",
    "items": "id",
    "item_keywords": "item_id, keyword",
    "listings": "id",
    "listing_matches": "listing_id, item_id",
    "ebay_listing_prices": "item_id, ebay_listing_id",
}
TIMEOUT_SECONDS = 900


def _build(path, cwd):
    assert BUILDER.is_file(), "tests/tools/build_bench_db.py does not exist yet"
    return subprocess.run(
        [sys.executable, str(BUILDER), str(path)],
        cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
    )


def _fingerprints(path):
    """SHA-256 of every table's rows in primary-key order."""
    conn = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
    try:
        result = {}
        for table, order in TABLE_ORDER.items():
            digest = hashlib.sha256()
            for row in conn.execute(f"SELECT * FROM {table} ORDER BY {order}"):
                digest.update(repr(row).encode("utf-8"))
            result[table] = digest.hexdigest()
        return result
    finally:
        conn.close()


def test_rc22_c47_builder_refuses_an_existing_path_and_leaves_it_untouched(tmp_path):
    existing = tmp_path / "already_here.db"
    existing.write_bytes(b"precious bytes")

    result = _build(existing, tmp_path)

    assert result.returncode != 0
    assert existing.read_bytes() == b"precious bytes"


def test_rc22_c47_two_builds_have_identical_contents_and_the_documented_shape(tmp_path):
    first, second = tmp_path / "first.db", tmp_path / "second.db"

    for path in (first, second):
        result = _build(path, tmp_path)
        assert result.returncode == 0, result.stderr
        assert path.is_file()

    assert _fingerprints(first) == _fingerprints(second)

    conn = sqlite3.connect(f"{first.resolve().as_uri()}?mode=ro", uri=True)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        # the spec's benchmark parameters
        assert conn.execute("SELECT COUNT(*) FROM brands").fetchone()[0] == 5
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 200
        assert conn.execute("SELECT COUNT(*) FROM items WHERE active = 1").fetchone()[0] == 180
        assert conn.execute("SELECT COUNT(*) FROM item_keywords").fetchone()[0] == 200 * 4
        assert conn.execute("SELECT COUNT(*) FROM ebay_listing_prices").fetchone()[0] == 500_000
        assert conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 50_000
        assert conn.execute("SELECT COUNT(*) FROM listing_matches").fetchone()[0] == 55_000
        # roughly 80 percent of pairs carry deal-time values, and about 10 percent of those are watches
        priced = conn.execute("SELECT COUNT(*) FROM listing_matches WHERE deal_kind IS NOT NULL").fetchone()[0]
        watches = conn.execute("SELECT COUNT(*) FROM listing_matches WHERE deal_kind = 'auction_watch'").fetchone()[0]
        assert 43_000 <= priced <= 45_000
        assert 4_000 <= watches <= 4_800
        # production never runs ANALYZE, so the benchmark must not either
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'sqlite_stat1'").fetchone()[0] == 0
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()
