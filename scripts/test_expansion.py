"""Local guards for append-only deployment; no cloud credentials or API calls."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import infrastructure
import topology
import verification
from test_runtime_support import lab

ROOT = Path(__file__).resolve().parents[1]


class ExpansionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.initial = topology.load(ROOT)
        self.extended = copy.deepcopy(self.initial)
        self.extended["nodes"]["es4"] = {"host": 34, "role": "es"}

    def test_extension_and_repeat_preserve_existing_topology(self) -> None:
        self.assertEqual(topology.validate_extension(self.initial, self.extended), {"es4"})
        self.assertEqual(topology.validate_extension(self.extended, self.extended), set())

    def test_readdress_removal_and_unsupported_roles_are_rejected(self) -> None:
        for mutation in ("address", "remove", "role"):
            desired = copy.deepcopy(self.extended)
            if mutation == "address":
                desired["nodes"]["es1"]["host"] = 30
            elif mutation == "remove":
                del desired["nodes"]["es1"]
            else:
                desired["nodes"]["es4"]["role"] = "db"
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                topology.validate_extension(self.initial, desired)

    def test_identity_guard_detects_missing_or_replaced_old_resource(self) -> None:
        for after in ({}, {"es1": "different"}):
            with self.assertRaises(RuntimeError):
                topology.assert_preserved({"es1": "original"}, after)

    def test_repeat_apply_preserves_prepared_credentials_and_placement(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tools") as directory:
            root = Path(directory)
            variables = root / "terraform-selectel/terraform.tfvars.json"
            variables.parent.mkdir()
            variables.write_text(json.dumps({"image_id": "existing-image", "admin_cidrs": ["192.0.2.1/32"]}))
            api = MagicMock(ROOT=root)
            api.save.side_effect = lambda path, value: path.write_text(json.dumps(value))
            infrastructure.apply(api, self.extended)
            value = json.loads(variables.read_text())
            self.assertEqual(value["image_id"], "existing-image")
            self.assertEqual(value["admin_cidrs"], ["192.0.2.1/32"])
            self.assertEqual(value["nodes"], self.extended["nodes"])
            api.checked_plan.assert_called_once_with()

    def test_failed_extension_never_destroys_existing_stand(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tools") as directory:
            root = Path(directory)
            old = {"private_ips": {n: "10.77.0." + str(v["host"]) for n, v in self.initial["nodes"].items()}}
            with (
                patch.multiple(lab, ROOT=root, LOCAL=root / ".local"),
                patch.object(lab.topology, "load", return_value=self.extended),
                patch.object(lab, "managed_ids", return_value={"existing": "id"}),
                patch.object(lab, "lab_output", return_value=old),
                patch(
                    "verification.snapshot",
                    return_value={
                        "run_id": "synthetic",
                        "before": {},
                        "server_ids": dict.fromkeys(old["private_ips"], "id"),
                    },
                ),
                patch("infrastructure.apply", side_effect=RuntimeError("join failed")),
                patch.object(lab, "diagnostics"),
                patch.object(lab, "cleanup") as cleanup,
            ):
                with self.assertRaisesRegex(RuntimeError, "join failed"):
                    lab.deploy()
                cleanup.assert_not_called()

    def test_replacement_plan_is_rejected_before_apply(self) -> None:
        plan = {"resource_changes": [{"address": "old-db", "change": {"actions": ["delete", "create"]}}]}
        with (
            patch.object(lab, "managed_ids", return_value={}),
            patch.object(lab, "tf", side_effect=[None, json.dumps(plan)]),
            self.assertRaisesRegex(ValueError, "Unexpected mutation"),
        ):
            lab.checked_plan()

    def test_pending_canary_is_reused_without_new_write(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tools") as directory:
            api = MagicMock(LOCAL=Path(directory))
            saved = {"run_id": "pending"}
            (api.LOCAL / "expansion-pending.json").write_text(json.dumps(saved))
            self.assertEqual(verification.snapshot(api, {}), saved)
            api.connect.assert_not_called()

    def test_extension_plan_allows_only_new_node_vm_disk_and_port(self) -> None:
        cases = [
            ("openstack_compute_instance_v2", "node"),
            ("openstack_networking_port_v2", "node"),
            ("openstack_blockstorage_volume_v3", "boot"),
        ]
        for kind, name in cases:
            change = {"type": kind, "name": name, "index": "es4", "address": kind + "." + name}
            infrastructure.validate_node_creation(change, {"es4"})
            with self.assertRaises(ValueError):
                infrastructure.validate_node_creation({**change, "index": "es1"}, {"es4"})
        with self.assertRaises(ValueError):
            infrastructure.validate_node_creation(
                {"type": "openstack_networking_router_v2", "name": "lab", "address": "router"}, {"es4"}
            )

    def test_changed_identity_is_rejected_even_when_plan_says_noop(self) -> None:
        change = {
            "type": "openstack_compute_instance_v2",
            "name": "node",
            "index": "es1",
            "address": "old-node",
            "change": {"actions": ["no-op"], "before": {"id": "replaced"}},
        }
        with self.assertRaisesRegex(ValueError, "before apply"):
            infrastructure.validate_existing_identity(
                change, {("openstack_compute_instance_v2", "node", "es1"): "original"}
            )

    def test_partial_initial_deploy_resumes_without_reconstructing_a_pool(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tools") as directory:
            root = Path(directory)
            (root / "terraform-selectel").mkdir()
            (root / "terraform-selectel/terraform.tfvars.json").write_text("{}")
            with (
                patch.object(lab, "ROOT", root),
                patch.object(lab, "LOCAL", root / ".local"),
                patch.object(lab, "managed_ids", return_value={}),
                patch.object(lab.pools, "selected", return_value={"phase": "creating"}),
                patch.object(lab, "deploy") as deploy,
                patch.object(lab, "deployment_candidates") as candidates,
            ):
                lab.deploy_selected()
                deploy.assert_called_once_with()
                candidates.assert_not_called()
