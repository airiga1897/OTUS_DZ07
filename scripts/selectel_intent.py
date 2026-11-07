"""Write-ahead resource selectors for the API-created-before-state failure window."""

from collections.abc import Mapping, Sequence
from typing import Literal, Protocol, TypedDict

from terraform_state_types import StateEntry
from topology import Topology


class CreationIntent(TypedDict):
    operation: Literal["create"]
    kind: str
    pool: str
    project_id: str
    owner: str
    name_prefix: str
    resource_types: list[str]


class NamespacePort(Protocol):
    PREFIX: str


class IntentPort(Protocol):
    @property
    def desired(self) -> Topology: ...
    @property
    def credentials(self) -> Mapping[str, str]: ...
    @property
    def owner(self) -> str: ...
    @property
    def api(self) -> NamespacePort: ...
    @property
    def journal(self) -> Mapping[str, object]: ...


KINDS = {
    "node": ["openstack_compute_instance_v2", "openstack_blockstorage_volume_v3", "openstack_networking_port_v2"],
    "network": [
        "openstack_networking_network_v2",
        "openstack_networking_subnet_v2",
        "openstack_networking_router_v2",
        "openstack_networking_secgroup_v2",
        "openstack_compute_servergroup_v2",
    ],
    "global": ["selectel_global_router_router_v1"],
    "connection": ["selectel_global_router_vpc_network_v1", "selectel_global_router_vpc_subnet_v1"],
}


def intent(backend: IntentPort, name: str, candidate: Mapping[str, object]) -> CreationIntent:
    if name in backend.desired["nodes"]:
        kind = "node"
    elif name == "global":
        kind = "global"
    elif name.startswith("network-"):
        kind = "network"
    elif name.startswith("connection-"):
        kind = "connection"
    else:
        raise ValueError("Unknown stage creation intent")
    pool = candidate.get("region")
    if not isinstance(pool, str) or not pool:
        raise ValueError("Stage creation intent requires a confirmed pool")
    types = list(KINDS[kind])
    if kind == "node" and backend.desired["nodes"][name]["role"] == "app":
        types.append("openstack_networking_floatingip_v2")
    return {
        "operation": "create",
        "kind": kind,
        "pool": pool,
        "project_id": backend.credentials["project_id"],
        "owner": backend.owner,
        "name_prefix": backend.api.PREFIX + "-" + name,
        "resource_types": types,
    }


def validate(backend: IntentPort, name: str) -> CreationIntent | None:
    stages = backend.journal["stages"]
    if not isinstance(stages, dict) or not isinstance(stages.get(name), dict):
        raise ValueError("Invalid saved stage record")
    record = stages[name]
    value = record.get("intent")
    if value is None:
        return None
    candidate = record.get("candidate")
    if not isinstance(candidate, dict):
        raise ValueError("Invalid saved stage placement")
    expected = intent(backend, name, candidate)
    if value != expected:
        raise ValueError("Stage intent differs from its saved ownership and placement")
    return expected


def check_unrecorded(
    backend: IntentPort,
    name: str,
    entries: Sequence[StateEntry],
    found: Mapping[str, Mapping[str, Mapping[str, object]]],
) -> None:
    value = validate(backend, name)
    if value is None:
        return  # Legacy stages retain their existing state and namespace audit.
    known: set[tuple[str, str]] = set()
    for entry in entries:
        identifier = entry["attrs"].get("id")
        if not isinstance(identifier, str) or not identifier:
            raise ValueError("Unknown saved resource identity")
        known.add((entry["type"], identifier))
    prefix, owner = value["name_prefix"], value["owner"]
    for kind in value["resource_types"]:
        for identifier, item in found.get(kind, {}).items():
            label, description = item.get("name"), item.get("description")
            if (label is not None and not isinstance(label, str)) or (
                description is not None and not isinstance(description, str)
            ):
                raise ValueError("Unknown API resource ownership fields")
            label, description = label or "", description or ""
            matching_name = label == prefix or label.startswith(prefix + "-")
            matching_fip = description.startswith(prefix + ":" + owner)
            if (matching_name or matching_fip) and (kind, identifier) not in known:
                # A namespace match cannot authorize deletion or an automatic import.
                raise RuntimeError(
                    "API resource in the saved attempt is missing from state; explicit recovery required"
                )
