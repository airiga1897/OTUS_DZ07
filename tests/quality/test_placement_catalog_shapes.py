"""Placement catalog shapes retain opaque bindings for candidate-level rejection."""

import pytest
from selectel_catalog_types import checked, flavor_rows, volume_rows


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {1: "bad"},
        {"flavors": None},
        {"flavors": [None]},
        {"flavors": [{"id": None, "name": "SL1"}]},
        {"flavors": [{"id": "id", "name": None}]},
        {"volume_types": [{"name": None}]},
        {"availabilityZoneInfo": [{"zoneName": "ru-2a", "zoneState": None}]},
        {"availabilityZoneInfo": [{"zoneName": "ru-2a", "zoneState": {"available": 1}}]},
        {"availability_zones": [{"name": "ru-2a", "resource": "network", "state": None}]},
    ],
)
def test_unknown_catalog_shapes_are_errors(value: object) -> None:
    with pytest.raises(ValueError, match="placement catalog"):
        checked(value)


def test_valid_catalog_preserves_unknown_metadata_and_opaque_bindings() -> None:
    value = {
        "flavors": [{"id": "f1", "name": "SL1", "vcpus": 2, "ram": 8192, "availability_zones": None}],
        "volume_types": [{"name": "universal.ru-2a", "extra_specs": None}],
        "availabilityZoneInfo": [{"zoneName": "ru-2a", "zoneState": {"available": True}}],
        "availability_zones": [{"name": "ru-2a", "resource": "network", "state": "available"}],
        "extra_specs": None,
        "other": {"not_consumed": None},
    }
    assert checked(value) is value
    assert checked({}) == {}
    assert flavor_rows([]) == []
    assert volume_rows([{"name": "universal.opaque", "extra_specs": []}]) == [
        {"name": "universal.opaque", "extra_specs": []}
    ]


def test_region_only_legacy_binding_is_not_a_complete_creation_candidate() -> None:
    from pool_selection import CandidateUnavailable, network_placement

    assert network_placement({}, "ru-2", None) == {
        "network_availability_zone": None,
        "router_availability_zone": None,
    }
    with pytest.raises(CandidateUnavailable, match="confirmed compute segment"):
        network_placement({}, "ru-2", None, {"network": {"ru-2a"}, "router": {"ru-2a"}})
