"""Saved stage journals retain partial and legacy cleanup metadata safely."""

from pathlib import Path

import pytest
from stage_journal import checked
from staged_runtime import read_journal


@pytest.mark.parametrize("field", ["intent", "optional_es4", "run_settings"])
def test_nullable_legacy_metadata(field: str) -> None:
    value: dict[str, object] = {"stages": {"es1": {"intent": None}}} if field == "intent" else {field: None}
    assert checked(value) == value


def test_corrupt_saved_root_preserves_recovery(tmp_path: Path) -> None:
    path = tmp_path / ".local/staged-deployment.json"
    path.parent.mkdir()
    path.write_text("null")
    with pytest.raises(ValueError, match="stage metadata"):
        read_journal(tmp_path)
    assert path.read_text() == "null"


@pytest.mark.parametrize("value", [None, [], 1, True, "secret", {1: "bad"}])
def test_invalid_root(value: object) -> None:
    with pytest.raises(ValueError, match="stage metadata"):
        checked(value)


@pytest.mark.parametrize(
    "value",
    [
        {"scope": {"project_id": None}},
        {"admin_cidrs": None},
        {"admin_cidrs": [2]},
        {"configured": 1},
        {"provider": False},
        {"topology": {"nodes": {"db1": {"host": True, "role": "db"}}}},
        {"topology": {"nodes": {"db1": {"host": 10, "role": None}}}},
        {"stages": {"es1": {"output": {"roles": [False]}}}},
        {"stages": {"es1": {"output": {"id": 3}}}},
        {"stages": {"es1": {"audit_entries": {}}}},
        {"stages": {"es1": {"audit_entries": [{"type": None, "attrs": {}}]}}},
        {"stages": {"es1": {"audit_entries": [{"type": "vm", "attrs": {}, "index": True}]}}},
        {"stages": {"es1": {"intent": {}}}},
        {"optional_es4": {"requested_node": None}},
    ],
)
def test_invalid_known_fields(value: object) -> None:
    with pytest.raises(ValueError):
        checked(value)


def test_partial_stages_and_legacy_topology_remain_readable() -> None:
    topology = {"nodes": {"db1": {"host": 20, "role": "db"}}}
    value = {
        "scope": {"project_id": "local-project"},
        "pool_subnets": {"ru-2": "10.1.0.0/24"},
        "run_settings": {"old_unused_setting": None},
        "topology": topology,
        "address_policy": {},
        "admin_cidrs": ["1.2.3.4/32"],
        "optional_es4": {"base_topology": topology, "requested_node": "es4"},
        "stages": {
            "db1": {
                "candidate": {},
                "inputs": {"unused_old_image": None},
                "status": "creating",
                "lineage": None,
                "output": {"public_ip": None, "network_zone": "ru-2a", "roles": ["db"]},
                "identities": {"vm": "saved-id"},
                "intent": {
                    "operation": "create",
                    "kind": "node",
                    "pool": "ru-2",
                    "project_id": "local",
                    "owner": "local",
                    "name_prefix": "local-db1",
                    "resource_types": ["vm"],
                },
                "audit_entries": [
                    {"type": "vm", "attrs": {}, "index": 0, "address": "node[0]"},
                    {"type": "vm", "attrs": {}, "index": None},
                    {"type": "vm", "attrs": {}, "index": "db1"},
                ],
            }
        },
    }
    assert checked(value) is value
    assert checked({}) == {}
    assert checked({"optional_es4": {}}) == {"optional_es4": {}}


def test_wrong_intent_operation_rejected() -> None:
    intent = dict.fromkeys(("operation", "kind", "pool", "project_id", "owner", "name_prefix"), "delete")
    with pytest.raises(ValueError, match="creation intent"):
        checked({"stages": {"es1": {"intent": intent}}})


def test_partial_output_without_role_list() -> None:
    assert checked({"stages": {"network-ru-2": {"output": {"network_id": "saved-network"}}}}) == {
        "stages": {"network-ru-2": {"output": {"network_id": "saved-network"}}}
    }


@pytest.mark.parametrize("region", [None, True, 2, []])
def test_unconfirmed_region_cannot_reach_terraform(region: object) -> None:
    from selectel_staged import terraform_environment

    with pytest.raises(ValueError, match="confirmed region"):
        terraform_environment({"region": "ru-2"}, {"region": region})


def test_corrupt_backend_journal_blocks_before_cloud_clients(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace
    from unittest.mock import Mock

    import selectel_staged

    path = tmp_path / ".local/staged-deployment.json"
    path.parent.mkdir()
    path.write_text("null")
    monkeypatch.setattr(selectel_staged, "load_credentials", lambda: {"project_id": "local-project"})
    cloud = Mock(side_effect=AssertionError("Cloud constructor must not be reached"))
    monkeypatch.setattr(selectel_staged, "SelectelAdapter", cloud)
    with pytest.raises(ValueError, match="stage metadata"):
        selectel_staged.Backend(SimpleNamespace(ROOT=tmp_path))
    cloud.assert_not_called()
    assert path.read_text() == "null"


def test_invalid_partial_identity_is_not_returned() -> None:
    from unittest.mock import Mock

    from selectel_staged import Backend

    backend = object.__new__(Backend)
    backend.journal = {"stages": {"global": {}}}
    backend.store = Mock()
    backend.store.entries.return_value = [{"type": "router", "attrs": {"id": 3}}]
    with pytest.raises(ValueError, match="identity is unconfirmed"):
        backend.saved_id("global", "router", "router_id")


@pytest.mark.parametrize("payload", ["null", "[]", '{"id": 3}', '{"roles": [null]}'])
def test_terraform_output_is_checked_before_return(tmp_path: Path, payload: str) -> None:
    from unittest.mock import Mock, patch

    from stage_store import StageStore

    api = Mock(spec_set=["ROOT", "TF", "RUN", "redact"])
    api.ROOT = tmp_path
    store = StageStore(api, {})
    with (
        patch.object(store, "tf", side_effect=["", "", "{}", "", payload]),
        patch.object(store, "identities", return_value={}),
        pytest.raises(ValueError),
    ):
        store.apply("es1")
