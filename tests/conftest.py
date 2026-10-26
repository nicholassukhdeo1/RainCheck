"""Shared fixtures for the RainCheck test suite.

The fixture names here are a contract that later backlog items reuse.
"""
import copy
import json
import socket
import sqlite3
from pathlib import Path

import dotenv
import pytest
import requests

# Replace dotenv.load_dotenv with a no-op BEFORE any test module (and therefore
# any src/ module) is imported.  The src/ modules do `from dotenv import
# load_dotenv` and call it at import time, which binds whatever function exists
# at that moment.  A fixture would run too late: by the time it executed, the
# modules could already have been imported and would already have loaded the
# real secrets from src/.env into os.environ.  Patching at conftest import time
# is the only way to guarantee the no-op is the thing they bind.
dotenv.load_dotenv = lambda *args, **kwargs: False

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

_ENV_TO_CLEAR = (
    "SENDER_EMAIL",
    "SENDER_PASSWORD",
    "RECEIVER_EMAIL",
    "EBAY_CLIENT_ID",
    "EBAY_CLIENT_SECRET",
)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    for name in _ENV_TO_CLEAR:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RAINCHECK_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("RAINCHECK_DRY_RUN", "1")


@pytest.fixture(autouse=True)
def _block_network(monkeypatch):
    def _blocked(*args, **kwargs):
        raise RuntimeError("Network access is blocked in tests")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
    # Also block DNS, so a hostname request fails with the same error instead
    # of making a real lookup and raising requests.ConnectionError.
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)


def assert_db_is_temp(tmp_path):
    """Safety net: never let a DB-touching fixture run against the real database.

    If the code under test does not yet honor RAINCHECK_DB_PATH (no
    get_db_path), this fails loudly instead of writing to src/raincheck.db.
    """
    import db

    assert hasattr(db, "get_db_path"), (
        "db.get_db_path is missing; refusing to touch a database that may be the real one"
    )
    assert Path(db.get_db_path()).resolve() == (tmp_path / "test.db").resolve(), (
        "db.get_db_path() does not point at the temp database"
    )


@pytest.fixture
def fixtures_dir():
    return FIXTURES_DIR


@pytest.fixture
def temp_db(tmp_path):
    import db

    assert_db_is_temp(tmp_path)
    db.init_db()
    return tmp_path / "test.db"


@pytest.fixture
def test_catalog(monkeypatch):
    import db

    path = FIXTURES_DIR / "catalog.json"
    monkeypatch.setattr(db, "CATALOG_PATH", path)
    return path


@pytest.fixture
def seeded_db(temp_db, test_catalog):
    import db

    db.seed_from_catalog()
    return temp_db


@pytest.fixture
def db_conn(tmp_path):
    import db

    assert_db_is_temp(tmp_path)
    conn = db.get_connection()
    yield conn
    conn.close()


class FakeResponse:
    def __init__(self, *, json_data=None, text=None, status=200):
        if text is None:
            text = json.dumps(json_data) if json_data is not None else ""
        self.status_code = status
        self.text = text
        self.content = text.encode("utf-8")
        self._json = json_data

    def json(self):
        if self._json is not None:
            return copy.deepcopy(self._json)
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Error", response=self)


class FakeHTTP:
    def __init__(self):
        self._routes = {}
        self.calls = []

    def add(self, method, url, *, json=None, text=None, status=200):
        self._routes[(method.upper(), url)] = (json, text, status)

    def _handle(self, method, url, kwargs):
        self.calls.append((method, url, kwargs))
        key = (method, url)
        if key not in self._routes:
            raise AssertionError(f"Unregistered HTTP call: {method} {url}")
        json_data, text, status = self._routes[key]
        return FakeResponse(json_data=json_data, text=text, status=status)

    def get(self, url, **kwargs):
        return self._handle("GET", url, kwargs)

    def post(self, url, **kwargs):
        return self._handle("POST", url, kwargs)


@pytest.fixture
def fake_http(monkeypatch):
    fake = FakeHTTP()
    monkeypatch.setattr(requests, "get", fake.get)
    monkeypatch.setattr(requests, "post", fake.post)
    return fake


@pytest.fixture
def sent_emails(monkeypatch):
    import yahoo_scrape

    emails = []
    monkeypatch.setattr(
        yahoo_scrape, "send_email", lambda subject, body: emails.append((subject, body))
    )
    return emails
