"""Malformed provider collection fields must reject read-only drift safely."""

from unittest.mock import Mock

import pytest
from selectel_staged import Backend


@pytest.mark.parametrize("old,new", [([["nested"]], []), ([], [["nested"]])])
def test_malformed_members_reject_drift(old: object, new: object) -> None:
    backend = object.__new__(Backend)
    backend.desired = {"nodes": {}}
    backend.journal = {"stages": {}}
    assert (
        backend.allow_refresh(
            "network-ru-2",
            {
                "type": "openstack_compute_servergroup_v2",
                "address": "server_group",
                "change": {
                    "actions": ["update"],
                    "before": {"members": old},
                    "after": {"members": new},
                },
            },
        )
        is False
    )


def test_malformed_network_tags_reject_drift() -> None:
    backend = object.__new__(Backend)
    backend.api = Mock(PREFIX="local")
    backend.journal = {
        "deployment_id": "deployment",
        "stages": {
            "connection-ru-2": {"output": {"network_id": "attachment"}},
        },
    }
    assert (
        backend.allow_refresh(
            "network-ru-2",
            {
                "type": "openstack_networking_network_v2",
                "address": "network",
                "change": {
                    "actions": ["update"],
                    "before": {"all_tags": [[]]},
                    "after": {
                        "all_tags": ["local-deployment", "l3vpn-network", "l3vpn-network-attachment"],
                    },
                },
            },
        )
        is False
    )


@pytest.mark.parametrize(
    "value,expected", [(None, set()), ([], set()), (["a", "a"], {"a"}), ("a", None), (2, None), ([2], None)]
)
def test_provider_string_collection_contract(value: object, expected: set[str] | None) -> None:
    from selectel_refresh import string_set

    assert string_set(value) == expected
