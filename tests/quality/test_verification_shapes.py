"""Recovery checkpoints must not supply shell syntax to remote verification."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from verification import snapshot


@pytest.mark.parametrize("run_id", ["probe; touch /tmp/other", "a\nb", "../other", None])
def test_unsafe_saved_run_id_blocks_before_ssh(tmp_path: Path, run_id: object) -> None:
    path = tmp_path / "expansion-pending.json"
    path.write_text(json.dumps({"run_id": run_id}))
    original = path.read_bytes()
    api = Mock(LOCAL=tmp_path)
    with pytest.raises(ValueError, match="verification"):
        snapshot(api, {})
    api.connect.assert_not_called()
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {1: "bad"},
        {"run_id": ""},
        {"run_id": "x" * 129},
        {"run_id": "проверка"},
        {"before": None},
        {"before": {"new_record": []}},
        {"server_ids": []},
        {"server_ids": {"app": None}},
        {"elastic_checkpoint": "true"},
    ],
)
def test_malformed_checkpoint_does_not_expose_values(value: object) -> None:
    from verification_types import checked

    with pytest.raises(ValueError, match="verification"):
        checked(value)


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"run_id": "pending"},
        {"run_id": "a" * 32, "before": {}},
        {"run_id": "a" * 32, "server_ids": {"app": "local"}, "before": {}},
    ],
)
def test_incomplete_checkpoint_cannot_start_remote_check(value: object) -> None:
    from verification import expansion

    api = Mock()
    with pytest.raises(ValueError, match="Incomplete verification"):
        expansion(api, value)
    assert api.mock_calls == []


def test_valid_and_partial_checkpoints_remain_readable() -> None:
    from verification_types import checked

    value = {
        "run_id": "legacy-check_1",
        "before": {"new_record": {"id": 12}},
        "server_ids": {"app": "synthetic-vm"},
        "elastic_checkpoint": True,
        "unrelated_metadata": None,
    }
    assert checked(value) is value
    assert checked({"run_id": "pending"}) == {"run_id": "pending"}
    assert checked({"server_ids": {}, "elastic_checkpoint": False}) == {"server_ids": {}, "elastic_checkpoint": False}
