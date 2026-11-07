"""Inventory uses one saved operation snapshot, without a second state read."""

from pathlib import Path
from unittest.mock import Mock, patch

import configuration
import pytest
from inventory_types import Inventory


@pytest.mark.parametrize(
    "saved,expected", [({"bootstrap": True, "finalized": False}, True), (None, False), ({"finalized": True}, False)]
)
def test_bootstrap_uses_one_operation_snapshot(tmp_path: Path, saved: object, expected: bool) -> None:
    local = tmp_path / ".local"
    local.mkdir()
    (local / "secrets.json").write_text("{}")
    api = Mock(ROOT=tmp_path, LOCAL=local, STACK={})
    value: Inventory = {"all": {"vars": {}}}
    with (
        patch("render.ansible_inventory", return_value=value),
        patch("ansible_operation.read", side_effect=[saved, None]) as read,
    ):
        result = configuration.operation_inventory(api, existing=True)
    assert result["all"]["vars"]["dz07_bootstrap"] is expected
    read.assert_called_once_with(api)
