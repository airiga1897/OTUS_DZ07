"""Rejected plans and diagnostic failures must never apply or erase recovery data."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import stage_store as stages
from cloud_contract import TerraformError
from plan_diagnostics import PlannedResource, PlanView

ROOT = Path(__file__).resolve().parents[1]


class StageStoreFailureMatrix(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(dir=ROOT / ".tools")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.api = SimpleNamespace(
            ROOT=self.root, RUN=self.root / ".local/runs", TF=self.root / "terraform", redact=lambda s: s
        )
        self.store = stages.StageStore(
            self.api,
            {"stages": {"es1": {"candidate": {"region": "ru-9"}}}},
            environment=lambda candidate: {},
            cli_config=self.root / "terraform.rc",
        )

    def test_paths_missing_modules_and_unconfigured_environment_fail_before_cli(self) -> None:
        for name in ("../foreign", "ES1", "es1/x", ""):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.store.folder(name)
        with self.assertRaisesRegex(ValueError, "module is missing"):
            self.store.configure("es1", self.root / "missing", {})
        self.store.environment = None
        with patch("subprocess.run") as process, self.assertRaisesRegex(ValueError, "environment"):
            self.store.tf("es1", "apply")
        process.assert_not_called()

    def test_terraform_failure_survives_failure_to_write_private_log(self) -> None:
        self.api.RUN.parent.mkdir(parents=True)
        self.api.RUN.write_text("blocks directory creation")
        with (
            patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1, b"", b"original fault")),
            patch("placement_diagnostics.capture_terraform", side_effect=OSError("evidence disk unavailable")),
            self.assertRaises(TerraformError) as caught,
        ):
            self.store.tf("es1", "apply")
        self.assertEqual(caught.exception.output, "original fault")
        self.assertEqual(caught.exception.diagnostics_error_type, "OSError")
        with (
            patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, b"OK", b"")),
            self.assertRaises(OSError),
        ):
            self.store.tf("es1", "apply")

    def test_known_identity_delete_and_forbidden_plan_paths(self) -> None:
        change: PlannedResource = {"address": "vm", "change": {"actions": ["delete"], "before": {"id": "owned"}}}
        with self.assertRaisesRegex(ValueError, "outside"):
            stages.check_plan(PlanView(resource_changes=[change]), {}, destroy=True)
        with self.assertRaisesRegex(ValueError, "identity"):
            stages.check_plan(PlanView(resource_changes=[change]), {"vm": "different"}, destroy=True)
        with self.assertRaisesRegex(ValueError, "unchanged"):
            stages.check_plan(PlanView(resource_changes=[change]), {"vm": "owned"}, destroy=True, additions_only=True)
        stages.check_plan(
            PlanView(resource_changes=[{"address": "data.vm", "mode": "data", "change": {"actions": ["read"]}}]), {}
        )
        stages.check_plan(PlanView(resource_drift=[{"address": "vm", "change": {"actions": ["no-op"]}}]), {})
        drift: PlannedResource = {"address": "vm", "change": {"actions": ["delete"], "before": {"id": "owned"}}}
        stages.check_plan(PlanView(resource_drift=[drift]), {"vm": "owned"}, destroy=True)

    def test_plan_diagnostics_failure_preserves_original_guard_and_never_applies(self) -> None:
        plan = {
            "resource_changes": [
                {
                    "address": "vm",
                    "change": {
                        "actions": ["update"],
                        "before": {"id": "owned"},
                        "after": {"id": "owned", "name": "changed"},
                    },
                }
            ]
        }
        tf_mock = Mock(side_effect=lambda name, op, *args: json.dumps(plan) if op == "show" else "")
        with (
            patch.object(self.store, "identities", Mock(return_value={"vm": "owned"})),
            patch.object(self.store, "tf", tf_mock),
            patch("placement_diagnostics.capture_terraform", side_effect=PermissionError("denied")),
            self.assertRaises(ValueError) as caught,
        ):
            self.store.apply("es1")
        self.assertEqual(caught.exception.__dict__["terraform_operation"], "plan-guard")
        self.assertEqual(caught.exception.__dict__["diagnostics_error_type"], "PermissionError")
        self.assertNotIn("apply", [call.args[1] for call in tf_mock.call_args_list])

    def test_configure_reuses_lock_and_indexed_identities_ignore_data_sources(self) -> None:
        source = self.root / "provider"
        source.mkdir()
        (source / ".terraform.lock.hcl").write_text("pinned provider")
        self.store.configure("es1", source, {"name": "synthetic"})
        lock = self.store.folder("es1") / ".terraform.lock.hcl"
        self.assertEqual(lock.read_text(), "pinned provider")
        (source / ".terraform.lock.hcl").write_text("changed source lock")
        self.store.configure("es1", source, {})
        self.assertEqual(lock.read_text(), "pinned provider")
        state = {
            "resources": [
                {"mode": "data"},
                {
                    "mode": "managed",
                    "module": "module.stage",
                    "type": "vm",
                    "name": "nodes",
                    "instances": [{"index_key": "es1", "attributes": {"id": "owned"}}],
                },
            ]
        }
        (self.store.folder("es1") / "terraform.tfstate").write_text(json.dumps(state))
        self.assertEqual(self.store.identities("es1"), {'module.stage.vm.nodes["es1"]': "owned"})

    def test_posix_lock_protocol_releases_on_exception_and_is_reentrant(self) -> None:
        fcntl = SimpleNamespace(LOCK_EX=1, LOCK_NB=2, LOCK_UN=4, flock=Mock())
        with (
            patch("sys.platform", "linux"),
            patch.dict(sys.modules, fcntl=fcntl),
            self.assertRaisesRegex(RuntimeError, "interrupted"),
            stages.operation(self.root / ".local"),
        ):
            with stages.operation(self.root / ".local"):
                self.assertEqual(fcntl.flock.call_count, 1)
            raise RuntimeError("interrupted")
        self.assertEqual([call.args[1] for call in fcntl.flock.call_args_list], [3, 4])
