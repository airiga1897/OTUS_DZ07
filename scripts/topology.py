"""Four-node topology with standalone PostgreSQL on app and optional es4."""

import ipaddress
import json
from collections.abc import Mapping
from pathlib import Path
from typing import TypedDict, cast

from service_config import ServiceSettings
from service_config import load as load_services


class Node(TypedDict):
    host: int
    role: str


class Topology(TypedDict):
    nodes: dict[str, Node]


class AddressedNodes(TypedDict, total=False):
    private_ips: dict[str, str]
    node_roles: dict[str, str]


def load(root: Path) -> Topology:
    value = json.loads((root / "config/topology.json").read_text(encoding="utf-8"))
    validate(value)
    if not set(services(root)["initial_masters"]) <= set(value["nodes"]):
        raise ValueError("DZ07 requires three separate Elasticsearch masters")
    return cast(Topology, value)


def validate(value: object) -> None:
    if not isinstance(value, dict) or set(value) != {"nodes"} or not isinstance(value["nodes"], dict):
        raise ValueError("Invalid topology schema")
    nodes = value["nodes"]
    if any(not isinstance(node, dict) or set(node) != {"host", "role"} for node in nodes.values()):
        raise ValueError("Invalid node schema")
    if nodes.get("app") != {"host": 10, "role": "app"}:
        raise ValueError("The existing app address and role are immutable")
    required = {"app", "es1", "es2", "es3"}
    if not required <= set(nodes) <= required | {"es4"} or any(nodes[n]["role"] != "es" for n in set(nodes) - {"app"}):
        raise ValueError("Topology requires three ES masters and only optional es4; PostgreSQL runs on app")
    hosts = [v["host"] for v in nodes.values()]
    if any(type(h) is not int or not 2 <= h <= 254 for h in hosts) or len(set(hosts)) != len(hosts):
        raise ValueError("Node addresses must be unique usable /24 host offsets")


def validate_extension(previous: object, desired: object) -> set[str]:
    validate(previous)
    validate(desired)
    old, new = cast(Topology, previous), cast(Topology, desired)
    if any(new["nodes"].get(n) != v for n, v in old["nodes"].items()):
        raise ValueError("Existing nodes cannot be removed, renamed or readdressed")
    return set(new["nodes"]) - set(old["nodes"])


def from_lab(lab: AddressedNodes) -> Topology:
    return {
        "nodes": {
            n: {
                "host": int(ipaddress.ip_address(ip)) % 256,
                "role": lab.get("node_roles", {}).get(
                    n, "app" if n == "app" else "es" if n.startswith("es") else "unsupported"
                ),
            }
            for n, ip in lab["private_ips"].items()
        },
    }


def assert_preserved(before: Mapping[str, object], after: Mapping[str, object]) -> None:
    if any(after.get(key) != value for key, value in before.items()):
        raise RuntimeError("Existing infrastructure identities changed")


def services(root: Path) -> ServiceSettings:
    return load_services(root)


def elastic_names(lab: AddressedNodes) -> list[str]:
    return sorted(
        n for n in lab["private_ips"] if lab.get("node_roles", {}).get(n, "es" if n.startswith("es") else "") == "es"
    )
