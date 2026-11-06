"""Offline acceptance tests for RC-21 live keyword management.

The public CLI boundary is ``live_keywords.main(argv=None)``.  AWS access is
replaced at the boto3 Session boundary, and Lambda behavior is exercised by
calling the existing public ``lambda_handler.handler`` entry point.
"""

from __future__ import annotations

import hashlib
import importlib
import io
import json
import sqlite3
import sys
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

import db
from dbhelpers import dump_all, keyword_sets, rows, sha256, user_version


ACCOUNT = "510258803420"
REGION = "us-east-2"
BUCKET = "raincheck-test-bucket"
KEY = "raincheck.db"
STACK = "raincheck"
FUNCTION = "raincheck-raincheck"
PROTOCOL = "keyword-v1"
_LAST_OUTPUT = ""


def _fresh_module(monkeypatch, name):
    sys.modules.pop(name, None)
    module = importlib.import_module(name)
    return module


@pytest.fixture
def lambda_module(monkeypatch):
    boto3_stub = SimpleNamespace(client=lambda *_args, **_kwargs: None)
    monkeypatch.setitem(sys.modules, "boto3", boto3_stub)
    module = _fresh_module(monkeypatch, "lambda_handler")
    yield module
    sys.modules.pop("lambda_handler", None)


class _Body(io.BytesIO):
    @property
    def closed_by_client(self):
        return self.closed


class _LambdaS3:
    def __init__(self, data, *, version="version-1", etag='"etag-1"'):
        self.data = data
        self.version = version
        self.etag = etag
        self.gets = []
        self.puts = []
        self.bodies = []
        self.put_error = None

    def get_object(self, **kwargs):
        self.gets.append(kwargs)
        body = _Body(self.data)
        self.bodies.append(body)
        return {"Body": body, "VersionId": self.version, "ETag": self.etag}

    def put_object(self, **kwargs):
        self.puts.append(kwargs)
        if self.put_error:
            raise self.put_error
        body = kwargs["Body"]
        self.data = body.read() if hasattr(body, "read") else bytes(body)
        self.version = "version-2"
        return {"VersionId": self.version, "ETag": '"etag-2"'}


class _NoUse:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        self.calls.append(name)
        raise AssertionError(f"management request must not call {name}")


class _AllowSSM:
    def get_parameter(self, **kwargs):
        return {"Parameter": {"Type": "SecureString", "Value": "synthetic-value"}}


def _configure_lambda(monkeypatch, module, s3, *, marker=PROTOCOL, ssm=None):
    services = {"s3": s3, "ssm": ssm or _NoUse()}
    calls = []

    def client(service, **kwargs):
        calls.append((service, kwargs))
        return services[service]

    monkeypatch.setattr(module.boto3, "client", client)
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setenv("RAINCHECK_DB_BUCKET", BUCKET)
    monkeypatch.setenv("RAINCHECK_DB_KEY", KEY)
    monkeypatch.setenv("RAINCHECK_TMP_CAPACITY_BYTES", str(512 * 1024 * 1024))
    monkeypatch.setenv("RAINCHECK_SSM_EBAY_CLIENT_ID", "/test/id")
    monkeypatch.setenv("RAINCHECK_SSM_EBAY_CLIENT_SECRET", "/test/secret")
    monkeypatch.setenv("RAINCHECK_SSM_SENDER_EMAIL", "/test/sender")
    monkeypatch.setenv("RAINCHECK_SSM_SENDER_PASSWORD", "/test/password")
    monkeypatch.setenv("RAINCHECK_DRY_RUN", "0")
    if marker is None:
        monkeypatch.delenv("RAINCHECK_MANAGEMENT_PROTOCOL", raising=False)
    else:
        monkeypatch.setenv("RAINCHECK_MANAGEMENT_PROTOCOL", marker)
    return calls


def _management_event(**overrides):
    event = {
        "raincheck_management": PROTOCOL,
        "operation_id": "00000000-0000-4000-8000-000000000001",
        "item_id": 1,
        "action": "add",
        "kind": "include",
        "keyword": "sneakers",
        "expected_version_id": "version-1",
    }
    event.update(overrides)
    return event


def _table_snapshot(path):
    return dump_all(path)


def test_rc21_ac1_cli_list_is_read_only_and_never_invokes_or_changes_schedule(
    seeded_db, monkeypatch, tmp_path
):
    cli_path = Path(__file__).parents[1] / "src" / "live_keywords.py"
    assert cli_path.is_file(), "RC-21 public CLI is not present"
    cli = _fresh_module(monkeypatch, "live_keywords")
    calls = []
    _install_cli_session(monkeypatch, cli, calls, seeded_db.read_bytes(), tmp_path)
    before = seeded_db.read_bytes()

    assert _run_cli(cli, ["list", "1"]) == 0

    output = capsys_read()
    assert "ramon" in output.lower()
    assert "include" in output.lower() and "exclude" in output.lower()
    assert _run_cli(cli, ["list", "1"]) == 0
    assert capsys_read() == output
    assert not any(name == "invoke" for name, _ in calls)
    assert not any(name in {"put_object", "update_schedule", "enable_schedule", "disable_schedule"}
                   for name, _ in calls)
    assert seeded_db.read_bytes() == before


def test_rc21_ac1_cli_list_reports_no_items_clearly(tmp_path, monkeypatch):
    empty = tmp_path / "empty.db"
    with closing(sqlite3.connect(empty)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript((Path(__file__).parents[1] / "src" / "schema.sql").read_text())
        connection.execute("PRAGMA user_version = 4")
    cli = _cli_for_test(monkeypatch)
    calls = []
    _install_cli_session(monkeypatch, cli, calls, empty.read_bytes(), tmp_path)
    assert _run_cli(cli, ["list"]) == 0
    assert "no items" in capsys_read().lower()


def test_rc21_ac2_edit_changes_only_one_keyword_and_owns_item(seeded_db, monkeypatch, tmp_path):
    cli = _cli_for_test(monkeypatch)
    baseline = _table_snapshot(seeded_db)
    calls = []
    s3 = _install_cli_session(monkeypatch, cli, calls, seeded_db.read_bytes(), tmp_path)

    assert _run_cli(cli, ["add", "1", "--include", "sneakers"]) == 0

    snapshot = tmp_path / "remote-after.db"
    snapshot.write_bytes(s3.uploaded)
    after = _table_snapshot(snapshot)
    assert "sneakers" in keyword_sets(snapshot)["rick owens ramones"]["include"]
    for table in ("brands", "listings", "listing_matches", "ebay_listing_prices"):
        assert after[table] == baseline[table]
    before_keywords = set(baseline["item_keywords"])
    after_keywords = set(after["item_keywords"])
    assert after_keywords - before_keywords == {(1, "sneakers", "include")}
    assert before_keywords - after_keywords == set()
    before_items = {row[0]: row for row in baseline["items"]}
    after_items = {row[0]: row for row in after["items"]}
    assert after_items[1][-1] == "cli"
    assert after_items[1][:-1] == before_items[1][:-1]
    assert after_items[2] == before_items[2]
    assert len(s3.versions) == 2


@pytest.mark.parametrize(("action", "kind", "term"), [
    ("add", "include", "ramon"),
    ("remove", "exclude", "absent-term"),
])
def test_rc21_ac3_noop_preserves_ownership_and_skips_invoke_and_upload(
    seeded_db, monkeypatch, tmp_path, action, kind, term
):
    cli = _cli_for_test(monkeypatch)
    before = seeded_db.read_bytes()
    calls = []
    _install_cli_session(monkeypatch, cli, calls, before, tmp_path)

    assert _run_cli(cli, [action, "1", f"--{kind}", term]) == 0

    assert "no change" in capsys_read().lower()
    assert seeded_db.read_bytes() == before
    assert not any(name in {"invoke", "put_object"} for name, _ in calls)


@pytest.mark.parametrize(("action", "kind", "term"), [
    ("add", "include", " SNEAKERS "),
    ("remove", "exclude", " FAKE "),
])
def test_rc21_ac4_database_keyword_api_normalizes_terms(action, kind, term, seeded_db):
    change = getattr(db, "change_keyword", None)
    assert callable(change), "RC-21 single-term database operation is missing"
    assert change(1, action, kind, term) is True
    assert change(1, action, kind, term.strip().lower()) is False


@pytest.mark.parametrize(("item_id", "action", "kind", "keyword"), [
    (1, "add", "include", "  "),
    (1, "add", "include", "fake"),
    (1, "remove", "include", "ramon"),
    (999, "add", "include", "sneakers"),
])
def test_rc21_ac4_database_api_rejects_invalid_edits_atomically(
    item_id, action, kind, keyword, seeded_db
):
    change = getattr(db, "change_keyword", None)
    assert callable(change), "RC-21 single-term database operation is missing"
    if (item_id, action, kind, keyword) == (1, "remove", "include", "ramon"):
        with closing(sqlite3.connect(seeded_db)) as connection:
            connection.execute(
                "DELETE FROM item_keywords WHERE item_id = ? AND kind = ? AND keyword <> ?",
                (item_id, kind, keyword),
            )
            connection.commit()
    before = _table_snapshot(seeded_db)
    with pytest.raises(ValueError):
        change(item_id, action, kind, keyword)
    assert _table_snapshot(seeded_db) == before


def test_rc21_ac4_database_change_closes_connection_on_success_and_error(
    seeded_db, monkeypatch
):
    change = getattr(db, "change_keyword", None)
    assert callable(change), "RC-21 single-term database operation is missing"
    original_connect = sqlite3.connect
    closed = []

    class TrackedConnection(sqlite3.Connection):
        def close(self):
            closed.append(True)
            super().close()

    def connect(*args, **kwargs):
        return original_connect(*args, factory=TrackedConnection, **kwargs)

    monkeypatch.setattr(db.sqlite3, "connect", connect)
    assert change(1, "add", "include", "boots") is True
    with pytest.raises(ValueError):
        change(999, "add", "include", "boots")
    assert len(closed) == 2


@pytest.mark.parametrize(("action", "kind", "term"), [
    ("add", "include", "  "),
    ("add", "include", "fake"),
])
def test_rc21_ac4_invalid_keyword_edits_fail_without_remote_write(
    seeded_db, monkeypatch, tmp_path, action, kind, term
):
    cli = _cli_for_test(monkeypatch)
    calls = []
    _install_cli_session(monkeypatch, cli, calls, seeded_db.read_bytes(), tmp_path)
    assert _run_cli(cli, [action, "1", f"--{kind}", term]) == 1
    assert not any(name in {"invoke", "put_object"} for name, _ in calls)


def test_rc21_ac4_removing_last_include_or_editing_unknown_item_fails_before_invoke(
    seeded_db, monkeypatch, tmp_path
):
    with closing(sqlite3.connect(seeded_db)) as connection:
        connection.execute("DELETE FROM item_keywords WHERE item_id = 1 AND keyword != ?", ("ramon",))
        connection.commit()
    cli = _cli_for_test(monkeypatch)
    for argv in (["remove", "1", "--include", "ramon"],
                 ["add", "999", "--include", "sneakers"]):
        calls = []
        _install_cli_session(monkeypatch, cli, calls, seeded_db.read_bytes(), tmp_path)
        assert _run_cli(cli, argv) == 1
        assert not any(name == "invoke" for name, _ in calls)


def test_rc21_ac6_backup_and_dispatch_record_are_durable_before_invoke(
    seeded_db, monkeypatch, tmp_path
):
    cli = _cli_for_test(monkeypatch)
    calls = []
    _install_cli_session(monkeypatch, cli, calls, seeded_db.read_bytes(), tmp_path)
    assert _run_cli(cli, ["add", "1", "--include", "sneakers"]) == 0
    invoke_index = next(i for i, (name, _) in enumerate(calls) if name == "invoke")
    artifacts = tmp_path / "RainCheck-backups"
    backup = next(artifacts.glob("*.db"))
    record = next(artifacts.glob("*.json"))
    assert backup.resolve().is_relative_to(artifacts.resolve())
    assert backup.exists() and backup.stat().st_size > 0
    backup_conn = sqlite3.connect(backup)
    try:
        assert backup_conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup_conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert backup_conn.execute("PRAGMA user_version").fetchone()[0] == 4
    finally:
        backup_conn.close()
    journal = json.loads(record.read_text(encoding="utf-8"))
    assert journal["original_version_id"] == "version-1"
    assert journal["original_etag"] == '"etag-1"'
    assert journal["dispatched_at"]
    assert journal["status"] == "verified"
    # Reopen output records the final state; the fake invoke checks the pre-dispatch state.
    assert calls[invoke_index][1]["InvocationType"] == "RequestResponse"


def test_rc21_ac7_management_lambda_skips_all_daily_job_boundaries(
    seeded_db, lambda_module, monkeypatch
):
    original = seeded_db.read_bytes()
    s3 = _LambdaS3(original)
    ssm = _NoUse()
    calls = _configure_lambda(monkeypatch, lambda_module, s3, ssm=ssm)
    job_calls = []
    monkeypatch.setattr(lambda_module.ebay_pricing, "refresh_market_prices", lambda: job_calls.append("ebay"))
    monkeypatch.setattr(lambda_module.yahoo_scrape, "main", lambda: job_calls.append("yahoo"))
    email_calls = []
    monkeypatch.setattr(lambda_module.sendemail, "send_email", lambda *a, **k: email_calls.append((a, k)))
    try:
        result = lambda_module.handler(_management_event(), None)
    except lambda_module.RunnerError as exc:
        pytest.fail(f"management event was routed through the daily path: {exc}")

    assert result["status"] in {"changed", "unchanged"}
    assert job_calls == []
    assert email_calls == []
    assert ssm.calls == []
    assert [service for service, _ in calls] == ["s3"]
    assert all(body.closed_by_client for body in s3.bodies)
    assert len(s3.puts) == 1
    assert s3.puts[0]["IfMatch"] == '"etag-1"'
    assert s3.puts[0]["Metadata"]["raincheck-operation-id"] == _management_event()["operation_id"]


@pytest.mark.parametrize("bad_event", [
    {"raincheck_management": "future-v2"},
    {**_management_event(), "extra": True},
    {**_management_event(), "item_id": True},
    {**_management_event(), "item_id": 0},
    {**_management_event(), "action": "replace"},
    {key: value for key, value in _management_event().items() if key != "keyword"},
    ["malformed"],
])
def test_rc21_ac7_malformed_or_unknown_management_event_fails_before_aws(
    lambda_module, monkeypatch, bad_event
):
    calls = []
    monkeypatch.setattr(lambda_module.boto3, "client", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setenv("RAINCHECK_DB_BUCKET", BUCKET)
    monkeypatch.setenv("RAINCHECK_DB_KEY", KEY)
    monkeypatch.setenv("RAINCHECK_TMP_CAPACITY_BYTES", str(512 * 1024 * 1024))
    monkeypatch.setenv("RAINCHECK_SSM_EBAY_CLIENT_ID", "/test/id")
    monkeypatch.setenv("RAINCHECK_SSM_EBAY_CLIENT_SECRET", "/test/secret")
    monkeypatch.setenv("RAINCHECK_SSM_SENDER_EMAIL", "/test/sender")
    monkeypatch.setenv("RAINCHECK_SSM_SENDER_PASSWORD", "/test/password")
    monkeypatch.setenv("RAINCHECK_MANAGEMENT_PROTOCOL", PROTOCOL)

    with pytest.raises(lambda_module.RunnerError):
        lambda_module.handler(bad_event, None)
    assert calls == []


@pytest.mark.parametrize("marker", [None, "future-v2"])
def test_rc21_ac7_wrong_lambda_management_marker_fails_before_aws(
    lambda_module, monkeypatch, marker
):
    calls = []
    _configure_lambda(monkeypatch, lambda_module, _LambdaS3(b"unused"), marker=marker)
    monkeypatch.setattr(lambda_module.boto3, "client", lambda *a, **k: calls.append((a, k)))
    with pytest.raises(lambda_module.RunnerError):
        lambda_module.handler(_management_event(), None)
    assert calls == []


def test_rc21_ac8_cli_reports_busy_and_does_not_touch_schedule(seeded_db, monkeypatch, tmp_path):
    cli = _cli_for_test(monkeypatch)
    calls = []
    s3 = _install_cli_session(monkeypatch, cli, calls, seeded_db.read_bytes(), tmp_path,
                              invoke_error="TooManyRequestsException")
    assert _run_cli(cli, ["add", "1", "--include", "sneakers"]) == 1
    output = capsys_read().lower()
    assert "busy" in output and ("retry" in output or "try again" in output)
    assert not any(name in {"update_schedule", "disable_schedule", "enable_schedule"}
                   for name, _ in calls)
    assert list(s3.versions) == ["version-1"]


@pytest.mark.parametrize("case", ["stale", "412", "409"])
def test_rc21_ac9_stale_or_conditional_conflict_never_succeeds(
    lambda_module, monkeypatch, seeded_db, case
):
    s3 = _LambdaS3(seeded_db.read_bytes(), version="version-2" if case == "stale" else "version-1")
    if case in {"412", "409"}:
        s3.put_error = ClientError({"Error": {"Code": case, "Message": "conflict"}}, "PutObject")
    _configure_lambda(monkeypatch, lambda_module, s3)
    try:
        result = lambda_module.handler(_management_event(), None)
    except lambda_module.RunnerError as exc:
        pytest.fail(f"management edit did not return a structured {case} rejection: {exc}")
    assert result["status"] in {"stale", "conflict", "rejected"}
    assert s3.data == seeded_db.read_bytes()
    if case == "stale":
        assert s3.puts == []
    else:
        assert len(s3.puts) == 1


def test_rc21_ac9_management_upload_timeout_disables_sdk_retry_and_stays_uncertain(
    seeded_db, lambda_module, monkeypatch
):
    s3 = _LambdaS3(seeded_db.read_bytes())
    s3.put_error = TimeoutError("upload response lost")
    calls = _configure_lambda(monkeypatch, lambda_module, s3, ssm=_AllowSSM())
    monkeypatch.setattr(
        lambda_module.ebay_pricing,
        "refresh_market_prices",
        lambda: SimpleNamespace(had_failure=False, failed_item_ids=()),
    )
    monkeypatch.setattr(lambda_module.yahoo_scrape, "main", lambda: 0)

    try:
        result = lambda_module.handler(_management_event(), None)
    except lambda_module.RunnerError:
        assert len(s3.puts) == 1
    else:
        assert len(s3.puts) == 1
        assert result.get("status") not in {"rejected", "conflict"}

    s3_client_config = next(kwargs["config"] for service, kwargs in calls if service == "s3")
    assert s3_client_config.connect_timeout > 0
    assert s3_client_config.read_timeout > 0
    assert s3_client_config.retries.get("total_max_attempts") == 1


def test_rc21_ac10_success_verifies_exact_new_version_and_checksum(
    seeded_db, monkeypatch, tmp_path
):
    cli = _cli_for_test(monkeypatch)
    calls = []
    s3 = _install_cli_session(monkeypatch, cli, calls, seeded_db.read_bytes(), tmp_path)
    assert _run_cli(cli, ["add", "1", "--include", "sneakers"]) == 0
    invoke = next(payload for name, payload in calls if name == "invoke")
    assert invoke.get("InvocationType") == "RequestResponse"
    event = json.loads(invoke["Payload"])
    assert set(event) == {
        "raincheck_management", "operation_id", "item_id", "action", "kind",
        "keyword", "expected_version_id",
    }
    assert event["raincheck_management"] == PROTOCOL
    assert event["item_id"] == 1 and type(event["item_id"]) is int
    assert event["action"] == "add" and event["kind"] == "include"
    assert event["keyword"] == "sneakers"
    assert event["expected_version_id"] == "version-1"
    assert invoke["FunctionName"] == FUNCTION
    assert any(kwargs.get("VersionId") == "version-2" for name, kwargs in calls if name == "get_object")
    assert not any(name == "put_object" for name, _ in calls)
    assert s3.invoke_response["sha256"] == hashlib.sha256(
        s3.versions["version-2"]["data"]
    ).hexdigest()
    assert all(body.closed_by_client for body in s3.bodies)
    assert "verified" in capsys_read().lower()


@pytest.mark.parametrize("failure", [
    "function-error", "service-error", "timeout", "timeout-no-upload",
    "bad-version-read", "checksum", "corrupt-upload",
])
def test_rc21_ac11_post_dispatch_failures_remain_uncertain_and_recoverable(
    seeded_db, monkeypatch, tmp_path, failure
):
    cli = _cli_for_test(monkeypatch)
    calls = []
    _install_cli_session(monkeypatch, cli, calls, seeded_db.read_bytes(), tmp_path, invoke_error=failure)
    assert _run_cli(cli, ["add", "1", "--include", "sneakers"]) == 1
    assert "success" not in capsys_read().lower()
    record = next(tmp_path.glob("**/*.json"))
    assert json.loads(record.read_text(encoding="utf-8"))["status"] in {
        "uncertain", "dispatching", "failed"
    }


def test_rc21_ac12_recovery_is_read_only_and_does_not_infer_absence_from_head(
    seeded_db, monkeypatch, tmp_path
):
    cli = _cli_for_test(monkeypatch)
    calls = []
    _install_cli_session(monkeypatch, cli, calls, seeded_db.read_bytes(), tmp_path,
                         invoke_error="timeout-no-upload")
    assert _run_cli(cli, ["add", "1", "--include", "sneakers"]) == 1
    record = next(tmp_path.glob("**/*.json"))
    calls.clear()
    assert _run_cli(cli, ["recover", str(record)]) == 1
    assert not any(name in {"invoke", "put_object"} for name, _ in calls)
    assert "uncertain" in capsys_read().lower()


def test_rc21_ac12_recovery_finds_paginated_receipt_and_reports_latest_state(
    seeded_db, monkeypatch, tmp_path
):
    cli = _cli_for_test(monkeypatch)
    calls = []
    s3 = _install_cli_session(monkeypatch, cli, calls, seeded_db.read_bytes(), tmp_path,
                              invoke_error="timeout")
    assert _run_cli(cli, ["add", "1", "--include", "sneakers"]) == 1
    record = next((tmp_path / "RainCheck-backups").glob("*.json"))

    later = tmp_path / "daily-copy.db"
    later.write_bytes(s3.versions["version-2"]["data"])
    with closing(sqlite3.connect(later)) as connection:
        connection.execute(
            "INSERT INTO item_keywords (item_id, keyword, kind) VALUES (?, ?, ?)",
            (1, "boots", "include"),
        )
        connection.commit()
    s3.add_version("version-3", later.read_bytes())
    calls.clear()

    assert _run_cli(cli, ["recover", str(record)]) == 0
    assert "applied" in capsys_read().lower()
    assert "latest" in capsys_read().lower()
    assert "boots" in capsys_read().lower()
    assert sum(name == "list_object_versions" for name, _ in calls) >= 2
    assert any(name == "head_object" and kwargs.get("VersionId") == "version-2"
               for name, kwargs in calls)
    assert not any(name in {"invoke", "put_object"} for name, _ in calls)


def test_rc21_ac12_unresolved_operation_blocks_a_second_edit(
    seeded_db, monkeypatch, tmp_path
):
    cli = _cli_for_test(monkeypatch)
    calls = []
    _install_cli_session(monkeypatch, cli, calls, seeded_db.read_bytes(), tmp_path,
                         invoke_error="timeout-no-upload")
    assert _run_cli(cli, ["add", "1", "--include", "sneakers"]) == 1
    assert _run_cli(cli, ["add", "1", "--include", "boots"]) == 1
    assert sum(name == "invoke" for name, _ in calls) == 1
    assert "unresolved" in capsys_read().lower() or "recover" in capsys_read().lower()


def test_rc21_ac13_commands_never_change_schedule_configuration(seeded_db, monkeypatch, tmp_path):
    cli = _cli_for_test(monkeypatch)
    calls = []
    _install_cli_session(monkeypatch, cli, calls, seeded_db.read_bytes(), tmp_path)
    assert _run_cli(cli, ["list"]) == 0
    schedule_calls = [entry for entry in calls if "schedule" in entry[0].lower()]
    assert schedule_calls == []


def test_rc21_ac14_offline_boundary_uses_temp_schema4_database_and_fake_aws(
    seeded_db, monkeypatch, tmp_path
):
    assert Path(db.get_db_path()).resolve() == seeded_db.resolve()
    assert user_version(seeded_db) == 4
    cli = _cli_for_test(monkeypatch)
    calls = []
    _install_cli_session(monkeypatch, cli, calls, seeded_db.read_bytes(), tmp_path)
    assert _run_cli(cli, ["list"]) == 0
    assert all(isinstance(name, str) for name, _ in calls)
    assert Path(db.get_db_path()).resolve() == seeded_db.resolve()


def test_rc21_ac11_aws_clients_have_timeouts_and_mutating_invoke_has_no_retries(
    seeded_db, monkeypatch, tmp_path
):
    cli = _cli_for_test(monkeypatch)
    calls = []
    _install_cli_session(monkeypatch, cli, calls, seeded_db.read_bytes(), tmp_path)
    assert _run_cli(cli, ["add", "1", "--include", "sneakers"]) == 0
    client_configs = [kwargs.get("config") for name, kwargs in calls if name == "client"]
    assert client_configs
    assert all(config is not None and config.connect_timeout > 0 and config.read_timeout > 0
               for config in client_configs)
    lambda_configs = [kwargs["config"] for name, kwargs in calls
                      if name == "client" and kwargs["service"] == "lambda"]
    assert lambda_configs
    assert all(config.retries.get("total_max_attempts") == 1 for config in lambda_configs)
def test_rc21_ac5_cli_rejects_each_preflight_mismatch_before_invoke(
    seeded_db, monkeypatch, tmp_path
):
    cli = _cli_for_test(monkeypatch)
    for mismatch in (
        "account", "stack", "missing-output", "database-output", "function-output",
        "db-bucket", "db-key", "function-name", "dry-run", "timeout",
        "missing-marker", "marker", "concurrency",
    ):
        calls = []
        _install_cli_session(monkeypatch, cli, calls, seeded_db.read_bytes(), tmp_path,
                             preflight_mismatch=mismatch)
        assert _run_cli(cli, ["add", "1", "--include", f"term-{mismatch}"]) == 1
        assert not any(name == "invoke" for name, _ in calls), mismatch


def _cli_for_test(monkeypatch):
    path = Path(__file__).parents[1] / "src" / "live_keywords.py"
    assert path.is_file(), "RC-21 public CLI is not present"
    return _fresh_module(monkeypatch, "live_keywords")


def _install_cli_session(monkeypatch, cli, calls, data, tmp_path, *, invoke_error=None,
                         preflight_mismatch=None):
    """Provide deterministic clients; implementation must use boto3.Session(profile_name=...)."""
    artifacts = tmp_path / "RainCheck-backups"
    s3 = _CliS3(data, calls)
    lambda_client = _CliLambda(calls, s3, artifacts, invoke_error=invoke_error)
    sts = _CliSTS(calls, mismatch=preflight_mismatch)
    cloudformation = _CliCloudFormation(calls, mismatch=preflight_mismatch)
    lambda_client.preflight_mismatch = preflight_mismatch
    clients = {"s3": s3, "lambda": lambda_client, "sts": sts,
               "cloudformation": cloudformation}

    class Session:
        def __init__(self, *, profile_name=None, region_name=None, **kwargs):
            calls.append(("session", {"profile_name": profile_name, "region_name": region_name, **kwargs}))
            assert profile_name == "raincheck"
            if region_name is not None:
                assert region_name == REGION
            self.region_name = region_name

        def client(self, service, **kwargs):
            calls.append(("client", {"service": service, **kwargs}))
            assert kwargs.get("region_name", self.region_name) == REGION
            return clients[service]

    monkeypatch.setattr(cli.boto3, "Session", Session)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return s3


class _CliSTS:
    def __init__(self, calls, mismatch=None): self.calls, self.mismatch = calls, mismatch
    def close(self): pass
    def get_caller_identity(self):
        self.calls.append(("get_caller_identity", {}))
        return {"Account": "000000000000" if self.mismatch == "account" else ACCOUNT}


class _CliCloudFormation:
    def __init__(self, calls, mismatch=None): self.calls, self.mismatch = calls, mismatch
    def close(self): pass
    def describe_stacks(self, **kwargs):
        self.calls.append(("describe_stacks", kwargs))
        outputs = {"DatabaseBucketName": BUCKET, "DatabaseKey": KEY, "FunctionName": FUNCTION}
        if self.mismatch == "stack":
            return {"Stacks": []}
        if self.mismatch == "missing-output": outputs.pop("DatabaseKey")
        if self.mismatch == "database-output": outputs["DatabaseKey"] = "other.db"
        if self.mismatch == "function-output": outputs["FunctionName"] = "other-function"
        return {"Stacks": [{"Outputs": [{"OutputKey": k, "OutputValue": v} for k, v in outputs.items()]}]}


class _CliLambda:
    def __init__(self, calls, s3, artifacts, invoke_error=None):
        self.calls, self.s3, self.artifacts = calls, s3, artifacts
        self.invoke_error = invoke_error

    def close(self): pass

    def get_function_configuration(self, **kwargs):
        self.calls.append(("get_function_configuration", kwargs))
        mismatch = getattr(self, "preflight_mismatch", None)
        marker = None if mismatch == "missing-marker" else ("wrong" if mismatch == "marker" else PROTOCOL)
        return {"FunctionName": "other" if mismatch == "function-name" else FUNCTION,
                "Timeout": 841 if mismatch == "timeout" else 840,
                "Environment": {"Variables": {
                    "RAINCHECK_DB_BUCKET": "other-bucket" if mismatch == "db-bucket" else BUCKET,
                    "RAINCHECK_DB_KEY": "other.db" if mismatch == "db-key" else KEY,
                    "RAINCHECK_DRY_RUN": "1" if mismatch == "dry-run" else "0",
                    **({} if marker is None else {"RAINCHECK_MANAGEMENT_PROTOCOL": marker}),
                }}}

    def get_function_concurrency(self, **kwargs):
        self.calls.append(("get_function_concurrency", kwargs))
        return {"ReservedConcurrentExecutions": 2 if getattr(self, "preflight_mismatch", None) == "concurrency" else 1}

    def invoke(self, **kwargs):
        self.calls.append(("invoke", kwargs))
        payload = json.loads(kwargs["Payload"])
        if self.invoke_error == "TooManyRequestsException":
            raise ClientError({"Error": {"Code": "TooManyRequestsException", "Message": "busy"}}, "Invoke")
        if self.invoke_error == "timeout-no-upload":
            raise TimeoutError("response lost before observed upload")
        if self.invoke_error == "service-error":
            raise ClientError({"Error": {"Code": "ServiceException", "Message": "failed"}}, "Invoke")
        if self.invoke_error == "function-error":
            self._apply(payload)
            return {"StatusCode": 200, "FunctionError": "Unhandled", "Payload": _Body(b"{}")} 
        if self.invoke_error in {None, "timeout", "bad-version-read", "checksum", "corrupt-upload"}:
            self._apply(payload)
        if self.invoke_error == "timeout":
            raise TimeoutError("response lost after upload")
        if self.invoke_error in {"bad-version-read", "corrupt-upload"}:
            self.s3.bad_version_read = self.invoke_error == "bad-version-read"
            self.s3.corrupt_version = self.invoke_error == "corrupt-upload"
        digest = hashlib.sha256(self.s3.versions["version-2"]["data"]).hexdigest()
        if self.invoke_error == "checksum": digest = "invalid-checksum"
        response = {"status": "changed", "operation_id": payload["operation_id"],
                    "original_version_id": payload["expected_version_id"],
                    "new_version_id": "version-2", "sha256": digest,
                    "before": self.before, "after": self.after}
        self.s3.invoke_response = response
        return {"StatusCode": 200, "Payload": _Body(json.dumps(response).encode())}

    def _apply(self, payload):
        # Prove the backup and the fsynced dispatch record exist before emulating
        # the first instruction of the remote mutating operation.
        assert self.artifacts.is_dir()
        backups = list(self.artifacts.glob("*.db"))
        records = list(self.artifacts.glob("*.json"))
        assert backups and records
        record = json.loads(records[-1].read_text(encoding="utf-8"))
        assert record["original_version_id"] == payload["expected_version_id"]
        assert record["original_etag"] == '"etag-1"'
        assert record["dispatched_at"]
        assert record["status"] == "dispatching"
        prior_calls = self.calls[:-1]
        assert any(name == "get_object" and kwargs.get("VersionId") == payload["expected_version_id"]
                   for name, kwargs in prior_calls)
        assert any(name == "list_object_versions" for name, _ in prior_calls)

        path = self.artifacts / "lambda-copy.db"
        path.write_bytes(self.s3.data)
        try:
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                self.before = {
                    kind: [row[0] for row in connection.execute(
                        "SELECT keyword FROM item_keywords WHERE item_id = ? AND kind = ? ORDER BY keyword",
                        (payload["item_id"], kind),
                    )]
                    for kind in ("include", "exclude")
                }
                keyword = payload["keyword"].strip().lower()
                if payload["action"] == "add":
                    connection.execute(
                        "INSERT INTO item_keywords (item_id, keyword, kind) VALUES (?, ?, ?)",
                        (payload["item_id"], keyword, payload["kind"]),
                    )
                else:
                    connection.execute(
                        "DELETE FROM item_keywords WHERE item_id = ? AND keyword = ? AND kind = ?",
                        (payload["item_id"], keyword, payload["kind"]),
                    )
                connection.execute("UPDATE items SET config_source = 'cli' WHERE id = ?", (payload["item_id"],))
                connection.commit()
                self.after = {
                    kind: [row[0] for row in connection.execute(
                        "SELECT keyword FROM item_keywords WHERE item_id = ? AND kind = ? ORDER BY keyword",
                        (payload["item_id"], kind),
                    )]
                    for kind in ("include", "exclude")
                }
            self.s3.add_version("version-2", path.read_bytes(), payload["operation_id"])
        finally:
            path.unlink(missing_ok=True)


class _CliS3:
    def __init__(self, data, calls, mismatch=None):
        self.data, self.calls = data, calls
        self.versions = {"version-1": {"data": data, "metadata": {}, "etag": '"etag-1"'}}
        self.latest = "version-1"
        self.bad_version_read = False
        self.corrupt_version = False
        self.bodies = []

    def close(self): pass

    def get_paginator(self, operation_name):
        self.calls.append(("get_paginator", {"operation_name": operation_name}))
        assert operation_name == "list_object_versions"
        s3 = self

        class Paginator:
            def paginate(self, **kwargs):
                while True:
                    response = s3.list_object_versions(**kwargs)
                    yield response
                    if not response.get("IsTruncated"):
                        return
                    kwargs = {
                        "Bucket": kwargs["Bucket"],
                        "Prefix": kwargs["Prefix"],
                        "KeyMarker": response["NextKeyMarker"],
                        "VersionIdMarker": response["NextVersionIdMarker"],
                    }

        return Paginator()

    @property
    def uploaded(self):
        return self.versions.get("version-2", {}).get("data")

    def add_version(self, version_id, data, operation_id=None):
        metadata = {"raincheck-operation-id": operation_id} if operation_id else {}
        self.versions[version_id] = {"data": data, "metadata": metadata, "etag": '"etag-2"'}
        self.latest = version_id

    def get_object(self, **kwargs):
        self.calls.append(("get_object", kwargs))
        version_id = kwargs.get("VersionId", self.latest)
        if version_id == "version-2" and self.bad_version_read:
            raise RuntimeError("version not found")
        data = self.versions[version_id]["data"]
        if version_id == "version-2" and self.corrupt_version:
            data = b"not a sqlite snapshot"
        body = _Body(data)
        self.bodies.append(body)
        return {"Body": body, "VersionId": version_id,
                "ETag": self.versions[version_id]["etag"]}
    def head_object(self, **kwargs):
        self.calls.append(("head_object", kwargs))
        version_id = kwargs.get("VersionId", self.latest)
        version = self.versions[version_id]
        return {"VersionId": version_id, "ETag": version["etag"],
                "Metadata": dict(version["metadata"])}
    def list_object_versions(self, **kwargs):
        self.calls.append(("list_object_versions", kwargs))
        ordered = list(reversed(self.versions))
        marker = kwargs.get("VersionIdMarker")
        start = ordered.index(marker) + 1 if marker in ordered else 0
        page = ordered[start:start + 1]
        truncated = start + 1 < len(ordered)
        response = {
            "Versions": [{"Key": KEY, "VersionId": version_id, "IsLatest": version_id == self.latest}
                         for version_id in page],
            "IsTruncated": truncated,
        }
        if truncated:
            response["NextKeyMarker"] = KEY
            response["NextVersionIdMarker"] = page[-1]
        return response


def _run_cli(cli, argv):
    global _LAST_OUTPUT
    output = io.StringIO()
    with redirect_stdout(output), redirect_stderr(output):
        result = cli.main(argv)
    _LAST_OUTPUT = output.getvalue()
    return result


def capsys_read():
    return _LAST_OUTPUT
