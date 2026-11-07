"""Check committed application data, HTTP and media across repeated configuration."""

import hashlib
import importlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, TypedDict, cast

if TYPE_CHECKING:
    import psycopg
    from stack_types import StackSettings


type Row = tuple[object, ...]


class QueryResult(Protocol):
    def fetchone(self) -> Row | None: ...
    def fetchall(self) -> list[Row]: ...


class QueryConnection(Protocol):
    def execute(self, query: str, params: Sequence[object] | None = None) -> QueryResult: ...


class ProbeRecord(TypedDict):
    id: int
    slug: str
    body: str


def checked_record(value: object) -> ProbeRecord:
    if (
        not isinstance(value, dict)
        or type(value.get("id")) is not int
        or not isinstance(value.get("slug"), str)
        or not isinstance(value.get("body"), str)
    ):
        raise ValueError("Invalid portal verification record")
    return cast(ProbeRecord, value)


def one_row(conn: QueryConnection, query: str) -> Row:
    result = conn.execute(query).fetchone()
    if not result:
        raise AssertionError("Required database query returned no row")
    return result


if __package__:
    from . import runtime_config as _runtime_config
else:
    _runtime_config = importlib.import_module("runtime_config")

APP: Path = _runtime_config.APP
BASE: Path = _runtime_config.BASE
SHARED: Path = _runtime_config.SHARED
STACK: "StackSettings" = _runtime_config.STACK

PYTHON = str(APP / "venv/bin/python")
if __name__ == "__main__" and sys.prefix != str(APP / "venv"):
    os.execv(PYTHON, [PYTHON, __file__, *sys.argv[1:]])
lab: dict[str, object] = {}
passwords: dict[str, str] = {}


def initialize() -> None:
    global lab, passwords
    if sys.prefix != str(APP / "venv"):
        os.execv(PYTHON, [PYTHON, __file__, *sys.argv[1:]])
    settings = json.loads((BASE / "settings.json").read_text())
    lab, passwords = settings["lab"], settings["passwords"]


def check_database() -> None:
    with sql_connect() as conn:
        if one_row(conn, "SELECT pg_is_in_recovery()")[0]:
            raise AssertionError("Standalone PostgreSQL must be writable")


def sql_connect(host: str = "127.0.0.1") -> "psycopg.Connection[Row]":
    import psycopg

    return psycopg.connect(
        host=host,
        port=STACK["postgresql"]["port"],
        user="otus",
        password=passwords["app"],
        dbname="otus",
        connect_timeout=3,
        options="-c statement_timeout=10000",
    )


def check_record(conn: QueryConnection, record: ProbeRecord) -> None:
    row = conn.execute("SELECT id,slug,body FROM knowledge_article WHERE id=%s", (record["id"],)).fetchone()
    if row != (record["id"], record["slug"], record["body"]):
        raise AssertionError("Committed record is absent or changed")


def insert_record(mode: str) -> ProbeRecord:
    with sql_connect() as conn:
        if not one_row(conn, "SELECT NOT pg_is_in_recovery()")[0]:
            raise AssertionError("PostgreSQL is not writable")
        if (
            one_row(
                conn,
                "SELECT count(*) FROM knowledge_initialdatastate WHERE key='portal-initial-v1' "
                "AND status IN ('loaded','preserved_existing')",
            )[0]
            != 1
        ):
            raise AssertionError("Initial-data state missing")
        previous_max = one_row(conn, "SELECT COALESCE(MAX(id),0) FROM knowledge_article")[0]
        if type(previous_max) is not int or previous_max < 0:
            raise AssertionError("Database maximum primary key is unconfirmed")
    output = subprocess.check_output([PYTHON, str(BASE / "scripts/remote/portal_probe.py"), mode], text=True)
    record = checked_record(json.loads(output))
    if record["id"] <= previous_max:
        raise AssertionError("Article sequence did not advance beyond existing primary keys")
    # Read through a new connection after COMMIT.
    with sql_connect() as conn:
        check_record(conn, record)
    return record


def check_http(record: ProbeRecord) -> None:
    for path in ["/", "/health/ready"]:
        with urllib.request.urlopen("http://127.0.0.1" + path, timeout=5) as response:
            body = response.read()
            if response.status != 200 or not body:
                raise AssertionError("Portal unavailable")
            if path == "/" and record["slug"].encode() in body:
                raise AssertionError("Verification record appeared on the public portal")
    try:
        urllib.request.urlopen("http://127.0.0.1/articles/" + record["slug"] + "/", timeout=5).close()
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
    else:
        raise AssertionError("Verification record is publicly accessible")


def media_manifest(root: Path = SHARED / "media") -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file() and not p.is_symlink()
    }


def check_media(expected: Mapping[str, str], root: Path = SHARED / "media") -> None:
    actual = media_manifest(root)
    if any(actual.get(name) != digest for name, digest in expected.items()):
        raise AssertionError("Existing media was removed or changed")


def check_http_media(run_id: str, expected: Mapping[str, str]) -> None:
    name = "probes/" + run_id + ".txt"
    with urllib.request.urlopen("http://127.0.0.1/media/" + name, timeout=5) as response:
        if response.status != 200 or hashlib.sha256(response.read()).hexdigest() != expected[name]:
            raise AssertionError("Portal media content changed")


def manifest_row(table: str, row: Row) -> Row:
    # Migration 0003 only changes visibility of legacy verification rows.
    # Keep checking every other field, including their committed content.
    if (
        table == "knowledge_article"
        and len(row) == 9
        and row[1] == row[2]
        and row[3] == 6
        and row[4] == "proof"
        and row[6] == ""
        and isinstance(row[2], str)
        and isinstance(row[5], str)
        and re.fullmatch(r"(baseline|failover|recovered|expanded)-[0-9a-f]{32}", row[2])
        and re.fullmatch(r"committed-[0-9a-f]{32}", row[5])
    ):
        return (*row[:7], False, row[8])
    return row


def data_manifest(conn: QueryConnection) -> dict[str, dict[str, str]]:
    tables = ["knowledge_article", "auth_user", "auth_user_groups", "auth_user_user_permissions"]
    return {
        table: {
            str(row[0]): hashlib.sha256(json.dumps(manifest_row(table, row), default=str).encode()).hexdigest()
            for row in conn.execute("SELECT * FROM " + table + " ORDER BY id").fetchall()
        }
        for table in tables
    }


def check_data(expected: Mapping[str, Mapping[str, str]], conn: QueryConnection) -> None:
    actual = data_manifest(conn)
    if any(actual[table].get(pk) != digest for table, rows in expected.items() for pk, digest in rows.items()):
        raise AssertionError("Existing application data was removed or changed")


def main() -> None:
    initialize()
    import psycopg

    mode, run_id = sys.argv[1:3]
    if mode not in ["baseline", "expanded"] or not re.fullmatch("[0-9a-f]{32}", run_id):
        raise ValueError("Invalid verification context")
    folder = APP / "probes"
    folder.mkdir(exist_ok=True)
    context_path = folder / (run_id + ".json")
    context = json.loads(context_path.read_text()) if context_path.exists() else {}
    if mode == "baseline" and not context:
        media = SHARED / "media/probes"
        media.mkdir(exist_ok=True)
        (media / (run_id + ".txt")).write_text("media-" + uuid.uuid4().hex)
        context["media"] = media_manifest()
        with sql_connect() as conn:
            context["data"] = data_manifest(conn)
        context_path.write_text(json.dumps(context))
    required = [] if mode == "baseline" else [context["baseline"]]
    record = None
    last_error = ""
    for _attempt in range(150):
        try:
            check_database()
            with sql_connect() as conn:
                check_data(context["data"], conn)
                for preserved in required:
                    check_record(conn, preserved)
            check_media(context["media"])
            check_http_media(run_id, context["media"])
            if record is None:
                record = insert_record(mode)
            check_http(record)
            break
        except (AssertionError, OSError, psycopg.Error) as error:
            last_error = type(error).__name__ + ": " + str(error)
            time.sleep(2)
    else:
        raise RuntimeError("SQL/HTTP verification failed: " + last_error)
    context[mode] = record
    context_path.write_text(json.dumps(context))
    result = {
        "mode": mode,
        "run_id": run_id,
        "database_host": "app",
        "sql_and_http": True,
        "application_view_write": True,
        "media_preserved": True,
        "committed_record_preserved": True,
        "new_write_succeeded": True,
        "new_record": record,
        "preserved_records": required,
        "initial_data": json.loads((APP / "initial-data-result.json").read_text()),
    }
    print(json.dumps(result))


if __name__ == "__main__":
    main()
