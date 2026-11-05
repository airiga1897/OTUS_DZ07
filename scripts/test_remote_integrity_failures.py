"""Remote acceptance rejects lost documents, unexpected members and unavailable Kibana."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from remote import elk_api as elk
from remote import initial_data

ROOT = Path(__file__).resolve().parents[1]


class RemoteIntegrityFailures(unittest.TestCase):
    def test_preservation_rejects_extra_members_and_failed_post_configuration_write(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tools") as directory:
            root = Path(directory)
            (root / "scaling-checkpoint.json").write_text(
                '{"cluster_uuid":"same","nodes":{"node-id":"es1"},"nonce":"canary"}'
            )
            for extra in (True, False):

                def api(path: str, *args: object, extra: bool = extra) -> object:
                    if path == "/_nodes":
                        return {
                            "nodes": {"node-id": {"name": "es1"}, **({"unexpected": {"name": "es2"}} if extra else {})}
                        }
                    if path == "/":
                        return {"cluster_uuid": "same"}
                    if path.endswith("/probe"):
                        return {"found": True, "_source": {"nonce": "canary"}}
                    return {"_source": {"nonce": "wrong"}}

                with (
                    self.subTest(extra=extra),
                    patch.object(elk, "BASE", root),
                    patch.object(elk, "C", {"ips": {"es1": "10.77.0.31"}}),
                    patch.object(elk, "api", side_effect=api),
                    self.assertRaisesRegex(RuntimeError, "membership" if extra else "Write/read"),
                ):
                    elk.preservation()

    def test_optional_failure_probe_rejects_lost_before_or_after_document(self) -> None:
        for before_found in (False, True):

            def api(path: str, *args: object, before_found: bool = before_found) -> object:
                if path.endswith("/before"):
                    return {"found": before_found, "_source": {"message": "OTUS_DZ07 committed before master shutdown"}}
                return {"found": False}

            with (
                patch.object(elk, "C", {"ips": {"es1": "10.77.0.31", "es2": "10.77.0.32", "es3": "10.77.0.33"}}),
                patch.object(elk, "wait_health", return_value={"status": "yellow"}),
                patch.object(elk, "api", side_effect=api),
                self.assertRaisesRegex(RuntimeError, "lost"),
            ):
                elk.failure()

    def test_kibana_retry_exhaustion_never_creates_view_or_claims_ready(self) -> None:
        with (
            patch.object(elk, "C", {"ip": "10.77.0.10", "kibana_port": 5601}),
            patch.object(elk, "S", {"elastic": "synthetic"}),
            patch("urllib.request.urlopen", side_effect=OSError("synthetic unavailable")) as transport,
            patch("time.sleep") as sleep,
            self.assertRaisesRegex(RuntimeError, "Kibana not ready"),
        ):
            elk.kibana()
        self.assertEqual(transport.call_count, 120)
        self.assertEqual(sleep.call_count, 120)
        self.assertTrue(all(call.args[0].full_url.endswith("/api/status") for call in transport.call_args_list))

    def test_empty_database_creates_role_and_database_but_repeat_does_not_reset_them(self) -> None:
        connection = Mock()
        connection.execute.return_value.fetchone.side_effect = [None, None]
        initial_data.prepare_database(connection, "synthetic")
        commands = [str(call.args[0]) for call in connection.execute.call_args_list]
        self.assertTrue(any("CREATE ROLE" in command for command in commands))
        self.assertIn("CREATE DATABASE otus OWNER otus", commands)
        connection.reset_mock()
        connection.execute.return_value.fetchone.side_effect = [(1,), ("otus",)]
        initial_data.prepare_database(connection, "synthetic")
        self.assertEqual(connection.execute.call_count, 2)
        self.assertTrue(all(str(call.args[0]).startswith("SELECT") for call in connection.execute.call_args_list))
