"""Types and shape checks for clone-local placement settings and saved choices."""

from collections.abc import Mapping
from typing import TypedDict, cast


class VMProfile(TypedDict):
    vcpus: int
    ram_mb: int


class SavedVMProfile(TypedDict, total=False):
    vcpus: int
    ram_mb: int


class NetworkPlacement(TypedDict, total=False):
    network: str
    router: str
    evidence_url: str


class AddressPolicy(TypedDict, total=False):
    cloud_gateway: int
    global_gateway: int
    service_hosts: list[int]
    dhcp_first: int
    dhcp_last: int


class SequentialPlacement(TypedDict, total=False):
    enabled: bool
    subnet_pool: str
    pool_subnets: dict[str, str]
    node_order: list[str]
    addresses: AddressPolicy


class PlacementPolicy(TypedDict, total=False):
    region: str | None
    availability_zone: str | None
    volume_type: str
    image_id: str
    flavor_ids: dict[str, str]
    network_availability_zone: str | None
    router_availability_zone: str | None
    project_id: str
    phase: str
    provider: str
    namespace: str
    image_name: str
    image_checksums: list[str]
    image_os_distro: str
    preferred_segments: list[str]
    segment: str | None
    operator_ip_services: list[str]
    admin_cidrs: list[str]
    network_placement: dict[str, NetworkPlacement]
    sequential_placement: SequentialPlacement
    require_distinct_hosts: bool
    pools: list[str]
    preferred_pools: list[str]
    excluded_pools: list[str]
    excluded_segments: list[str]
    disk_type_patterns: list[str]
    flavor_name_patterns: list[str]
    profiles: dict[str, SavedVMProfile]
    boot_disk_gb: int


def _fields(value: Mapping[str, object], fields: Mapping[str, type]) -> None:
    for key, kind in fields.items():
        if key in value and type(value[key]) is not kind:
            raise ValueError("Invalid placement field type: " + key)


def _lists(value: Mapping[str, object], keys: tuple[str, ...], kind: type = str) -> None:
    for key in keys:
        if key in value:
            items = value[key]
            if not isinstance(items, list) or any(type(item) is not kind for item in items):
                raise ValueError("Invalid placement list: " + key)


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("Invalid placement object")
    return cast(dict[str, object], value)


def _strings(value: object) -> None:
    items = _object(value)
    if any(not isinstance(item, str) for item in items.values()):
        raise ValueError("Invalid placement string map")


def _sequential(value: object) -> None:
    section = _object(value)
    _fields(section, {"enabled": bool, "subnet_pool": str})
    _lists(section, ("node_order",))
    if "pool_subnets" in section:
        _strings(section["pool_subnets"])
    if "addresses" in section:
        addresses = _object(section["addresses"])
        _fields(addresses, dict.fromkeys(("cloud_gateway", "global_gateway", "dhcp_first", "dhcp_last"), int))
        _lists(addresses, ("service_hosts",), int)


def checked(value: object) -> PlacementPolicy:
    """Check present fields; required combinations and semantics stay with their consumers."""
    data = _object(value)
    strings = (
        "image_name",
        "image_os_distro",
        "volume_type",
        "image_id",
        "project_id",
        "phase",
        "provider",
        "namespace",
    )
    _fields(data, dict.fromkeys(strings, str) | {"require_distinct_hosts": bool, "boot_disk_gb": int})
    for key in ("region", "availability_zone", "network_availability_zone", "router_availability_zone", "segment"):
        if key in data and data[key] is not None and not isinstance(data[key], str):
            raise ValueError("Invalid optional placement identity: " + key)
    _lists(
        data,
        (
            "image_checksums",
            "preferred_segments",
            "operator_ip_services",
            "admin_cidrs",
            "pools",
            "preferred_pools",
            "excluded_pools",
            "excluded_segments",
            "disk_type_patterns",
            "flavor_name_patterns",
        ),
    )
    if "flavor_ids" in data:
        _strings(data["flavor_ids"])
    if "profiles" in data:
        for profile in _object(data["profiles"]).values():
            values = _object(profile)
            _fields(values, {"vcpus": int, "ram_mb": int})
    if "network_placement" in data:
        for placement in _object(data["network_placement"]).values():
            _fields(_object(placement), dict.fromkeys(("network", "router", "evidence_url"), str))
    if "sequential_placement" in data:
        _sequential(data["sequential_placement"])
    return cast(PlacementPolicy, data)


def required_profiles(value: object) -> dict[str, VMProfile]:
    profiles = checked(value).get("profiles", {})
    if not profiles or any(not {"vcpus", "ram_mb"} <= profile.keys() for profile in profiles.values()):
        raise ValueError("Complete VM profiles are required before placement discovery")
    return cast(dict[str, VMProfile], profiles)
