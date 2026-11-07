"""Interruption, scope and cleanup regression tests in temporary clones only."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import staged_runtime
import test_staged_lifecycle as lifecycle_fixture
from local_runtime import cleanup_runtime
from plan_diagnostics import PlannedResource, PlanView
from selectel_global_router import GlobalRouter
from selectel_staged import Backend, validate_policy
from selectel_staged_audit import assert_stage_owned
from stage_journal import StageRecord
from stage_store import StageStore, atomic_json, check_plan, require_context
from terraform_state_types import StateEntry

ROOT = Path(__file__).resolve().parents[1]


class StageSafetyTests(unittest.TestCase):
    def test_server_group_refresh_requires_exact_live_owned_es_members(self) -> None:
        backend = Backend.__new__(Backend)
        backend.desired = {"nodes": {"es1": {"host": 31, "role": "es"}, "app": {"host": 10, "role": "app"}}}
        backend.journal = {"stages": {"es1": {"candidate": {"region": "ru-3"}}}}
        store = StageStore.__new__(StageStore)
        backend.store = store
        owned: list[StateEntry] = [{"type": "openstack_compute_instance_v2", "attrs": {"id": "owned"}}]
        drift: PlannedResource = {
            "address": "module.stage.group",
            "type": "openstack_compute_servergroup_v2",
            "change": {
                "actions": ["update"],
                "before": {"id": "group", "members": []},
                "after": {"id": "group", "members": ["owned"]},
            },
        }
        after = drift["change"].get("after")
        before = drift["change"].get("before")
        if not isinstance(after, dict) or not isinstance(before, dict):
            raise AssertionError("expected drift objects")

        def allowed(value: PlannedResource) -> bool:
            return backend.allow_refresh("network-ru-3", value)

        plan: PlanView = {"resource_drift": [drift]}
        with patch.object(store, "entries", return_value=owned):
            check_plan(plan, {drift["address"]: "group"}, allow_drift=allowed)
            updates: tuple[dict[str, object], ...] = (
                {"members": ["foreign"]},
                {"members": []},
                {"policies": ["soft-anti-affinity"]},
            )
            for changes in updates:
                with patch.dict(after, changes), self.assertRaises(ValueError):
                    check_plan(plan, {drift["address"]: "group"}, allow_drift=allowed)
            with patch.dict(before, {"members": ["foreign"]}), self.assertRaises(ValueError):
                check_plan(plan, {drift["address"]: "group"}, allow_drift=allowed)

    def test_provider_refresh_only_accepts_empty_tags_or_its_saved_attachment(self) -> None:
        backend = Backend.__new__(Backend)
        backend.api = SimpleNamespace(PREFIX="synthetic")
        backend.journal = {
            "deployment_id": "run",
            "stages": {"connection-ru-9": {"output": {"network_id": "attached"}}},
        }
        drift: PlannedResource = {
            "address": "module.stage.network",
            "type": "openstack_networking_network_v2",
            "change": {
                "actions": ["update"],
                "before": {"id": "same", "all_tags": [backend.owner]},
                "after": {"id": "same", "all_tags": [backend.owner, "l3vpn-network", "l3vpn-network-attached"]},
            },
        }
        after = drift["change"].get("after")
        if not isinstance(after, dict):
            raise AssertionError("expected drift object")

        def allowed(value: PlannedResource) -> bool:
            return backend.allow_refresh("network-ru-9", value)

        plan: PlanView = {"resource_drift": [drift]}
        check_plan(plan, {drift["address"]: "same"}, allow_drift=allowed)
        extras: tuple[dict[str, object], ...] = (
            {"id": "foreign"},
            {"all_tags": [backend.owner, "foreign"]},
            {"name": "changed"},
        )
        for extra in extras:
            with patch.dict(after, extra), self.assertRaises(ValueError):
                check_plan(plan, {drift["address"]: "same"}, allow_drift=allowed)
        drift["type"] = "openstack_networking_subnet_v2"
        drift["change"]["before"] = {"id": "same", "tags": None}
        drift["change"]["after"] = {"id": "same", "tags": []}
        check_plan(plan, {drift["address"]: "same"}, allow_drift=allowed)
        with self.assertRaises(ValueError):
            check_plan(plan, {drift["address"]: "same"})

    def test_global_subnet_nullable_project_requires_its_owned_parent_network(self) -> None:
        kind = "selectel_global_router_vpc_subnet_v1"
        attrs: dict[str, object] = {"id": "subnet", "network_id": "network", "os_subnet_id": "private-subnet"}
        entry: StateEntry = {"type": kind, "attrs": attrs}
        subnet: dict[str, object] = {**attrs, "project_id": None, "tags": ["synthetic-run"], "status": "ACTIVE"}
        parent: dict[str, object] = {"id": "network", "project_id": "project", "tags": ["synthetic-run"]}
        backend = Backend.__new__(Backend)
        backend.api = SimpleNamespace(PREFIX="synthetic")
        backend.credentials = {"project_id": "project"}
        backend.journal = {
            "deployment_id": "run",
            "stages": {"connection": {"status": "pending"}},
        }
        store = StageStore.__new__(StageStore)
        backend.store = store
        router = GlobalRouter.__new__(GlobalRouter)
        backend.global_router = router
        with (
            patch.object(store, "entries", return_value=[entry]),
            patch.object(router, "listing", return_value=[parent]),
            patch("selectel_staged_audit.inventory", return_value={kind: {"subnet": subnet}}),
        ):
            assert_stage_owned(backend, "connection", active=True)
            mismatches: tuple[dict[str, object], ...] = (
                {"project_id": "foreign"},
                {"network_id": "foreign"},
                {"os_subnet_id": "foreign"},
            )
            for changes in mismatches:
                with patch.dict(subnet, changes), self.assertRaises(ValueError):
                    assert_stage_owned(backend, "connection")
            with patch.dict(parent, {"project_id": "foreign"}), self.assertRaises(ValueError):
                assert_stage_owned(backend, "connection")
            with patch.dict(parent, {"tags": []}), self.assertRaises(ValueError):
                assert_stage_owned(backend, "connection")

    def test_generated_module_paths_are_relative_to_avoid_windows_mklink(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            module = root / "infrastructure/provider-module"
            module.mkdir(parents=True)
            (module / "main.tf").write_text('output "result" { value = {} }')
            store = StageStore(SimpleNamespace(ROOT=root), {"stages": {}})
            store.configure("node", module, {})
            config = json.loads((store.folder("node") / "main.tf.json").read_text())
            source = config["module"]["stage"]["source"]
            self.assertTrue(source.startswith("../"))
            self.assertNotIn("\\", source)
            self.assertEqual((store.folder("node") / source).resolve(), module.resolve())

    def test_atomic_journal_replace_failure_preserves_the_old_context(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "journal.json"
            path.write_text('{"old":true}')
            with patch("stage_store.os.replace", side_effect=OSError("interrupted")), self.assertRaises(OSError):
                atomic_json(path, {"new": True}, root)
            self.assertEqual(json.loads(path.read_text()), {"old": True})
            self.assertFalse(path.with_name("journal.json.next").exists())

    def test_missing_context_never_permits_erasing_stage_state_or_keys(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            stage = root / ".local/stages/es2"
            stage.mkdir(parents=True)
            (stage / "terraform.tfstate").write_text('{"resources":[]}')
            key = root / ".local/key"
            key.write_text("synthetic recovery key")
            with self.assertRaises(ValueError):
                require_context(root)
            with self.assertRaises(ValueError):
                cleanup_runtime(root)
            self.assertEqual(key.read_text(), "synthetic recovery key")

    def test_dirty_stage_state_blocks_local_cleanup_even_after_a_bad_success_marker(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            stage = root / ".local/stages/es2"
            stage.mkdir(parents=True)
            (stage / "terraform.tfstate").write_text(
                json.dumps({"resources": [{"mode": "managed", "instances": [{"attributes": {"id": "owned"}}]}]})
            )
            (root / ".local/staged-deployment.json").write_text(
                '{"cleanup_verified":true,"stages":{"es2":{"status":"deleted"}}}'
            )
            with self.assertRaises(ValueError):
                cleanup_runtime(root)
            self.assertTrue((stage / "terraform.tfstate").exists())

    def test_partial_delete_drift_can_only_remove_previously_saved_id(self) -> None:
        drift: PlanView = {
            "resource_drift": [
                {"address": "module.stage.vm", "change": {"actions": ["delete"], "before": {"id": "one"}}}
            ]
        }
        check_plan(drift, {"module.stage.vm": "one"}, destroy=True)
        for destroy, identifier in ((False, "one"), (True, "foreign")):
            with self.assertRaises(ValueError):
                check_plan(drift, {"module.stage.vm": identifier}, destroy=destroy)

    def test_api_absence_only_resumes_a_recorded_deletion(self) -> None:
        entry: StateEntry = {"type": "openstack_compute_instance_v2", "attrs": {"id": "one"}}
        record: StageRecord = {"status": "active", "audit_entries": [entry]}
        backend = Backend.__new__(Backend)
        backend.journal = {"stages": {"es2": record}}
        store = StageStore.__new__(StageStore)
        backend.store = store
        with (
            patch.object(store, "entries", return_value=[entry]),
            patch("selectel_staged_audit.inventory", return_value={"openstack_compute_instance_v2": {}}),
        ):
            with self.assertRaises(ValueError):
                assert_stage_owned(backend, "es2", active=False)
            record["status"] = "deleting"
            assert_stage_owned(backend, "es2", active=False)
            record["audit_entries"] = []
            with self.assertRaises(ValueError):
                assert_stage_owned(backend, "es2", active=False)

    def test_vm_addresses_cannot_overlap_dhcp_or_service_gateway(self) -> None:
        from run_settings import placement

        policy = placement(ROOT)
        desired = json.loads((ROOT / "config/topology.json").read_text())
        for host in (2, 200, 249, 253, 254):
            desired["nodes"]["es2"]["host"] = host
            with self.subTest(host=host), self.assertRaises(ValueError):
                validate_policy(policy, desired)

    def test_terraform_uses_default_workspace_and_only_explicit_operator_environment(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            api = SimpleNamespace(
                ROOT=root, TF=root / "synthetic-terraform.exe", RUN=root / "logs", redact=lambda value: value
            )
            credentials = {
                "domain_name": "synthetic-account",
                "project_id": "synthetic-project",
                "username": "synthetic-user",
                "password": "synthetic-secret",
                "region": "ru-9",
            }
            from selectel_staged import terraform_environment

            store = StageStore(
                api,
                {"stages": {"es2": {"candidate": {"region": "ru-3"}}}},
                environment=lambda candidate: terraform_environment(credentials, candidate),
                cli_config=root / "terraform-selectel.rc",
            )
            with (
                patch.dict(
                    os.environ,
                    {
                        "OS_TOKEN": "foreign",
                        "OS_CLOUD": "foreign",
                        "TF_WORKSPACE": "foreign",
                        "TF_CLI_ARGS_apply": "-destroy",
                        "INFRA_PROJECT_ID": "foreign",
                    },
                ),
                patch(
                    "stage_store.subprocess.run", return_value=subprocess.CompletedProcess([], 0, b"{}", b"")
                ) as process,
            ):
                store.tf("es2", "plan")
            env = process.call_args.kwargs["env"]
            self.assertEqual(env["TF_WORKSPACE"], "default")
            self.assertEqual(env["OS_REGION_NAME"], "ru-3")
            self.assertEqual(env["OS_PROJECT_ID"], "synthetic-project")
            self.assertNotIn("OS_TOKEN", env)
            self.assertNotIn("OS_CLOUD", env)
            self.assertNotIn("TF_CLI_ARGS_apply", env)
            self.assertNotIn("INFRA_PROJECT_ID", env)
            self.assertTrue(Path(env["TF_DATA_DIR"]).is_relative_to(root))

    def test_global_router_pagination_cannot_repeat_the_same_page(self) -> None:
        client = GlobalRouter.__new__(GlobalRouter)
        items = [{"id": str(i)} for i in range(200)]
        with patch.object(client, "get", return_value=items) as request:
            with self.assertRaises(ValueError):
                client.listing("routers")
            self.assertEqual(request.call_count, 2)

    def test_cleanup_attempts_other_nodes_when_one_stage_fails(self) -> None:
        helper = lifecycle_fixture.RetainedLifecycleTest()
        with tempfile.TemporaryDirectory() as folder:
            api, provider = helper.fixture(Path(folder))
            with helper.patches(provider):
                staged_runtime.deploy(api)
            cleanup = provider.cleanup_attempt

            def fail_one(name: str) -> None:
                if name == "es3":
                    raise RuntimeError("one stage failed")
                cleanup(name)

            with (
                helper.patches(provider),
                patch.object(provider, "cleanup_attempt", fail_one),
                self.assertRaises(RuntimeError),
            ):
                staged_runtime.cleanup(api)
            self.assertEqual(set(provider.objects), {"es3"})
            self.assertTrue(provider.path.exists())

    def test_test_cycle_refuses_saved_stages_before_authentication(self) -> None:
        import test_cycle

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / ".local").mkdir()
            (root / ".local/staged-deployment.json").write_text("{}")
            api = SimpleNamespace(ROOT=root, LOCAL=root / ".local", cloud=Mock(), managed_ids=Mock(return_value={}))
            with self.assertRaises(RuntimeError):
                test_cycle.reject_existing(api)
            api.cloud.assert_not_called()
