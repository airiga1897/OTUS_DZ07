"""Execute the remote worker itself with a deterministic SQL/HTTP transport."""

import contextlib
import http.client
import io
import json
import runpy
import sys
import tempfile
import types
import unittest
import urllib.error
from collections.abc import Sequence
from pathlib import Path
from typing import Literal
from unittest.mock import MagicMock, Mock, patch

import psycopg
from remote import initial_data
from remote import verify as worker
from stack_types import StackSettings

ROOT = Path(__file__).resolve().parents[1]


class Result:
    def __init__(self, row: worker.Row | None = None, rows: list[worker.Row] | None = None) -> None:
        self.row = row
        self.rows = rows or []

    def fetchone(self) -> worker.Row | None:
        return self.row

    def fetchall(self) -> list[worker.Row]:
        return self.rows


class Database:
    def __init__(self) -> None:
        self.rows: list[worker.Row] = [(1, "lesson", "lesson", 7, "description", "initial-body", "", True, "timestamp")]
        self.record: worker.ProbeRecord | None = None
        self.queries: list[tuple[str, Sequence[object] | None]] = []

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *args: object) -> Literal[False]:
        return False

    def current_max(self) -> int:
        identifiers: list[int] = []
        for row in self.rows:
            identifier = row[0]
            if not isinstance(identifier, int):
                raise AssertionError("Expected integer primary key")
            identifiers.append(identifier)
        return max(identifiers)

    def execute(self, query: str, params: Sequence[object] | None = None) -> Result:
        self.queries.append((query, params))
        if query == "SELECT pg_is_in_recovery()":
            return Result((False,))
        if query == "SELECT NOT pg_is_in_recovery()":
            return Result((True,))
        if "knowledge_initialdatastate" in query:
            return Result((1,))
        if query.startswith("SELECT COALESCE(MAX"):
            return Result((self.current_max(),))
        if query.startswith("SELECT id,slug,body"):
            if self.record is None:
                raise AssertionError("Expected a committed record")
            return Result((self.record["id"], self.record["slug"], self.record["body"]))
        if query.startswith("SELECT * FROM knowledge_article"):
            return Result(rows=list(self.rows))
        if query.startswith("SELECT * FROM auth_"):
            return Result(rows=[])
        raise AssertionError("Unexpected SQL: " + query)

    def application_write(self, args: Sequence[str], **kwargs: object) -> str:
        self.record = worker.checked_record(
            {
                "id": self.current_max() + 1,
                "slug": "baseline-" + "a" * 32,
                "body": "committed-" + "b" * 32,
            }
        )
        self.rows.append(
            (
                self.record["id"],
                self.record["slug"],
                self.record["slug"],
                6,
                "proof",
                self.record["body"],
                "",
                False,
                "timestamp",
            )
        )
        return json.dumps(self.record)


class Response(io.BytesIO):
    status = 200


class RuntimeConfig(types.ModuleType):
    BASE: Path
    APP: Path
    SHARED: Path
    STACK: StackSettings


class RemoteWorkerContracts(unittest.TestCase):
    def folder(self) -> Path:
        directory = tempfile.TemporaryDirectory(dir=ROOT / ".tools")
        self.addCleanup(directory.cleanup)
        return Path(directory.name)

    def test_script_baseline_retry_commits_once_and_preserves_media_and_initial_rows(self) -> None:
        root = self.folder()
        app = root / "portal"
        app.mkdir()
        shared = root / "shared"
        (shared / "media").mkdir(parents=True)
        settings = {"lab": {"private_ips": {"app": "10.77.0.10"}}, "passwords": {"app": "synthetic"}}
        (root / "settings.json").write_text(json.dumps(settings))
        (app / "initial-data-result.json").write_text('{"status":"loaded"}')
        config = RuntimeConfig("runtime_config")
        config.BASE = root
        config.APP = app
        config.SHARED = shared
        config.STACK = worker.STACK
        database = Database()
        requests: list[str] = []
        attempt = {"root": 0}
        run_id = "a" * 32

        def transport(url: str, **kwargs: object) -> Response:
            requests.append(url)
            if "/articles/" in url:
                raise urllib.error.HTTPError(url, 404, "Not found", http.client.HTTPMessage(), None)
            if "/media/" in url:
                return Response((shared / "media/probes" / f"{run_id}.txt").read_bytes())
            if url == "http://127.0.0.1/":
                attempt["root"] += 1
                if attempt["root"] == 1:
                    raise OSError("transient synthetic HTTP outage")
            return Response(b"public lessons")

        output = io.StringIO()
        with (
            patch.dict(sys.modules, {"runtime_config": config}),
            patch.object(sys, "prefix", str(app / "venv")),
            patch.object(sys, "argv", ["verify.py", "baseline", run_id]),
            patch("psycopg.connect", return_value=database),
            patch("urllib.request.urlopen", side_effect=transport),
            patch("subprocess.check_output", side_effect=database.application_write) as write,
            patch("time.sleep"),
            contextlib.redirect_stdout(output),
        ):
            runpy.run_path(str(Path(worker.__file__)), run_name="__main__")
        result = json.loads(output.getvalue())
        self.assertTrue(result["sql_and_http"])
        self.assertTrue(result["media_preserved"])
        self.assertEqual(result["database_host"], "app")
        self.assertEqual(result["new_record"]["id"], 2)
        self.assertEqual(write.call_count, 1)
        self.assertEqual(len(database.rows), 2)
        self.assertEqual(database.rows[0][5], "initial-body")
        saved = json.loads((app / "probes" / f"{run_id}.json").read_text())
        self.assertEqual(saved["baseline"], database.record)
        self.assertIn("probes/" + run_id + ".txt", saved["media"])
        self.assertGreaterEqual(attempt["root"], 2)

    def test_worker_redirects_to_its_venv_before_reading_credentials(self) -> None:
        class RestartError(Exception):
            pass

        root = self.folder()
        config = RuntimeConfig("runtime_config")
        config.BASE = root
        config.APP = root / "portal"
        config.SHARED = root / "shared"
        config.STACK = worker.STACK
        with (
            patch.dict(sys.modules, {"runtime_config": config}),
            patch.object(sys, "prefix", "wrong-prefix"),
            patch("os.execv", side_effect=RestartError) as restart,
            self.assertRaises(RestartError),
        ):
            runpy.run_path(worker.__file__, run_name="__main__")
        self.assertEqual(restart.call_args.args[0], str(config.APP / "venv/bin/python"))
        self.assertFalse((root / "settings.json").exists())

    def test_primary_connection_retries_and_refuses_readonly_server(self) -> None:
        connection = Mock()
        connection.execute.return_value.fetchone.return_value = (True,)
        with (
            patch(
                "psycopg.connect",
                side_effect=[psycopg.OperationalError("synthetic"), connection],
            ),
            patch("remote.initial_data.time.sleep"),
        ):
            self.assertIs(initial_data.wait_for_primary("synthetic"), connection)
        connection.execute.return_value.fetchone.return_value = (False,)
        with (
            patch("psycopg.connect", return_value=connection),
            self.assertRaisesRegex(RuntimeError, "writable"),
        ):
            initial_data.wait_for_primary("synthetic")
        connection.close.assert_called_once()
        with (
            patch("psycopg.connect", side_effect=psycopg.OperationalError("synthetic")),
            patch("remote.initial_data.time.sleep"),
            self.assertRaisesRegex(RuntimeError, "unavailable"),
        ):
            initial_data.wait_for_primary("synthetic")

    def test_standalone_database_must_be_writable(self) -> None:
        connection = MagicMock()
        connection.__enter__.return_value.execute.return_value.fetchone.return_value = (True,)
        with (
            patch.object(worker, "lab", {"private_ips": {"app": "127.0.0.1"}}),
            patch.object(worker, "sql_connect", return_value=connection),
            self.assertRaisesRegex(AssertionError, "writable"),
        ):
            worker.check_database()

    def test_insert_requires_import_marker_writable_database_and_advanced_pk(self) -> None:
        for replies, message in [
            ([(False,)], "writable"),
            ([(True,), (0,)], "Initial-data"),
            ([(True,), (1,), (4,)], "sequence"),
        ]:
            connection = MagicMock()
            connection.__enter__.return_value.execute.return_value.fetchone.side_effect = replies
            with (
                patch.object(worker, "sql_connect", return_value=connection),
                patch(
                    "subprocess.check_output", return_value='{"id":4,"slug":"synthetic","body":"committed"}'
                ) as write,
            ):
                with self.subTest(message=message), self.assertRaisesRegex(AssertionError, message):
                    worker.insert_record("baseline")
                if message != "sequence":
                    write.assert_not_called()

    def test_http_and_media_detect_changed_or_public_control_data(self) -> None:
        import urllib.error

        record: worker.ProbeRecord = {"id": 1, "slug": "private-control", "body": "committed"}
        for replies in (
            [Response(b"")],
            [Response(b"private-control")],
            [
                Response(b"lessons"),
                Response(b"ready"),
                urllib.error.HTTPError("http://synthetic", 500, "broken", http.client.HTTPMessage(), None),
            ],
        ):
            with (
                patch("urllib.request.urlopen", side_effect=replies),
                self.assertRaises((AssertionError, urllib.error.HTTPError)),
            ):
                worker.check_http(record)
        root = self.folder()
        (root / "one").write_bytes(b"original")
        expected = worker.media_manifest(root)
        worker.check_media(expected, root)
        (root / "one").write_bytes(b"changed")
        with self.assertRaisesRegex(AssertionError, "media"):
            worker.check_media(expected, root)
        with (
            patch("urllib.request.urlopen", return_value=Response(b"changed")),
            self.assertRaisesRegex(AssertionError, "media"),
        ):
            worker.check_http_media("run", {"probes/run.txt": "original"})

    def test_invalid_verifier_context_fails_before_any_sql_or_filesystem_write(self) -> None:
        for args in (["verify.py", "destroy", "a" * 32], ["verify.py", "baseline", "bad"]):
            with (
                patch.object(worker, "initialize"),
                patch.object(sys, "argv", args),
                patch.object(worker, "sql_connect") as sql,
                self.assertRaises(ValueError),
            ):
                worker.main()
            sql.assert_not_called()
