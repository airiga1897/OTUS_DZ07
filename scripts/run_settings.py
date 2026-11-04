"""Single reader of literal, non-secret run settings; no credential sources."""

import copy
import hashlib
import ipaddress
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import TypedDict

from placement_config import PlacementPolicy, checked
from placement_config import VMProfile as VMProfile


class RunSettings(TypedDict):
    require_distinct_hosts: bool
    preferred_pools: list[str]
    excluded_pools: list[str]
    excluded_segments: list[str]
    disk_type_patterns: list[str]
    flavor_name_patterns: list[str]
    profiles: dict[str, VMProfile]
    boot_disk_gb: int


ROOT = Path(__file__).resolve().parents[1]
FIELDS = {
    "REQUIRE_DISTINCT_HOSTS",
    "SELECTEL_PREFERRED_POOLS",
    "SELECTEL_EXCLUDED_POOLS",
    "SELECTEL_EXCLUDED_SEGMENTS",
    "SELECTEL_FLAVOR_NAME_PATTERNS",
    "SELECTEL_DISK_TYPE_PATTERNS",
    "APP_VCPUS",
    "APP_RAM_MB",
    "ES_VCPUS",
    "ES_RAM_MB",
    "BOOT_DISK_GB",
}


def dotenv(path: Path) -> dict[str, str]:
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        raise ValueError("Run settings must not be filesystem links")
    result = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or key not in FIELDS:
            raise ValueError("Invalid run-setting key or syntax")
        if key in result:
            raise ValueError("Duplicate run-setting key: " + key)
        if value.startswith(('"', "'")):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError("Unclosed quote: " + key)
            value = value[1:-1]
        if "$" in value or "`" in value:
            raise ValueError("Run settings require literal values: " + key)
        result[key] = value
    return result


def _literal_values(root: Path, environ: Mapping[str, str] | None) -> dict[str, str]:
    root = Path(root)
    values = dotenv(root / ".env.example")
    if set(values) != FIELDS:
        raise ValueError(".env.example must define every supported run setting")
    if (root / ".env").exists():
        values.update(dotenv(root / ".env"))
    environment = os.environ if environ is None else environ
    values.update({key: environment[key] for key in FIELDS if key in environment})
    return values


def _lists(values: Mapping[str, str]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for key, field in [
        ("SELECTEL_PREFERRED_POOLS", "preferred_pools"),
        ("SELECTEL_EXCLUDED_POOLS", "excluded_pools"),
        ("SELECTEL_EXCLUDED_SEGMENTS", "excluded_segments"),
        ("SELECTEL_DISK_TYPE_PATTERNS", "disk_type_patterns"),
        ("SELECTEL_FLAVOR_NAME_PATTERNS", "flavor_name_patterns"),
    ]:
        items = [item.strip() for item in values[key].split(",") if item.strip()]
        if len(set(items)) != len(items) or (field not in ("excluded_pools", "excluded_segments") and not items):
            raise ValueError("Empty or duplicate run-setting list: " + key)
        pattern = (
            r"[A-Za-z0-9.*?_-]+"
            if field == "disk_type_patterns"
            else r"[A-Za-z0-9.*?{}_\-]+"
            if field == "flavor_name_patterns"
            else r"ru-\d+[a-z]"
            if field == "excluded_segments"
            else r"[a-z][a-z0-9]*-\d+"
        )
        if any(not re.fullmatch(pattern, item) for item in items):
            raise ValueError("Invalid run-setting list: " + key)
        result[field] = items
    for pattern in result["flavor_name_patterns"]:
        try:
            pattern.format(vcpus=2, ram_mb=8192)
        except (KeyError, ValueError, IndexError) as error:
            raise ValueError("Invalid flavor name pattern") from error
    if set(result["preferred_pools"]) & set(result["excluded_pools"]):
        raise ValueError("A preferred pool is explicitly excluded")
    return result


def _positive(values: Mapping[str, str], key: str) -> int:
    if not re.fullmatch(r"[1-9]\d*", values[key]):
        raise ValueError("Positive integer required: " + key)
    return int(values[key])


def _profile(values: Mapping[str, str], role: str) -> VMProfile:
    profile = VMProfile(
        vcpus=_positive(values, role.upper() + "_VCPUS"), ram_mb=_positive(values, role.upper() + "_RAM_MB")
    )
    if profile["vcpus"] < 2 or profile["ram_mb"] < 8192 or profile["ram_mb"] % 1024:
        raise ValueError("VM settings require at least 2 vCPU and 8192 MiB in whole GiB")
    return profile


def load(root: str | Path = ROOT, environ: Mapping[str, str] | None = None) -> RunSettings:
    values = _literal_values(Path(root), environ)
    boolean = values["REQUIRE_DISTINCT_HOSTS"].lower()
    if boolean not in ("true", "false"):
        raise ValueError("REQUIRE_DISTINCT_HOSTS must be true or false")
    lists = _lists(values)
    profiles = {role: _profile(values, role) for role in ("app", "es")}
    if not re.fullmatch(r"[1-9]\d*", values["BOOT_DISK_GB"]) or int(values["BOOT_DISK_GB"]) < 20:
        raise ValueError("BOOT_DISK_GB must be at least 20")
    return RunSettings(
        require_distinct_hosts=boolean == "true",
        preferred_pools=lists["preferred_pools"],
        excluded_pools=lists["excluded_pools"],
        excluded_segments=lists["excluded_segments"],
        disk_type_patterns=lists["disk_type_patterns"],
        flavor_name_patterns=lists["flavor_name_patterns"],
        profiles=profiles,
        boot_disk_gb=int(values["BOOT_DISK_GB"]),
    )


def placement(root: str | Path = ROOT) -> PlacementPolicy:
    root = Path(root)
    result = checked(json.loads((root / "config/placement.json").read_text(encoding="utf-8-sig")))
    if (root / ".env.example").exists():
        duplicate = {
            "pools",
            "preferred_pools",
            "excluded_pools",
            "excluded_segments",
            "disk_type_patterns",
            "profiles",
            "boot_disk_gb",
            "require_distinct_hosts",
            "flavor_name_patterns",
        } & result.keys()
        if duplicate:
            raise ValueError("Run settings duplicated in placement.json: " + ", ".join(sorted(duplicate)))
        result = checked({**result, **load(root), "pools": ["auto"]})
    elif not {"pools", "profiles", "boot_disk_gb"} <= result.keys():
        raise ValueError("Missing .env.example")
    return checked(result)


def snapshot(policy: Mapping[str, object]) -> dict[str, object]:
    return copy.deepcopy(
        {
            key: policy[key]
            for key in (
                "profiles",
                "boot_disk_gb",
                "disk_type_patterns",
                "require_distinct_hosts",
                "preferred_pools",
                "excluded_pools",
                "excluded_segments",
                "flavor_name_patterns",
            )
            if key in policy
        }
    )


def pool_subnet(policy: PlacementPolicy, pool: str, assigned: Mapping[str, str] | None = None) -> str:
    configured = policy["sequential_placement"]["pool_subnets"]
    if pool in configured:
        return configured[pool]
    network = ipaddress.ip_network(policy["sequential_placement"]["subnet_pool"])
    if network.version != 4 or not network.is_private or network.prefixlen > 24:
        raise ValueError("Private IPv4 subnet_pool with prefix <=24 required")
    count = 2 ** (24 - network.prefixlen)
    start = int.from_bytes(hashlib.sha256(pool.encode()).digest()[:4], "big") % count
    occupied = [ipaddress.ip_network(c) for c in [*configured.values(), *(assigned or {}).values()]]
    for offset in range(count):
        subnet = ipaddress.ip_network((int(network.network_address) + ((start + offset) % count) * 256, 24))
        if not any(subnet.overlaps(other) for other in occupied):
            return str(subnet)
    raise ValueError("Subnet allocation range exhausted")
