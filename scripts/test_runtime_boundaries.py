"""Exercise actual local-context and orchestration guards in isolated clones."""

import hashlib
import hmac
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import staged_runtime as runtime
import test_staged_contracts as fixtures
from cloud_contract import InfrastructureResult

ROOT = Path(__file__).resolve().parents[1]


class RuntimeBoundaries(unittest.TestCase):
    def root(self) -> Path:
        folder = tempfile.TemporaryDirectory(dir=ROOT / ".tools")
        self.addCleanup(folder.cleanup)
        return Path(folder.name)

    def context(self) -> tuple[SimpleNamespace, InfrastructureResult]:
        return fixtures.LocalDistributedResultTests().fixture(self.root())

    def test_local_ids_and_output_reject_each_stale_context_dimension(self) -> None:
        api, result = self.context()
        self.assertEqual(len(runtime.managed_ids(api)), 12)
        journal_path = api.LOCAL / "staged-deployment.json"
        original = json.loads(journal_path.read_text())
        for change in ("provider", "scope", "namespace", "status", "lineage", "identities", "disk"):
            value = json.loads(json.dumps(original))
            if change in ("provider", "namespace"):
                value[change] = "foreign"
            if change == "scope":
                value["scope"] = {"project_id": "foreign"}
            if change == "status":
                value["stages"]["es1"]["status"] = "pending"
            if change == "lineage":
                value["stages"]["es1"]["lineage"] = "foreign"
            if change == "identities":
                value["stages"]["es1"]["identities"] = {"foreign": "id"}
            if change == "disk":
                state = api.LOCAL / "stages/es1/terraform.tfstate"
                saved = json.loads(state.read_text())
                saved["resources"].pop()
                state.write_text(json.dumps(saved))
            journal_path.write_text(json.dumps(value))
            with self.subTest(change=change), self.assertRaises(ValueError):
                runtime.output(api)

    def test_open_failure_keeps_local_url_and_does_not_authenticate(self) -> None:
        api, _ = self.context()
        stream = io.StringIO()
        with (
            patch("sys.stdout", stream),
            patch("selectel_staged.load_credentials", side_effect=AssertionError("No auth")),
        ):
            self.assertEqual(runtime.open_portal(api, Mock(return_value=False)), "http://192.0.2.1")
        self.assertIn("Use the URL", stream.getvalue())

    def test_backend_cache_and_preflight_order(self) -> None:
        root = self.root()
        local = root / ".local"
        local.mkdir()
        provider = SimpleNamespace(
            policy={"operator_ip_services": ["synthetic"], "admin_cidrs": []},
            journal={"admin_cidrs": []},
            validate_existing=Mock(),
            preflight=Mock(),
        )
        api = SimpleNamespace(ROOT=root, LOCAL=local, _STAGED_BACKEND=provider)
        self.assertIs(runtime.backend(api), provider)
        api._STAGED_BACKEND = None
        with (
            patch("staged_runtime.backend_factory", return_value=provider) as factory,
            patch("operator_network.discover_cidrs", return_value=["8.8.8.8/32"]),
        ):
            self.assertIs(runtime.backend(api), provider)
            self.assertIs(runtime.preflight(api), provider)
        self.assertEqual(provider.journal["admin_cidrs"], ["8.8.8.8/32"])
        provider.validate_existing.assert_called_once()
        provider.preflight.assert_called_once()
        self.assertEqual(factory.call_count, 2)

    def test_prepare_orders_tool_and_identity_creation_after_checked_preflight(self) -> None:
        root = self.root()
        (root / ".local").mkdir()
        (root / "config").mkdir()
        services = json.loads((ROOT / "config/services.json").read_text())
        services["artifact_delivery"] = "cached"
        (root / "config/services.json").write_text(json.dumps(services))
        provider = Mock(policy={"operator_ip_services": ["synthetic"], "admin_cidrs": []}, journal={"admin_cidrs": []})
        api = SimpleNamespace(ROOT=root, LOCAL=root / ".local", _TEST_CYCLE_ID=None, prepare_identity=Mock())
        events = Mock()
        events.attach_mock(provider.preflight, "preflight")
        events.attach_mock(provider.prepare_tools, "tools")
        events.attach_mock(provider.save, "save")
        events.attach_mock(api.prepare_identity, "identity")
        with (
            patch("staged_runtime.backend_factory", return_value=provider),
            patch("operator_network.discover_cidrs", return_value=["8.8.8.8/32"]),
        ):
            runtime.prepare(api)
        self.assertEqual([call[0] for call in events.mock_calls], ["preflight", "tools", "save", "identity"])

    def test_cleanup_aggregates_errors_without_erasing_recovery_data(self) -> None:
        root = self.root()
        local = root / ".local"
        local.mkdir()
        key = local / "key"
        key.write_text("synthetic")
        provider = Mock(
            journal={
                "stages": {
                    "deleted": {"status": "deleted"},
                    "bad": {"status": "pending"},
                    "good": {"status": "pending"},
                }
            },
            desired={"nodes": {"deleted": {}, "bad": {}, "good": {}}},
        )
        provider.cleanup_order.return_value = ["deleted", "bad", "good"]
        provider.cleanup_attempt.side_effect = [RuntimeError("synthetic-sensitive-detail"), None]
        api = SimpleNamespace(ROOT=root, LOCAL=local, _TEST_CYCLE_ID=None, save=Mock())
        with (
            patch("staged_runtime.backend_factory", return_value=provider),
            self.assertRaisesRegex(RuntimeError, "bad"),
        ):
            runtime.cleanup(api)
        self.assertEqual([call.args[0] for call in provider.cleanup_attempt.call_args_list], ["bad", "good"])
        report = api.save.call_args.args[1]
        self.assertTrue(report["recovery_files_preserved"])
        self.assertEqual(report["stage_errors"], {"bad": "RuntimeError"})
        self.assertNotIn("synthetic-sensitive-detail", json.dumps(report))
        self.assertTrue(key.exists())

    def test_nonempty_state_blocks_audit_and_preserves_key(self) -> None:
        root = self.root()
        local = root / ".local"
        local.mkdir()
        (local / "key").write_text("synthetic")
        provider = Mock(
            journal={"stages": {}},
            desired={"nodes": {}},
            cleanup_order=Mock(return_value=[]),
            identities=Mock(return_value={"owned": "id"}),
        )
        api = SimpleNamespace(ROOT=root, LOCAL=local, _TEST_CYCLE_ID=None, save=Mock())
        with (
            patch("staged_runtime.backend_factory", return_value=provider),
            self.assertRaisesRegex(RuntimeError, "Managed resources"),
        ):
            runtime.cleanup(api)
        provider.audit_deletion.assert_not_called()
        self.assertTrue((local / "key").exists())

    def test_signed_route_retries_transport_but_never_accepts_bad_signature(self) -> None:
        root = self.root()
        local = root / ".local"
        local.mkdir()
        (local / "secrets.json").write_text('{"api":"synthetic"}')
        nonce = "a" * 48
        ip = "8.8.8.8"
        signature = hmac.new(b"synthetic", (nonce + "|" + ip).encode(), hashlib.sha256).hexdigest()
        body = {"nonce": nonce, "ip": ip, "signature": signature}
        provider = SimpleNamespace(journal={"admin_cidrs": ["1.1.1.1/32"]}, extend_admin_access=Mock())
        provider.extend_admin_access.side_effect = lambda lab, cidr: provider.journal["admin_cidrs"].append(cidr)
        api = SimpleNamespace(ROOT=root, LOCAL=local, _STAGED_BACKEND=provider, save=Mock())
        with (
            patch("secrets.token_hex", return_value=nonce),
            patch(
                "urllib.request.urlopen",
                side_effect=[OSError(), io.BytesIO(b"bad-json"), io.BytesIO(json.dumps(body).encode())],
            ),
            patch("staged_runtime.time.sleep") as sleep,
        ):
            runtime.discover_route(api, {"site_url": "http://192.0.2.1"})
        self.assertEqual(sleep.call_count, 2)
        provider.extend_admin_access.assert_called_once()
        self.assertEqual(api.save.call_args.args[1]["admin_cidrs"], ["1.1.1.1/32", "8.8.8.8/32"])
        provider.extend_admin_access.reset_mock()
        body["signature"] = "invalid"
        with (
            patch("secrets.token_hex", return_value=nonce),
            patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(body).encode())),
            self.assertRaises(ValueError),
        ):
            runtime.discover_route(api, {"site_url": "http://192.0.2.1"})
        provider.extend_admin_access.assert_not_called()
        with (
            patch("urllib.request.urlopen", side_effect=OSError()),
            patch("staged_runtime.time.sleep"),
            self.assertRaisesRegex(RuntimeError, "preserve the deployment"),
        ):
            runtime.discover_route(api, {"site_url": "http://192.0.2.1"})
        self.assertTrue((local / "secrets.json").exists())
