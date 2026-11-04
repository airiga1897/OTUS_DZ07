"""A computed boot attachment is not a request to modify existing infrastructure."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from selectel_staged import Backend
from stage_store import StageStore, check_plan

ROOT = Path(__file__).resolve().parents[1]


class VolumeRefreshTests(unittest.TestCase):
    def fixture(self):
        backend = Backend.__new__(Backend)
        backend.desired = {"nodes": {"app": {"role": "app"}}}
        address = "module.stage.openstack_blockstorage_volume_v3.boot"
        backend.store = SimpleNamespace(
            entries=Mock(
                return_value=[
                    {"type": "openstack_compute_instance_v2", "attrs": {"id": "own-vm"}},
                    {"type": "openstack_blockstorage_volume_v3", "address": address, "attrs": {"id": "own-disk"}},
                ]
            )
        )
        drift = {
            "address": address,
            "type": "openstack_blockstorage_volume_v3",
            "change": {
                "actions": ["update"],
                "before": {"id": "own-disk", "attachment": []},
                "after": {"id": "own-disk", "attachment": [{"instance_id": "own-vm", "device": "/dev/vda"}]},
            },
        }
        return backend, drift

    def validate(self, backend, drift, changes=None) -> None:
        check_plan(
            {"resource_drift": [drift], "resource_changes": changes or []},
            {drift["address"]: "own-disk"},
            additions_only=True,
            allow_drift=lambda value: backend.allow_refresh("app", value),
        )

    def test_discovery_of_own_boot_attachment_is_read_only(self) -> None:
        backend, drift = self.fixture()
        self.validate(
            backend,
            drift,
            [{"address": drift["address"], "change": {"actions": ["no-op"], "before": drift["change"]["after"]}}],
        )

    def test_foreign_detached_multiple_or_unproven_attachments_are_rejected(self) -> None:
        for attachment in ([], [{"instance_id": "foreign"}], [{}, {}], [None], "unknown"):
            backend, drift = self.fixture()
            drift["change"]["after"]["attachment"] = attachment
            with self.subTest(attachment=attachment), self.assertRaises(ValueError):
                self.validate(backend, drift)
        backend, drift = self.fixture()
        backend.store.entries.return_value = []
        with self.assertRaises(ValueError):
            self.validate(backend, drift)

    def test_identity_size_type_and_planned_changes_remain_blocked(self) -> None:
        for field, value in (
            ("id", "foreign"),
            ("size", 99),
            ("volume_type", "foreign"),
            ("availability_zone", "foreign"),
        ):
            backend, drift = self.fixture()
            drift["change"]["after"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.validate(backend, drift)
        for action in (["update"], ["delete", "create"], ["delete"]):
            backend, drift = self.fixture()
            with self.subTest(action=action), self.assertRaises(ValueError):
                self.validate(
                    backend,
                    drift,
                    [{"address": drift["address"], "change": {"actions": action, "before": {"id": "own-disk"}}}],
                )

    def test_previous_foreign_attachment_and_wrong_saved_volume_are_rejected(self) -> None:
        backend, drift = self.fixture()
        drift["change"]["before"]["attachment"] = [{"instance_id": "foreign"}]
        with self.assertRaises(ValueError):
            self.validate(backend, drift)
        backend, drift = self.fixture()
        backend.store.entries.return_value[1]["attrs"]["id"] = "foreign"
        with self.assertRaises(ValueError):
            self.validate(backend, drift)

    def test_rejected_plan_preserves_only_field_names_before_any_apply(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tools") as directory:
            root = Path(directory)
            api = SimpleNamespace(
                ROOT=root, LOCAL=root / ".local", redact=lambda value: value, _TEST_CYCLE_ID="synthetic"
            )
            store = StageStore(api, {})
            store.identities = Mock(return_value={"volume": "same"})
            plan = {
                "resource_drift": [
                    {
                        "address": "volume",
                        "type": "openstack_blockstorage_volume_v3",
                        "change": {
                            "actions": ["update"],
                            "before": {"id": "same", "metadata": {}},
                            "after": {"id": "same", "metadata": {"password": "never-persist-this"}},
                        },
                    }
                ]
            }
            store.tf = Mock(side_effect=lambda name, op, *args: json.dumps(plan) if op == "show" else "")
            with self.assertRaises(ValueError) as caught:
                store.apply("app", unchanged=True)
            self.assertEqual(caught.exception.terraform_operation, "plan-guard")
            saved = (root / caught.exception.failure_evidence).read_text()
            self.assertNotIn("never-persist-this", saved)
            self.assertIn("metadata", saved)
            self.assertNotIn("apply", [call.args[1] for call in store.tf.call_args_list])
