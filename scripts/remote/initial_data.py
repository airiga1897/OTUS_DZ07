"""Initialize local PostgreSQL with migrations and the guarded bundled fixture."""

import importlib
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

import psycopg
from psycopg import sql

if TYPE_CHECKING:
    from stack_types import StackSettings

if __package__:
    from . import runtime_config as _runtime_config
else:
    _runtime_config = importlib.import_module("runtime_config")

APP: Path = _runtime_config.APP
BASE: Path = _runtime_config.BASE
SHARED: Path = _runtime_config.SHARED
STACK: "StackSettings" = _runtime_config.STACK
LOGGER = logging.getLogger(__name__)


def wait_for_primary(password: str) -> psycopg.Connection:
    for _ in range(60):
        try:
            conn = psycopg.connect(
                host="127.0.0.1",
                port=STACK["postgresql"]["port"],
                user="postgres",
                password=password,
                dbname="postgres",
                connect_timeout=3,
                autocommit=True,
            )
        except psycopg.OperationalError:
            time.sleep(2)
            continue
        row = conn.execute("SELECT NOT pg_is_in_recovery()").fetchone()
        if not row or not row[0]:
            conn.close()
            raise RuntimeError("PostgreSQL must be writable")
        return conn
    raise RuntimeError("Local PostgreSQL connection unavailable")


def prepare_database(conn: psycopg.Connection, password: str) -> None:
    if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", ("otus",)).fetchone():
        conn.execute(sql.SQL("CREATE ROLE otus LOGIN PASSWORD {}").format(sql.Literal(password)))
    row = conn.execute("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=%s", ("otus",)).fetchone()
    if row and row[0] != "otus":
        raise RuntimeError("Existing portal database has a different owner")
    if not row:
        conn.execute("CREATE DATABASE otus OWNER otus")


def application_environment(passwords: dict[str, str]) -> dict[str, str]:
    return {
        **os.environ,
        "DJANGO_SECRET_KEY": passwords["django"],
        "DB_HOST": "127.0.0.1",
        "DB_PORT": str(STACK["postgresql"]["port"]),
        "DB_NAME": "otus",
        "DB_USER": "otus",
        "DB_PASSWORD": passwords["app"],
        "REQUIRE_SHARED_MOUNT": "0",
        "SHARED_ROOT": str(SHARED),
    }


def main() -> None:
    passwords = json.loads((BASE / "settings.json").read_text())["passwords"]
    with wait_for_primary(passwords["postgres"]) as conn:
        prepare_database(conn, passwords["app"])
    env = application_environment(passwords)
    manage = [str(APP / "venv/bin/python"), str(APP / "app/manage.py")]
    migration = subprocess.run([*manage, "migrate", "--noinput"], check=True, env=env, capture_output=True, text=True)
    output = subprocess.run([*manage, "load_initial_data"], check=True, env=env, capture_output=True, text=True)
    report = json.loads(output.stdout)
    (APP / "initial-data-result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    changed = "No migrations to apply." not in migration.stdout or report["loaded_now"]
    print("PORTAL_CHANGED=" + str(changed).lower())
    LOGGER.info("Portal initial data: %s", report["status"])


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
