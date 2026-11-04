"""Exercise standalone SQL retries and committed-data preservation."""

import contextlib
import importlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg

ROOT = Path(__file__).resolve().parents[1]


class PortalRetryTests(unittest.TestCase):
    def run_verifier(self, mode, transient=False):
        module = importlib.import_module("remote.verify")
        run_id = "a" * 32
        old = {"id": 1, "slug": "baseline", "body": "baseline"}
        new = {"id": 3, "slug": mode, "body": "new"}
        with tempfile.TemporaryDirectory(dir=ROOT / ".tools") as directory:
            app = Path(directory)
            (app / "probes").mkdir()
            context = {"baseline": old, "media": {}, "data": {}}
            (app / "probes" / (run_id + ".json")).write_text(json.dumps(context))
            (app / "initial-data-result.json").write_text("{}")
            connection = MagicMock()
            connection.__enter__.return_value.execute.return_value.fetchone.return_value = (False,)
            calls = []

            def connect(*args):
                calls.append(args)
                if transient and len(calls) == 1:
                    raise psycopg.errors.ConnectionTimeout("synthetic transient outage")
                return connection

            output = io.StringIO()
            with (
                patch.object(module, "APP", app),
                patch.object(module, "initialize"),
                patch.object(module, "lab", {"private_ips": dict.fromkeys(["app", "es1", "es2", "es3"], "127.0.0.1")}),
                patch.object(module.sys, "argv", ["verify.py", mode, run_id]),
                patch.object(module, "sql_connect", side_effect=connect),
                patch.object(module, "check_database"),
                patch.object(module, "check_data"),
                patch.object(module, "check_record") as read_back,
                patch.object(module, "check_media"),
                patch.object(module, "check_http_media"),
                patch.object(module, "check_http"),
                patch.object(module, "insert_record", return_value=new) as insert,
                patch.object(module.time, "sleep") as sleep,
                contextlib.redirect_stdout(output),
            ):
                module.main()
                insert.assert_called_once_with(mode)
                self.assertIn(old, [call.args[1] for call in read_back.call_args_list])
                self.assertEqual(sleep.call_count, int(transient))
            return json.loads(output.getvalue())

    def test_transient_sql_connection_is_retried_without_losing_the_canary(self) -> None:
        result = self.run_verifier("expanded", transient=True)
        self.assertEqual(result["database_host"], "app")
        self.assertTrue(result["new_write_succeeded"])

    def test_repeat_check_preserves_the_committed_baseline(self) -> None:
        result = self.run_verifier("expanded")
        self.assertEqual(len(result["preserved_records"]), 1)
        self.assertTrue(result["committed_record_preserved"])
