"""Observed read-only refresh must preserve ownership, binding and configured fields."""

import copy
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import selectel_refresh as refresh
from selectel_staged import Backend
from stage_store import check_plan

ROOT = Path(__file__).resolve().parents[1]


def fixture():
    b = Backend.__new__(Backend)
    b.api = SimpleNamespace(PREFIX="otus-dz07")
    b.desired = {"nodes": {"app": {"role": "app"}}}
    inputs = {
        "private_ip": "10.77.0.10",
        "network_id": "own-network",
        "subnet_id": "own-subnet",
        "security_group_id": "own-sg",
    }
    b.journal = {"deployment_id": "synthetic", "stages": {"app": {"inputs": inputs}}}
    rows = [
        (
            "openstack_compute_instance_v2",
            "node",
            None,
            {"id": "own-vm", "name": "otus-dz07-app", "metadata": {"dz07_owner": b.owner}},
        ),
        ("openstack_blockstorage_volume_v3", "boot", None, {"id": "own-disk"}),
        (
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
        ("openstack_networking_floatingip_v2", "app", 0, {"id": "own-fip", "address": "8.8.8.8"}),
        (
            "openstack_networking_floatingip_associate_v2",
            "app",
            0,
            {"id": "association", "floating_ip": "8.8.8.8", "port_id": "own-port"},
        ),
    ]
    entries = [
        {"type": kind, "address": "module.stage." + kind + "." + name, "index": index, "attrs": attrs}
        for kind, name, index, attrs in rows
    ]
    b.store = SimpleNamespace(entries=Mock(return_value=entries))
    drifts = json.loads((ROOT / "tests/fixtures/selectel-refresh-observed.json").read_text())["resource_drift"]
    for drift in drifts:
        entry = next(e for e in entries if e["type"] == drift["type"])
        for side in ("before", "after"):
            drift["change"][side] = {**copy.deepcopy(entry["attrs"]), **drift["change"][side]}
    return b, drifts, entries


class NodeRefreshTests(unittest.TestCase):
    def test_managed_metadata_mismatch_is_rejected_independently_of_plan_fields(self) -> None:
        b, drifts, entries = fixture()
        d = drifts[1]
        d["change"]["before"]["metadata"] = {}
        self.assertFalse(refresh._metadata(d["change"]["before"], d["change"]["after"], b.owner))

    def test_binding_rejects_incomplete_or_mismatched_saved_network(self) -> None:
        b, drifts, entries = fixture()
        d = drifts[3]
        for side in ("before", "after"):
            before, after = copy.deepcopy(d["change"]["before"]), copy.deepcopy(d["change"]["after"])
            (before if side == "before" else after)["network_id"] = "foreign"
            with self.subTest(side=side):
                self.assertFalse(
                    refresh._binding(
                        before, after, entries[0]["attrs"], b.journal["stages"]["app"]["inputs"], {"device_id"}
                    )
                )

    def test_unexplained_scheduler_hints_drift_remains_blocked(self) -> None:
        b, drifts, entries = fixture()
        d = drifts[1]
        d["change"]["after"]["scheduler_hints"] = [{"group": "unconfirmed"}]
        with self.assertRaises(ValueError):
            self.validate(b, [d])

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
            b, drifts, entries = fixture()
            d = drifts[2]
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
                b.journal["stages"]["app"]["inputs"].pop("subnet_id")
            with self.subTest(mutation=mutation):
                self.assertFalse(refresh.allow_node_refresh(b, "app", d))

    def test_malformed_refresh_and_nonempty_tags_are_rejected(self) -> None:
        for mutation in ("kind", "before", "after", "tags-before", "tags-after", "unchanged", "unknown"):
            b, drifts, entries = fixture()
            d = drifts[1]
            if mutation == "kind":
                d["type"] = "unrecognized"
            elif mutation in ("before", "after"):
                d["change"][mutation] = []
            elif mutation.startswith("tags-"):
                d["change"][mutation[5:]]["tags"] = ["unrecognized"]
            elif mutation == "unchanged":
                d["change"]["after"] = copy.deepcopy(d["change"]["before"])
            else:
                d["change"]["after"]["unexpected"] = True
            with self.subTest(mutation=mutation):
                self.assertFalse(refresh.allow_node_refresh(b, "app", d))

    def test_metadata_updates_and_removal_preserve_managed_values(self) -> None:
        b, drifts, entries = fixture()
        d = drifts[1]
        for old, new in ((None, "maintenance"), ("maintenance", "ready"), ("ready", None)):
            for side, value in (("before", old), ("after", new)):
                d["change"][side]["all_metadata"] = {"dz07_owner": b.owner}
                if value is not None:
                    d["change"][side]["all_metadata"]["x_cloud_maintenance"] = value
            self.assertTrue(refresh.allow_node_refresh(b, "app", d))
        d["change"]["before"]["all_metadata"] = None
        self.assertFalse(refresh.allow_node_refresh(b, "app", d))

    def test_dns_schema_accepts_only_saved_address_and_valid_own_hostname(self) -> None:
        b, drifts, entries = fixture()
        d = drifts[3]
        before, after = d["change"]["before"], d["change"]["after"]
        self.assertTrue(refresh._dns(before, after, "10.77.0.10", "otus-dz07-app"))
        cases = [
            ("before", "dns_assignment", None),
            ("after", "dns_assignment", None),
            ("after", "dns_assignment", []),
            ("after", "dns_assignment", [None]),
            ("before", "dns_name", "foreign"),
        ]
        for side, key, value in cases:
            old, new = copy.deepcopy(before), copy.deepcopy(after)
            (old if side == "before" else new)[key] = value
            with self.subTest(key=key, value=value):
                self.assertFalse(refresh._dns(old, new, "10.77.0.10", "otus-dz07-app"))
        for key, value in (
            ("hostname", "foreign"),
            ("unexpected", True),
            ("fqdn", None),
            ("fqdn", "foreign.cloud.test."),
            ("fqdn", "otus-dz07-app.-invalid."),
        ):
            new = copy.deepcopy(after)
            new["dns_assignment"][0][key] = value
            with self.subTest(key=key, value=value):
                self.assertFalse(refresh._dns(before, new, "10.77.0.10", "otus-dz07-app"))

    def test_qos_discovery_requires_initial_binding_and_canonical_uuid(self) -> None:
        identifier = "00000000-0000-4000-8000-000000000004"
        self.assertTrue(refresh._qos(None, identifier))
        self.assertTrue(refresh._qos("", identifier))
        for before, after in ((identifier, identifier), ("", None), ("", "invalid"), ("", identifier.replace("-", ""))):
            with self.subTest(before=before, after=after):
                self.assertFalse(refresh._qos(before, after))

    def test_binding_without_derived_fields_and_partial_previous_binding(self) -> None:
        b, drifts, entries = fixture()
        d = drifts[3]
        for key in ("dns_assignment", "dns_name", "qos_policy_id"):
            d["change"]["after"][key] = copy.deepcopy(d["change"]["before"][key])
        self.assertTrue(refresh.allow_node_refresh(b, "app", d))
        d["change"]["before"]["device_id"] = "own-vm"
        self.assertTrue(refresh.allow_node_refresh(b, "app", d))
        for value in (None, "compute:", "network:router"):
            d["change"]["after"]["device_owner"] = value
            with self.subTest(value=value):
                self.assertFalse(refresh.allow_node_refresh(b, "app", d))

    def validate(self, b, drifts, changes=None) -> None:
        ids = {d["address"]: d["change"]["before"]["id"] for d in drifts}
        changes = (
            changes
            if changes is not None
            else [
                {"address": d["address"], "change": {"actions": ["no-op"], "before": d["change"]["after"]}}
                for d in drifts
            ]
        )
        check_plan(
            {"resource_drift": drifts, "resource_changes": changes},
            ids,
            additions_only=True,
            allow_drift=lambda drift: b.allow_refresh("app", drift),
        )

    def test_observed_plan_refresh_is_accepted_with_all_existing_changes_noop(self) -> None:
        b, drifts, entries = fixture()
        self.validate(b, drifts)

    def test_planned_writes_and_replacements_still_fail_after_allowed_refresh(self) -> None:
        for actions in (["update"], ["delete", "create"], ["delete"]):
            b, drifts, entries = fixture()
            changes = [{"address": drifts[1]["address"], "change": {"actions": actions, "before": {"id": "own-vm"}}}]
            with self.subTest(actions=actions), self.assertRaises(ValueError):
                self.validate(b, drifts, changes)

    def test_unknown_fields_wrong_id_and_missing_saved_identity_fail_closed(self) -> None:
        for index in (1, 2, 3):
            for change in ("id", "unknown", "missing", "address"):
                b, drifts, entries = fixture()
                d = drifts[index]
                if change == "id":
                    d["change"]["after"]["id"] = "foreign"
                elif change == "unknown":
                    d["change"]["after"]["flavor_id"] = "foreign"
                elif change == "missing":
                    entries[:] = [e for e in entries if e["type"] != d["type"]]
                elif change == "address":
                    d["address"] += ".foreign"
                with self.subTest(index=index, change=change), self.assertRaises(ValueError):
                    self.validate(b, [d])

    def test_vm_metadata_can_only_observe_maintenance_and_preserve_owner(self) -> None:
        for change in ("owner", "other-key", "managed-metadata", "remove-owner", "bad-value"):
            b, drifts, entries = fixture()
            d = drifts[1]
            after = d["change"]["after"]
            if change == "owner":
                after["all_metadata"]["dz07_owner"] = "foreign"
            elif change == "other-key":
                after["all_metadata"]["unrecognized"] = "value"
            elif change == "managed-metadata":
                after["metadata"]["dz07_owner"] = "foreign"
            elif change == "remove-owner":
                after["all_metadata"].pop("dz07_owner")
            elif change == "bad-value":
                after["all_metadata"]["x_cloud_maintenance"] = {}
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.validate(b, [d])

    def test_floating_address_cannot_detach_rebind_or_attach_to_internal_es(self) -> None:
        for change in ("detach", "port", "ip", "association", "role", "previous-port"):
            b, drifts, entries = fixture()
            d = drifts[2]
            if change == "detach":
                d["change"]["after"]["port_id"] = ""
            elif change == "port":
                d["change"]["after"]["port_id"] = "foreign"
            elif change == "ip":
                d["change"]["after"]["fixed_ip"] = "10.77.0.99"
            elif change == "association":
                entries[-1]["attrs"]["floating_ip"] = "1.1.1.1"
            elif change == "role":
                b.desired["nodes"]["app"]["role"] = "es"
            elif change == "previous-port":
                d["change"]["before"]["port_id"] = "foreign"
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.validate(b, [d])

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
            b, drifts, entries = fixture()
            d = drifts[3]
            after = d["change"]["after"]
            if change == "device":
                after["device_id"] = "foreign"
            elif change == "owner":
                after["device_owner"] = "network:router_interface"
            elif change == "network":
                after["network_id"] = "foreign"
            elif change == "sg":
                after["security_group_ids"] = []
            elif change == "ip":
                after["fixed_ip"][0]["ip_address"] = "10.77.0.99"
            elif change == "dns-ip":
                after["dns_assignment"][0]["ip_address"] = "10.77.0.99"
            elif change == "dns-name":
                after["dns_name"] = "foreign"
            elif change == "qos":
                after["qos_policy_id"] = "unknown"
            elif change == "previous-qos":
                d["change"]["before"]["qos_policy_id"] = "00000000-0000-4000-8000-000000000009"
            elif change == "already-bound":
                d["change"]["before"].update(device_id="own-vm", device_owner="compute:nova")
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.validate(b, [d])
