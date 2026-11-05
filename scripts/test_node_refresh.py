"""Observed read-only refresh must preserve ownership, binding and configured fields."""

import copy
import json
import unittest
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, cast

import selectel_refresh as refresh
from plan_diagnostics import PlannedResource
from selectel_staged import Backend
from stage_store import StageStore, check_plan
from terraform_state_types import StateEntry

ROOT = Path(__file__).resolve().parents[1]


def object_dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(type(key) is not str for key in value):
        raise AssertionError(value)
    return cast(dict[str, object], value)


def dict_rows(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise AssertionError(value)
    rows: list[dict[str, object]] = []
    for item in value:
        rows.append(object_dict(item))
    return rows


def changed(drift: PlannedResource, side: Literal["before", "after"]) -> dict[str, object]:
    value = drift["change"]["before"] if side == "before" else drift["change"]["after"]
    return object_dict(value)


def change_mapping(drift: PlannedResource) -> dict[str, object]:
    change = drift["change"]
    if not isinstance(change, dict) or any(type(key) is not str for key in change):
        raise AssertionError(change)
    return cast(dict[str, object], change)


class EntryStore(StageStore):
    def __init__(self, rows: list[StateEntry]) -> None:
        self.rows = rows

    def entries(self, name: str) -> list[StateEntry]:
        return self.rows


def load_drifts(entries: Sequence[StateEntry]) -> list[PlannedResource]:
    document = json.loads((ROOT / "tests/fixtures/selectel-refresh-observed.json").read_text())
    if not isinstance(document, dict):
        raise AssertionError(document)
    rows = document.get("resource_drift")
    if not isinstance(rows, list):
        raise AssertionError(rows)
    drifts: list[PlannedResource] = []
    for row in rows:
        if not isinstance(row, dict):
            raise AssertionError(row)
        address, kind, change = row.get("address"), row.get("type"), row.get("change")
        if not isinstance(address, str) or not isinstance(kind, str) or not isinstance(change, dict):
            raise AssertionError(row)
        actions = change.get("actions")
        if not isinstance(actions, list) or any(type(action) is not str for action in actions):
            raise AssertionError(actions)
        planned_actions = [action for action in actions if isinstance(action, str)]
        entry = next(item for item in entries if item["type"] == kind)
        before = {**copy.deepcopy(entry["attrs"]), **object_dict(change.get("before"))}
        after = {**copy.deepcopy(entry["attrs"]), **object_dict(change.get("after"))}
        drifts.append(
            {
                "address": address,
                "type": kind,
                "change": {"actions": planned_actions, "before": before, "after": after},
            }
        )
    return drifts


def fixture() -> tuple[Backend, list[PlannedResource], list[StateEntry]]:
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
    disk_attrs: dict[str, object] = {"id": "own-disk"}
    fixed_ip: list[dict[str, object]] = [{"subnet_id": "own-subnet", "ip_address": "10.77.0.10"}]
    port_attrs: dict[str, object] = {
        "id": "own-port",
        "name": "otus-dz07-app",
        "network_id": "own-network",
        "security_group_ids": ["own-sg"],
        "fixed_ip": fixed_ip,
    }
    floating_attrs: dict[str, object] = {"id": "own-fip", "address": "8.8.8.8"}
    association_attrs: dict[str, object] = {"id": "association", "floating_ip": "8.8.8.8", "port_id": "own-port"}
    rows: list[tuple[str, str, int | None, dict[str, object]]] = [
        ("openstack_compute_instance_v2", "node", None, vm_attrs),
        ("openstack_blockstorage_volume_v3", "boot", None, disk_attrs),
        ("openstack_networking_port_v2", "node", None, port_attrs),
        ("openstack_networking_floatingip_v2", "app", 0, floating_attrs),
        ("openstack_networking_floatingip_associate_v2", "app", 0, association_attrs),
    ]
    entries: list[StateEntry] = [
        {"type": kind, "address": "module.stage." + kind + "." + name, "index": index, "attrs": attrs}
        for kind, name, index, attrs in rows
    ]
    backend.store = EntryStore(entries)
    return backend, load_drifts(entries), entries


class NodeRefreshTests(unittest.TestCase):
    def test_managed_metadata_mismatch_is_rejected_independently_of_plan_fields(self) -> None:
        backend, drifts, entries = fixture()
        drift = drifts[1]
        changed(drift, "before")["metadata"] = {}
        self.assertFalse(refresh._metadata(changed(drift, "before"), changed(drift, "after"), backend.owner))

    def test_binding_rejects_incomplete_or_mismatched_saved_network(self) -> None:
        backend, drifts, entries = fixture()
        drift = drifts[3]
        for side in ("before", "after"):
            before, after = copy.deepcopy(changed(drift, "before")), copy.deepcopy(changed(drift, "after"))
            (before if side == "before" else after)["network_id"] = "foreign"
            with self.subTest(side=side):
                self.assertFalse(
                    refresh._binding(
                        before,
                        after,
                        entries[0]["attrs"],
                        object_dict(backend.journal["stages"]["app"].get("inputs")),
                        {"device_id"},
                    )
                )

    def test_unexplained_scheduler_hints_drift_remains_blocked(self) -> None:
        backend, drifts, entries = fixture()
        drift = drifts[1]
        changed(drift, "after")["scheduler_hints"] = [{"group": "unconfirmed"}]
        with self.assertRaises(ValueError):
            self.validate(backend, [drift])

    def test_refresh_requires_complete_saved_ownership(self) -> None:
        for mutation in (
            "duplicate-vm",
            "empty-vm-id",
            "empty-target-id",
            "vm-name",
            "vm-owner",
            "missing-port",
            "missing-association",
            "missing-input",
        ):
            backend, drifts, entries = fixture()
            drift = drifts[2]
            if mutation == "duplicate-vm":
                entries.append(copy.deepcopy(entries[0]))
            elif mutation == "empty-vm-id":
                entries[0]["attrs"]["id"] = ""
            elif mutation == "empty-target-id":
                entries[3]["attrs"]["id"] = ""
            elif mutation == "vm-name":
                entries[0]["attrs"]["name"] = "foreign"
            elif mutation == "vm-owner":
                entries[0]["attrs"]["metadata"] = {}
            elif mutation == "missing-port":
                entries.pop(2)
            elif mutation == "missing-association":
                entries.pop()
            else:
                object_dict(backend.journal["stages"]["app"].get("inputs")).pop("subnet_id")
            with self.subTest(mutation=mutation):
                self.assertFalse(refresh.allow_node_refresh(backend, "app", drift))

    def test_malformed_refresh_and_nonempty_tags_are_rejected(self) -> None:
        for mutation in ("kind", "before", "after", "tags-before", "tags-after", "unchanged", "unknown"):
            backend, drifts, entries = fixture()
            drift = drifts[1]
            if mutation == "kind":
                drift["type"] = "unrecognized"
            elif mutation in ("before", "after"):
                change_mapping(drift)[mutation] = []
            elif mutation == "tags-before":
                changed(drift, "before")["tags"] = ["unrecognized"]
            elif mutation == "tags-after":
                changed(drift, "after")["tags"] = ["unrecognized"]
            elif mutation == "unchanged":
                drift["change"]["after"] = copy.deepcopy(changed(drift, "before"))
            else:
                changed(drift, "after")["unexpected"] = True
            with self.subTest(mutation=mutation):
                self.assertFalse(refresh.allow_node_refresh(backend, "app", drift))

    def test_metadata_updates_and_removal_preserve_managed_values(self) -> None:
        backend, drifts, entries = fixture()
        drift = drifts[1]
        for old, new in ((None, "maintenance"), ("maintenance", "ready"), ("ready", None)):
            for side in ("before", "after"):
                value = old if side == "before" else new
                metadata: dict[str, object] = {"dz07_owner": backend.owner}
                if value is not None:
                    metadata["x_cloud_maintenance"] = value
                changed(drift, side)["all_metadata"] = metadata
            self.assertTrue(refresh.allow_node_refresh(backend, "app", drift))
        changed(drift, "before")["all_metadata"] = None
        self.assertFalse(refresh.allow_node_refresh(backend, "app", drift))

    def test_dns_schema_accepts_only_saved_address_and_valid_own_hostname(self) -> None:
        backend, drifts, entries = fixture()
        drift = drifts[3]
        before, after = changed(drift, "before"), changed(drift, "after")
        self.assertTrue(refresh._dns(before, after, "10.77.0.10", "otus-dz07-app"))
        cases: tuple[tuple[Literal["before", "after"], str, object], ...] = (
            ("before", "dns_assignment", None),
            ("after", "dns_assignment", None),
            ("after", "dns_assignment", []),
            ("after", "dns_assignment", [None]),
            ("before", "dns_name", "foreign"),
        )
        for side, key, value in cases:
            old, new = copy.deepcopy(before), copy.deepcopy(after)
            (old if side == "before" else new)[key] = value
            with self.subTest(key=key, value=value):
                self.assertFalse(refresh._dns(old, new, "10.77.0.10", "otus-dz07-app"))
        updates: tuple[tuple[str, object], ...] = (
            ("hostname", "foreign"),
            ("unexpected", True),
            ("fqdn", None),
            ("fqdn", "foreign.cloud.test."),
            ("fqdn", "otus-dz07-app.-invalid."),
        )
        for key, updated in updates:
            new = copy.deepcopy(after)
            dict_rows(new["dns_assignment"])[0][key] = updated
            with self.subTest(key=key, value=updated):
                self.assertFalse(refresh._dns(before, new, "10.77.0.10", "otus-dz07-app"))

    def test_qos_discovery_requires_initial_binding_and_canonical_uuid(self) -> None:
        identifier = "00000000-0000-4000-8000-000000000004"
        self.assertTrue(refresh._qos(None, identifier))
        self.assertTrue(refresh._qos("", identifier))
        for previous, following in (
            (identifier, identifier),
            ("", None),
            ("", "invalid"),
            ("", identifier.replace("-", "")),
        ):
            with self.subTest(before=previous, after=following):
                self.assertFalse(refresh._qos(previous, following))

    def test_binding_without_derived_fields_and_partial_previous_binding(self) -> None:
        backend, drifts, entries = fixture()
        drift = drifts[3]
        for key in ("dns_assignment", "dns_name", "qos_policy_id"):
            changed(drift, "after")[key] = copy.deepcopy(changed(drift, "before")[key])
        self.assertTrue(refresh.allow_node_refresh(backend, "app", drift))
        changed(drift, "before")["device_id"] = "own-vm"
        self.assertTrue(refresh.allow_node_refresh(backend, "app", drift))
        for value in (None, "compute:", "network:router"):
            changed(drift, "after")["device_owner"] = value
            with self.subTest(value=value):
                self.assertFalse(refresh.allow_node_refresh(backend, "app", drift))

    def validate(
        self, backend: Backend, drifts: list[PlannedResource], changes: list[PlannedResource] | None = None
    ) -> None:
        ids: dict[str, str] = {}
        for drift in drifts:
            identifier = changed(drift, "before")["id"]
            if not isinstance(identifier, str):
                raise AssertionError(identifier)
            ids[drift["address"]] = identifier
        if changes is None:
            changes = [
                {
                    "address": drift["address"],
                    "change": {"actions": ["no-op"], "before": changed(drift, "after")},
                }
                for drift in drifts
            ]
        check_plan(
            {"resource_drift": drifts, "resource_changes": changes},
            ids,
            additions_only=True,
            allow_drift=lambda drift: backend.allow_refresh("app", drift),
        )

    def test_observed_plan_refresh_is_accepted_with_all_existing_changes_noop(self) -> None:
        backend, drifts, entries = fixture()
        self.validate(backend, drifts)

    def test_planned_writes_and_replacements_still_fail_after_allowed_refresh(self) -> None:
        for actions in (["update"], ["delete", "create"], ["delete"]):
            backend, drifts, entries = fixture()
            before: dict[str, object] = {"id": "own-vm"}
            changes: list[PlannedResource] = [
                {"address": drifts[1]["address"], "change": {"actions": list(actions), "before": before}}
            ]
            with self.subTest(actions=actions), self.assertRaises(ValueError):
                self.validate(backend, drifts, changes)

    def test_unknown_fields_wrong_id_and_missing_saved_identity_fail_closed(self) -> None:
        for index in (1, 2, 3):
            for change in ("id", "unknown", "missing", "address"):
                backend, drifts, entries = fixture()
                drift = drifts[index]
                if change == "id":
                    changed(drift, "after")["id"] = "foreign"
                elif change == "unknown":
                    changed(drift, "after")["flavor_id"] = "foreign"
                elif change == "missing":
                    entries[:] = [entry for entry in entries if entry["type"] != drift.get("type")]
                elif change == "address":
                    drift["address"] += ".foreign"
                with self.subTest(index=index, change=change), self.assertRaises(ValueError):
                    self.validate(backend, [drift])

    def test_vm_metadata_can_only_observe_maintenance_and_preserve_owner(self) -> None:
        for change in ("owner", "other-key", "managed-metadata", "remove-owner", "bad-value"):
            backend, drifts, entries = fixture()
            drift = drifts[1]
            after = changed(drift, "after")
            if change == "owner":
                object_dict(after["all_metadata"])["dz07_owner"] = "foreign"
            elif change == "other-key":
                object_dict(after["all_metadata"])["unrecognized"] = "value"
            elif change == "managed-metadata":
                object_dict(after["metadata"])["dz07_owner"] = "foreign"
            elif change == "remove-owner":
                object_dict(after["all_metadata"]).pop("dz07_owner")
            elif change == "bad-value":
                object_dict(after["all_metadata"])["x_cloud_maintenance"] = {}
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.validate(backend, [drift])

    def test_floating_address_cannot_detach_rebind_or_attach_to_internal_es(self) -> None:
        for change in ("detach", "port", "ip", "association", "role", "previous-port"):
            backend, drifts, entries = fixture()
            drift = drifts[2]
            if change == "detach":
                changed(drift, "after")["port_id"] = ""
            elif change == "port":
                changed(drift, "after")["port_id"] = "foreign"
            elif change == "ip":
                changed(drift, "after")["fixed_ip"] = "10.77.0.99"
            elif change == "association":
                entries[-1]["attrs"]["floating_ip"] = "1.1.1.1"
            elif change == "role":
                backend.desired["nodes"]["app"]["role"] = "es"
            elif change == "previous-port":
                changed(drift, "before")["port_id"] = "foreign"
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.validate(backend, [drift])

    def test_port_binding_dns_qos_and_network_cannot_change_existing_contract(self) -> None:
        for change in (
            "device",
            "owner",
            "network",
            "sg",
            "ip",
            "dns-ip",
            "dns-name",
            "qos",
            "previous-qos",
            "already-bound",
        ):
            backend, drifts, entries = fixture()
            drift = drifts[3]
            after = changed(drift, "after")
            if change == "device":
                after["device_id"] = "foreign"
            elif change == "owner":
                after["device_owner"] = "network:router_interface"
            elif change == "network":
                after["network_id"] = "foreign"
            elif change == "sg":
                after["security_group_ids"] = []
            elif change == "ip":
                dict_rows(after["fixed_ip"])[0]["ip_address"] = "10.77.0.99"
            elif change == "dns-ip":
                dict_rows(after["dns_assignment"])[0]["ip_address"] = "10.77.0.99"
            elif change == "dns-name":
                after["dns_name"] = "foreign"
            elif change == "qos":
                after["qos_policy_id"] = "unknown"
            elif change == "previous-qos":
                changed(drift, "before")["qos_policy_id"] = "00000000-0000-4000-8000-000000000009"
            elif change == "already-bound":
                changed(drift, "before").update(device_id="own-vm", device_owner="compute:nova")
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.validate(backend, [drift])
