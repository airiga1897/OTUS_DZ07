"""Reject corrupt saved infrastructure before it reaches inventory or cleanup."""

from typing import cast

import pytest
from cloud_contract import InfrastructureResult


def document() -> dict[str, object]:
    return {
        "provider": "selectel",
        "scope": {"project_id": "synthetic"},
        "namespace": "otus-dz07",
        "placement": {"region": "ru-2", "zone": "ru-2a"},
        "nodes": {
            "app": {
                "roles": ["app"],
                "id": "app-id",
                "private_ip": "10.77.0.10",
                "public_ip": "8.8.8.8",
                "volume_id": "disk-id",
                "port_id": "port-id",
            },
        },
        "subnet_cidr": "10.77.0.0/24",
    }


@pytest.mark.parametrize("value", [None, [], "infra", 42, {}])
def test_invalid_infrastructure_envelope(value: object) -> None:
    with pytest.raises(ValueError):
        InfrastructureResult.from_dict(value)


@pytest.mark.parametrize(
    "path,value",
    [
        (("provider",), None),
        (("namespace",), []),
        (("scope",), None),
        (("scope",), {"project_id": []}),
        (("scope",), {1: "synthetic"}),
        (("placement",), None),
        (("placement", "region"), []),
        (("placement", "network_zone"), []),
        (("placement", "router_zone"), 7),
        (("placement", "pool_subnets"), None),
        (("placement", "pool_subnets"), {"ru-2": []}),
        (("placement", "node_placements"), None),
        (("placement", "node_placements"), {"app": []}),
        (("placement", "node_placements"), {1: {"region": "ru-2"}}),
        (("nodes",), None),
        (("nodes",), []),
        (("nodes",), {1: {}}),
        (("nodes", "app"), None),
        (("nodes", "app", "roles"), "app"),
        (("nodes", "app", "roles"), [["app"]]),
        (("nodes", "app", "id"), []),
        (("nodes", "app", "private_ip"), None),
        (("nodes", "app", "region"), []),
        (("nodes", "app", "zone"), 1),
        (("nodes", "app", "subnet_cidr"), None),
        (("nodes", "app", "public_ip"), 1),
        (("nodes", "app", "public_ip"), None),
        (("nodes", "app", "volume_id"), ["volume"]),
        (("nodes", "app", "port_id"), {"port": "id"}),
    ],
)
def test_malformed_infrastructure_fields(path: tuple[str, ...], value: object) -> None:
    raw = document()
    parent = raw
    for key in path[:-1]:
        parent = cast(dict[str, object], parent[key])
    parent[path[-1]] = value
    with pytest.raises(ValueError):
        InfrastructureResult.from_dict(raw)


def test_valid_infrastructure_still_generates_the_same_application_view() -> None:
    raw = document()
    placement = cast(dict[str, object], raw["placement"])
    placement.update(network_zone=None, router_zone="ru-2a")
    result = InfrastructureResult.from_dict(raw)
    assert result.application()["server_ids"] == {"app": "app-id"}
    assert result.application()["node_regions"] == {"app": "ru-2"}


def test_explicit_node_subnet_requires_saved_region_binding() -> None:
    raw = document()
    nodes = cast(dict[str, dict[str, object]], raw["nodes"])
    nodes["app"]["subnet_cidr"] = "10.77.0.0/24"
    with pytest.raises(ValueError, match="outside the saved network contract"):
        InfrastructureResult.from_dict(raw)


def test_private_es_terraform_output_accepts_null_public_ip() -> None:
    raw = document()
    nodes = cast(dict[str, object], raw["nodes"])
    nodes["es1"] = {
        "roles": ["es"],
        "id": "es-id",
        "private_ip": "10.77.0.31",
        "public_ip": None,
        "volume_id": "es-volume",
        "port_id": "es-port",
    }
    result = InfrastructureResult.from_dict(raw)
    assert result.nodes["es1"]["public_ip"] is None
    assert result.application()["private_ips"]["es1"] == "10.77.0.31"
