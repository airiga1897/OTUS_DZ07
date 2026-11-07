"""Placement schema rejects malformed external shapes without rewriting policy."""

import copy
import json
from pathlib import Path

import pytest
from placement_config import checked

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("value", [None, [], "PRIVATE", 1, {1: "PRIVATE"}])
def test_non_object_or_non_string_keys_are_rejected(value: object) -> None:
    with pytest.raises(ValueError, match="placement object") as error:
        checked(value)
    assert "PRIVATE" not in str(error.value)


@pytest.mark.parametrize(
    "key",
    [
        "image_name",
        "image_os_distro",
        "volume_type",
        "image_id",
        "project_id",
        "phase",
        "provider",
        "namespace",
        "require_distinct_hosts",
        "boot_disk_gb",
    ],
)
def test_scalar_types_are_explicit(key: str) -> None:
    with pytest.raises(ValueError, match="field type"):
        checked({key: None})


@pytest.mark.parametrize(
    "key", ["region", "availability_zone", "network_availability_zone", "router_availability_zone", "segment"]
)
def test_unresolved_discovery_identity_allows_none_but_not_collections(key: str) -> None:
    assert checked({key: None}) == {key: None}
    assert checked({key: "ru-2a"}) == {key: "ru-2a"}
    with pytest.raises(ValueError, match="optional placement identity"):
        checked({key: []})


@pytest.mark.parametrize(
    "key",
    [
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
    ],
)
@pytest.mark.parametrize("invalid", [None, "ru-2", [None], [True]])
def test_string_lists_never_accept_scalars_or_other_element_types(key: str, invalid: object) -> None:
    with pytest.raises(ValueError, match="placement list"):
        checked({key: invalid})
    assert checked({key: []}) == {key: []}


@pytest.mark.parametrize("profile", [None, [], {"vcpus": True, "ram_mb": 4096}, {"vcpus": 2, "ram_mb": "4096"}])
def test_present_vm_profile_fields_have_integer_types(profile: object) -> None:
    with pytest.raises(ValueError):
        checked({"profiles": {"app": profile}})


@pytest.mark.parametrize("field", ["flavor_ids", "profiles", "network_placement", "sequential_placement"])
def test_nested_sections_require_objects(field: str) -> None:
    with pytest.raises(ValueError, match="placement object"):
        checked({field: []})


@pytest.mark.parametrize("field", ["flavor_ids", "pool_subnets"])
def test_identifier_and_subnet_maps_require_strings(field: str) -> None:
    root: dict[str, object] = {field: {"ru-2": None}}
    if field == "pool_subnets":
        root = {"sequential_placement": root}
    with pytest.raises(ValueError, match="string map"):
        checked(root)


@pytest.mark.parametrize("value", [None, {"network": []}, {"router": False}, {"evidence_url": 1}])
def test_network_override_shapes_are_checked(value: object) -> None:
    with pytest.raises(ValueError):
        checked({"network_placement": {"ru-3b": value}})


@pytest.mark.parametrize(
    "section",
    [
        {"enabled": "true"},
        {"subnet_pool": None},
        {"node_order": [None]},
        {"pool_subnets": []},
        {"addresses": None},
        {"addresses": {"cloud_gateway": True}},
        {"addresses": {"global_gateway": "2"}},
        {"addresses": {"dhcp_first": None}},
        {"addresses": {"dhcp_last": []}},
        {"addresses": {"service_hosts": [True]}},
    ],
)
def test_sequential_network_policy_does_not_coerce_values(section: object) -> None:
    with pytest.raises(ValueError):
        checked({"sequential_placement": section})


def test_valid_policy_is_not_normalized_or_relocated() -> None:
    document = json.loads((ROOT / "config/placement.json").read_text())
    document.update(
        profiles={"app": {"vcpus": 2, "ram_mb": 4096}},
        boot_disk_gb=20,
        require_distinct_hosts=False,
        pools=["auto"],
        preferred_pools=["ru-2", "ru-9", "ru-3"],
        excluded_pools=["ru-7"],
        excluded_segments=[],
        disk_type_patterns=["universal.*", "basicssd.*"],
        flavor_name_patterns=["SL1.*"],
        flavor_ids={"app": "synthetic"},
        image_id="synthetic",
        volume_type="universal.ru-2a",
        project_id="synthetic",
        phase="prepared",
        provider="selectel",
        namespace="otus-dz07",
        image_os_distro="ubuntu",
    )
    document["image_name"] = "Ubuntu — проверка Unicode"
    before = copy.deepcopy(document)
    assert checked(document) is document
    assert document == before
    assert checked({"profiles": {}, "network_placement": {}, "sequential_placement": {}}) == {
        "profiles": {},
        "network_placement": {},
        "sequential_placement": {},
    }


@pytest.mark.parametrize("value", [[], "PRIVATE", {"region": []}])
def test_corrupt_saved_selection_blocks_policy_and_is_not_overwritten(tmp_path: Path, value: object) -> None:
    import pool_selection

    config = tmp_path / "config"
    config.mkdir()
    (config / "placement.json").write_text(json.dumps({"pools": ["ru-2"], "profiles": {}, "boot_disk_gb": 20}))
    local = tmp_path / ".local"
    local.mkdir()
    path = local / "placement.json"
    original = json.dumps(value)
    path.write_text(original)
    with pytest.raises(ValueError):
        pool_selection.policy(tmp_path, "placement.json", "context.json")
    with pytest.raises(ValueError):
        pool_selection.phase(tmp_path, "destroyed")
    assert path.read_text() == original
    assert not (local / "context.json").exists()


def test_legacy_null_selection_preserves_recovery_region_and_file(tmp_path: Path) -> None:
    import pool_selection

    (tmp_path / "config").mkdir()
    (tmp_path / "config/placement.json").write_text(json.dumps({"pools": ["ru-2"], "profiles": {}, "boot_disk_gb": 20}))
    local = tmp_path / ".local"
    local.mkdir()
    path = local / "placement.json"
    path.write_text("null")
    (local / "context.json").write_text(json.dumps({"placement": {"region": "ru-3"}}))
    assert pool_selection.selected(tmp_path) is None
    assert pool_selection.policy(tmp_path, "placement.json", "context.json")["region"] == "ru-3"
    pool_selection.phase(tmp_path, "cleanup_pending")
    assert path.read_text() == "null"


@pytest.mark.parametrize("profile", [{}, {"vcpus": 2}, {"ram_mb": 4096}])
def test_partial_legacy_profiles_are_readable_but_cannot_start_discovery(profile: object) -> None:
    from unittest.mock import Mock, patch

    import pool_selection

    value = {"pools": ["ru-2"], "profiles": {"app": profile}, "boot_disk_gb": 20, "disk_type_patterns": ["universal.*"]}
    assert checked(value) == value
    authenticate = Mock()
    with (
        patch.object(pool_selection, "authenticate", authenticate),
        pytest.raises(ValueError, match="Complete VM profiles"),
    ):
        next(pool_selection.candidates(value, {}))
    authenticate.assert_not_called()


def test_complete_profiles_keep_the_same_mapping_and_empty_profiles_are_rejected() -> None:
    from placement_config import required_profiles

    profiles = {"app": {"vcpus": 2, "ram_mb": 4096}}
    assert required_profiles({"profiles": profiles}) is profiles
    with pytest.raises(ValueError, match="Complete VM profiles"):
        required_profiles({"profiles": {}})
