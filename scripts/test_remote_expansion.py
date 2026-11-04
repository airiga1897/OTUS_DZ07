"""Remote verifier is importable offline; its IO is replaced by local mocks."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def verifier():
    import importlib

    return importlib.import_module("remote.verify")


class RemoteExpansionTests(unittest.TestCase):
    def test_http_verification_requires_probe_to_be_private(self) -> None:
        module = verifier()
        import urllib.error

        record = {"slug": "baseline-synthetic", "body": "committed-content"}
        response = MagicMock()
        response.__enter__.return_value.status = 200
        response.__enter__.return_value.read.return_value = b"course materials"
        missing = urllib.error.HTTPError("http://127.0.0.1/articles/baseline-synthetic/", 404, "Not Found", {}, None)
        with patch.object(module.urllib.request, "urlopen", side_effect=[response, response, missing]):
            module.check_http(record)
        with (
            patch.object(module.urllib.request, "urlopen", return_value=response),
            self.assertRaisesRegex(AssertionError, "publicly accessible"),
        ):
            module.check_http(record)

    def test_legacy_visibility_migration_preserves_manifest_but_content_changes_fail(self) -> None:
        module = verifier()
        conn = Mock()
        slug = "baseline-" + "a" * 32
        row = (7, slug, slug, 6, "proof", "committed-" + "b" * 32, "", True, "timestamp")
        conn.execute.return_value.fetchall.side_effect = [[row], [], [], []]
        expected = module.data_manifest(conn)
        hidden = (*row[:7], False, row[8])
        conn.execute.return_value.fetchall.side_effect = [[hidden], [], [], []]
        module.check_data(expected, conn)
        edited = (*hidden[:5], "committed-" + "c" * 32, *hidden[6:])
        conn.execute.return_value.fetchall.side_effect = [[edited], [], [], []]
        with self.assertRaises(AssertionError):
            module.check_data(expected, conn)

    def test_expansion_writes_a_distinct_record_after_reading_the_baseline(self) -> None:
        module = verifier()
        run_id = "a" * 32
        old = {"id": 1, "slug": "baseline", "body": "old"}
        new = {"id": 2, "slug": "expanded", "body": "new"}
        with tempfile.TemporaryDirectory(dir=ROOT / ".tools") as directory:
            app = Path(directory)
            (app / "probes").mkdir()
            (app / "probes" / (run_id + ".json")).write_text(json.dumps({"baseline": old, "media": {}, "data": {}}))
            (app / "initial-data-result.json").write_text("{}")
            connection = MagicMock()
            connection.__enter__.return_value.execute.return_value.fetchone.return_value = (10,)
            output = io.StringIO()
            with (
                patch.object(module, "APP", app),
                patch.object(module, "initialize"),
                patch.object(
                    module,
                    "lab",
                    {"private_ips": dict.fromkeys(["app", "es1", "es2", "es3"], "127.0.0.1")},
                ),
                patch.object(module.sys, "argv", ["verify.py", "expanded", run_id]),
                patch.object(module, "sql_connect", return_value=connection),
                patch.object(module, "check_database"),
                patch.object(module, "check_data"),
                patch.object(module, "check_record"),
                patch.object(module, "check_media"),
                patch.object(module, "check_http_media"),
                patch.object(module, "check_http"),
                patch.object(module, "insert_record", return_value=new) as insert,
                contextlib.redirect_stdout(output),
            ):
                module.main()
            insert.assert_called_once_with("expanded")
            self.assertEqual(json.loads(output.getvalue())["new_record"], new)

    def test_media_manifest_detects_changes_and_allows_new_files(self) -> None:
        module = verifier()
        with tempfile.TemporaryDirectory(dir=ROOT / ".tools") as directory:
            root = Path(directory)
            (root / "existing.png").write_bytes(b"original")
            expected = module.media_manifest(root)
            (root / "new.png").write_bytes(b"new")
            module.check_media(expected, root)
            (root / "existing.png").write_bytes(b"changed")
            with self.assertRaises(AssertionError):
                module.check_media(expected, root)

    def test_data_manifest_detects_edits_and_allows_new_rows(self) -> None:
        module = verifier()
        conn = Mock()
        conn.execute.return_value.fetchall.return_value = [(1, "original")]
        expected = module.data_manifest(conn)
        conn.execute.return_value.fetchall.return_value = [(1, "original"), (2, "new")]
        module.check_data(expected, conn)
        conn.execute.return_value.fetchall.return_value = [(1, "edited")]
        with self.assertRaises(AssertionError):
            module.check_data(expected, conn)
