"""Shape checks for saved stage metadata, including legacy cleanup topologies."""

from collections.abc import Mapping
from typing import TypedDict, cast

from placement_config import AddressPolicy, PlacementPolicy
from placement_config import checked as checked_placement
from selectel_intent import CreationIntent
from terraform_state_types import StateEntry
from topology import Topology


class StageOutput(TypedDict, total=False):
    id: str
    volume_id: str
    port_id: str
    private_ip: str
    public_ip: str | None
    roles: list[str]
    region: str
    zone: str
    subnet_cidr: str
    gateway_ip: str
    network_id: str
    subnet_id: str
    security_group_id: str
    server_group_id: str
    external_network_name: str
    cidr: str
    network_zone: str | None
    router_zone: str | None
    router_id: str


class StageRecord(TypedDict, total=False):
    candidate: PlacementPolicy
    status: str
    kind: str
    inputs: dict[str, object]
    output: StageOutput
    identities: dict[str, str]
    lineage: str | None
    intent: CreationIntent | None
    audit_entries: list[StateEntry]


class OptionalNodeRequest(TypedDict, total=False):
    base_topology: Topology
    requested_node: str


class StageJournal(TypedDict, total=False):
    provider: str
    scope: dict[str, str]
    project_id: str
    namespace: str
    deployment_id: str
    stages: dict[str, StageRecord]
    configured: bool
    run_settings: Mapping[str, object] | None
    bootstrap_region: str
    require_distinct_hosts: bool
    pool_subnets: dict[str, str]
    topology: Topology
    address_policy: AddressPolicy
    admin_cidrs: list[str]
    destroy_requested: bool
    cleanup_verified: bool
    admin_access_pending: str
    stack_sha256: str
    optional_es4: OptionalNodeRequest | None


def mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("Invalid saved stage metadata object")
    return cast(dict[str, object], value)


def string_map(value: object) -> dict[str, str]:
    values = mapping(value)
    if any(not isinstance(item, str) for item in values.values()):
        raise ValueError("Invalid saved stage string map")
    return cast(dict[str, str], values)


def strings(value: object) -> None:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("Invalid saved stage string list")


def fields(value: Mapping[str, object], names: tuple[str, ...], kind: type, *, nullable: bool = False) -> None:
    for name in names:
        if name in value and not (nullable and value[name] is None) and type(value[name]) is not kind:
            raise ValueError("Invalid saved stage field type: " + name)


def saved_topology(value: object) -> Topology:
    """Check representation only; old PG roles are retained for cleanup, never deployed."""
    nodes = mapping(mapping(value).get("nodes"))
    for node in nodes.values():
        record = mapping(node)
        if type(record.get("host")) is not int or not isinstance(record.get("role"), str):
            raise ValueError("Invalid saved topology node shape")
    return cast(Topology, value)


def output(value: object) -> StageOutput:
    values = mapping(value)
    fields(
        values,
        (
            "id",
            "volume_id",
            "port_id",
            "private_ip",
            "region",
            "zone",
            "subnet_cidr",
            "gateway_ip",
            "network_id",
            "subnet_id",
            "security_group_id",
            "server_group_id",
            "external_network_name",
            "cidr",
            "router_id",
        ),
        str,
    )
    fields(values, ("public_ip", "network_zone", "router_zone"), str, nullable=True)
    if "roles" in values:
        strings(values["roles"])
    return cast(StageOutput, values)


def record(value: object) -> StageRecord:
    values = mapping(value)
    fields(values, ("status", "kind"), str)
    fields(values, ("lineage",), str, nullable=True)
    if "candidate" in values:
        checked_placement(values["candidate"])
    if "inputs" in values:
        mapping(values["inputs"])
    if "output" in values:
        output(values["output"])
    if "identities" in values:
        string_map(values["identities"])
    if "intent" in values and values["intent"] is not None:
        intent = mapping(values["intent"])
        if (
            any(
                not isinstance(intent.get(key), str)
                for key in ("operation", "kind", "pool", "project_id", "owner", "name_prefix")
            )
            or intent.get("operation") != "create"
        ):
            raise ValueError("Invalid saved creation intent shape")
        strings(intent.get("resource_types"))
    if "audit_entries" in values:
        entries = values["audit_entries"]
        if not isinstance(entries, list):
            raise ValueError("Invalid saved audit entries")
        for entry in entries:
            item = mapping(entry)
            if not isinstance(item.get("type"), str):
                raise ValueError("Invalid saved audit resource type")
            mapping(item.get("attrs"))
            fields(item, ("address",), str)
            index = item.get("index")
            if index is not None and type(index) not in (str, int):
                raise ValueError("Invalid saved audit resource index")
    return cast(StageRecord, values)


def checked(value: object) -> StageJournal:
    values = mapping(value)
    fields(
        values,
        (
            "provider",
            "project_id",
            "namespace",
            "deployment_id",
            "bootstrap_region",
            "admin_access_pending",
            "stack_sha256",
        ),
        str,
    )
    fields(values, ("configured", "require_distinct_hosts", "destroy_requested", "cleanup_verified"), bool)
    for key in ("scope", "pool_subnets"):
        if key in values:
            string_map(values[key])
    if "run_settings" in values and values["run_settings"] is not None:
        mapping(values["run_settings"])
    if "topology" in values:
        saved_topology(values["topology"])
    if "address_policy" in values:
        checked_placement({"sequential_placement": {"addresses": values["address_policy"]}})
    if "admin_cidrs" in values:
        strings(values["admin_cidrs"])
    if "optional_es4" in values and values["optional_es4"] is not None:
        optional = mapping(values["optional_es4"])
        if "base_topology" in optional:
            saved_topology(optional["base_topology"])
        fields(optional, ("requested_node",), str)
    if "stages" in values:
        for stage in mapping(values["stages"]).values():
            record(stage)
    return cast(StageJournal, values)
