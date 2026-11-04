"""Incomplete ownership records must block recovery rather than authorize cleanup."""

from dataclasses import dataclass, field

import pytest
import selectel_intent as intents
from terraform_state_types import StateEntry
from topology import Topology


@dataclass
class Namespace:
    PREFIX: str = "otus-dz07"


def initial_topology() -> Topology:
    return {"nodes": {"app": {"host": 10, "role": "app"}}}


@dataclass
class IntentFixture:
    desired: Topology = field(default_factory=initial_topology)
    credentials: dict[str, str] = field(default_factory=lambda: {"project_id": "synthetic-project"})
    owner: str = "synthetic-owner"
    api: Namespace = field(default_factory=Namespace)
    journal: dict[str, object] = field(default_factory=dict)


def fixture(record: object) -> IntentFixture:
    return IntentFixture(journal={"stages": {"app": record}})


def saved() -> IntentFixture:
    backend = fixture({})
    backend.journal["stages"] = {
        "app": {"candidate": {"region": "ru-2"}, "intent": intents.intent(backend, "app", {"region": "ru-2"})}
    }
    return backend


@pytest.mark.parametrize("pool", [None, [], {}, True, 1, ""])
def test_creation_requires_a_confirmed_pool(pool: object) -> None:
    with pytest.raises(ValueError, match="confirmed pool"):
        intents.intent(fixture({}), "app", {"region": pool})


@pytest.mark.parametrize("stages", [None, [], {}, {"app": None}, {"app": []}])
def test_missing_saved_stage_is_not_valid_recovery(stages: object) -> None:
    backend = IntentFixture(journal={"stages": stages})
    with pytest.raises(ValueError, match="saved stage record"):
        intents.validate(backend, "app")


@pytest.mark.parametrize("candidate", [None, [], "PRIVATE"])
def test_placement_shape_must_be_known_before_comparing_ownership(candidate: object) -> None:
    backend = fixture({"candidate": candidate, "intent": {"operation": "create"}})
    with pytest.raises(ValueError, match="saved stage placement") as error:
        intents.validate(backend, "app")
    assert "PRIVATE" not in str(error.value)


@pytest.mark.parametrize("identifier", [None, [], {}, 0, ""])
def test_unknown_state_id_cannot_authorize_recovery(identifier: object) -> None:
    entries: list[StateEntry] = [{"type": "openstack_compute_instance_v2", "attrs": {"id": identifier}}]
    with pytest.raises(ValueError, match="saved resource identity"):
        intents.check_unrecorded(saved(), "app", entries, {})


@pytest.mark.parametrize("field", ["name", "description"])
@pytest.mark.parametrize("value", [[], {}, True, 0])
def test_unknown_api_ownership_fields_block_audit(field: str, value: object) -> None:
    found = {"openstack_compute_instance_v2": {"synthetic-id": {field: value}}}
    with pytest.raises(ValueError, match="ownership fields"):
        intents.check_unrecorded(saved(), "app", [], found)


def test_nullable_optional_ownership_fields_do_not_match_namespace() -> None:
    intents.check_unrecorded(
        saved(), "app", [], {"openstack_compute_instance_v2": {"foreign-id": {"name": None, "description": None}}}
    )


def test_legacy_record_without_intent_is_not_rewritten() -> None:
    record = {"candidate": None}
    backend = fixture(record)
    assert intents.validate(backend, "app") is None
    assert backend.journal == {"stages": {"app": record}}
