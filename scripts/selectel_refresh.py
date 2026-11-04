"""Selectel/OpenStack discovery of bindings after independent resources are created.

These predicates accept read-only observations, never Terraform writes. StageStore
separately checks identities and requires no-op for every existing planned resource.
"""

import json
import re
import uuid
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from plan_diagnostics import PlannedResource
from terraform_state_types import StateEntry

if TYPE_CHECKING:
    from selectel_staged import Backend

VM = "openstack_compute_instance_v2"
PORT = "openstack_networking_port_v2"
FIP = "openstack_networking_floatingip_v2"
ASSOCIATION = "openstack_networking_floatingip_associate_v2"
NODE_KINDS = {VM, PORT, FIP}


def string_set(value: object) -> set[str] | None:
    """Provider membership and tag fields are nullable lists of strings."""
    if value is None:
        return set()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return None
    return set(value)


def _scheduler(before: object, after: object, inputs: Mapping[str, object], role: str) -> bool:
    """Only equivalent empty defaults around the same configured group."""
    if role != "es" or inputs.get("require_distinct_hosts") is not True:
        return False
    group = inputs.get("server_group_id")
    if not isinstance(group, str) or not group:
        return False
    defaults = {
        "different_host": [],
        "same_host": [],
        "query": [],
        "different_cell": [],
        "target_cell": "",
        "build_near_host_ip": "",
        "additional_properties": {},
    }
    for value in (before, after):
        if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
            return False
        row = value[0]
        if row.get("group") != group or set(row) - {"group", *defaults}:
            return False
        if any(
            row.get(key) is not None and (type(row[key]) is not type(empty) or row[key] != empty)
            for key, empty in defaults.items()
        ):
            return False
    return True


def _one(entries: Sequence[StateEntry], kind: str) -> StateEntry | None:
    matches = [e for e in entries if e["type"] == kind]
    return matches[0] if len(matches) == 1 else None


def _address(entry: StateEntry) -> str:
    address = entry.get("address", "").lstrip(".")
    if entry.get("index") is not None:
        address += "[" + json.dumps(entry["index"]) + "]"
    return address


def _metadata(before: Mapping[str, object], after: Mapping[str, object], owner: str) -> bool:
    managed = {"dz07_owner": owner}
    if before.get("metadata") != managed or after.get("metadata") != managed:
        return False
    for value in (before.get("all_metadata"), after.get("all_metadata")):
        if (
            not isinstance(value, dict)
            or value.get("dz07_owner") != owner
            or set(value) - {"dz07_owner", "x_cloud_maintenance"}
            or any(not isinstance(v, str) for v in value.values())
        ):
            return False
    return True


def _port_contract(port: Mapping[str, object], inputs: Mapping[str, object]) -> bool:
    required = ("network_id", "subnet_id", "security_group_id", "private_ip")
    if not all(isinstance(inputs.get(key), str) and inputs[key] for key in required):
        return False
    return (
        port.get("network_id") == inputs["network_id"]
        and port.get("security_group_ids") == [inputs["security_group_id"]]
        and port.get("fixed_ip") == [{"subnet_id": inputs["subnet_id"], "ip_address": inputs["private_ip"]}]
    )


def _floating(
    before: Mapping[str, object],
    after: Mapping[str, object],
    entry: Mapping[str, object],
    port: Mapping[str, object],
    association: StateEntry | None,
    inputs: Mapping[str, object],
    role: str,
) -> bool:
    if role != "app" or association is None or not _port_contract(port, inputs):
        return False
    expected = {"port_id": port["id"], "fixed_ip": inputs["private_ip"]}
    return (
        bool(entry.get("address"))
        and before.get("address") == after.get("address") == entry["address"]
        and association["attrs"].get("floating_ip") == entry["address"]
        and association["attrs"].get("port_id") == port["id"]
        and all(after.get(k) == v and before.get(k) in (None, "", v) for k, v in expected.items())
    )


def _dns(before: Mapping[str, object], after: Mapping[str, object], address: object, name: object) -> bool:
    if not isinstance(address, str) or not isinstance(name, str):
        return False
    old, new = before.get("dns_assignment"), after.get("dns_assignment")
    if (
        before.get("dns_name") not in (None, "", name)
        or after.get("dns_name") != name
        or not isinstance(old, list)
        or not isinstance(new, list)
        or len(new) != 1
        or any(not isinstance(row, dict) or row.get("ip_address") != address for row in old + new)
    ):
        return False
    row = new[0]
    fqdn = row.get("fqdn")
    return (
        not set(row) - {"ip_address", "hostname", "fqdn"}
        and row.get("hostname") == name
        and isinstance(fqdn, str)
        and fqdn.rstrip(".").startswith(name + ".")
        and all(
            re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", part) for part in fqdn.rstrip(".").split(".")
        )
    )


def _qos(before: object, after: object) -> bool:
    # Only discover the previously unknown policy at initial Nova port binding.
    # Replacing/removing an already observed policy is not a deployment operation.
    if before not in (None, "") or not isinstance(after, str):
        return False
    try:
        return str(uuid.UUID(after)) == after
    except ValueError:
        return False


def _binding(
    before: Mapping[str, object],
    after: Mapping[str, object],
    vm: Mapping[str, object],
    inputs: Mapping[str, object],
    changed: set[str],
) -> bool:
    if not _port_contract(before, inputs) or not _port_contract(after, inputs):
        return False
    owner = after.get("device_owner")
    if (
        after.get("device_id") != vm["id"]
        or before.get("device_id") not in (None, "", vm["id"])
        or not isinstance(owner, str)
        or not re.fullmatch(r"compute:[A-Za-z0-9_-]+", owner)
        or before.get("device_owner") not in (None, "", owner)
    ):
        return False
    derived = changed & {"dns_name", "dns_assignment", "qos_policy_id"}
    if derived and (before.get("device_id") not in (None, "") or before.get("device_owner") not in (None, "")):
        return False
    if derived & {"dns_name", "dns_assignment"} and not _dns(before, after, inputs["private_ip"], vm["name"]):
        return False
    return "qos_policy_id" not in changed or _qos(before.get("qos_policy_id"), after.get("qos_policy_id"))


def allow_node_refresh(backend: "Backend", name: str, drift: PlannedResource) -> bool:
    kind = drift["type"]
    if kind not in NODE_KINDS:
        return False
    before, after = drift["change"].get("before"), drift["change"].get("after")
    if not isinstance(before, dict) or not isinstance(after, dict):
        return False
    changed = {k for k in before.keys() | after.keys() if before.get(k) != after.get(k)}
    if "tags" in changed:
        if before.get("tags") not in (None, []) or after.get("tags") not in (None, []):
            return False
        changed.remove("tags")
    allowed = {
        VM: {"all_metadata", "scheduler_hints"},
        FIP: {"port_id", "fixed_ip"},
        PORT: {"device_id", "device_owner", "dns_assignment", "dns_name", "qos_policy_id"},
    }
    if not changed or changed - allowed[kind]:
        return False
    entries = backend.store.entries(name)
    target, vm = _one(entries, kind), _one(entries, VM)
    if (
        target is None
        or vm is None
        or not vm["attrs"].get("id")
        or _address(target) != drift["address"]
        or not target["attrs"].get("id")
        or before.get("id") != after.get("id")
        or after.get("id") != target["attrs"]["id"]
        or vm["attrs"].get("name") != backend.api.PREFIX + "-" + name
        or vm["attrs"].get("metadata") != {"dz07_owner": backend.owner}
    ):
        return False
    if kind == VM:
        inputs = backend.journal["stages"][name].get("inputs", {})
        return _metadata(before, after, backend.owner) and (
            "scheduler_hints" not in changed
            or _scheduler(
                before.get("scheduler_hints"),
                after.get("scheduler_hints"),
                inputs,
                backend.desired["nodes"][name]["role"],
            )
        )
    port = _one(entries, PORT)
    if port is None:
        return False
    inputs = backend.journal["stages"][name].get("inputs", {})
    if kind == FIP:
        return _floating(
            before,
            after,
            target["attrs"],
            port["attrs"],
            _one(entries, ASSOCIATION),
            inputs,
            backend.desired["nodes"][name]["role"],
        )
    return _binding(before, after, vm["attrs"], inputs, changed)
