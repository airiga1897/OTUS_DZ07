"""Test-cycle ownership and cleanup tests use only isolated roots and mock APIs."""

import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import test_cycle
import topology
from local_runtime import cleanup_runtime
from selectel_adapter import SelectelAdapter

ROOT = Path(__file__).resolve().parents[1]


class CycleSafetyTests(unittest.TestCase):
    def test_missing_security_group_api_is_skipped_only_when_extension_is_absent(self) -> None:
        for supported in (True, False):
            cloud = Mock(endpoints={"network": "https://example.invalid"})

            def get(service, path, *, supported=supported):
                base = path.split("?", 1)[0]
                if base == "/v2.0/security-groups":
                    raise urllib.error.HTTPError("https://example.invalid", 404, "Not Found", {}, None)
                if base == "/v2.0/extensions":
                    return {"extensions": [{"alias": "security-group"}] if supported else []}
                key = base.rstrip("/").rsplit("/", 1)[-1].replace("-", "_")
                return {key: []}

            cloud.get.side_effect = get
            if supported:
                with self.assertRaises(urllib.error.HTTPError):
                    test_cycle.reject_cloud_resources(SimpleNamespace(PREFIX="otus-dz07"), cloud)
            else:
                test_cycle.reject_cloud_resources(SimpleNamespace(PREFIX="otus-dz07"), cloud)

    def test_read_only_audit_does_not_require_an_image_catalog(self) -> None:
        cloud = SelectelAdapter.__new__(SelectelAdapter)
        cloud.config, cloud.token = {}, "synthetic"
        cloud.catalog = [
            {
                "type": kind,
                "endpoints": [{"interface": "public", "region": "ru-4", "url": "https://example.invalid/" + kind}],
            }
            for kind in ("compute", "network")
        ]
        self.assertEqual(set(cloud.read_only_pool("ru-4").endpoints), {"compute", "network"})

    def fixture(self, root):
        local = root / ".local"
        local.mkdir()
        (root / "terraform-selectel").mkdir()
        return SimpleNamespace(
            ROOT=root,
            LOCAL=local,
            PREFIX="otus-dz07",
            _TEST_CYCLE_ID=None,
            _TOPOLOGY_OVERRIDE=None,
            managed_ids=Mock(return_value={}),
            cloud=Mock(),
            desired_topology=lambda: topology.load(ROOT),
            save=lambda path, value: path.write_text(json.dumps(value)),
            cleanup=Mock(side_effect=lambda: cleanup_runtime(root)),
        )

    def test_existing_stand_context_or_operation_is_rejected_before_auth_and_cleanup(self) -> None:
        for name in ("context.json", "placement.json", "test-cycle.json", "expansion-pending.json"):
            with self.subTest(name=name), tempfile.TemporaryDirectory(dir=ROOT / ".tools") as d:
                api = self.fixture(Path(d))
                (api.LOCAL / name).write_text("{}")
                with self.assertRaises(RuntimeError):
                    test_cycle.run(api)
                api.cloud.assert_not_called()
                api.cleanup.assert_not_called()

    def test_nonempty_state_backup_is_rejected_before_auth(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tools") as d:
            api = self.fixture(Path(d))
            (api.ROOT / "terraform-selectel/terraform.tfstate.backup").write_text(
                json.dumps({"resources": [{"instances": [{"attributes": {"id": "preserve-me"}}]}]})
            )
            with self.assertRaises(RuntimeError):
                test_cycle.run(api)
            api.cloud.assert_not_called()

    def test_orphan_cloud_resource_is_rejected_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tools") as d:
            api = self.fixture(Path(d))
            cloud = api.cloud.return_value
            cloud.catalog = [{"type": "compute", "endpoints": [{"region": "ru-7", "interface": "public"}]}]
            cloud.resource_regions.return_value = ["ru-7"]
            cloud.read_only_pool.return_value.get.return_value = {"servers": [{"name": "otus-dz07-db1"}]}
            cloud.read_only_pool.return_value.endpoints = {"compute": "https://example.invalid"}
            with self.assertRaises(RuntimeError):
                test_cycle.run(api)
            api.cloud.assert_called_once_with(bind=False)
            self.assertEqual({p.name for p in api.LOCAL.iterdir()}, {"stage-operation.lock"})
            api.cleanup.assert_not_called()

    def test_cycle_failure_cleans_only_its_isolated_root(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tools") as d:
            api = self.fixture(Path(d))
            (api.LOCAL / "operator.json").write_text("{}")
            (api.ROOT / "docs").mkdir()

            def fail(_) -> None:
                (api.LOCAL / "key").write_text("synthetic")
                raise RuntimeError("join failed")

            with (
                patch.object(test_cycle, "reject_existing"),
                patch.object(test_cycle, "stages", side_effect=fail),
                self.assertRaisesRegex(RuntimeError, "join failed"),
            ):
                test_cycle.run(api)
            api.cleanup.assert_called_once_with()
            self.assertEqual({p.name for p in api.LOCAL.iterdir()}, {"operator.json", "stage-operation.lock"})
            report = json.loads((api.ROOT / "docs/test-cycle-result.json").read_text())
            self.assertFalse(report["success"])
            self.assertFalse(report["stages_completed"])
            self.assertTrue(report["state_and_api_cleanup_verified"])
            self.assertEqual(report["error_type"], "RuntimeError")
            self.assertIn("finished_utc", report)
            self.assertNotIn("join failed", json.dumps(report))

    def test_failed_cleanup_preserves_context_and_allows_explicit_destroy_only(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tools") as d:
            api = self.fixture(Path(d))
            (api.LOCAL / "key").write_text("synthetic")
            api.cleanup.side_effect = RuntimeError("API audit failed")
            with (
                patch.object(test_cycle, "reject_existing"),
                patch.object(test_cycle, "stages", return_value={}),
                self.assertRaisesRegex(RuntimeError, "API audit failed"),
            ):
                test_cycle.run(api)
            self.assertTrue((api.LOCAL / "key").exists())
            report = json.loads((api.ROOT / "docs/test-cycle-result.json").read_text())
            self.assertFalse(report["success"])
            self.assertFalse(report["state_and_api_cleanup_verified"])
            self.assertEqual(report["cleanup_error_type"], "RuntimeError")
            self.assertEqual(json.loads((api.LOCAL / "test-cycle.json").read_text())["status"], "cleanup_failed")
            with self.assertRaises(RuntimeError):
                test_cycle.require_access(api)
            test_cycle.require_access(api, cleanup=True)

    def test_success_report_is_written_only_after_verified_cleanup(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tools") as d:
            api = self.fixture(Path(d))
            (api.ROOT / "docs").mkdir()
            with (
                patch.object(test_cycle, "reject_existing"),
                patch.object(test_cycle, "stages", return_value={"success": True}),
            ):
                test_cycle.run(api)
            report = json.loads((api.ROOT / "docs/test-cycle-result.json").read_text())
            self.assertTrue(report["state_and_api_cleanup_verified"])
            self.assertTrue(report["stages_completed"])
            self.assertTrue(report["success"])
            self.assertIn("run_id", report)
            self.assertFalse((api.LOCAL / "test-cycle.json").exists())

    def test_terraform_error_evidence_is_linked_and_saved_before_cleanup(self) -> None:
        import subprocess

        from cloud_contract import TerraformError
        from stage_store import StageStore

        with tempfile.TemporaryDirectory(dir=ROOT / ".tools") as d:
            api = self.fixture(Path(d))
            api.TF, api.RUN, api.redact = api.ROOT / "synthetic-tf", api.LOCAL / "runs", lambda text: text
            store = StageStore(
                api,
                {"stages": {"connection-ru-3": {"candidate": {"region": "ru-3"}}}},
                environment=lambda candidate: {},
                cli_config=api.ROOT / "terraform-selectel.rc",
            )

            def fail(_) -> None:
                store.tf("connection-ru-3", "apply", "stage.tfplan")

            def clean() -> None:
                evidence = list((api.ROOT / "docs/terraform-faults").glob("*.json"))
                self.assertEqual(len(evidence), 1)
                self.assertIn("network ERROR", evidence[0].read_text())
                cleanup_runtime(api.ROOT)

            api.cleanup.side_effect = clean
            with (
                patch.object(test_cycle, "reject_existing"),
                patch.object(test_cycle, "stages", side_effect=fail),
                patch(
                    "stage_store.subprocess.run", return_value=subprocess.CompletedProcess([], 1, b"", b"network ERROR")
                ),
                self.assertRaises(TerraformError),
            ):
                test_cycle.run(api)
            report = json.loads((api.ROOT / "docs/test-cycle-result.json").read_text())
            self.assertTrue(report["state_and_api_cleanup_verified"])
            self.assertEqual(report["terraform_stage"], "connection-ru-3")
            self.assertTrue((api.ROOT / report["failure_evidence"]).exists())

    def test_progress_preserves_completed_steps_and_failed_stage(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tools") as d:
            api = self.fixture(Path(d))

            def fail(api) -> None:
                test_cycle.progress(api, "initial_deploy")
                test_cycle.progress(api, "add_replica")
                raise RuntimeError("synthetic-sensitive-error")

            with (
                patch.object(test_cycle, "reject_existing"),
                patch.object(test_cycle, "stages", side_effect=fail),
                self.assertRaises(RuntimeError),
            ):
                test_cycle.run(api)
            report = json.loads((api.ROOT / "docs/test-cycle-result.json").read_text())
            self.assertEqual(report["completed_steps"], ["initial_deploy"])
            self.assertEqual(report["failed_stage"], "add_replica")
            self.assertTrue(report["state_and_api_cleanup_verified"])

    def test_default_stages_repeat_without_adding_any_nodes(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tools") as d:
            api = self.fixture(Path(d))
            identities, calls = {}, []

            def deploy() -> None:
                desired = api._TOPOLOGY_OVERRIDE or topology.load(ROOT)
                for name in desired["nodes"]:
                    identities.setdefault(name, "synthetic-" + name)
                calls.append(set(identities))

            api.deploy_selected, api.deploy = deploy, deploy
            api.managed_ids = lambda: dict(identities)
            api.lab_output = lambda: {"server_ids": dict(identities)}
            api.verify = Mock()
            with (
                patch("verification.snapshot", return_value={"run_id": "canary"}),
                patch.object(test_cycle, "check_original") as check,
                patch("observability.command") as elastic,
            ):
                result = test_cycle.stages(api)
            self.assertEqual([len(c) for c in calls], [4, 4])
            self.assertEqual(calls[-1], calls[-2])
            self.assertEqual(check.call_count, 1)
            self.assertTrue(result["repeat_deploy_verified"])
            self.assertEqual(result["database_nodes"], 1)
            self.assertNotIn("added_nodes", result)
            self.assertNotIn("es4", calls[-1])
            self.assertNotIn("db4", calls[-1])
            self.assertEqual([c.args[1] for c in elastic.call_args_list], ["checkpoint", "preservation"])
            self.assertIsNone(api._TOPOLOGY_OVERRIDE)
            api.verify.assert_called_once_with(False)
