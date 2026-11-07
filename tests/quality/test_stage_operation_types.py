"""Only confirmed pools reach network creation and retained-stage inputs."""

from unittest.mock import Mock

import pytest
from selectel_stage_operations import candidate_region, ensure_pool


@pytest.mark.parametrize("value", [None, True, 2, [], {}, ""])
def test_unknown_pool_never_reaches_infrastructure(value: object) -> None:
    backend = Mock()
    with pytest.raises(ValueError, match="confirmed pool"):
        ensure_pool(backend, {"region": value})
    assert backend.mock_calls == []


def test_confirmed_pool_is_preserved() -> None:
    assert candidate_region({"region": "ru-2", "unrelated": None}) == "ru-2"


def test_missing_pool_is_rejected() -> None:
    with pytest.raises(ValueError, match="confirmed pool"):
        candidate_region({})


@pytest.mark.parametrize("size", [None, True, "20", [], -1])
def test_invalid_pending_volume_size_cannot_credit_quota(size: object) -> None:
    from pool_selection import CandidateUnavailable
    from selectel_staged import Backend
    from selectel_staged_capacity import pending_credits
    from stage_store import StageStore

    backend = object.__new__(Backend)
    backend.desired = {"nodes": {"es1": {"host": 31, "role": "es"}}}
    backend.policy = {"profiles": {"es": {"vcpus": 2, "ram_mb": 8192}}}
    backend.journal = {"stages": {"es1": {"status": "pending", "candidate": {"region": "ru-2"}}}}
    backend.store = Mock(spec_set=StageStore)
    entries = [{"type": "openstack_blockstorage_volume_v3", "attrs": {"size": size}}]
    backend.store.entries.return_value = entries
    with pytest.raises(CandidateUnavailable, match="volume size is unconfirmed"):
        pending_credits(backend, "ru-2")
