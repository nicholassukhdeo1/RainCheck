"""Safely manage one keyword in the versioned production database."""

import argparse
from contextlib import ExitStack, closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

import db


ACCOUNT = "510258803420"
REGION = "us-east-2"
STACK = "raincheck"
FUNCTION = "raincheck-raincheck"
KEY = "raincheck.db"
PROTOCOL = "keyword-v1"
READ_CONFIG = Config(connect_timeout=5, read_timeout=30,
                     retries={"total_max_attempts": 3})
# The Invoke may commit before a response is lost, so the SDK must never resend it.
INVOKE_CONFIG = Config(connect_timeout=5, read_timeout=860,
                       retries={"total_max_attempts": 1})
TABLE_QUERIES = {
    "brands": "SELECT * FROM brands",
    "items": "SELECT * FROM items",
    "item_keywords": "SELECT * FROM item_keywords",
    "listings": "SELECT * FROM listings",
    "listing_matches": "SELECT * FROM listing_matches",
    "ebay_listing_prices": "SELECT * FROM ebay_listing_prices",
}


class CommandError(RuntimeError):
    pass


def _positive_id(value):
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("item ID must be positive") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("item ID must be positive")
    return number


def _parser():
    parser = argparse.ArgumentParser(description="Manage one live RainCheck keyword")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="show production keywords").add_argument(
        "item_id", nargs="?", type=_positive_id)
    for action in ("add", "remove"):
        edit = commands.add_parser(action, help=f"{action} one production keyword")
        edit.add_argument("item_id", type=_positive_id)
        kind = edit.add_mutually_exclusive_group(required=True)
        kind.add_argument("--include")
        kind.add_argument("--exclude")
    commands.add_parser("recover", help="inspect one uncertain operation").add_argument(
        "record", type=Path)
    return parser


def _close_client(client):
    close = getattr(client, "close", None)
    if close is not None:
        close()


@contextmanager
def _db_path(path):
    previous = os.environ.get("RAINCHECK_DB_PATH")
    os.environ["RAINCHECK_DB_PATH"] = str(path)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("RAINCHECK_DB_PATH", None)
        else:
            os.environ["RAINCHECK_DB_PATH"] = previous


def _check_database(path):
    try:
        with closing(sqlite3.connect(path)) as connection:
            if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise CommandError("Database integrity check failed")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise CommandError("Database foreign key check failed")
            if connection.execute("PRAGMA user_version").fetchone()[0] != db.SCHEMA_VERSION:
                raise CommandError("Database schema version is not supported")
    except sqlite3.Error as exc:
        raise CommandError("Invalid SQLite database snapshot") from exc


def _keywords(path, item_id):
    with closing(sqlite3.connect(path)) as connection:
        item = connection.execute("SELECT name FROM items WHERE id = ?", (item_id,)).fetchone()
        if item is None:
            raise CommandError(f"Unknown item ID: {item_id}")
        rows = connection.execute(
            "SELECT keyword, kind FROM item_keywords WHERE item_id = ? ORDER BY keyword",
            (item_id,),
        ).fetchall()
    return item[0], {kind: [term for term, row_kind in rows if row_kind == kind]
                     for kind in ("include", "exclude")}


def _all_items(path):
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute("SELECT id FROM items ORDER BY id").fetchall()


def _rows(path):
    # Fixed table names make the full row comparison independent of row order.
    with closing(sqlite3.connect(path)) as connection:
        result = {table: sorted(connection.execute(query).fetchall(), key=repr)
                  for table, query in TABLE_QUERIES.items()}
        result["schema"] = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
        return result


def _expected(path, record, target):
    shutil.copyfile(path, target)
    with _db_path(target):
        try:
            changed = db.change_keyword(record["item_id"], record["action"],
                                        record["kind"], record["keyword"])
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
    return changed


def _verify_delta(backup, actual, record):
    _check_database(actual)
    with tempfile.TemporaryDirectory(prefix="raincheck-expected-") as directory:
        expected = Path(directory) / "expected.db"
        if not _expected(backup, record, expected):
            raise CommandError("Operation did not change the backup")
        if _rows(expected) != _rows(actual):
            raise CommandError("Uploaded database contains unexpected row changes")
    return _keywords(actual, record["item_id"])[1]


def _download(s3, bucket, key, path, version_id=None):
    args = {"Bucket": bucket, "Key": key}
    if version_id is not None:
        args["VersionId"] = version_id
    response = s3.get_object(**args)
    body = response["Body"]
    try:
        with path.open("wb") as output:
            while chunk := body.read(1024 * 1024):
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
    finally:
        body.close()
    version = response.get("VersionId")
    etag = response.get("ETag")
    if not isinstance(version, str) or not version or not isinstance(etag, str) or not etag:
        raise CommandError("Database object lacks a VersionId or ETag")
    if version_id is not None and version != version_id:
        raise CommandError("Exact S3 version read returned a different version")
    return version, etag


def _version_entries(s3, bucket, key):
    pages = s3.get_paginator("list_object_versions").paginate(Bucket=bucket, Prefix=key)
    for page in pages:
        for entry in page.get("Versions", []):
            if entry.get("Key") == key and entry.get("VersionId"):
                yield entry


def _preflight(session, resources, mutating):
    def client(service, config=READ_CONFIG):
        result = session.client(service, region_name=REGION, config=config)
        resources.callback(_close_client, result)
        return result

    sts = client("sts")
    if sts.get_caller_identity().get("Account") != ACCOUNT:
        raise CommandError("Unexpected AWS account")
    cloudformation = client("cloudformation")
    stacks = cloudformation.describe_stacks(StackName=STACK).get("Stacks", [])
    if len(stacks) != 1:
        raise CommandError("Production stack was not found")
    outputs = {row.get("OutputKey"): row.get("OutputValue")
               for row in stacks[0].get("Outputs", [])}
    bucket, key, function = (outputs.get(name) for name in
                             ("DatabaseBucketName", "DatabaseKey", "FunctionName"))
    if not isinstance(bucket, str) or not bucket or key != KEY or function != FUNCTION:
        raise CommandError("Unexpected or missing production stack outputs")
    lambda_client = None
    if mutating:
        lambda_client = client("lambda", INVOKE_CONFIG)
        configuration = lambda_client.get_function_configuration(FunctionName=function)
        variables = configuration.get("Environment", {}).get("Variables", {})
        if (configuration.get("FunctionName") != function
                or variables.get("RAINCHECK_DB_BUCKET") != bucket
                or variables.get("RAINCHECK_DB_KEY") != key
                or variables.get("RAINCHECK_DRY_RUN") != "0"
                or variables.get("RAINCHECK_MANAGEMENT_PROTOCOL") != PROTOCOL
                or type(configuration.get("Timeout")) is not int
                or not 0 < configuration["Timeout"] <= 840):
            raise CommandError("Production Lambda configuration does not match the stack")
        concurrency = lambda_client.get_function_concurrency(FunctionName=function)
        if concurrency.get("ReservedConcurrentExecutions") != 1:
            raise CommandError("Production Lambda must have one reserved execution slot")
    s3 = client("s3")
    return bucket, key, function, lambda_client, s3


def _backup_dir():
    path = Path.home() / "RainCheck-backups"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def _save_record(path, record):
    # Replace and sync the journal before a possible remote send.
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(record, output, sort_keys=True, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _unresolved(directory):
    for path in directory.glob("raincheck-keywords-*.json"):
        try:
            status = json.loads(path.read_text(encoding="utf-8")).get("status")
        except (OSError, ValueError):
            raise CommandError(f"Unreadable operation record: {path}") from None
        if status not in ("verified", "not_applied"):
            raise CommandError(f"Unresolved operation {path}; run recover {path}")


def _display(path, item_id):
    ids = [item_id] if item_id is not None else [row[0] for row in _all_items(path)]
    if not ids:
        print("No items in production database.")
    for number in ids:
        name, words = _keywords(path, number)
        print(f"{number}: {name}")
        print("  include: " + ", ".join(words["include"]))
        print("  exclude: " + ", ".join(words["exclude"]))


def _edit(args, bucket, key, function, lambda_client, s3, current, version, etag):
    kind = "include" if args.include is not None else "exclude"
    term = args.include if kind == "include" else args.exclude
    record = {"operation_id": str(uuid.uuid4()), "item_id": args.item_id,
              "action": args.command, "kind": kind, "keyword": term,
              "original_version_id": version, "original_etag": etag,
              "bucket": bucket, "key": key, "function": function}
    directory = _backup_dir()
    _unresolved(directory)
    with tempfile.TemporaryDirectory(prefix="raincheck-preview-") as preview_dir:
        preview = Path(preview_dir) / "preview.db"
        changed = _expected(current, record, preview)
    name, before = _keywords(current, args.item_id)
    if not changed:
        print(f"No change for {args.item_id} ({name}): {kind} {term.strip().lower()}")
        return 0
    # Prove an exact version read and version listing are permitted before dispatch.
    with tempfile.TemporaryDirectory(prefix="raincheck-version-") as directory_path:
        exact = Path(directory_path) / "original.db"
        exact_version, exact_etag = _download(s3, bucket, key, exact, version)
        if exact_etag != etag or exact.read_bytes() != current.read_bytes():
            raise CommandError("Original S3 version differs from the current snapshot")
    if not any(entry["VersionId"] == version for entry in _version_entries(s3, bucket, key)):
        raise CommandError("Original database version is absent from S3 version history")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stem = f"raincheck-keywords-{stamp}-{record['operation_id']}"
    backup = directory / f"{stem}.db"
    journal = directory / f"{stem}.json"
    with backup.open("xb") as output, current.open("rb") as source:
        shutil.copyfileobj(source, output)
        output.flush()
        os.fsync(output.fileno())
    _check_database(backup)
    if backup.read_bytes() != current.read_bytes():
        raise CommandError("Local backup does not match downloaded version")
    record.update({"backup_path": str(backup),
                   "backup_sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
                   "status": "prepared"})
    _save_record(journal, record)
    record.update({"status": "dispatching", "dispatched_at": datetime.now(timezone.utc).isoformat()})
    _save_record(journal, record)
    payload = {"raincheck_management": PROTOCOL, "operation_id": record["operation_id"],
               "item_id": args.item_id, "action": args.command, "kind": kind,
               "keyword": term.strip().lower(), "expected_version_id": version}
    try:
        response = lambda_client.invoke(FunctionName=function, InvocationType="RequestResponse",
                                        Payload=json.dumps(payload).encode("utf-8"))
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "TooManyRequestsException":
            record.update({"status": "not_applied", "no_upload_reason": "busy"})
            _save_record(journal, record)
            print(f"Busy: Lambda has an invocation running. Retry later. Backup: {backup}")
            return 1
        return _uncertain(journal, record, backup)
    except Exception:
        return _uncertain(journal, record, backup)
    try:
        body = response["Payload"]
        try:
            result = json.loads(body.read())
        finally:
            body.close()
        record["response"] = result
        record["status"] = "response"
        _save_record(journal, record)
        if response.get("StatusCode") != 200 or response.get("FunctionError"):
            return _uncertain(journal, record, backup)
        if (not isinstance(result, dict) or result.get("operation_id") != record["operation_id"]):
            return _uncertain(journal, record, backup)
        if result.get("status") == "rejected" and result.get("reason") in ("invalid", "stale", "conflict"):
            record.update({"status": "not_applied", "no_upload_reason": result["reason"]})
            _save_record(journal, record)
            print(f"{result['reason'].capitalize()}: no upload. Backup: {backup}")
            return 1
        if (result.get("status") != "changed" or result.get("original_version_id") != version
                or not isinstance(result.get("new_version_id"), str)
                or not result["new_version_id"] or result["new_version_id"] == version
                or not isinstance(result.get("sha256"), str)
                or result.get("before") != before):
            return _uncertain(journal, record, backup)
        with tempfile.TemporaryDirectory(prefix="raincheck-verify-") as verify_dir:
            uploaded = Path(verify_dir) / "uploaded.db"
            new_version, _ = _download(s3, bucket, key, uploaded, result["new_version_id"])
            checksum = hashlib.sha256(uploaded.read_bytes()).hexdigest()
            if checksum != result["sha256"]:
                raise CommandError("Uploaded version checksum mismatch")
            after = _verify_delta(backup, uploaded, record)
            if after != result.get("after"):
                raise CommandError("Lambda keyword receipt does not match uploaded version")
        record.update({"status": "verified", "new_version_id": new_version,
                       "sha256": checksum})
        _save_record(journal, record)
        print(f"Verified {args.command} {kind} {term.strip().lower()} for {args.item_id} ({name}).")
        print(f"Backup: {backup}; original version: {version}; new version: {new_version}")
        return 0
    except Exception:
        return _uncertain(journal, record, backup)


def _uncertain(journal, record, backup):
    record["status"] = "uncertain"
    _save_record(journal, record)
    print(f"Uncertain outcome. Backup: {backup}. Run: python3 src/live_keywords.py recover {journal}")
    return 1


def _recover(record_path, bucket, key, s3):
    record = json.loads(record_path.read_text(encoding="utf-8"))
    if record.get("bucket") != bucket or record.get("key") != key:
        raise CommandError("Record does not belong to this production database")
    backup = Path(record["backup_path"])
    _check_database(backup)
    if hashlib.sha256(backup.read_bytes()).hexdigest() != record.get("backup_sha256"):
        raise CommandError("Local backup checksum does not match operation record")
    matched = []
    for entry in _version_entries(s3, bucket, key):
        version = entry["VersionId"]
        head = s3.head_object(Bucket=bucket, Key=key, VersionId=version)
        if head.get("Metadata", {}).get("raincheck-operation-id") == record["operation_id"]:
            matched.append(version)
    if len(matched) == 1:
        with tempfile.TemporaryDirectory(prefix="raincheck-recover-") as directory:
            applied = Path(directory) / "applied.db"
            _download(s3, bucket, key, applied, matched[0])
            _verify_delta(backup, applied, record)
            latest = Path(directory) / "latest.db"
            try:
                _download(s3, bucket, key, latest)
                _check_database(latest)
                _, words = _keywords(latest, record["item_id"])
                latest_text = f"Latest keywords: {words}"
            except (CommandError, OSError, sqlite3.Error, ClientError):
                latest_text = "Latest keyword state could not be verified"
        record.update({"status": "verified", "new_version_id": matched[0]})
        _save_record(record_path, record)
        print(f"Applied in version {matched[0]}. {latest_text}")
        return 0
    if len(matched) > 1:
        raise CommandError("Multiple operation receipts; outcome remains uncertain")
    if record.get("status") == "not_applied" and record.get("no_upload_reason"):
        print(f"Not applied: {record['no_upload_reason']}")
        return 0
    print("Uncertain: no matching S3 receipt. Investigate before a fresh edit.")
    return 1


def main(argv=None):
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    try:
        session = boto3.Session(profile_name="raincheck", region_name=REGION)
        if session.region_name not in (None, REGION):
            raise CommandError("Unexpected AWS Region")
        with ExitStack() as resources:
            bucket, key, function, lambda_client, s3 = _preflight(
                session, resources, args.command in ("add", "remove"))
            if args.command == "recover":
                return _recover(args.record, bucket, key, s3)
            with tempfile.TemporaryDirectory(prefix="raincheck-live-") as directory:
                current = Path(directory) / "current.db"
                version, etag = _download(s3, bucket, key, current)
                _check_database(current)
                if args.command == "list":
                    _display(current, args.item_id)
                    return 0
                return _edit(args, bucket, key, function, lambda_client, s3,
                             current, version, etag)
    except (ClientError, BotoCoreError):
        print("AWS request failed. Check raincheck profile credentials and sign in again.",
              file=sys.stderr)
        return 1
    except (CommandError, OSError, sqlite3.Error, ValueError, KeyError) as exc:
        print(f"Live keyword command failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
