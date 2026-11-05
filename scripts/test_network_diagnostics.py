"""Read-only diagnostics tested with synthetic processes and console API only."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cloud_contract import StandDescription
from network_diagnostics import BootstrapNetworkError, console_evidence, require_bootstrap_network
from remote.network_diagnostics import COMMANDS, collect
from test_runtime_support import lab

ROOT = Path(__file__).resolve().parents[1]


class NetworkDiagnosticsTests(unittest.TestCase):
    def test_fallback_requires_current_positive_evidence_of_bootstrap_failure(self) -> None:
        stand: StandDescription = {}
        for node, failed in [
            ({"guest_evidence_found": True, "expected_private_ipv4_present": False}, True),
            ({"guest_evidence_found": True, "expected_private_ipv4_present": True}, False),
            ({"guest_evidence_found": True, "expected_private_ipv4_present": None}, False),
            ({"guest_evidence_found": False, "error_type": "TimeoutError"}, False),
        ]:
            with patch("network_diagnostics.console_evidence", return_value={"nodes": {"app": node}}) as collect:
                if failed:
                    with self.assertRaises(BootstrapNetworkError):
                        require_bootstrap_network(lab, stand)
                else:
                    require_bootstrap_network(lab, stand)
                collect.assert_called_once_with(lab, stand)

    def test_commands_are_bounded_read_only_and_never_read_private_configuration(self) -> None:
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, "synthetic"))
        with tempfile.TemporaryDirectory(dir=ROOT / ".tools") as d:
            path = Path(d)
            (path / "50-cloud-init.yaml").write_text("private-synthetic-password")
            report = collect(runner, path)
        self.assertEqual(report["netplan_files"], ["50-cloud-init.yaml"])
        self.assertNotIn("private-synthetic-password", json.dumps(report))
        self.assertEqual(runner.call_count, len(COMMANDS))
        self.assertTrue(all(c.kwargs["timeout"] == 10 for c in runner.call_args_list))
        self.assertNotIn("apply", str(COMMANDS))
        self.assertNotIn("restart", str(COMMANDS))

    def test_missing_tools_and_timeouts_preserve_remaining_diagnostics(self) -> None:
        runner = Mock(side_effect=FileNotFoundError("synthetic-sensitive-error"))
        result = collect(runner)
        self.assertEqual(len(result["checks"]), len(COMMANDS))
        self.assertNotIn("synthetic-sensitive-error", json.dumps(result))
        runner.side_effect = subprocess.TimeoutExpired(["ip"], 10)
        self.assertEqual(collect(runner)["checks"]["addresses"]["error_type"], "TimeoutExpired")

    def test_console_saves_only_structured_network_records_and_not_raw_console(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tools") as d:
            root = Path(d)
            cloud = Mock()
            cloud.get.return_value = {
                "output": 'private-unrelated-console\nDZ07_NETWORK_DIAGNOSTICS={"read_only": true}\n'
            }
            (root / "docs").mkdir()
            api = SimpleNamespace(
                ROOT=root, RUN=root, cloud=lambda: cloud, save=lambda p, v: p.write_text(json.dumps(v))
            )
            console_evidence(api, {"server_ids": {"app": "synthetic-id"}})
            text = (root / "network-console-app.json").read_text()
            self.assertTrue(json.loads(text)["guest_evidence_found"])
            self.assertNotIn("private-unrelated-console", text)
            self.assertNotIn("synthetic-id", (root / "docs/network-diagnostics-result.json").read_text())
            cloud.get.side_effect = RuntimeError("private-api-error")
            console_evidence(api, {"server_ids": {"app": "synthetic-id"}})
            self.assertNotIn("private-api-error", (root / "network-console-app.json").read_text())
