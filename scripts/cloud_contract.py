"""Small, clone-local contracts shared by lifecycle and cloud adapters."""

import ipaddress
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import NotRequired, TypedDict, cast


class InfrastructurePlacement(TypedDict, total=False):
    region: str
    zone: str
    network_zone: str | None
    router_zone: str | None
    node_placements: dict[str, dict[str, str]]
    pool_subnets: dict[str, str]


class InfrastructureNode(TypedDict):
    id: str
    roles: list[str]
    private_ip: str
    volume_id: str
    port_id: str
    public_ip: NotRequired[str | None]
    region: NotRequired[str]
    zone: NotRequired[str]
    subnet_cidr: NotRequired[str]


class StandDescription(TypedDict, total=False):
    site_url: str
    app_public_ip: str
    private_ips: dict[str, str]
    server_ids: dict[str, str]
    volume_ids: dict[str, str]
    port_ids: dict[str, str]
    node_regions: dict[str, str]
    node_zones: dict[str, str]
    node_roles: dict[str, str]
    subnet_cidr: str


@dataclass(frozen=True)
class ConfigurationTarget:
    lab: StandDescription
    deletion_verified: bool


def _string_map(value: object) -> bool:
    return isinstance(value, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in value.items())


def _placement_shape(value: object) -> None:
    if not isinstance(value, dict):
        raise ValueError("Invalid infrastructure placement shape")
    for key in ("region", "zone"):
        if key in value and not isinstance(value[key], str):
            raise ValueError("Invalid infrastructure placement identity")
    for key in ("network_zone", "router_zone"):
        if key in value and value[key] is not None and not isinstance(value[key], str):
            raise ValueError("Invalid infrastructure network placement")
    if "pool_subnets" in value and not _string_map(value["pool_subnets"]):
        raise ValueError("Invalid infrastructure subnet map")
    if "node_placements" in value:
        placements = value["node_placements"]
        if not isinstance(placements, dict) or any(
            not isinstance(name, str) or not _string_map(row) for name, row in placements.items()
        ):
            raise ValueError("Invalid infrastructure node placement map")


def _node_shape(name: object, node: object) -> None:
    if not isinstance(name, str) or not isinstance(node, dict):
        raise ValueError("Invalid node name or roles")
    roles = node.get("roles")
    if (
        not isinstance(roles, list)
        or any(not isinstance(role, str) for role in roles)
        or not re.fullmatch(r"[a-z][a-z0-9-]*", name)
        or not roles
        or set(roles) - {"app", "es"}
    ):
        raise ValueError("Invalid node name or roles")
    if any(not isinstance(node.get(key), str) for key in ("id", "private_ip", "volume_id", "port_id")):
        raise ValueError("Invalid node identity field shape")
    if any(key in node and not isinstance(node[key], str) for key in ("region", "zone", "subnet_cidr")):
        raise ValueError("Invalid node address or placement field shape")
    if node.get("public_ip") is not None and not isinstance(node["public_ip"], str):
        raise ValueError("Invalid node public address shape")


def _application_address(node: InfrastructureNode) -> str:
    address = node.get("public_ip")
    if not isinstance(address, str):
        raise ValueError("Application public address is required")
    return address


def checked_node(name: str, value: object) -> InfrastructureNode:
    """Validate a completed node before projecting partial stage metadata."""
    _node_shape(name, value)
    return cast(InfrastructureNode, value)


def _infrastructure_shape(value: Mapping[str, object]) -> None:
    if any(not isinstance(value.get(key), str) for key in ("provider", "namespace", "subnet_cidr")):
        raise ValueError("Invalid infrastructure identity shape")
    if not _string_map(value.get("scope")):
        raise ValueError("Invalid infrastructure scope shape")
    _placement_shape(value.get("placement"))
    nodes = value.get("nodes")
    if not isinstance(nodes, dict):
        raise ValueError("Invalid infrastructure node map")
    for name, node in nodes.items():
        _node_shape(name, node)


class InfrastructureUnavailableError(RuntimeError):
    """No usable local infrastructure result is available for this command."""


class TerraformError(RuntimeError):
    # Operation wrappers attach these when available; observers use getattr
    # until the corresponding diagnostic step has succeeded.
    terraform_stage: str
    terraform_operation: str
    failure_evidence: str
    diagnostics_error_type: str

    def __init__(self, output: str) -> None:
        super().__init__("Terraform failed; inspect the private local log")
        self.output = output


class CapabilityStatus(str, Enum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Capability:
    status: CapabilityStatus
    reason: str


def require_capabilities(found: dict[str, Capability], required: list[str]) -> None:
    for name in required:
        value = found.get(name, Capability(CapabilityStatus.UNKNOWN, "No discovery result"))
        if value.status != CapabilityStatus.SUPPORTED:
            raise ValueError(f"Required capability {name}: {value.status.value}; {value.reason}")


def validate_binding(saved: Mapping[str, object], expected: Mapping[str, object]) -> None:
    for key in ("provider", "scope", "namespace", "placement"):
        if not saved.get(key) or saved[key] != expected.get(key):
            raise ValueError("Stand ownership mismatch: " + key)


@dataclass(frozen=True)
class InfrastructureResult:
    provider: str
    scope: dict[str, str]
    namespace: str
    placement: InfrastructurePlacement
    nodes: dict[str, InfrastructureNode]
    subnet_cidr: str

    @classmethod
    def from_dict(cls, value: object) -> "InfrastructureResult":
        if not isinstance(value, dict):
            raise ValueError("Invalid infrastructure document")
        _infrastructure_shape(value)
        result = cls(
            provider=value["provider"],
            scope=cast(dict[str, str], value["scope"]),
            namespace=value["namespace"],
            placement=cast(InfrastructurePlacement, value["placement"]),
            nodes=cast(dict[str, InfrastructureNode], value["nodes"]),
            subnet_cidr=value["subnet_cidr"],
        )
        result.validate()
        return result

    def validate(self) -> None:
        _infrastructure_shape({key: getattr(self, key) for key in self.__dataclass_fields__})
        if not re.fullmatch(r"[a-z][a-z0-9-]+", self.provider) or not self.scope or not all(self.scope.values()):
            raise ValueError("Provider and resource scope are required")
        if not re.fullmatch(r"[a-z][a-z0-9-]+", self.namespace) or not all(
            self.placement.get(k) for k in ("region", "zone")
        ):
            raise ValueError("Namespace and placement are required")
        ipaddress.ip_network(self.subnet_cidr)
        ids, addresses = set(), set()
        apps = []
        if not self.nodes:
            raise ValueError("Infrastructure has no nodes")
        for name, node in self.nodes.items():
            address = ipaddress.ip_address(node["private_ip"])
            node_subnet = ipaddress.ip_network(node.get("subnet_cidr", self.subnet_cidr))
            if node.get("subnet_cidr") and (
                not node.get("region")
                or self.placement.get("pool_subnets", {}).get(node["region"]) != node["subnet_cidr"]
                or self.placement.get("node_placements", {}).get(name)
                != {"region": node.get("region"), "zone": node.get("zone")}
            ):
                raise ValueError("Node placement/subnet is outside the saved network contract")
            if address not in node_subnet or str(address) in addresses or not node.get("id") or node["id"] in ids:
                raise ValueError("Duplicate or invalid node identity/address")
            if not node.get("volume_id") or not node.get("port_id"):
                raise ValueError("Node storage and network identities are required")
            ids.add(node["id"])
            addresses.add(str(address))
            if "app" in node["roles"]:
                ipaddress.IPv4Address(_application_address(node))
                apps.append(name)
        if apps != ["app"]:
            raise ValueError("This stack requires one application/controller node named app")

    def binding(self) -> dict[str, object]:
        return {key: getattr(self, key) for key in ("provider", "scope", "namespace", "placement")}

    def application(self) -> StandDescription:
        """Technology settings use addresses and roles, never provider resource types."""
        return {
            "private_ips": {n: v["private_ip"] for n, v in self.nodes.items()},
            "node_roles": {n: v["roles"][0] for n, v in self.nodes.items()},
            "app_public_ip": _application_address(self.nodes["app"]),
            "subnet_cidr": self.subnet_cidr,
            "server_ids": {n: v["id"] for n, v in self.nodes.items()},
            "volume_ids": {n: v["volume_id"] for n, v in self.nodes.items()},
            "port_ids": {n: v["port_id"] for n, v in self.nodes.items()},
            "node_regions": {n: v.get("region", self.placement["region"]) for n, v in self.nodes.items()},
            "node_zones": {n: v.get("zone", self.placement["zone"]) for n, v in self.nodes.items()},
        }
