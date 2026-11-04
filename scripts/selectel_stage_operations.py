"""Selectel stage execution; all plans and ownership guards retained."""

import ipaddress
import json
from collections.abc import Mapping
from typing import TYPE_CHECKING

import pool_selection as pools
import topology
from cloud_contract import StandDescription, TerraformError
from plan_diagnostics import PlanView
from progress_logging import get_logger
from stage_journal import StageOutput
from staged_deploy import PlacementExhausted, place

if TYPE_CHECKING:
    from selectel_staged import Backend

LOGGER = get_logger(__name__)


def candidate_region(candidate: Mapping[str, object]) -> str:
    region = candidate.get("region")
    if not isinstance(region, str) or not region:
        raise ValueError("Stage placement requires a confirmed pool")
    return region


def ensure_pool(backend: "Backend", candidate: Mapping[str, object]) -> StageOutput:
    pool = candidate_region(candidate)
    network = "network-" + pool
    new_network = not backend.completed(network)
    if new_network:
        from selectel_staged_capacity import require_network_capacity

        require_network_capacity(backend, pool)
    choice = {k: candidate[k] for k in ("region", "network_availability_zone", "router_availability_zone")}
    network_choices = [choice]
    for options in backend.choices.values():
        for item in options:
            if item["region"] == pool:
                domain = {k: item[k] for k in ("region", "network_availability_zone", "router_availability_zone")}
                if domain not in network_choices:
                    network_choices.append(domain)
    try:
        place(backend, network, network_choices)
    except PlacementExhausted as error:
        if backend.store.identities(network):
            raise
        raise pools.CandidateUnavailable("No network scheduling candidate succeeded in " + pool) from error
    if new_network:
        backend._temporary_networks.add(network)
    return backend.journal["stages"][network]["output"]


def connect_pool(backend: "Backend", pool: str) -> None:
    connected = {
        candidate_region(record["candidate"])
        for name, record in backend.journal["stages"].items()
        if name.startswith("connection-") and record["status"] != "deleted"
    }
    router = backend.saved_id("global", "selectel_global_router_router_v1", "router_id")
    backend.global_router.preflight(sorted(connected | {pool}), backend.api.PREFIX, router)
    place(backend, "global", [{"region": backend.credentials["region"]}])
    place(backend, "connection-" + pool, [{"region": pool}])


def inputs(backend: "Backend", name: str) -> tuple[str, dict[str, object]]:
    candidate = backend.journal["stages"][name]["candidate"]
    pool = candidate_region(candidate)
    label = backend.api.PREFIX + "-" + name
    slots = backend.journal["address_policy"]
    if name == "global":
        return "global", {"name": label, "owner": backend.owner}
    if name.startswith("network-"):
        cidr = backend.journal["pool_subnets"][pool]
        return "network", {
            "region": pool,
            "name": label,
            "cidr": cidr,
            "owner": backend.owner,
            "cloud_gateway_host": slots["cloud_gateway"],
            "global_gateway_host": slots["global_gateway"],
            "dhcp_first_host": slots["dhcp_first"],
            "dhcp_last_host": slots["dhcp_last"],
            "network_zone": candidate["network_availability_zone"],
            "router_zone": candidate["router_availability_zone"],
            "admin_cidrs": list(backend.journal["admin_cidrs"]),
            "kibana_port": topology.services(backend.root)["kibana_port"],
            "peer_cidrs": [v for k, v in backend.journal["pool_subnets"].items() if k != pool],
        }
    if name.startswith("connection-"):
        network = backend.journal["stages"]["network-" + pool]["output"]
        return "connection", {
            "region": pool,
            "name": label,
            "owner": backend.owner,
            "project_id": backend.credentials["project_id"],
            "router_id": backend.journal["stages"]["global"]["output"]["router_id"],
            "os_network_id": network["network_id"],
            "os_subnet_id": network["subnet_id"],
            "cidr": network["cidr"],
            "global_gateway_host": slots["global_gateway"],
            "service_hosts": slots["service_hosts"],
        }
    network = backend.ensure_pool(candidate)
    role = backend.desired["nodes"][name]["role"]
    if not backend.saved_id(name, "openstack_compute_instance_v2", "id"):
        backend.check_node_quota(candidate, role)
    secrets_path = backend.root / ".local/secrets.json"
    passwords = json.loads(secrets_path.read_text())
    return "node", {
        "region": pool,
        "owner": backend.owner,
        "node_name": name,
        "role": role,
        "name_prefix": backend.api.PREFIX,
        "require_distinct_hosts": backend.journal.get("require_distinct_hosts", True),
        "zone": candidate["availability_zone"],
        "private_ip": str(ipaddress.ip_network(network["cidr"])[backend.desired["nodes"][name]["host"]]),
        "subnet_cidr": network["cidr"],
        "subnet_gateway": network["gateway_ip"],
        "image_id": candidate["image_id"],
        "flavor_id": candidate["flavor_ids"][role],
        "volume_type": candidate["volume_type"],
        "boot_disk_gb": backend.policy["boot_disk_gb"],
        "network_id": network["network_id"],
        "subnet_id": network["subnet_id"],
        "security_group_id": network["security_group_id"],
        "server_group_id": network["server_group_id"],
        "external_network_name": network["external_network_name"],
        "ssh_public_key_path": str(backend.root / ".local/otus_dz07.pub"),
        "route_probe_key": passwords["api"],
    }


def configure_stage(backend: "Backend", name: str, kind: str, inputs: Mapping[str, object]) -> None:
    if kind not in ("network", "node", "global", "connection"):
        raise ValueError("Unknown Selectel stage module")
    provider = "selectel" if kind in ("global", "connection") else "openstack"
    source = "selectel/selectel" if provider == "selectel" else "terraform-provider-openstack/openstack"
    backend.store.configure(
        name, backend.root / "terraform-selectel/staged" / kind, inputs, providers={provider: {"source": source}}
    )


def extend_admin_access(backend: "Backend", lab: StandDescription, cidr: str) -> list[str]:
    pending = backend.journal.get("admin_access_pending")
    if pending is not None and pending != cidr:
        raise ValueError("Finish the pending administrative access change first")
    if cidr in backend.journal["admin_cidrs"] and pending is None:
        return backend.journal["admin_cidrs"]
    name = "network-" + lab["node_regions"]["app"]
    record = backend.journal["stages"][name]
    if cidr not in backend.journal["admin_cidrs"]:
        backend.journal["admin_cidrs"].append(cidr)
    backend.journal["admin_access_pending"] = cidr
    record["inputs"]["admin_cidrs"] = list(backend.journal["admin_cidrs"])
    backend.save()
    backend.configure_stage(name, "network", record["inputs"])

    def validate(plan: PlanView) -> None:
        from operator_network import validate_access_plan

        for change in plan.get("resource_changes", []):
            if change.get("mode") != "data" and change["change"]["actions"] != ["no-op"]:
                validate_access_plan(change, cidr, record["output"]["security_group_id"])

    backend.store.apply(name, validate_extra=validate)
    backend.finish(name)
    backend.journal.pop("admin_access_pending", None)
    backend.save()
    return backend.journal["admin_cidrs"]


def apply(backend: "Backend", name: str) -> StageOutput:
    if name in backend.desired["nodes"]:
        from selectel_staged_audit import faults

        known_fault = json.dumps(faults(backend, name)).lower()
        if any(marker in known_fault for marker in ("no valid host", "novalidhost", "not enough hosts")):
            raise TerraformError("No valid host in the saved failed attempt")
    kind, inputs = backend.inputs(name)
    record = backend.journal["stages"][name]
    record["kind"], record["inputs"] = kind, inputs
    backend.save()
    backend.configure_stage(name, kind, inputs)
    output = backend.store.apply(name)
    record["output"] = output
    backend.save()
    from selectel_staged_audit import assert_stage_owned

    assert_stage_owned(backend, name, active=True)
    return output


def finish(backend: "Backend", name: str) -> None:
    record = backend.journal["stages"][name]
    record["identities"] = backend.store.identities(name)
    record["lineage"] = backend.store.state(name).get("lineage")
    record["status"] = "active"
    backend.save()
    if name in backend.desired["nodes"]:
        backend.connect_pool(candidate_region(record["candidate"]))


def reapply(backend: "Backend", name: str) -> None:
    from selectel_staged_audit import assert_stage_owned

    assert_stage_owned(backend, name, active=True)
    record = backend.journal["stages"][name]
    backend.configure_stage(name, record["kind"], record["inputs"])
    record["output"] = backend.store.apply(name, unchanged=True)
    backend.save()
    if name in backend.desired["nodes"]:
        backend.connect_pool(candidate_region(record["candidate"]))
