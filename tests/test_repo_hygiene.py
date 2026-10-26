"""Repository hygiene tests (RC-02 criteria 1-3).

Only read-only git commands are run (ls-files, check-ignore, rev-parse).
check-ignore uses --no-index so the patterns are tested regardless of whether
a path is currently tracked.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_SUFFIXES = (".db", ".db-journal", ".db-wal", ".db-shm")


def _git(*args):
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=10
    )


def _git_available():
    if shutil.which("git") is None:
        return "git is not on PATH"
    try:
        result = _git("rev-parse", "--is-inside-work-tree")
    except (subprocess.TimeoutExpired, OSError) as exc:
        return f"git could not run: {exc}"
    if result.returncode != 0 or result.stdout.strip() != "true":
        return "not inside a git work tree"
    return None


_SKIP_REASON = _git_available()
pytestmark = pytest.mark.skipif(_SKIP_REASON is not None, reason=_SKIP_REASON or "")


def test_rc02_c1_no_database_file_is_tracked():
    result = _git("ls-files")
    assert result.returncode == 0, result.stderr
    tracked = [p for p in result.stdout.splitlines() if p.endswith(DB_SUFFIXES)]
    assert tracked == []


@pytest.mark.parametrize(
    "path",
    [
        "src/raincheck.db",
        "src/raincheck.db-journal",
        "src/raincheck.db-wal",
        "src/raincheck.db-shm",
        "raincheck-test.db",
        "data/nested/backup.db",
    ],
)
def test_rc02_c2_database_paths_are_ignored(path):
    result = _git("check-ignore", "--no-index", "-q", path)
    assert result.returncode == 0, f"{path} should be ignored"


@pytest.mark.parametrize(
    "path",
    [
        "src/schema.sql",
        "src/catalog.json",
        "src/db.py",
        "tests/fixtures/catalog.json",
        "requirements.txt",
    ],
)
def test_rc02_c3_source_paths_are_not_ignored(path):
    result = _git("check-ignore", "--no-index", "-q", path)
    assert result.returncode == 1, f"{path} should not be ignored"
