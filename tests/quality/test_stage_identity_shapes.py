"""Unconfirmed saved resource IDs cannot pass the retained-stage plan boundary."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from stage_store import StageStore


@pytest.mark.parametrize("identifier", [None, [], {}, 0, True, ""])
def test_invalid_saved_identity_blocks_plan_preparation(tmp_path: Path, identifier: object) -> None:
    store = StageStore(SimpleNamespace(ROOT=tmp_path), {})
    folder = store.folder("es1")
    folder.mkdir(parents=True)
    state = folder / "terraform.tfstate"
    state.write_text(
        json.dumps(
            {
                "resources": [
                    {
                        "mode": "managed",
                        "type": "openstack_compute_instance_v2",
                        "name": "node",
                        "instances": [{"attributes": {"id": identifier}}],
                    }
                ]
            }
        )
    )
    original = state.read_bytes()
    with pytest.raises(ValueError, match="resource identity is unconfirmed"):
        store.identities("es1")
    assert state.read_bytes() == original
