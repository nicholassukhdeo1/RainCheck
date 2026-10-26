import importlib
import json
import os
import sqlite3
from pathlib import Path

import pytest
import requests

import db

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
TABLES = {"brands", "items", "item_keywords", "listings", "listing_matches"}


def _tables(path):
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows}


# --- Harness and safety -----------------------------------------------------


def test_c02_unfaked_network_call_is_blocked():
    with pytest.raises(RuntimeError, match="Network access is blocked"):
        requests.get("https://example.com")


def test_c03_env_points_into_tmp_path_and_dry_run_on(tmp_path):
    db_path = Path(os.environ["RAINCHECK_DB_PATH"])
    assert tmp_path in db_path.parents
    assert os.environ.get("RAINCHECK_DRY_RUN") == "1"


def test_c04_credentials_absent_even_after_reloading_modules_that_load_dotenv():
    import sendemail

    importlib.reload(sendemail)
    for name in ("SENDER_PASSWORD", "EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET"):
        assert name not in os.environ


# --- Configuration ----------------------------------------------------------


def test_c06_default_db_path_when_env_unset(monkeypatch):
    monkeypatch.delenv("RAINCHECK_DB_PATH", raising=False)
    assert Path(db.get_db_path()) == SRC_DIR / "raincheck.db"
    assert Path(db.get_db_path()).is_absolute()


def test_c07_empty_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("RAINCHECK_DB_PATH", "")
    assert Path(db.get_db_path()) == SRC_DIR / "raincheck.db"


def test_c08_init_db_creates_file_at_env_path_and_prints_it(monkeypatch, tmp_path, capsys):
    target = tmp_path / "x.db"
    monkeypatch.setenv("RAINCHECK_DB_PATH", str(target))
    assert db.get_db_path() == str(target)  # fails early, before any write, if unsupported
    db.init_db()
    assert target.exists()
    assert TABLES <= _tables(target)
    assert str(target) in capsys.readouterr().out


def test_c09_path_is_not_cached_between_connections(monkeypatch, tmp_path):
    first, second = tmp_path / "a.db", tmp_path / "b.db"
    monkeypatch.setenv("RAINCHECK_DB_PATH", str(first))
    assert db.get_db_path() == str(first)
    db.get_connection().close()
    monkeypatch.setenv("RAINCHECK_DB_PATH", str(second))
    db.get_connection().close()
    assert first.exists() and second.exists()


def test_c11_load_catalog_default_works_from_unrelated_cwd(monkeypatch, tmp_path):
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    expected = json.loads((SRC_DIR / "catalog.json").read_text())
    assert db.load_catalog() == expected


def test_c12_load_catalog_with_explicit_path(tmp_path):
    p = tmp_path / "c.json"
    data = [{"brand": "B", "item name": "i"}]
    p.write_text(json.dumps(data))
    assert db.load_catalog(p) == data
    assert db.load_catalog(str(p)) == data


@pytest.fixture
def ebay_pricing():
    # Imported lazily so an import-time KeyError fails each test individually
    # instead of aborting collection of the whole suite.
    return importlib.import_module("ebay_pricing")


def test_c15_reload_ebay_pricing_without_credentials_does_not_raise(ebay_pricing):
    assert "EBAY_CLIENT_ID" not in os.environ
    assert "EBAY_CLIENT_SECRET" not in os.environ
    importlib.reload(ebay_pricing)


def test_c16_get_ebay_token_without_client_id_raises_key_error(
    ebay_pricing, monkeypatch, fake_http, fixtures_dir
):
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "secret")
    monkeypatch.delenv("EBAY_CLIENT_ID", raising=False)
    fake_http.add(
        "POST",
        "https://api.ebay.com/identity/v1/oauth2/token",
        json=json.loads((fixtures_dir / "ebay" / "token.json").read_text()),
    )
    with pytest.raises(KeyError):
        ebay_pricing.get_ebay_token()
