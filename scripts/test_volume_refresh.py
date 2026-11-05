"""A computed boot attachment is not a request to modify existing infrastructure."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from plan_diagnostics import PlannedResource, PlanView
from selectel_staged import Backend
from stage_store import StageStore, check_plan
from terraform_state_types import StateEntry

ROOT = Path(__file__).resolve().parents[1]


class VolumeRefreshTests(unittest.TestCase):
    def fixture(self) -> tuple[Backend, PlannedResource, Mock]:
        backend = Backend.__new__(Backend)
        backend.__dict__["desired"] = {"nodes": {"app": {"role": "app"}}}
        address = "module.stage.openstack_blockstorage_volume_v3.boot"
        entries: list[StateEntry] = [
            {"type": "openstack_compute_instance_v2", "attrs": {"id": "own-vm"}},
            {"type": "openstack_blockstorage_volume_v3", "address": address, "attrs": {"id": "own-disk"}},
        ]
        entries_mock = Mock(return_value=entries)
        backend.__dict__["store"] = SimpleNamespace(entries=entries_mock)
        drift: PlannedResource = {
            "address": address,
            "type": "openstack_blockstorage_volume_v3",
            "change": {
                "actions": ["update"],
                "before": {"id": "own-disk", "attachment": []},
                "after": {"id": "own-disk", "attachment": [{"instance_id": "own-vm", "device": "/dev/vda"}]},
            },
        }
        return backend, drift, entries_mock

    def change_maps(self, drift: PlannedResource) -> tuple[dict[str, object], dict[str, object]]:
        before = drift["change"].get("before")
        after = drift["change"].get("after")
        if not isinstance(before, dict) or not isinstance(after, dict):
            raise AssertionError("Expected drift before/after objects")
        return before, after

    def validate(self, backend: Backend, drift: PlannedResource, changes: list[PlannedResource] | None = None) -> None:
        address = drift["address"]
        plan: PlanView = {"resource_drift": [drift], "resource_changes": changes or []}
        check_plan(
            plan,
            {address: "own-disk"},
            additions_only=True,
            allow_drift=lambda value: backend.allow_refresh("app", value),
        )

    def test_discovery_of_own_boot_attachment_is_read_only(self) -> None:
        backend, drift, _ = self.fixture()
        self.validate(
            backend,
            drift,
            [{"address": drift["address"], "change": {"actions": ["no-op"], "before": drift["change"]["after"]}}],
        )

    def test_foreign_detached_multiple_or_unproven_attachments_are_rejected(self) -> None:
        attachments: tuple[object, ...] = ([], [{"instance_id": "foreign"}], [{}, {}], [None], "unknown")
        for attachment in attachments:
            backend, drift, _ = self.fixture()
            _, after = self.change_maps(drift)
            after["attachment"] = attachment
            with self.subTest(attachment=attachment), self.assertRaises(ValueError):
                self.validate(backend, drift)
        backend, drift, entries_mock = self.fixture()
        entries_mock.return_value = []
        with self.assertRaises(ValueError):
            self.validate(backend, drift)

    def test_identity_size_type_and_planned_changes_remain_blocked(self) -> None:
        for field, value in (
            ("id", "foreign"),
            ("size", 99),
            ("volume_type", "foreign"),
            ("availability_zone", "foreign"),
        ):
            backend, drift, _ = self.fixture()
            _, after = self.change_maps(drift)
            after[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.validate(backend, drift)
        for action in (["update"], ["delete", "create"], ["delete"]):
            backend, drift, _ = self.fixture()
            with self.subTest(action=action), self.assertRaises(ValueError):
                self.validate(
                    backend,
                    drift,
                    [{"address": drift["address"], "change": {"actions": action, "before": {"id": "own-disk"}}}],
                )

    def test_previous_foreign_attachment_and_wrong_saved_volume_are_rejected(self) -> None:
        backend, drift, _ = self.fixture()
        before, _ = self.change_maps(drift)
        before["attachment"] = [{"instance_id": "foreign"}]
        with self.assertRaises(ValueError):
            self.validate(backend, drift)
        backend, drift, entries_mock = self.fixture()
        saved_entries = entries_mock.return_value
        saved_entries[1]["attrs"]["id"] = "foreign"
        with self.assertRaises(ValueError):
            self.validate(backend, drift)

    def test_rejected_plan_preserves_only_field_names_before_any_apply(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tools") as directory:
            root = Path(directory)
            api = SimpleNamespace(
                ROOT=root, LOCAL=root / ".local", redact=lambda value: value, _TEST_CYCLE_ID="synthetic"
            )
            store = StageStore(api, {})
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

            def tf(name: str, op: str, *args: str) -> str:
                return json.dumps(plan) if op == "show" else ""

            tf_mock = Mock(side_effect=tf)
            with (
                patch.object(store, "identities", Mock(return_value={"volume": "same"})),
                patch.object(store, "tf", tf_mock),
                self.assertRaises(ValueError) as caught,
            ):
                store.apply("app", unchanged=True)
            self.assertEqual(caught.exception.__dict__["terraform_operation"], "plan-guard")
            saved = (root / str(caught.exception.__dict__["failure_evidence"])).read_text()
            self.assertNotIn("never-persist-this", saved)
            self.assertIn("metadata", saved)
            self.assertNotIn("apply", [call.args[1] for call in tf_mock.call_args_list])
