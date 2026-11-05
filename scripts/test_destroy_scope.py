"""Deletion scope and write-ahead recovery, with isolated data and no cloud calls."""

import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import selectel_intent as intents
import selectel_scope_audit as scope
import selectel_staged as staged
from placement_config import PlacementPolicy
from stage_store import RecoveryContextMissing, require_context
from terraform_state_types import StateEntry

ROOT = Path(__file__).resolve().parents[1]


class DestroyScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory(dir=ROOT / ".tools")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.local = self.root / ".local"
        self.local.mkdir()
        self.api = SimpleNamespace(ROOT=self.root, LOCAL=self.local, PREFIX="otus-dz07", save=Mock())

    def context(self, value: object) -> None:
        (self.local / "context.json").write_text(json.dumps(value))

    def test_saved_scope_ignores_new_preferences_and_collects_node_attempt_pools(self) -> None:
        self.context(
            {
                "project_id": "p",
                "namespace": "otus-dz07",
                "region": "ru-2",
                "nodes": {"app": {"region": "ru-3"}},
                "placement": {"node_placements": {"es1": {"region": "ru-9"}}},
            }
        )
        self.assertEqual(scope.saved_scope(self.api, "p"), ["ru-2", "ru-3", "ru-9"])

    def test_missing_or_unbound_context_requires_expanded_search(self) -> None:
        self.assertIsNone(scope.saved_scope(self.api, "p"))
        self.context({"project_id": "p", "region": "ru-2"})
        self.assertIsNone(scope.saved_scope(self.api, "p"))
        self.context({"project_id": "p", "namespace": "otus-dz07"})
        self.assertIsNone(scope.saved_scope(self.api, "p"))

    def test_foreign_or_malformed_context_and_nonempty_state_are_refused(self) -> None:
        for value in (
            {"project_id": "foreign"},
            {"project_id": "p", "namespace": "foreign"},
            {"project_id": "p", "provider": "other"},
            {"project_id": "p", "namespace": "otus-dz07", "region": {}},
            {"project_id": "p", "namespace": "otus-dz07", "region": ""},
        ):
            self.context(value)
            with self.assertRaises(ValueError):
                scope.saved_scope(self.api, "p")
        (self.local / "context.json").unlink()
        state = self.root / "terraform-selectel/terraform.tfstate"
        state.parent.mkdir()
        state.write_text(json.dumps({"resources": [{"mode": "managed", "instances": [{"attributes": {"id": "own"}}]}]}))
        with self.assertRaisesRegex(ValueError, "Non-empty state"):
            scope.saved_scope(self.api, "p")

    def test_pool_audit_uses_saved_scope_or_all_resource_catalog_pools(self) -> None:
        cloud = Mock()
        cloud.resource_regions.return_value = ["ru-2", "ru-7", "ru-5"]
        with (
            patch("selectel_staged.load_credentials", return_value={"project_id": "p"}),
            patch("selectel_adapter.SelectelAdapter", return_value=cloud),
            patch("selectel_scope_audit.audit_pool", return_value={"excluded_services": []}) as audit,
            patch("selectel_staged.reject_shared_namespace") as shared,
        ):
            self.context({"project_id": "p", "namespace": "otus-dz07", "region": "ru-2"})
            result = staged.audit_empty_scope(self.api)
            self.assertEqual(result["pools_audited"], ["ru-2"])
            cloud.resource_regions.assert_not_called()
            shared.assert_called_once()
            (self.local / "context.json").unlink()
            audit.reset_mock()
            result = staged.audit_empty_scope(self.api)
            self.assertEqual(result["scope_source"], "expanded-service-catalog")
            self.assertEqual([c.args[2] for c in audit.call_args_list], ["ru-2", "ru-7", "ru-5"])
            self.assertTrue(result["audit_complete"])

    def test_stage_audit_includes_failed_attempt_but_not_global_bootstrap_pool(self) -> None:
        b = staged.Backend.__new__(staged.Backend)
        b.api, b.global_router = self.api, Mock()
        b.global_router.assert_absent = Mock()
        b.journal = {
            "deployment_id": "own",
            "stages": {
                "global": {"candidate": {"region": "ru-1"}, "status": "deleted"},
                "app": {"candidate": {"region": "ru-2"}, "status": "deleted"},
                "es1": {"candidate": {"region": "ru-9"}, "status": "pending"},
                "network-ru-2": {"candidate": {"region": "ru-2"}, "status": "deleted"},
            },
        }
        with (
            patch("selectel_staged_audit.assert_stage_absent") as absent,
            patch("selectel_staged_audit.audit_namespace") as audit,
        ):
            result = b.audit_deletion()
        self.assertEqual(result["pools_audited"], ["ru-2", "ru-9"])
        self.assertEqual(absent.call_count, 4)
        self.assertEqual([c.args[1] for c in audit.call_args_list], ["ru-2", "ru-9"])
        b.global_router.assert_absent.assert_called_once_with({}, "otus-dz07-own")

    def test_independent_reads_finish_and_preserve_first_error(self) -> None:
        events = []
        original = PermissionError("not authorized")

        def fail() -> None:
            events.append("denied")
            raise original

        def other() -> None:
            events.append("other")
            raise TimeoutError()

        with self.assertRaises(PermissionError) as raised:
            scope.read_independent([("ru-2", fail), ("ru-9", other), ("global", lambda: events.append("global"))], 2)
        self.assertIs(raised.exception, original)
        self.assertEqual(set(events), {"denied", "other", "global"})
        self.assertEqual(
            getattr(original, "audit_incomplete", None), {"ru-2": "PermissionError", "ru-9": "TimeoutError"}
        )
        self.assertEqual(scope.read_independent([], 1), {})
        self.assertEqual(scope.read_independent([("x", lambda: 7)], 1), {"x": 7})

    def test_missing_context_blocks_mutation_and_recovery_report_preserves_error(self) -> None:
        stages = self.local / "stages/attempt"
        stages.mkdir(parents=True)
        with self.assertRaises(RecoveryContextMissing):
            require_context(self.root)
        original = RecoveryContextMissing("binding missing")
        self.api.save.side_effect = OSError("cannot write")
        with patch("selectel_staged.audit_empty_scope", side_effect=TimeoutError()) as audit:
            staged.recovery_search(self.api, original)
        audit.assert_called_once_with(self.api, expanded=True)
        self.assertTrue(stages.exists())
        report = self.api.save.call_args.args[1]
        self.assertFalse(report["success"])
        self.assertFalse(report["audit_complete"])
        self.assertEqual(report["recovery_error_type"], "RecoveryContextMissing")

    def test_create_intent_is_saved_before_any_create_and_orphans_block_deletion(self) -> None:
        b = staged.Backend.__new__(staged.Backend)
        b.api, b.credentials = self.api, {"project_id": "p"}
        b.desired = {"nodes": {"app": {"host": 10, "role": "app"}, "es1": {"host": 31, "role": "es"}}}
        b.store, b.journal = Mock(), {"deployment_id": "owner", "stages": {}}
        candidate: PlacementPolicy = {"region": "ru-2"}
        with patch.object(b, "save") as save, patch.object(b.store, "identities", return_value={}):
            b.begin("app", candidate)
            save.assert_called_once()
            value = b.journal["stages"]["app"]["intent"]
            assert value is not None
            self.assertEqual(value["pool"], "ru-2")
            self.assertIn("openstack_networking_floatingip_v2", value["resource_types"])
            kind = "openstack_compute_instance_v2"
            found = {kind: {"unrecorded": {"name": "otus-dz07-app"}}}
            with self.assertRaisesRegex(RuntimeError, "missing from state"):
                intents.check_unrecorded(b, "app", [], found)
            entries: list[StateEntry] = [{"type": kind, "attrs": {"id": "unrecorded"}}]
            intents.check_unrecorded(b, "app", entries, found)
            intents.check_unrecorded(b, "app", [], {kind: {"foreign": {"name": "other-app"}}})
            fip = "openstack_networking_floatingip_v2"
            with self.assertRaisesRegex(RuntimeError, "missing from state"):
                intents.check_unrecorded(b, "app", [], {fip: {"ip": {"description": "otus-dz07-app:" + b.owner}}})
            value["pool"] = "ru-9"
            with self.assertRaises(ValueError):
                intents.validate(b, "app")
        for name in ("es1", "global", "network-ru-2", "connection-ru-2"):
            self.assertEqual(intents.intent(b, name, candidate)["pool"], "ru-2")
        with self.assertRaises(ValueError):
            intents.intent(b, "unknown", candidate)
        b.journal["stages"]["legacy"] = {"candidate": candidate}
        intents.check_unrecorded(b, "legacy", [], {})

    def test_worker_limit_is_explicit_and_validated(self) -> None:
        self.assertEqual(scope.workers(self.root), 1)
        (self.root / "config").mkdir()
        for value in (0, 9, True, "3"):
            (self.root / "config/cloud.json").write_text(json.dumps({"audit": {"read_workers": value}}))
            with self.assertRaises(ValueError):
                scope.workers(self.root)
        (self.root / "config/cloud.json").write_text('{"audit":{"read_workers":3}}')
        self.assertEqual(scope.workers(self.root), 3)

    def test_parallel_reads_never_exceed_configured_limit(self) -> None:
        gate, lock = threading.Event(), threading.Lock()
        counts = {"active": 0, "maximum": 0}

        def read() -> str:
            with lock:
                counts["active"] += 1
                counts["maximum"] = max(counts["maximum"], counts["active"])
                if counts["active"] == 2:
                    gate.set()
            try:
                if not gate.wait(2):
                    raise TimeoutError("second reader did not start")
                return "read"
            finally:
                with lock:
                    counts["active"] -= 1

        result = scope.read_independent([(str(i), read) for i in range(6)], 2)
        self.assertEqual(len(result), 6)
        self.assertEqual(counts["maximum"], 2)

    def test_expanded_scope_includes_network_only_and_volume_only_regions(self) -> None:
        from selectel_adapter import SelectelAdapter

        cloud = SelectelAdapter.__new__(SelectelAdapter)
        cloud.catalog = [
            {
                "type": service,
                "endpoints": [{"interface": "public", "region": region, "url": "https://example.invalid"}],
            }
            for service, region in (("network", "ru-7"), ("volumev3", "ru-5"), ("image", "ru-6"), ("compute", "ru-2"))
        ]
        self.assertEqual(cloud.resource_regions(), ["ru-2", "ru-5", "ru-7"])

    def test_missing_catalog_scope_is_unknown_not_success(self) -> None:
        cloud = Mock()
        cloud.resource_regions.return_value = []
        with (
            patch("selectel_staged.load_credentials", return_value={"project_id": "p"}),
            patch("selectel_adapter.SelectelAdapter", return_value=cloud),
            self.assertRaisesRegex(ValueError, "scope is unknown"),
        ):
            staged.audit_empty_scope(self.api)

    def test_partial_service_catalog_cannot_prove_all_resource_types_absent(self) -> None:
        regional = Mock(endpoints={"network": "https://example.invalid/network"})
        with (
            patch("selectel_staged_audit.objects", return_value=[]),
            self.assertRaisesRegex(ValueError, "Incomplete resource audit"),
        ):
            scope.audit_pool(self.api, regional, "ru-5", Mock())
