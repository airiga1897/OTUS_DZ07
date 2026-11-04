"""Real alternative lifecycle paths; only external cloud/SSH IO is mocked."""

import hashlib
import hmac
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import local_runtime
import pool_selection
import selectel_scope_audit as scope
import selectel_staged_audit as audit
import staged_runtime
import test_access_recovery as access_fixtures
import test_controller_boundaries as controller_fixtures
import test_empty_scope_audit as empty_fixtures
import test_provider_audit as audit_fixtures
import test_runtime_contract as runtime_fixtures
import test_selectel_retained_contracts as retained_fixtures
import test_staged_lifecycle as lifecycle_fixtures
from cloud_contract import TerraformError

ROOT = Path(__file__).resolve().parents[1]


class RemainingOperationalBranches(unittest.TestCase):
    def root(self) -> Path:
        folder = tempfile.TemporaryDirectory(dir=ROOT / ".tools")
        self.addCleanup(folder.cleanup)
        return Path(folder.name)

    def backend(self):
        helper = retained_fixtures.RetainedSelectelContracts()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        return helper.backend

    def test_deleted_stages_are_not_revalidated_as_live_resources(self) -> None:
        b = self.backend()
        b.journal["stages"]["es1"] = {"status": "deleted", "candidate": {"region": "ru-9"}}
        with patch.object(audit, "assert_stage_owned") as owned:
            b.validate_existing()
        owned.assert_not_called()

    def test_finish_and_reapply_network_do_not_reconnect_a_vm(self) -> None:
        b = self.backend()
        name = "network-ru-9"
        b.journal["stages"][name] = {
            "status": "active",
            "candidate": {"region": "ru-9"},
            "kind": "network",
            "inputs": {},
        }
        b.store = Mock()
        b.store.identities.return_value = {"net": "own-network"}
        b.store.state.return_value = {"lineage": "own-lineage"}
        b.store.apply.return_value = {"network_id": "own-network"}
        b.configure_stage, b.connect_pool = Mock(), Mock()
        b.finish(name)
        with patch.object(audit, "assert_stage_owned"):
            b.reapply(name)
        b.connect_pool.assert_not_called()
        self.assertTrue(b.store.apply.call_args.kwargs["unchanged"])

    def test_existing_vm_input_does_not_reserve_another_vm_quota(self) -> None:
        b = self.backend()
        candidate = {
            "region": "ru-9",
            "availability_zone": "ru-9a",
            "image_id": "image",
            "flavor_ids": {"es": "flavor"},
            "volume_type": "universal.ru-9a",
        }
        b.journal["stages"]["es1"] = {"candidate": candidate}
        network = {
            "cidr": "10.77.0.0/24",
            "gateway_ip": "10.77.0.1",
            **{
                k: k
                for k in ("network_id", "subnet_id", "security_group_id", "server_group_id", "external_network_name")
            },
        }
        (b.root / ".local/secrets.json").write_text('{"api":"synthetic-probe-key"}')
        b.ensure_pool = Mock(return_value=network)
        b.saved_id = Mock(return_value="saved-vm")
        b.check_node_quota = Mock()
        kind, inputs = b.inputs("es1")
        self.assertEqual(kind, "node")
        self.assertEqual(inputs["image_id"], "image")
        b.check_node_quota.assert_not_called()

    def test_equivalent_data_and_noop_access_plans_are_accepted(self) -> None:
        b = access_fixtures.AdministrativeAccessRecoveryTests().backend()

        def apply(name, validate_extra) -> None:
            validate_extra({"resource_changes": []})
            validate_extra(
                {
                    "resource_changes": [
                        {"mode": "data", "change": {"actions": ["read"]}},
                        {"mode": "managed", "change": {"actions": ["no-op"]}},
                    ]
                }
            )

        b.store.apply.side_effect = apply
        b.extend_admin_access({"node_regions": {"app": "ru-3"}}, "1.1.1.1/32")
        self.assertNotIn("admin_access_pending", b.journal)

    def test_fault_without_a_private_log_still_saves_available_diagnostics(self) -> None:
        b = self.backend()
        with patch.object(audit, "faults", return_value={"read_only": True}):
            b.capture_fault("network-ru-9")
        reports = list((b.root / "docs/placement-faults").glob("*.json"))
        self.assertEqual(len(reports), 1)
        self.assertNotIn("terraform_error", json.loads(reports[0].read_text()))

    def test_network_terraform_error_is_classified_without_vm_fault_requests(self) -> None:
        b = self.backend()
        with patch.object(audit, "faults") as faults:
            self.assertFalse(b.is_capacity_error(TerraformError("configuration invalid"), "network-ru-9"))
        faults.assert_not_called()

    def test_already_deleted_temporary_network_needs_no_second_destroy(self) -> None:
        b = self.backend()
        b._temporary_networks = {"network-ru-9"}
        b.journal["stages"]["network-ru-9"] = {"status": "deleted"}
        b.cleanup_attempt = Mock()
        b.prune_unused_network("ru-9")
        b.cleanup_attempt.assert_not_called()
        self.assertNotIn("network-ru-9", b._temporary_networks)

    def test_saved_group_refresh_without_distinct_hosts_keeps_empty_membership(self) -> None:
        b = self.backend()
        b.journal["require_distinct_hosts"] = False
        b.journal["stages"]["es1"] = {"candidate": {"region": "ru-9"}}
        b.store.entries = Mock(return_value=[{"type": "openstack_compute_instance_v2", "attrs": {"id": "own-vm"}}])
        drift = {
            "type": "openstack_compute_servergroup_v2",
            "change": {"before": {"id": "same", "members": ["own-vm"]}, "after": {"id": "same", "members": []}},
        }
        self.assertTrue(b.allow_refresh("network-ru-9", drift))
        drift["change"]["after"]["members"] = ["foreign"]
        self.assertFalse(b.allow_refresh("network-ru-9", drift))

    def test_empty_state_without_binding_is_validated_without_cloud_reads(self) -> None:
        root = self.root()
        folder = root / "terraform-selectel"
        folder.mkdir()
        (folder / "terraform.tfstate").write_text('{"resources":[]}')
        adapter = Mock()
        with patch("cloud_settings.adapter", return_value=adapter):
            local_runtime.validate_state(root, {"project_id": "p", "region": "ru-9"}, "otus-dz07")
        adapter.partial_state_binding.assert_not_called()
        adapter.validate_managed_state.assert_called_once()

    def test_open_accepts_owned_non_vm_resources_without_treating_them_as_vm_ids(self) -> None:
        root = self.root()
        helper = runtime_fixtures.RuntimeContract()
        local, tf, state = helper.fixture(root, 7)
        state["resources"].append(
            {
                "mode": "managed",
                "type": "openstack_networking_network_v2",
                "instances": [
                    {"attributes": {"id": "net", "name": "otus-dz07-network", "tenant_id": "project", "region": "ru-9"}}
                ],
            }
        )
        (tf / "terraform.tfstate").write_text(json.dumps(state))
        opener = Mock(return_value=True)
        local_runtime.open_portal(root, 7, opener)
        opener.assert_called_once()

    def test_empty_backup_state_is_included_in_saved_scope_validation(self) -> None:
        root = self.root()
        (root / ".local").mkdir()
        (root / "terraform-selectel").mkdir()
        (root / "terraform-selectel/terraform.tfstate.backup").write_text('{"resources":[]}')
        api = SimpleNamespace(ROOT=root, LOCAL=root / ".local", PREFIX="otus-dz07")
        self.assertIsNone(scope.saved_scope(api, "project"))

    def test_missing_network_api_leaves_audit_incomplete_even_when_compute_is_empty(self) -> None:
        api = SimpleNamespace(PREFIX="otus-dz07")
        cloud = Mock(endpoints={"compute": "https://example.invalid"})
        with self.assertRaisesRegex(ValueError, "Incomplete resource audit"):
            scope.audit_pool(api, cloud, "ru-9", Mock())

    def test_stage_inventory_reuses_one_regional_client_for_multiple_types(self) -> None:
        b, entry, item = audit_fixtures.ProviderAuditTests().backend()
        b.region = Mock(return_value=Mock())
        extra = {"type": "openstack_blockstorage_volume_v3", "attrs": {"id": "disk"}}
        with patch.object(
            audit,
            "objects",
            side_effect=lambda cloud, service, path, key: (
                [{"id": entry["attrs"]["id"]}] if service == "compute" else [{"id": "disk"}]
            ),
        ):
            found = audit.inventory(b, "es1", [entry, extra])
        b.region.assert_called_once()
        self.assertIn("disk", found[extra["type"]])

    def test_global_subnet_uses_parent_already_in_the_same_inventory(self) -> None:
        kind = "selectel_global_router_vpc_subnet_v1"
        b, entry, item = audit_fixtures.ProviderAuditTests().backend(kind)
        item["project_id"] = None
        item["network_id"] = "parent"
        parent = {"id": "parent", "project_id": b.credentials["project_id"], "region": "ru-3", "tags": [b.owner]}
        b.global_router = Mock()
        with patch.object(
            audit,
            "inventory",
            return_value={
                kind: {entry["attrs"]["id"]: item},
                "selectel_global_router_vpc_network_v1": {"parent": parent},
            },
        ):
            audit.assert_stage_owned(b, "es1")
        b.global_router.listing.assert_not_called()

    def test_legacy_journal_without_settings_does_not_override_configuration(self) -> None:
        b = self.backend()
        b.path.write_text("{}")
        actual = pool_selection.policy(b.root, "placement.json", "context.json")
        self.assertEqual(actual["profiles"], b.policy["profiles"])
        self.assertIn("operator_ip_services", actual)

    def test_prepared_placement_can_reselect_without_a_context_or_resources(self) -> None:
        b = self.backend()
        previous = {
            "phase": "prepared",
            "provider": "selectel",
            "namespace": "otus-dz07",
            "project_id": "synthetic",
            "region": "ru-9",
            "availability_zone": "ru-9a",
        }
        (b.root / ".local/placement.json").write_text(json.dumps(previous))
        choice = {"region": "ru-3", "availability_zone": "ru-3b"}
        pool_selection.select(b.root, choice, "synthetic", "context.json")
        actual = json.loads((b.root / ".local/placement.json").read_text())
        self.assertEqual(actual["region"], "ru-3")
        self.assertEqual(actual["project_id"], "synthetic")

    def test_test_cycle_accepts_a_genuinely_empty_state_backup_before_cloud_reads(self) -> None:
        import test_cycle

        root = self.root()
        (root / ".local").mkdir()
        (root / "terraform-selectel").mkdir()
        (root / "terraform-selectel/terraform.tfstate.backup").write_text('{"resources":[],"outputs":{}}')
        cloud = Mock()
        cloud.resource_regions.return_value = []
        api = SimpleNamespace(
            ROOT=root, LOCAL=root / ".local", managed_ids=Mock(return_value={}), cloud=Mock(return_value=cloud)
        )
        with patch("cloud_settings.sequential_enabled", return_value=False):
            test_cycle.reject_existing(api)
        api.cloud.assert_called_once_with(bind=False)

    def test_empty_scope_audit_can_preserve_preparation_files_when_requested(self) -> None:
        helper = empty_fixtures.EmptyScopeAuditContracts()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        marker = helper.local / "test-cycle.json"
        marker.write_text('{"run_id":"synthetic","status":"cleanup_failed"}')
        with patch("local_runtime.cleanup_runtime") as clean, patch("artifact_lifecycle.release") as release:
            staged_runtime.cleanup_empty(helper.api, clean_local=False)
        clean.assert_not_called()
        release.assert_not_called()
        self.assertTrue(marker.exists())

    def test_confirmed_staged_cleanup_can_retain_local_files_for_candidate_retry(self) -> None:
        helper = lifecycle_fixtures.RetainedLifecycleTest()
        api, provider = helper.fixture(self.root())
        sentinel = api.LOCAL / "prepared"
        sentinel.touch()
        with (
            patch("test_cycle.require_access"),
            patch("staged_runtime.backend_factory", return_value=provider),
            patch("ansible_operation.before_destroy"),
            patch("local_runtime.cleanup_runtime") as clean,
        ):
            staged_runtime.cleanup(api, clean_local=False)
        clean.assert_not_called()
        self.assertTrue(sentinel.exists())
        self.assertTrue(json.loads((api.ROOT / "docs/cleanup-result.json").read_text())["success"])

    def test_legacy_route_already_allowed_does_not_apply_a_second_plan(self) -> None:
        from test_runtime_support import lab

        helper = controller_fixtures.ControllerBoundaries()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        path = helper.root / "terraform-selectel/terraform.tfvars.json"
        path.write_text('{"admin_cidrs":["8.8.8.8/32"]}')
        (helper.local / "secrets.json").write_text('{"api":"synthetic-key"}')
        result = {
            "nonce": "fresh",
            "ip": "8.8.8.8",
            "signature": hmac.new(b"synthetic-key", b"fresh|8.8.8.8", hashlib.sha256).hexdigest(),
        }
        with (
            patch.object(lab, "checked_plan") as plan,
            patch.object(lab, "tf") as tf,
            patch.object(lab.secrets, "token_hex", return_value="fresh"),
            patch("urllib.request.urlopen", return_value=io.StringIO(json.dumps(result))),
        ):
            lab.discover_route({"site_url": "http://example.invalid"})
        plan.assert_not_called()
        tf.assert_not_called()
