"""Malformed Terraform resource selectors cannot authorize adding a node."""

import pytest
from selectel_adapter import validate_node_creation


@pytest.mark.parametrize("kind", [None, [], {}, True, 1])
def test_unknown_resource_kind_is_rejected(kind: object) -> None:
    with pytest.raises(ValueError, match="only create resources"):
        validate_node_creation({"type": kind, "name": "node", "index": "es4", "address": "synthetic"}, {"es4"})


@pytest.mark.parametrize("index", [None, [], {}, True, 1, "", "es1"])
def test_only_an_explicit_new_node_can_be_selected(index: object) -> None:
    with pytest.raises(ValueError, match="only create resources"):
        validate_node_creation(
            {"type": "openstack_compute_instance_v2", "name": "node", "index": index, "address": "synthetic"},
            {"es4"},
        )


def test_unknown_address_is_not_formatted_into_diagnostics() -> None:
    with pytest.raises(ValueError, match="unconfirmed address") as error:
        validate_node_creation({"type": "foreign", "address": {"PRIVATE": "body"}}, {"es4"})
    assert "PRIVATE" not in str(error.value)


@pytest.mark.parametrize(
    "kind,name",
    [
        ("openstack_compute_instance_v2", "node"),
        ("openstack_networking_port_v2", "node"),
        ("openstack_blockstorage_volume_v3", "boot"),
    ],
)
def test_supported_new_node_resource_stays_allowed(kind: str, name: str) -> None:
    validate_node_creation({"type": kind, "name": name, "index": "es4", "address": "synthetic"}, {"es4"})
