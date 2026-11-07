"""Unknown owned-volume sizes cannot reduce the quota demand of a redeploy."""

import json
from pathlib import Path

import pytest
from pool_selection import remaining_demand


@pytest.mark.parametrize("size", [None, True, -1, "20"])
def test_unknown_volume_size_blocks_credit_without_changing_state(tmp_path: Path, size: object) -> None:
    path = tmp_path / "terraform-selectel/terraform.tfstate"
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {
                "resources": [
                    {
                        "mode": "managed",
                        "type": "openstack_blockstorage_volume_v3",
                        "instances": [{"attributes": {"size": size}}],
                    }
                ]
            }
        )
    )
    original = path.read_bytes()
    with pytest.raises(ValueError, match="volume size"):
        remaining_demand(tmp_path, {"disk_gb": 40, "port": 0}, {})
    assert path.read_bytes() == original


def test_unknown_namespace_name_does_not_mean_empty_project() -> None:
    from unittest.mock import Mock

    from pool_selection import reject_orphans

    client = Mock()
    client.get.side_effect = [{"servers": [{"name": None}]}, {"networks": []}]
    with pytest.raises(ValueError, match="name is unconfirmed"):
        reject_orphans(client, "otus-dz07", [])


@pytest.mark.parametrize("region", [None, ""])
def test_unconfirmed_selection_keeps_local_metadata(tmp_path: Path, region: object) -> None:
    from pool_selection import select

    with pytest.raises(ValueError, match="confirmed pool"):
        select(tmp_path, {"region": region}, "synthetic-project", "context.json")
    assert not (tmp_path / ".local/placement.json").exists()
    assert not (tmp_path / ".local/context.json").exists()


def test_unknown_saved_pool_keeps_recovery_metadata(tmp_path: Path) -> None:
    from pool_selection import select

    local = tmp_path / ".local"
    local.mkdir()
    saved = {
        "project_id": "synthetic-project",
        "provider": "selectel",
        "namespace": "otus-dz07",
        "phase": "destroyed",
        "region": None,
    }
    path = local / "placement.json"
    path.write_text(json.dumps(saved))
    context = local / "context.json"
    context.write_text('{"keep":"recovery"}')
    original = path.read_bytes(), context.read_bytes()
    with pytest.raises(ValueError, match="Saved placement pool is unconfirmed"):
        select(tmp_path, {"region": "ru-2"}, "synthetic-project", "context.json")
    assert (path.read_bytes(), context.read_bytes()) == original
