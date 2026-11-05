"""Safe retained scheduler evidence and semantic-empty normalization."""

import copy
import json
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from plan_diagnostics import PlannedChange, PlannedResource, PlanView, hints, summarize
from selectel_refresh import _scheduler
from selectel_staged import Backend
from stage_store import StageStore
from terraform_state_types import StateEntry

ROOT = Path(__file__).resolve().parents[1]


class RefreshStore(StageStore):
    def __init__(self, found: list[StateEntry]) -> None:
        self._found = found

    def entries(self, name: str) -> list[StateEntry]:
        return self._found


def state_entry(kind: str, name: str, index: str | int | None, attrs: Mapping[str, object]) -> StateEntry:
    return {"type": kind, "address": "module.stage." + kind + "." + name, "index": index, "attrs": dict(attrs)}


def object_map(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise AssertionError("expected object")
    copied: dict[str, object] = {}
    for key, item in value.items():
        if isinstance(key, str):
            copied[key] = item
    return copied


def planned_resources(value: object) -> list[PlannedResource]:
    if not isinstance(value, list):
        raise AssertionError("expected drift list")
    rows: list[PlannedResource] = []
    for item in value:
        if not isinstance(item, dict):
            raise AssertionError("expected drift")
        address = item.get("address")
        kind = item.get("type")
        change = item.get("change")
        if not isinstance(address, str) or not isinstance(kind, str) or not isinstance(change, dict):
            raise AssertionError("expected drift fields")
        actions = change.get("actions")
        if not isinstance(actions, list) or any(not isinstance(action, str) for action in actions):
            raise AssertionError("expected drift actions")
        planned: PlannedChange = {"actions": actions}
        for side in ("before", "after"):
            if side not in change:
                continue
            current = change[side]
            if current is not None and not isinstance(current, dict):
                raise AssertionError("expected drift side")
            mapped = None if current is None else object_map(current)
            if side == "before":
                planned["before"] = mapped
            else:
                planned["after"] = mapped
        rows.append({"address": address, "type": kind, "change": planned})
    return rows


def refresh_fixture() -> tuple[Backend, list[PlannedResource]]:
    backend = Backend.__new__(Backend)
    backend.api = SimpleNamespace(PREFIX="otus-dz07")
    backend.desired = {"nodes": {"app": {"host": 10, "role": "app"}}}
    inputs: dict[str, object] = {
        "private_ip": "10.77.0.10",
        "network_id": "own-network",
        "subnet_id": "own-subnet",
        "security_group_id": "own-sg",
    }
    backend.journal = {"deployment_id": "synthetic", "stages": {"app": {"inputs": inputs}}}
    vm_attrs: dict[str, object] = {
        "id": "own-vm",
        "name": "otus-dz07-app",
        "metadata": {"dz07_owner": backend.owner},
    }
    entries = [
        state_entry("openstack_compute_instance_v2", "node", None, vm_attrs),
        state_entry("openstack_blockstorage_volume_v3", "boot", None, {"id": "own-disk"}),
        state_entry(
            "openstack_networking_port_v2",
            "node",
            None,
            {
                "id": "own-port",
                "name": "otus-dz07-app",
                "network_id": "own-network",
                "security_group_ids": ["own-sg"],
                "fixed_ip": [{"subnet_id": "own-subnet", "ip_address": "10.77.0.10"}],
            },
        ),
        state_entry("openstack_networking_floatingip_v2", "app", 0, {"id": "own-fip", "address": "8.8.8.8"}),
        state_entry(
            "openstack_networking_floatingip_associate_v2",
            "app",
            0,
            {"id": "association", "floating_ip": "8.8.8.8", "port_id": "own-port"},
        ),
    ]
    backend.store = RefreshStore(entries)
    payload = json.loads((ROOT / "tests/fixtures/selectel-refresh-observed.json").read_text())
    if not isinstance(payload, dict):
        raise AssertionError("expected refresh fixture")
    drift_rows = payload["resource_drift"]
    if not isinstance(drift_rows, list):
        raise AssertionError("expected refresh drifts")
    for drift in drift_rows:
        if not isinstance(drift, dict):
            raise AssertionError("expected drift")
        change = drift.get("change")
        if not isinstance(change, dict):
            raise AssertionError("expected drift change")
        match = next(entry for entry in entries if entry["type"] == drift.get("type"))
        for side in ("before", "after"):
            current = change[side]
            if not isinstance(current, dict):
                raise AssertionError("expected drift side")
            change[side] = {**copy.deepcopy(match["attrs"]), **object_map(current)}
    return backend, planned_resources(drift_rows)


class SchedulerEvidence(unittest.TestCase):
    def test_equivalent_empty_defaults_keep_the_configured_group(self) -> None:
        inputs = {"require_distinct_hosts": True, "server_group_id": "own-group"}
        old = [{"group": "own-group", "additional_properties": None}]
        new = [{"group": "own-group", "additional_properties": {}, "same_host": [], "target_cell": ""}]
        self.assertTrue(_scheduler(old, new, inputs, "es"))
        self.assertTrue(_scheduler(new, old, inputs, "es"))

    def test_scheduler_cannot_change_group_or_add_placement_constraints(self) -> None:
        inputs = {"require_distinct_hosts": True, "server_group_id": "own-group"}
        good = [{"group": "own-group"}]
        rejected: tuple[object, ...] = (
            None,
            {},
            [],
            [None],
            good * 2,
            [{}],
            [{"group": "foreign"}],
            [{"group": "own-group", "unknown": ""}],
            [{"group": "own-group", "same_host": ["foreign"]}],
            [{"group": "own-group", "target_cell": False}],
            [{"group": "own-group", "query": ""}],
        )
        for bad in rejected:
            with self.subTest(bad=bad):
                self.assertFalse(_scheduler(good, bad, inputs, "es"))
                self.assertFalse(_scheduler(bad, good, inputs, "es"))
        for policy in (
            {},
            {"require_distinct_hosts": False},
            {"require_distinct_hosts": True},
            {"require_distinct_hosts": True, "server_group_id": 5},
        ):
            self.assertFalse(_scheduler(good, good, policy, "es"))
        self.assertFalse(_scheduler(good, good, inputs, "app"))

    def test_backend_accepts_only_equivalent_scheduler_defaults_with_noop_plan(self) -> None:
        b, drifts = refresh_fixture()
        b.desired["nodes"]["app"]["role"] = "es"
        b.journal["stages"]["app"]["inputs"].update(require_distinct_hosts=True, server_group_id="own-group")
        drift = drifts[1]
        before = drift["change"].get("before")
        after = drift["change"].get("after")
        if not isinstance(before, dict) or not isinstance(after, dict):
            raise AssertionError("expected scheduler drift")
        before["scheduler_hints"] = [{"group": "own-group", "additional_properties": None}]
        after["scheduler_hints"] = [{"group": "own-group", "additional_properties": {}}]
        self.assertTrue(b.allow_refresh("app", drift))
        observed = after["scheduler_hints"]
        if not isinstance(observed, list) or not observed or not isinstance(observed[0], dict):
            raise AssertionError("expected scheduler hint")
        observed[0]["group"] = "foreign"
        self.assertFalse(b.allow_refresh("app", drift))

    def test_structural_evidence_never_contains_arbitrary_values(self) -> None:
        value = [
            {
                "group": "secret-group",
                "query": ["PASSWORD-DO-NOT-SAVE"],
                "additional_properties": {"secret": "TOKEN-DO-NOT-SAVE"},
                "secret-field-name": "hidden",
            }
        ]
        result = hints(value, "secret-group")
        serialized = json.dumps(result)
        for secret in ("secret-group", "PASSWORD", "TOKEN", "secret-field-name"):
            self.assertNotIn(secret, serialized)
        items = result["items"]
        if not isinstance(items, list) or not items or not isinstance(items[0], dict):
            raise AssertionError("expected hint item")
        item = items[0]
        self.assertTrue(item["group_matches_saved"])
        self.assertEqual(item["unknown_fields"], 1)
        self.assertFalse(item["fields"]["query"]["empty"])
        self.assertEqual(hints(None, None), {"type": "NoneType"})
        self.assertEqual(hints([None], "")["items"], [{"type": "NoneType"}])

    def test_guard_saves_scheduler_shapes_before_apply_and_cleanup(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tools") as folder:
            root = Path(folder)
            api = SimpleNamespace(ROOT=root, LOCAL=root / ".local", redact=lambda v: v, _TEST_CYCLE_ID="synthetic")
            inputs = {"server_group_id": "PRIVATE-GROUP", "require_distinct_hosts": True}
            store = StageStore(api, {"stages": {"es1": {"inputs": inputs}}})
            hint: dict[str, object] = {"group": "PRIVATE-GROUP"}
            before: dict[str, object] = {
                "id": "PRIVATE-VM",
                "scheduler_hints": [hint],
                "user_data": "PRIVATE-PASSWORD",
            }
            after = copy.deepcopy(before)
            copied = after["scheduler_hints"]
            if not isinstance(copied, list) or not copied or not isinstance(copied[0], dict):
                raise AssertionError("scheduler hint missing")
            copied[0]["group"] = "PRIVATE-FOREIGN"
            plan = {
                "resource_drift": [
                    {
                        "address": "vm",
                        "type": "openstack_compute_instance_v2",
                        "change": {"actions": ["update"], "before": before, "after": after},
                    }
                ]
            }
            identities = Mock(return_value={"vm": "PRIVATE-VM"})

            def show(name: str, op: str, *args: str) -> str:
                return json.dumps(plan) if op == "show" else ""

            terraform = Mock(side_effect=show)
            with (
                patch.object(store, "identities", identities),
                patch.object(store, "tf", terraform),
                self.assertRaises(ValueError) as caught,
            ):
                store.apply("es1", unchanged=True)
            evidence_name = caught.exception.__dict__["failure_evidence"]
            evidence = (root / str(evidence_name)).read_text()
            self.assertNotIn("PRIVATE-", evidence)
            summary = json.loads(json.loads(evidence)["terraform_error"])
            details = summary["resource_drift"][0]["scheduler_hints"]
            self.assertTrue(details["before"]["items"][0]["group_matches_saved"])
            self.assertFalse(details["after"]["items"][0]["group_matches_saved"])
            self.assertNotIn("apply", [c.args[1] for c in terraform.call_args_list])
            self.assertTrue((root / str(evidence_name)).exists())
            # The real local cleanup keeps public evidence after state/API have
            # been confirmed absent by the isolated test fixture.
            from local_runtime import cleanup_runtime

            cleanup_runtime(root)
            self.assertTrue((root / str(evidence_name)).exists())

    def test_non_scheduler_evidence_remains_minimal(self) -> None:
        created: dict[str, object] = {"password": "SECRET"}
        change: PlannedChange = {"actions": ["create"], "before": None, "after": created}
        plan: PlanView = {"resource_changes": [{"address": "disk", "type": "volume", "change": change}]}
        result = summarize(plan, {})
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertEqual(result["resource_changes"][0]["changed_fields"], ["password"])
