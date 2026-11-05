"""Find a quota-feasible assignment before allocating any stage."""

from typing import TYPE_CHECKING

from placement_config import checked
from pool_selection import CandidateUnavailable
from quota_types import QuotaRecord, checked_quota
from selectel_api import json_object, json_object_or_empty

if TYPE_CHECKING:
    from selectel_staged import Backend

type QuotaCredits = dict[str | tuple[str | None, str], int]
type QuotaBudget = dict[tuple[str, str] | tuple[str, str | None, str], int]


def require_named_quota(item: object, needed: int, label: str, credit: int = 0) -> None:
    from pool_selection import capacity

    try:
        capacity(with_credit(item, credit), needed)
    except CandidateUnavailable as error:
        raise CandidateUnavailable(label + ": " + str(error)) from error


NETWORK_PORT_RESERVE = 6  # Conservative service-port reserve, excluding VM ports.


def network_port_demand(backend: "Backend", pool: str) -> int:
    if backend.completed("network-" + pool):
        return 0
    entries = backend.store.entries("network-" + pool)
    if any(e["type"] == "openstack_networking_network_v2" for e in entries):
        return 0  # Preserve the existing resume policy for an allocated network.
    return NETWORK_PORT_RESERVE


def free(item: object) -> int:
    if item is None:
        raise CandidateUnavailable("Required quota is unknown")
    item = checked_quota(item)
    if item["limit"] == -1:
        return 10**15
    return item["limit"] - item.get("in_use", item.get("used", 0)) - item.get("reserved", 0)


def pending_credits(backend: "Backend", pool: str) -> QuotaCredits:
    quota_credits: QuotaCredits = {}
    for name, node in backend.desired["nodes"].items():
        if backend.completed(name) or name not in backend.journal["stages"]:
            continue
        record = backend.journal["stages"][name]
        candidate = record["candidate"]
        if candidate["region"] != pool:
            continue
        profile = backend.policy["profiles"][node["role"]]
        for entry in backend.store.entries(name):
            if entry["type"] == "openstack_compute_instance_v2":
                for key, amount in [("instances", 1), ("cores", profile["vcpus"]), ("ram", profile["ram_mb"])]:
                    index = (candidate["availability_zone"], key)
                    quota_credits[index] = quota_credits.get(index, 0) + amount
            if entry["type"] == "openstack_blockstorage_volume_v3":
                size = entry["attrs"]["size"]
                if type(size) is not int or size < 0:
                    raise CandidateUnavailable("Pending volume size is unconfirmed")
                for key, amount in [
                    ("volumes", 1),
                    ("gigabytes", size),
                    ("gigabytes_" + candidate["volume_type"], size),
                ]:
                    quota_credits[key] = quota_credits.get(key, 0) + amount
            if entry["type"] in ("openstack_networking_port_v2", "openstack_networking_floatingip_v2"):
                key = "port" if entry["type"] == "openstack_networking_port_v2" else "floatingip"
                quota_credits[key] = quota_credits.get(key, 0) + 1
    return quota_credits


def with_credit(item: object, credit: int) -> QuotaRecord:
    if item is None:
        raise CandidateUnavailable("Required quota is unknown")
    value = checked_quota(item).copy()
    value["in_use"] = max(0, value.get("in_use", value.get("used", 0)) - credit)
    return value


def require_capacity(backend: "Backend") -> set[str]:
    pending = [n for n in backend.policy["sequential_placement"]["node_order"] if not backend.completed(n)]
    if not pending:
        return set()
    project = backend.credentials["project_id"]
    budgets: QuotaBudget = {}
    network_available: dict[str, bool] = {}
    for pool in backend.policy["pools"]:
        cloud = backend.region(pool)
        quota_set = json_object(
            cloud.get("compute", "/os-quota-sets/" + project + "/detail")["quota_set"],
            "Unknown compute quota payload",
        )
        compute = json_object_or_empty(quota_set.get("az_quotas"), "Unknown compute quota map")
        volumes = json_object(
            cloud.get("volumev3", "/os-quota-sets/" + project + "?usage=True")["quota_set"],
            "Unknown volume quota payload",
        )
        network = json_object(
            cloud.get("network", "/v2.0/quotas/" + project + "/details.json")["quota"],
            "Unknown network quota payload",
        )
        quota_credits = pending_credits(backend, pool)
        for zone, values in compute.items():
            zone_quotas = json_object(values, "Unknown compute segment quota payload")
            for key in ("instances", "cores", "ram"):
                budgets[(pool, zone, key)] = free(with_credit(zone_quotas.get(key), quota_credits.get((zone, key), 0)))
        for key, value in volumes.items():
            if isinstance(value, dict) and "limit" in value:
                budgets[(pool, key)] = free(with_credit(value, quota_credits.get(key, 0)))
        try:
            require_network_capacity(backend, pool)
            network_available[pool] = True
        except CandidateUnavailable:
            network_available[pool] = False
        budgets[(pool, "ports")] = free(with_credit(network.get("port"), quota_credits.get("port", 0)))
        budgets[(pool, "floatingip")] = free(with_credit(network.get("floatingip"), quota_credits.get("floatingip", 0)))
        budgets[(pool, "network_ports")] = network_port_demand(backend, pool)
    pending = [n for n in backend.policy["sequential_placement"]["node_order"] if not backend.completed(n)]
    disk = backend.policy["boot_disk_gb"]

    assignment: set[str] = set()

    def search(index: int, available: QuotaBudget, networks: set[str]) -> bool:
        if index == len(pending):
            assignment.update(networks)
            return True
        name = pending[index]
        role = backend.desired["nodes"][name]["role"]
        profile = backend.policy["profiles"][role]
        for choice in backend.choices[role]:
            from selectel_stage_operations import candidate_region

            candidate = checked(choice)
            pool, zone = candidate_region(candidate), candidate["availability_zone"]
            demand: QuotaBudget = {
                (pool, zone, "instances"): 1,
                (pool, zone, "cores"): profile["vcpus"],
                (pool, zone, "ram"): profile["ram_mb"],
                (pool, "gigabytes"): disk,
                (pool, "volumes"): 1,
                (pool, "gigabytes_" + candidate["volume_type"]): disk,
                (pool, "ports"): 1,
                (pool, "floatingip"): int(role == "app"),
            }
            if pool not in networks:
                demand[(pool, "ports")] += available[(pool, "network_ports")]
            if not network_available.get(pool, False):
                continue
            if any(key not in available or available[key] < amount for key, amount in demand.items()):
                continue
            following = {**available, **{key: available[key] - value for key, value in demand.items()}}
            if search(index + 1, following, networks | {pool}):
                return True
        return False

    if not search(0, budgets, set()):
        raise CandidateUnavailable("No quota-feasible assignment for all remaining nodes")
    return assignment


def require_network_capacity(backend: "Backend", pool: str) -> None:
    if backend.completed("network-" + pool):
        return
    cloud = backend.region(pool)
    project = backend.credentials["project_id"]
    network = json_object(
        cloud.get("network", "/v2.0/quotas/" + project + "/details.json")["quota"],
        "Unknown network quota payload",
    )
    needs = {
        "network": 1,
        "subnet": 1,
        "router": 1,
        "security_group": 1,
        "security_group_rule": len(backend.journal["pool_subnets"])
        + 3
        + 2 * len(backend.journal.get("admin_cidrs", [])),
        "port": network_port_demand(backend, pool),
    }
    kinds = {
        "network": "openstack_networking_network_v2",
        "subnet": "openstack_networking_subnet_v2",
        "router": "openstack_networking_router_v2",
        "security_group": "openstack_networking_secgroup_v2",
        "security_group_rule": "openstack_networking_secgroup_rule_v2",
    }
    entries = backend.store.entries("network-" + pool)
    for key, kind in kinds.items():
        needs[key] = max(0, needs[key] - sum(e["type"] == kind for e in entries))
    if any(e["type"] == kinds["security_group"] for e in entries):
        needs["security_group_rule"] = max(0, needs["security_group_rule"] - 2)
    for key, amount in needs.items():
        if free(network.get(key)) < amount:
            raise CandidateUnavailable("Network quota insufficient in " + pool + ": " + key)
    compute = json_object(
        cloud.get("compute", "/os-quota-sets/" + project + "/detail")["quota_set"],
        "Unknown compute quota payload",
    )
    group_needed = int(not any(e["type"] == "openstack_compute_servergroup_v2" for e in entries))
    members = compute.get("server_group_members")
    required_members = backend.journal.get("require_distinct_hosts", True)
    if free(compute.get("server_groups")) < group_needed or (
        required_members
        and (
            members is None
            or not isinstance(member_quota := members, dict)
            or type(member_quota.get("limit")) is not int
            or (member_quota["limit"] != -1 and member_quota["limit"] < 3)
        )
    ):
        raise CandidateUnavailable("Elasticsearch server-group quota unknown or insufficient in " + pool)
