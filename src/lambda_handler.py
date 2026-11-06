"""Run the existing pricing and deal jobs against one versioned S3 database."""

from contextlib import ExitStack, closing, contextmanager, redirect_stdout
import io
import hashlib
import logging
import os
from pathlib import Path
import sqlite3
import tempfile
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

import db
import ebay_pricing
import sendemail
import yahoo_scrape


logger = logging.getLogger(__name__)
REGION = "us-east-2"
# Reserve room for both copies plus SQLite's small temporary sidecars.
TEMP_HEADROOM_BYTES = 1024 * 1024
AWS_CONFIG = Config(connect_timeout=5, read_timeout=30, retries={"total_max_attempts": 3})
MANAGEMENT_S3_CONFIG = Config(connect_timeout=5, read_timeout=30,
                              retries={"total_max_attempts": 1})
PARAMETER_NAMES = {
    "RAINCHECK_SSM_EBAY_CLIENT_ID": "EBAY_CLIENT_ID",
    "RAINCHECK_SSM_EBAY_CLIENT_SECRET": "EBAY_CLIENT_SECRET",
    "RAINCHECK_SSM_SENDER_EMAIL": "SENDER_EMAIL",
    "RAINCHECK_SSM_SENDER_PASSWORD": "SENDER_PASSWORD",
    "RAINCHECK_SSM_RECEIVER_EMAIL": "RECEIVER_EMAIL",
}
REQUIRED_PARAMETER_NAMES = tuple(PARAMETER_NAMES)[:-1]


class RunnerError(RuntimeError):
    """An invocation failure safe to show in Lambda logs."""


def _configuration():
    """Validate every nonsecret setting before making an AWS call."""
    if os.environ.get("AWS_REGION") != REGION:
        raise RunnerError("Lambda must run in us-east-2")
    bucket = os.environ.get("RAINCHECK_DB_BUCKET", "").strip()
    key = os.environ.get("RAINCHECK_DB_KEY", "").strip()
    parameter_names = {
        setting: os.environ.get(setting, "").strip() for setting in PARAMETER_NAMES
    }
    capacity_text = os.environ.get("RAINCHECK_TMP_CAPACITY_BYTES", "")
    if (
        not bucket or not key or not capacity_text.isascii()
        or not capacity_text.isdecimal() or int(capacity_text) <= 0
        or any(not parameter_names[name] for name in REQUIRED_PARAMETER_NAMES)
    ):
        raise RunnerError("Invalid Lambda configuration")
    return bucket, key, int(capacity_text), parameter_names


def _close_client(client):
    close = getattr(client, "close", None)
    if close is not None:
        close()


def _fits_capacity(size, capacity):
    return 2 * size + TEMP_HEADROOM_BYTES <= capacity


def _download(s3, bucket, key, path, capacity):
    response = s3.get_object(Bucket=bucket, Key=key)
    body = response["Body"]
    with closing(body):
        size = 0
        with path.open("wb") as output:
            while chunk := body.read(1024 * 1024):
                size += len(chunk)
                if not _fits_capacity(size, capacity):
                    raise RunnerError("Database exceeds temporary storage capacity")
                output.write(chunk)
    etag = response.get("ETag")
    if not isinstance(etag, str) or not etag:
        raise RunnerError("Downloaded database has no ETag")
    return etag, response.get("VersionId")


def _check_database(connection):
    """Reject bad state before it can be used or replace the S3 object."""
    if [row[0] for row in connection.execute("PRAGMA integrity_check")] != ["ok"]:
        raise RunnerError("Database integrity check failed")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise RunnerError("Database foreign key check failed")
    if connection.execute("PRAGMA user_version").fetchone()[0] != db.SCHEMA_VERSION:
        raise RunnerError("Database schema version does not match this release")


def _load_credentials(ssm, parameter_names):
    credentials = {}
    for setting, environment_name in PARAMETER_NAMES.items():
        name = parameter_names[setting]
        if not name:
            continue
        response = ssm.get_parameter(Name=name, WithDecryption=True)
        parameter = response["Parameter"]
        value = parameter.get("Value")
        if parameter.get("Type") != "SecureString" or not isinstance(value, str) or not value.strip():
            raise RunnerError("Required credential is missing or blank")
        credentials[environment_name] = value
    credentials.setdefault("RECEIVER_EMAIL", None)
    return credentials


@contextmanager
def _environment(values):
    """Restore warm Lambda process variables after the invocation."""
    old = {name: os.environ.get(name) for name in values}
    try:
        for name, value in values.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        yield
    finally:
        for name, value in old.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _run_jobs():
    ebay_failed = False
    yahoo_failed = False
    dry_run = sendemail.is_dry_run()
    try:
        result = ebay_pricing.refresh_market_prices()
        ebay_failed = bool(result.had_failure)
    except (db.SchemaVersionError, sqlite3.Error):
        # A database failure is not a token failure; do not run another job against it.
        logger.warning("eBay database access failed; Yahoo skipped")
        return True
    except Exception:
        # The eBay job handles item errors itself; an unhandled error can arise during token acquisition.
        ebay_failed = True
        logger.warning("eBay pricing failed; running Yahoo with saved prices")

    try:
        if dry_run:
            # sendemail prints the full digest in dry run; discard it before CloudWatch sees stdout.
            with redirect_stdout(io.StringIO()):
                yahoo_result = yahoo_scrape.main()
        else:
            yahoo_result = yahoo_scrape.main()
        yahoo_failed = yahoo_result not in (None, 0)
    except SystemExit as exc:
        yahoo_failed = exc.code not in (None, 0)
    except Exception:
        yahoo_failed = True
    if yahoo_failed:
        logger.warning("Yahoo deals job failed")
    elif dry_run:
        logger.info("Lambda dry run completed")
    return ebay_failed or yahoo_failed


def _snapshot(working_path, snapshot_path, capacity):
    # backup() includes committed WAL pages in one standalone SQLite file.
    sidecar_size = sum(
        Path(str(working_path) + suffix).stat().st_size
        for suffix in ("", "-wal", "-shm")
        if Path(str(working_path) + suffix).exists()
    )
    if not _fits_capacity(sidecar_size, capacity):
        raise RunnerError("Database exceeds temporary storage capacity")
    with closing(sqlite3.connect(working_path)) as source:
        with closing(sqlite3.connect(snapshot_path)) as target:
            source.backup(target)
            _check_database(target)
    if working_path.stat().st_size + snapshot_path.stat().st_size > capacity:
        raise RunnerError("Database snapshot exceeds temporary storage capacity")


def _management_request(event):
    """Reject malformed management events before an AWS client is created."""
    if not isinstance(event, dict):
        return False
    if "raincheck_management" not in event:
        return False
    required = {"raincheck_management", "operation_id", "item_id", "action", "kind",
                "keyword", "expected_version_id"}
    try:
        valid_uuid = str(uuid.UUID(event.get("operation_id", ""))) == event.get("operation_id")
    except (TypeError, ValueError, AttributeError):
        valid_uuid = False
    if (set(event) != required or event["raincheck_management"] != "keyword-v1"
            or os.environ.get("RAINCHECK_MANAGEMENT_PROTOCOL") != "keyword-v1"
            or not valid_uuid or type(event["item_id"]) is not int or event["item_id"] <= 0
            or event["action"] not in ("add", "remove")
            or event["kind"] not in ("include", "exclude")
            or not isinstance(event["keyword"], str) or not event["keyword"].strip()
            or not isinstance(event["expected_version_id"], str)
            or not event["expected_version_id"].strip()):
        raise RunnerError("Invalid keyword management request")
    return True


def _keyword_sets(item_id):
    with closing(db.get_connection()) as connection:
        includes, excludes = db._item_keywords(connection, item_id)
    return {"include": list(includes), "exclude": list(excludes)}


def _manage_keyword(event):
    bucket, key, capacity, _ = _configuration()
    operation_id = event["operation_id"]
    with ExitStack() as resources:
        s3 = boto3.client("s3", region_name=REGION, config=MANAGEMENT_S3_CONFIG)
        resources.callback(_close_client, s3)
        with tempfile.TemporaryDirectory(prefix="raincheck-keyword-", dir="/tmp") as directory:
            working_path = Path(directory) / "working.db"
            snapshot_path = Path(directory) / "snapshot.db"
            etag, version_id = _download(s3, bucket, key, working_path, capacity)
            if not version_id or version_id != event["expected_version_id"]:
                return {"status": "rejected", "reason": "stale", "operation_id": operation_id}
            with _environment({"RAINCHECK_DB_PATH": str(working_path)}):
                with closing(db.get_connection()) as connection:
                    _check_database(connection)
                before = _keyword_sets(event["item_id"])
                try:
                    changed = db.change_keyword(event["item_id"], event["action"],
                                                event["kind"], event["keyword"])
                except ValueError:
                    return {"status": "rejected", "reason": "invalid", "operation_id": operation_id}
                after = _keyword_sets(event["item_id"])
            if not changed:
                return {"status": "unchanged", "operation_id": operation_id,
                        "original_version_id": version_id, "before": before, "after": after}
            _snapshot(working_path, snapshot_path, capacity)
            checksum = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
            try:
                with snapshot_path.open("rb") as body:
                    uploaded = s3.put_object(
                        Bucket=bucket, Key=key, Body=body, IfMatch=etag,
                        Metadata={"raincheck-operation-id": operation_id},
                    )
            except ClientError as exc:
                if str(exc.response.get("Error", {}).get("Code")) in ("412", "PreconditionFailed"):
                    return {"status": "rejected", "reason": "stale", "operation_id": operation_id}
                if str(exc.response.get("Error", {}).get("Code")) in ("409", "ConditionalRequestConflict"):
                    return {"status": "rejected", "reason": "conflict", "operation_id": operation_id}
                raise
            new_version = uploaded.get("VersionId")
            if not isinstance(new_version, str) or not new_version:
                raise RunnerError("Uploaded database has no version ID")
            return {"status": "changed", "operation_id": operation_id,
                    "original_version_id": version_id, "new_version_id": new_version,
                    "sha256": checksum, "before": before, "after": after}


def handler(event, context):
    """Run one isolated invocation; Scheduler event fields cannot change its resources."""
    if _management_request(event):
        try:
            return _manage_keyword(event)
        except RunnerError:
            raise
        except Exception:
            raise RunnerError("Keyword management failed") from None
    bucket, key, capacity, parameter_names = _configuration()
    try:
        with ExitStack() as resources:
            s3 = boto3.client("s3", region_name=REGION, config=AWS_CONFIG)
            resources.callback(_close_client, s3)
            ssm = boto3.client("ssm", region_name=REGION, config=AWS_CONFIG)
            resources.callback(_close_client, ssm)
            with tempfile.TemporaryDirectory(prefix="raincheck-", dir="/tmp") as directory:
                working_path = Path(directory) / "working.db"
                snapshot_path = Path(directory) / "snapshot.db"
                etag, version_id = _download(s3, bucket, key, working_path, capacity)
                with _environment({"RAINCHECK_DB_PATH": str(working_path)}):
                    # get_connection checks the release schema without creating a missing file.
                    with closing(db.get_connection()) as connection:
                        _check_database(connection)
                    credentials = _load_credentials(ssm, parameter_names)
                    with _environment(credentials):
                        jobs_failed = _run_jobs()
                try:
                    _snapshot(working_path, snapshot_path, capacity)
                    with snapshot_path.open("rb") as body:
                        s3.put_object(Bucket=bucket, Key=key, Body=body, IfMatch=etag)
                except Exception:
                    logger.warning("Database persistence failed; a sent digest may repeat")
                    raise RunnerError("Database persistence failed; a sent digest may repeat") from None
                if jobs_failed:
                    raise RunnerError("RainCheck job failed; valid database changes were saved")
                logger.info("RainCheck invocation finished; database version downloaded: %s", version_id)
                return {"status": "ok"}
    except RunnerError:
        raise
    except Exception:
        # AWS and SMTP exceptions can contain credentials or response bodies.
        raise RunnerError("RainCheck invocation failed") from None
