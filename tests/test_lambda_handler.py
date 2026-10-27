"""RC-11b offline acceptance tests for the Lambda runner."""

from __future__ import annotations

import importlib
import io
import json
import os
import sqlite3
import sys
from types import SimpleNamespace

import pytest

import db
import yahoo_scrape
from dbhelpers import rows, user_version


CONFIG = {
    "RAINCHECK_DB_BUCKET": "raincheck-test-bucket",
    "RAINCHECK_DB_KEY": "state/raincheck.db",
    "RAINCHECK_TMP_CAPACITY_BYTES": str(512 * 1024 * 1024),
    "RAINCHECK_SSM_EBAY_CLIENT_ID": "/raincheck/test/ebay-id",
    "RAINCHECK_SSM_EBAY_CLIENT_SECRET": "/raincheck/test/ebay-secret",
    "RAINCHECK_SSM_SENDER_EMAIL": "/raincheck/test/sender",
    "RAINCHECK_SSM_SENDER_PASSWORD": "/raincheck/test/sender-password",
    "RAINCHECK_SSM_RECEIVER_EMAIL": "/raincheck/test/receiver",
}
SECRETS = {
    "/raincheck/test/ebay-id": "synthetic-ebay-id",
    "/raincheck/test/ebay-secret": "synthetic-ebay-secret",
    "/raincheck/test/sender": "synthetic@example.invalid",
    "/raincheck/test/sender-password": "synthetic-mail-password",
    "/raincheck/test/receiver": "recipient@example.invalid",
}
BRAND_URL = yahoo_scrape.BRAND_URL.format("103538")
RATE_URL = yahoo_scrape.FRANKFURTER_RATE_URL
REAL_YAHOO_MAIN = yahoo_scrape.main


class TrackedBody(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.was_closed = False

    def close(self):
        self.was_closed = True
        super().close()


class FakeS3:
    def __init__(self, body, *, etag='"initial-etag"'):
        self.current = body
        self.etag = etag
        self.get_calls = []
        self.put_calls = []
        self.bodies = []
        self.put_error = None

    def get_object(self, **kwargs):
        self.get_calls.append(kwargs)
        body = TrackedBody(self.current)
        self.bodies.append(body)
        return {"Body": body, "ETag": self.etag, "VersionId": "version-1"}

    def put_object(self, **kwargs):
        self.put_calls.append(kwargs)
        if self.put_error is not None:
            raise self.put_error
        stream = kwargs["Body"]
        uploaded = stream.read() if hasattr(stream, "read") else bytes(stream)
        self.current = uploaded
        self.etag = '"updated-etag"'
        return {"ETag": self.etag, "VersionId": "version-2"}


class FakeSSM:
    def __init__(self, values=None, *, error=None):
        self.values = dict(SECRETS if values is None else values)
        self.error = error
        self.calls = []

    def get_parameter(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        name = kwargs["Name"]
        if name not in self.values:
            raise KeyError(name)
        return {"Parameter": {"Name": name, "Type": "SecureString", "Value": self.values[name]}}


@pytest.fixture
def handler_module(monkeypatch):
    """Import the not-yet-added handler with boto3 replaced by an offline stub."""
    boto3_stub = SimpleNamespace(client=lambda *_args, **_kwargs: None)
    monkeypatch.setitem(sys.modules, "boto3", boto3_stub)
    sys.modules.pop("lambda_handler", None)
    module = importlib.import_module("lambda_handler")
    yield module
    sys.modules.pop("lambda_handler", None)


@pytest.fixture
def aws_runtime(handler_module, monkeypatch, seeded_db):
    s3 = FakeS3(seeded_db.read_bytes())
    ssm = FakeSSM()
    clients = {"s3": s3, "ssm": ssm}
    client_calls = []

    def client_factory(service, **kwargs):
        client_calls.append((service, kwargs))
        return clients[service]

    monkeypatch.setattr(handler_module.boto3, "client", client_factory)
    for key, value in CONFIG.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("AWS_REGION", "us-east-2")
    monkeypatch.setenv("RAINCHECK_DRY_RUN", "0")
    monkeypatch.delenv("EBAY_CLIENT_ID", raising=False)
    monkeypatch.delenv("EBAY_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("SENDER_EMAIL", raising=False)
    monkeypatch.delenv("SENDER_PASSWORD", raising=False)
    monkeypatch.delenv("RECEIVER_EMAIL", raising=False)
    return SimpleNamespace(module=handler_module, s3=s3, ssm=ssm, clients=clients, client_calls=client_calls)


def _patch_jobs(monkeypatch, runtime, *, ebay=None, yahoo=None):
    import ebay_pricing

    calls = []

    def fake_ebay():
        calls.append("ebay")
        if ebay:
            return ebay()
        return SimpleNamespace(had_failure=False, failed_item_ids=())

    def fake_yahoo():
        calls.append("yahoo")
        if yahoo:
            return yahoo()
        return 0

    monkeypatch.setattr(ebay_pricing, "refresh_market_prices", fake_ebay)
    monkeypatch.setattr(yahoo_scrape, "main", fake_yahoo)
    return calls


_UNSET_EVENT = object()


def _handler(runtime, event=_UNSET_EVENT):
    return runtime.module.handler({} if event is _UNSET_EVENT else event, None)


def test_rc11_c3_empty_and_malformed_events_cannot_redirect_configured_resources(
    aws_runtime, monkeypatch
):
    runtime = aws_runtime
    _patch_jobs(monkeypatch, runtime)
    hostile_events = [
        {},
        {"bucket": "attacker-bucket", "key": "other.db", "region": "us-west-2"},
        {"detail": {"bucket": "attacker-bucket", "parameters": ["/other"]}},
        None,
        "not-an-event-object",
        ["malformed"],
    ]

    for event in hostile_events:
        _handler(runtime, event)

    assert runtime.s3.get_calls == [
        {"Bucket": CONFIG["RAINCHECK_DB_BUCKET"], "Key": CONFIG["RAINCHECK_DB_KEY"]}
    ] * len(hostile_events)
    assert all(call["Name"] in SECRETS for call in runtime.ssm.calls)
    assert all(call[1].get("region_name", "us-east-2") == "us-east-2" for call in runtime.client_calls)


def test_rc11_c3_wrong_selected_region_fails_before_aws_clients_or_jobs(
    aws_runtime, monkeypatch
):
    runtime = aws_runtime
    _patch_jobs(monkeypatch, runtime)
    monkeypatch.setenv("AWS_REGION", "us-west-2")

    with pytest.raises(Exception):
        _handler(runtime)

    assert runtime.client_calls == []


@pytest.mark.parametrize("bad_config", [
    ("RAINCHECK_DB_BUCKET", ""),
    ("RAINCHECK_DB_KEY", ""),
    ("RAINCHECK_SSM_EBAY_CLIENT_ID", ""),
    ("RAINCHECK_TMP_CAPACITY_BYTES", "not-an-integer"),
    ("RAINCHECK_TMP_CAPACITY_BYTES", "0"),
])
def test_rc11_c3_invalid_required_config_fails_before_aws_clients_or_jobs(
    aws_runtime, monkeypatch, bad_config
):
    runtime = aws_runtime
    _patch_jobs(monkeypatch, runtime)
    monkeypatch.setenv(*bad_config)

    with pytest.raises(Exception):
        _handler(runtime)

    assert runtime.client_calls == []
    assert runtime.s3.get_calls == []


@pytest.mark.parametrize("case", ["missing", "corrupt", "wrong-version", "foreign-key", "oversized"])
def test_rc11_c4_invalid_database_fails_closed_before_jobs_or_upload(
    aws_runtime, monkeypatch, tmp_path, case
):
    runtime = aws_runtime
    calls = _patch_jobs(monkeypatch, runtime)
    if case == "missing":
        runtime.s3.get_object = lambda **_kwargs: (_ for _ in ()).throw(KeyError("missing object"))
    elif case == "corrupt":
        runtime.s3.current = b"not a sqlite database"
    elif case == "wrong-version":
        path = tmp_path / "wrong-version.db"
        path.write_bytes(runtime.s3.current)
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA user_version = 3")
        conn.commit()
        conn.close()
        runtime.s3.current = path.read_bytes()
    elif case == "foreign-key":
        path = tmp_path / "bad-foreign-key.db"
        path.write_bytes(runtime.s3.current)
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("INSERT INTO listing_matches (listing_id, item_id, matched_at) VALUES (987654, 987654, 'test')")
        conn.commit()
        conn.close()
        runtime.s3.current = path.read_bytes()
    elif case == "oversized":
        monkeypatch.setenv("RAINCHECK_TMP_CAPACITY_BYTES", "1")

    original = runtime.s3.current
    with pytest.raises(Exception):
        _handler(runtime)

    assert calls == []
    assert runtime.s3.put_calls == []
    assert runtime.s3.current == original


def test_rc11_c4_integrity_check_failure_does_not_run_jobs_or_replace_object(
    aws_runtime, monkeypatch, tmp_path
):
    runtime = aws_runtime
    calls = _patch_jobs(monkeypatch, runtime)
    path = tmp_path / "integrity-failure.db"
    path.write_bytes(runtime.s3.current)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA writable_schema = ON")
    conn.execute("UPDATE sqlite_master SET sql = 'invalid schema text' WHERE type = 'table' AND name = 'brands'")
    conn.commit()
    conn.close()
    runtime.s3.current = path.read_bytes()
    original = runtime.s3.current

    with pytest.raises(Exception):
        _handler(runtime)

    assert calls == []
    assert runtime.s3.put_calls == []
    assert runtime.s3.current == original


def test_rc11_c5_invocations_use_unique_database_paths_and_remove_temporary_files(
    aws_runtime, monkeypatch
):
    runtime = aws_runtime
    paths = []

    def ebay():
        paths.append(os.environ["RAINCHECK_DB_PATH"])
        return SimpleNamespace(had_failure=False, failed_item_ids=())

    _patch_jobs(monkeypatch, runtime, ebay=ebay)
    _handler(runtime)
    _handler(runtime)

    assert len(set(paths)) == 2
    assert all(not os.path.exists(path) for path in paths)


def test_rc11_c5_temporary_directory_is_removed_after_job_failure(aws_runtime, monkeypatch):
    runtime = aws_runtime
    paths = []

    def fail_ebay():
        paths.append(os.environ["RAINCHECK_DB_PATH"])
        raise RuntimeError("synthetic failure")

    _patch_jobs(monkeypatch, runtime, ebay=fail_ebay)
    with pytest.raises(Exception):
        _handler(runtime)
    assert len(paths) == 1
    assert not os.path.exists(paths[0])


def test_rc11_c6_download_body_is_closed_on_success_and_failure(aws_runtime, monkeypatch):
    runtime = aws_runtime
    _patch_jobs(monkeypatch, runtime)
    _handler(runtime)
    assert runtime.s3.bodies[-1].was_closed

    runtime.s3.current = b"corrupt"
    with pytest.raises(Exception):
        _handler(runtime)
    assert runtime.s3.bodies[-1].was_closed


def test_rc11_c6_handler_closes_every_sqlite_connection_it_opens(aws_runtime, monkeypatch):
    runtime = aws_runtime
    tracked = []
    original_connect = sqlite3.connect

    class TrackedConnection:
        def __init__(self, connection):
            self.connection = connection
            self.closed = False
            tracked.append(self)

        def close(self):
            self.closed = True
            return self.connection.close()

        def backup(self, target, *args, **kwargs):
            if isinstance(target, TrackedConnection):
                target = target.connection
            return self.connection.backup(target, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.connection, name)

    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: TrackedConnection(
        original_connect(*args, **kwargs)
    ))
    _patch_jobs(monkeypatch, runtime)

    _handler(runtime)

    assert tracked
    assert all(connection.closed for connection in tracked)


@pytest.mark.parametrize("parameter_case", ["missing", "blank"])
def test_rc11_c7_required_parameter_missing_or_blank_stops_before_jobs_and_upload(
    aws_runtime, monkeypatch, parameter_case
):
    runtime = aws_runtime
    calls = _patch_jobs(monkeypatch, runtime)
    if parameter_case == "missing":
        runtime.ssm.values.pop(CONFIG["RAINCHECK_SSM_EBAY_CLIENT_SECRET"])
    else:
        runtime.ssm.values[CONFIG["RAINCHECK_SSM_EBAY_CLIENT_SECRET"]] = "   "

    with pytest.raises(Exception):
        _handler(runtime)

    assert calls == []
    assert runtime.s3.put_calls == []
    assert runtime.ssm.calls
    assert all(call["WithDecryption"] is True for call in runtime.ssm.calls)


def test_rc11_c8_secret_values_are_absent_from_logs_and_sanitized_errors(
    aws_runtime, monkeypatch, caplog, capsys
):
    runtime = aws_runtime
    secret = "do-not-leak-this-ssm-error-value"
    runtime.ssm.error = RuntimeError(secret)
    _patch_jobs(monkeypatch, runtime)

    with pytest.raises(Exception) as caught:
        _handler(runtime)

    assert secret not in str(caught.value)
    assert secret not in caplog.text
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err


def test_rc11_c9_success_runs_ebay_before_yahoo_once_and_uploads_job_changes(
    aws_runtime, monkeypatch, tmp_path
):
    runtime = aws_runtime
    order = []

    def ebay():
        order.append("ebay")
        conn = sqlite3.connect(os.environ["RAINCHECK_DB_PATH"])
        try:
            conn.execute("UPDATE items SET market_price_usd = 777 WHERE id = 1")
            conn.commit()
        finally:
            conn.close()
        return SimpleNamespace(had_failure=False, failed_item_ids=())

    def yahoo():
        order.append("yahoo")
        conn = sqlite3.connect(os.environ["RAINCHECK_DB_PATH"])
        try:
            conn.execute("UPDATE items SET market_price_usd = 666 WHERE id = 2")
            conn.commit()
        finally:
            conn.close()
        return 0

    _patch_jobs(monkeypatch, runtime, ebay=ebay, yahoo=yahoo)
    _handler(runtime)

    assert order == ["ebay", "yahoo"]
    assert len(runtime.s3.put_calls) == 1
    assert {call["Name"] for call in runtime.ssm.calls} == set(SECRETS)
    assert all(call["WithDecryption"] is True for call in runtime.ssm.calls)
    uploaded = tmp_path / "uploaded.db"
    uploaded.write_bytes(runtime.s3.current)
    assert rows(uploaded, "SELECT id, market_price_usd FROM items ORDER BY id") == [(1, 777.0), (2, 666.0)]


def test_rc11_c9_two_brand_yahoo_run_fetches_each_brand_and_sends_one_digest(
    aws_runtime, monkeypatch, seeded_db
):
    runtime = aws_runtime
    # Build a synthetic second brand and matching tracked item in the throwaway DB.
    conn = sqlite3.connect(seeded_db)
    try:
        conn.execute("INSERT INTO brands (id, name, yahoo_brand_id) VALUES (2, 'Brand B', '123456')")
        conn.execute("INSERT INTO items (id, brand_id, name, manual_price_usd, active, config_source) VALUES (99, 2, 'brand b ramones', 800, 1, 'cli')")
        conn.execute("INSERT INTO item_keywords (item_id, keyword, kind) VALUES (99, 'ramones', 'include')")
        conn.commit()
    finally:
        conn.close()
    runtime.s3.current = seeded_db.read_bytes()
    # To isolate RC-13's actual multi-brand pipeline, feed one synthetic page per brand.
    second_url = yahoo_scrape.BRAND_URL.format("123456")
    fake_pages = {
        BRAND_URL: _yahoo_page("AAA111", "Rick Owens Ramones synthetic", 1000),
        second_url: _yahoo_page("BBB222", "Brand B Ramones synthetic", 1000),
    }
    fetches = []

    def fetch(url):
        fetches.append(url)
        return fake_pages[url]

    sent = []
    monkeypatch.setattr(yahoo_scrape, "fetch_yahoo_page", fetch)
    monkeypatch.setattr(yahoo_scrape, "resolve_yen_to_usd_rate", lambda _date: 0.0062)
    monkeypatch.setattr(yahoo_scrape, "send_email", lambda *args: sent.append(args) or True)
    _patch_jobs(monkeypatch, runtime, ebay=lambda: SimpleNamespace(had_failure=False, failed_item_ids=()))
    monkeypatch.setattr(yahoo_scrape, "main", REAL_YAHOO_MAIN)

    _handler(runtime)

    assert sorted(fetches) == sorted([BRAND_URL, second_url])
    assert len(sent) == 1
    assert "Rick Owens / rick owens ramones" in sent[0][1]
    assert "Brand B / brand b ramones" in sent[0][1]


def test_rc11_c10_partial_ebay_failure_runs_yahoo_uploads_and_reports_failure(
    aws_runtime, monkeypatch, tmp_path
):
    runtime = aws_runtime
    order = []

    def ebay():
        order.append("ebay")
        conn = sqlite3.connect(os.environ["RAINCHECK_DB_PATH"])
        try:
            conn.execute("UPDATE items SET market_price_usd = 733 WHERE id = 1")
            conn.commit()
        finally:
            conn.close()
        return SimpleNamespace(had_failure=True, failed_item_ids=(1,))

    def yahoo():
        order.append("yahoo")
        return 0

    _patch_jobs(monkeypatch, runtime, ebay=ebay, yahoo=yahoo)
    with pytest.raises(Exception):
        _handler(runtime)

    assert order == ["ebay", "yahoo"]
    assert len(runtime.s3.put_calls) == 1
    uploaded = _database_from_s3(tmp_path=tmp_path, body=runtime.s3.current)
    assert rows(uploaded, "SELECT market_price_usd FROM items WHERE id = 1") == [(733.0,)]


def test_rc11_c11_ebay_token_failure_still_runs_yahoo_and_uploads_sanitely(
    aws_runtime, monkeypatch, caplog
):
    runtime = aws_runtime
    calls = []
    secret = "synthetic-token-secret-error"
    effective_prices = []

    def ebay():
        calls.append("ebay")
        raise RuntimeError(secret)

    def yahoo():
        calls.append("yahoo")
        effective_prices.extend(item.effective_price_usd for item in db.get_tracked_items())
        return 0

    _patch_jobs(monkeypatch, runtime, ebay=ebay, yahoo=yahoo)
    with pytest.raises(Exception) as caught:
        _handler(runtime)

    assert calls == ["ebay", "yahoo"]
    assert effective_prices == [800.0, 300.0]
    assert len(runtime.s3.put_calls) == 1
    assert secret not in str(caught.value)
    assert secret not in caplog.text


def test_rc11_c12_yahoo_failure_uploads_ebay_changes_then_fails(
    aws_runtime, monkeypatch, tmp_path
):
    runtime = aws_runtime
    order = []

    def ebay():
        order.append("ebay")
        conn = sqlite3.connect(os.environ["RAINCHECK_DB_PATH"])
        try:
            conn.execute("UPDATE items SET market_price_usd = 712 WHERE id = 1")
            conn.commit()
        finally:
            conn.close()
        return SimpleNamespace(had_failure=False, failed_item_ids=())

    def yahoo():
        order.append("yahoo")
        raise SystemExit(1)

    _patch_jobs(monkeypatch, runtime, ebay=ebay, yahoo=yahoo)
    with pytest.raises(Exception):
        _handler(runtime)

    assert order == ["ebay", "yahoo"]
    assert len(runtime.s3.put_calls) == 1
    uploaded = _database_from_s3(tmp_path=tmp_path, body=runtime.s3.current)
    assert rows(uploaded, "SELECT market_price_usd FROM items WHERE id = 1") == [(712.0,)]


def test_rc11_c13_smtp_failure_does_not_record_matches_and_preserves_ebay_change(
    aws_runtime, monkeypatch, seeded_db, tmp_path
):
    runtime = aws_runtime
    runtime.s3.current = seeded_db.read_bytes()

    def ebay_change():
        conn = sqlite3.connect(os.environ["RAINCHECK_DB_PATH"])
        try:
            conn.execute("UPDATE items SET market_price_usd = 645 WHERE id = 1")
            conn.commit()
        finally:
            conn.close()
        return SimpleNamespace(had_failure=False, failed_item_ids=())

    ebay_calls = _patch_jobs(monkeypatch, runtime, ebay=ebay_change)

    def real_yahoo_failure():
        monkeypatch.setattr(yahoo_scrape, "fetch_yahoo_page", lambda _url: _yahoo_page(
            "SMTP111", "Rick Owens Ramones synthetic", 1000
        ))
        monkeypatch.setattr(yahoo_scrape, "resolve_yen_to_usd_rate", lambda _date: 0.0062)
        monkeypatch.setattr(yahoo_scrape, "send_email", lambda *_args: False)
        return _run_real_yahoo()

    import yahoo_scrape as yahoo_module
    monkeypatch.setattr(yahoo_module, "main", real_yahoo_failure)
    before = rows(seeded_db, "SELECT COUNT(*) FROM listing_matches")[0][0]

    with pytest.raises(Exception):
        _handler(runtime)

    uploaded = _database_from_s3(tmp_path=tmp_path, body=runtime.s3.current)
    after = rows(uploaded, "SELECT COUNT(*) FROM listing_matches")[0][0]
    market_price = rows(uploaded, "SELECT market_price_usd FROM items WHERE id = 1")[0][0]
    assert after == before
    assert market_price == 645.0
    assert len(runtime.s3.put_calls) == 1
    assert ebay_calls == ["ebay"]


def test_rc11_c14_post_send_upload_failure_warns_digest_may_repeat(
    aws_runtime, monkeypatch, caplog
):
    runtime = aws_runtime
    runtime.s3.put_error = RuntimeError("offline upload failure")
    _patch_jobs(
        monkeypatch, runtime,
        ebay=lambda: SimpleNamespace(had_failure=False, failed_item_ids=()),
    )
    monkeypatch.setattr(yahoo_scrape, "fetch_yahoo_page", lambda _url: _yahoo_page(
        "UPLOAD111", "Rick Owens Ramones synthetic", 1000
    ))
    monkeypatch.setattr(yahoo_scrape, "resolve_yen_to_usd_rate", lambda _date: 0.0062)
    sent = []
    monkeypatch.setattr(yahoo_scrape, "send_email", lambda *args: sent.append(args) or True)
    monkeypatch.setattr(yahoo_scrape, "main", _run_real_yahoo)
    with pytest.raises(Exception):
        _handler(runtime)

    assert len(sent) == 1
    assert any("repeat" in record.getMessage().lower() for record in caplog.records)


def test_rc11_c8_smtp_exception_secret_is_not_exposed(aws_runtime, monkeypatch, caplog):
    runtime = aws_runtime
    secret = "synthetic-smtp-password-error"
    _patch_jobs(
        monkeypatch, runtime,
        ebay=lambda: SimpleNamespace(had_failure=False, failed_item_ids=()),
    )
    monkeypatch.setattr(yahoo_scrape, "fetch_yahoo_page", lambda _url: _yahoo_page(
        "SMTPSECRET1", "Rick Owens Ramones synthetic", 1000
    ))
    monkeypatch.setattr(yahoo_scrape, "resolve_yen_to_usd_rate", lambda _date: 0.0062)

    def smtp_error(*_args):
        raise RuntimeError(secret)

    monkeypatch.setattr(yahoo_scrape, "send_email", smtp_error)
    monkeypatch.setattr(yahoo_scrape, "main", _run_real_yahoo)

    with pytest.raises(Exception) as caught:
        _handler(runtime)

    assert secret not in str(caught.value)
    assert secret not in caplog.text


@pytest.mark.parametrize("status", [409, 412])
def test_rc11_c15_conditional_write_conflict_fails_without_refetch_or_retry(
    aws_runtime, monkeypatch, status
):
    runtime = aws_runtime
    runtime.s3.put_error = RuntimeError(f"{status} conditional write conflict")
    _patch_jobs(monkeypatch, runtime)
    original = runtime.s3.current

    with pytest.raises(Exception):
        _handler(runtime)

    assert len(runtime.s3.get_calls) == 1
    assert len(runtime.s3.put_calls) == 1
    assert runtime.s3.put_calls[0]["IfMatch"] == '"initial-etag"'
    assert runtime.s3.current == original


def test_rc11_c15_upload_uses_download_etag_as_ifmatch(aws_runtime, monkeypatch):
    runtime = aws_runtime
    _patch_jobs(monkeypatch, runtime)

    _handler(runtime)

    assert runtime.s3.put_calls[0]["IfMatch"] == '"initial-etag"'


def test_rc11_c16_snapshot_is_standalone_valid_and_contains_wal_commits(
    aws_runtime, monkeypatch, tmp_path
):
    runtime = aws_runtime
    saw_wal = []
    held_connections = []

    def ebay():
        path = os.environ["RAINCHECK_DB_PATH"]
        conn = sqlite3.connect(path)
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("UPDATE items SET market_price_usd = 901 WHERE id = 1")
            conn.commit()
            saw_wal.append(os.path.exists(path + "-wal"))
            held_connections.append(conn)
        finally:
            if not held_connections:
                conn.close()
        return SimpleNamespace(had_failure=False, failed_item_ids=())

    _patch_jobs(monkeypatch, runtime, ebay=ebay)
    try:
        _handler(runtime)
    finally:
        for conn in held_connections:
            conn.close()

    snapshot = tmp_path / "snapshot.db"
    snapshot.write_bytes(runtime.s3.current)
    assert saw_wal == [True]
    assert rows(snapshot, "PRAGMA integrity_check") == [("ok",)]
    assert rows(snapshot, "PRAGMA foreign_key_check") == []
    assert user_version(snapshot) == db.SCHEMA_VERSION
    assert rows(snapshot, "SELECT market_price_usd FROM items WHERE id = 1") == [(901.0,)]
    assert not os.path.exists(str(snapshot) + "-wal")
    assert not os.path.exists(str(snapshot) + "-shm")


def test_rc11_c24_dry_run_uses_synthetic_storage_and_suppresses_digest_from_logs(
    aws_runtime, monkeypatch, caplog, capsys
):
    runtime = aws_runtime
    temp_bucket = "raincheck-dry-run-only"
    monkeypatch.setenv("RAINCHECK_DB_BUCKET", temp_bucket)
    monkeypatch.setenv("RAINCHECK_DRY_RUN", "1")
    synthetic_recipient = "synthetic-recipient@example.invalid"
    runtime.ssm.values[CONFIG["RAINCHECK_SSM_RECEIVER_EMAIL"]] = synthetic_recipient
    import sendemail
    monkeypatch.setattr(sendemail.smtplib, "SMTP", lambda *_args, **_kwargs: pytest.fail(
        "dry-run must not open SMTP"
    ))
    title = "Rick Owens Ramones synthetic listing"
    html_page = _yahoo_page("DRY111", title, 1000)
    monkeypatch.setattr(yahoo_scrape, "fetch_yahoo_page", lambda _url: html_page)
    monkeypatch.setattr(yahoo_scrape, "resolve_yen_to_usd_rate", lambda _date: 0.0062)
    _patch_jobs(monkeypatch, runtime, ebay=lambda: SimpleNamespace(had_failure=False, failed_item_ids=()))
    monkeypatch.setattr(yahoo_scrape, "main", REAL_YAHOO_MAIN)

    _handler(runtime)

    captured = capsys.readouterr()
    emitted = captured.out + captured.err + caplog.text
    assert title not in emitted
    assert "<table" not in emitted.lower()
    assert synthetic_recipient not in emitted
    assert len(runtime.s3.put_calls) == 1
    assert runtime.s3.get_calls[0]["Bucket"] == temp_bucket
    assert runtime.s3.put_calls[0]["Bucket"] == temp_bucket


def _yahoo_page(auction_id, title, price_yen):
    record = {
        "auctionId": auction_id,
        "title": title,
        "price": price_yen,
        "isFixedPrice": True,
        "buyNowPrice": price_yen,
        "isFleamarketItem": False,
    }
    data = {
        "props": {"pageProps": {"initialState": {"search": {"items": {
            "listing": {"items": [record], "totalResultsAvailable": 1}
        }}}}},
        "page": "/category/list/[categoryId]",
    }
    return '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(data) + "</script>"


def _run_real_yahoo():
    return REAL_YAHOO_MAIN()


def _database_from_s3(*, tmp_path, body):
    path = tmp_path / "uploaded.db"
    path.write_bytes(body)
    return path
