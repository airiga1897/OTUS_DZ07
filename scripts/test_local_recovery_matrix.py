"""Recovery guards must reject stale context before opening or deleting anything."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import local_runtime as runtime
import test_cycle as cycle
import test_runtime_contract as fixtures

ROOT = Path(__file__).resolve().parents[1]


class LocalRecoveryMatrix(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(dir=ROOT / ".tools")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_open_rejects_each_foreign_or_stale_identity_before_browser(self) -> None:
        local, tf, original = fixtures.RuntimeContract().fixture(self.root, 5)
        context_path = local / "deployment.json"
        original_context = json.loads(context_path.read_text())
        saved_path = local / "lab.json"
        original_saved = json.loads(saved_path.read_text())
        changes = ("inactive", "lineage", "deployment", "project", "region", "name", "no-resources", "address")
        for change in changes:
            state, context, saved = copy.deepcopy(original), dict(original_context), copy.deepcopy(original_saved)
            attrs = state["resources"][0]["instances"][0]["attributes"]
            if change == "inactive":
                context["phase"] = "creating"
            elif change == "lineage":
                context["state_lineage"] = "stale"
            elif change == "deployment":
                saved["deployment_id"] = "foreign"
            elif change == "project":
                attrs["tenant_id"] = "foreign"
            elif change == "region":
                attrs["region"] = "foreign"
            elif change == "name":
                attrs["name"] = "foreign-app"
            elif change == "no-resources":
                state["resources"] = []
            elif change == "address":
                saved["app_public_ip"] = "192.0.2.11"
            (tf / "terraform.tfstate").write_text(json.dumps(state))
            context_path.write_text(json.dumps(context))
            saved_path.write_text(json.dumps(saved))
            browser = Mock()
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "контекста"):
                runtime.open_portal(self.root, 5, browser)
            browser.assert_not_called()
        context_path.write_text(json.dumps({**original_context, "availability_zone": None}))
        saved_path.write_text(json.dumps(original_saved))
        (tf / "terraform.tfstate").write_text(json.dumps(original))
        with patch("builtins.print") as output:
            self.assertEqual(runtime.open_portal(self.root, 5, Mock(return_value=False)), "http://192.0.2.10")
        self.assertIn("ru-9a", str(output.call_args_list))

    def test_cleanup_refuses_unconfirmed_stages_and_other_provider_state_preserving_keys(self) -> None:
        local = self.root / ".local"
        local.mkdir()
        key = local / "key"
        key.write_text("synthetic-recovery-key")
        stage_dir = local / "stages/es1"
        stage_dir.mkdir(parents=True)
        state_path = stage_dir / "terraform.tfstate"
        journal = local / "staged-deployment.json"
        for data in (
            {"cleanup_verified": False, "stages": {}},
            {"cleanup_verified": True, "stages": {"es1": {"status": "active"}}},
        ):
            journal.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "recovery files"):
                runtime.cleanup_runtime(self.root)
            self.assertTrue(key.exists())
        journal.write_text('{"cleanup_verified":true,"stages":{"es1":{"status":"deleted"}}}')
        state_path.write_text('{"resources":[{"mode":"managed","instances":[{}]}]}')
        with self.assertRaisesRegex(ValueError, "Sequential managed"):
            runtime.cleanup_runtime(self.root)
        state_path.write_text("{}")
        other = self.root / "terraform-other/terraform.tfstate"
        other.parent.mkdir()
        other.write_text('{"resources":[{"mode":"managed","instances":[{}]}]}')
        with self.assertRaisesRegex(ValueError, "Another provider"):
            runtime.cleanup_runtime(self.root)
        self.assertTrue(key.exists())
        other.write_text("{}")
        runtime.cleanup_runtime(self.root)
        self.assertFalse(key.exists())
        self.assertFalse(stage_dir.exists())

    def test_fresh_cycle_guard_checks_backups_locks_plans_context_and_empty_files(self) -> None:
        tf = self.root / "terraform-selectel"
        tf.mkdir()
        (self.root / "terraform-not-a-directory").write_text("not a directory")
        (self.root / ".local").mkdir()
        cycle.assert_empty(self.root)
        cases = {
            "terraform.tfstate.lock.info": "{}",
            "terraform.tfstate.backup": '{"outputs":{"lab":{}}}',
            "stage.tfplan": "plan",
        }
        for name, body in cases.items():
            path = tf / name
            path.write_text(body)
            with self.subTest(name=name), self.assertRaises(ValueError):
                cycle.assert_empty(self.root)
            path.unlink()
        (tf / "terraform.tfstate").write_text("{}")
        cycle.assert_empty(self.root)
        stages = self.root / ".local/stages/es1"
        stages.mkdir(parents=True)
        state = stages / "terraform.tfstate.backup"
        state.write_text("{}")
        with self.assertRaisesRegex(ValueError, "Sequential"):
            cycle.assert_empty(self.root)
        state.unlink()
        marker = self.root / ".local/test-cycle.json"
        marker.write_text("{}")
        with self.assertRaisesRegex(ValueError, "unfinished"):
            cycle.assert_empty(self.root)

    def test_cycle_reject_existing_before_authentication_and_handles_owned_markers(self) -> None:
        from types import SimpleNamespace

        api = SimpleNamespace(
            ROOT=self.root,
            LOCAL=self.root / ".local",
            _TEST_CYCLE_ID="current",
            managed_ids=Mock(return_value={"app": "existing"}),
            cloud=Mock(),
        )
        api.LOCAL.mkdir()
        with self.assertRaisesRegex(RuntimeError, "empty stand"):
            cycle.reject_existing(api)
        api.cloud.assert_not_called()
        marker = api.LOCAL / "test-cycle.json"
        marker.write_text('{"run_id":"current","status":"running"}')
        cycle.require_access(api)
        marker.write_text('{"run_id":"other","status":"running"}')
        with self.assertRaises(RuntimeError):
            cycle.require_access(api, cleanup=True)
        marker.write_text('{"run_id":"other","status":"cleanup_failed"}')
        cycle.require_access(api, cleanup=True)
        with self.assertRaises(RuntimeError):
            cycle.require_access(api)

    def test_missing_local_directory_cleanup_and_state_validation_are_noops(self) -> None:
        runtime.cleanup_runtime(self.root)
        runtime.validate_state(self.root, {}, "otus-dz07")
        self.assertEqual(list(self.root.iterdir()), [])
