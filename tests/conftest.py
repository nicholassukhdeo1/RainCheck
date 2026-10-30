"""Shared fixtures for the RainCheck test suite.

The fixture names here are a contract that later backlog items reuse.
"""
import copy
import json
import socket
import sqlite3
import sys
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


@pytest.fixture(autouse=True)
def _stub_daily_listing_images(monkeypatch):
    """Give legacy job tests deterministic photos without exercising image HTTP."""
    import yahoo_scrape
    from listing_images import PreparedImage

    def prepared_photo(listing_url, image_url):
        if not image_url:
            return None
        listing_id = listing_url.rsplit("/", 1)[-1]
        return PreparedImage(f"test-photo-{listing_id}", "image/jpeg", b"test thumbnail")

    monkeypatch.setattr(yahoo_scrape, "prepare_listing_image", prepared_photo)


@pytest.fixture(autouse=True)
def _disable_http_retry_waits(monkeypatch):
    """Keep all job integration tests offline in time as well as network."""
    try:
        import http_retry
    except ImportError:
        return

    original = http_retry.request_with_retry

    def without_real_wait(*args, **kwargs):
        kwargs.setdefault("sleep", lambda _seconds: None)
        return original(*args, **kwargs)

    monkeypatch.setattr(http_retry, "request_with_retry", without_real_wait)
    # Jobs may import the shared function directly, before the fixture runs.
    for module_name in ("ebay_pricing", "yahoo_scrape"):
        module = sys.modules.get(module_name)
        if module is not None and getattr(module, "request_with_retry", None) is original:
            monkeypatch.setattr(module, "request_with_retry", without_real_wait)


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
def legacy_db(tmp_path):
    """A pre-RC-03 (schema version 0) database with rows mirroring the old real one.

    Built with plain sqlite3 from the two SQL files, never through db.init_db,
    and left at PRAGMA user_version 0.  Returns the database path.
    """
    assert_db_is_temp(tmp_path)
    path = tmp_path / "test.db"
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript((FIXTURES_DIR / "db" / "schema_v0.sql").read_text(encoding="utf-8"))
        conn.executescript((FIXTURES_DIR / "db" / "legacy_v0_rows.sql").read_text(encoding="utf-8"))
        conn.commit()
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    finally:
        conn.close()
    return path


@pytest.fixture
def db_conn(tmp_path):
    import db

    assert_db_is_temp(tmp_path)
    conn = db.get_connection()
    yield conn
    conn.close()


class FakeResponse:
    def __init__(self, *, json_data=None, text=None, status=200, headers=None):
        if text is None:
            text = json.dumps(json_data) if json_data is not None else ""
        self.status_code = status
        self.headers = dict(headers or {})
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
        self._routes[(method.upper(), url)] = [(json, text, status, {})]

    def add_sequence(self, method, url, responses):
        """Register ordered offline responses as (json, text, status, headers) mappings."""
        sequence = []
        for response in responses:
            if isinstance(response, dict):
                sequence.append((
                    response.get("json"), response.get("text"),
                    response.get("status", 200), response.get("headers", {}),
                ))
            else:
                json_data, text, status, *tail = response
                sequence.append((json_data, text, status, tail[0] if tail else {}))
        if not sequence:
            raise ValueError("an HTTP response sequence must not be empty")
        self._routes[(method.upper(), url)] = sequence

    def _handle(self, method, url, kwargs):
        self.calls.append((method, url, kwargs))
        key = (method, url)
        if key not in self._routes:
            raise AssertionError(f"Unregistered HTTP call: {method} {url}")
        responses = self._routes[key]
        json_data, text, status, headers = responses.pop(0) if len(responses) > 1 else responses[0]
        return FakeResponse(json_data=json_data, text=text, status=status, headers=headers)

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

    def fake_send_email(subject, html_body, text_body, **kwargs):
        emails.append((subject, html_body, text_body))
        return True

    monkeypatch.setattr(yahoo_scrape, "send_email", fake_send_email)
    return emails
