"""Fault-report failures cannot replace the failed deployment's exception."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import placement_diagnostics as diagnostics
import pytest


@pytest.mark.parametrize("logging_failure", [False, True])
def test_unexpected_diagnostic_failure_preserves_primary_error(tmp_path: Path, logging_failure: bool) -> None:
    primary = RuntimeError("original infrastructure failure")
    api = SimpleNamespace(ROOT=tmp_path)
    with (
        patch.object(diagnostics, "capture", side_effect=TypeError("PRIVATE body")),
        patch.object(diagnostics.LOGGER, "error", side_effect=OSError("PRIVATE log") if logging_failure else None),
        pytest.raises(RuntimeError) as caught,
    ):
        try:
            raise primary
        except RuntimeError:
            assert diagnostics.preserve(api, primary) is None
            raise
    assert caught.value is primary


def diagnostic_fixture(tmp_path: Path, instance: object) -> tuple[SimpleNamespace, Mock, Path]:
    folder = tmp_path / "terraform-selectel"
    folder.mkdir()
    state = folder / "terraform.tfstate"
    state.write_text(
        json.dumps(
            {"resources": [{"mode": "managed", "type": "openstack_compute_instance_v2", "instances": [instance]}]}
        )
    )
    cloud = Mock()
    cloud.config = {"project_id": "synthetic-project"}

    def save(path: Path, value: object) -> None:
        assert path.resolve().is_relative_to(tmp_path.resolve())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    api = SimpleNamespace(
        ROOT=tmp_path,
        LOCAL=tmp_path / ".local",
        PREFIX="otus-dz07",
        placement=lambda: {"region": "ru-2", "availability_zone": "ru-2b"},
        cloud=lambda: cloud,
        save=save,
    )
    return api, cloud, state


@pytest.mark.parametrize("instance", [None, [], {"attributes": None}, {"attributes": []}])
def test_invalid_state_shape_is_reported_without_query_or_state_change(tmp_path: Path, instance: object) -> None:
    api, cloud, state = diagnostic_fixture(tmp_path, instance)
    before = state.read_bytes()
    path = diagnostics.capture(api, RuntimeError("synthetic failure"))
    report = json.loads(path.read_text())
    assert report["provider_error"]["type"] == "ValueError"
    assert report["nodes"] == {}
    cloud.get.assert_not_called()
    assert state.read_bytes() == before


@pytest.mark.parametrize("identifier", [None, [], ""])
def test_invalid_owned_instance_id_is_not_sent_to_api(tmp_path: Path, identifier: object) -> None:
    api, cloud, state = diagnostic_fixture(
        tmp_path,
        {
            "index_key": "app",
            "attributes": {"name": "otus-dz07-app", "tenant_id": "synthetic-project", "id": identifier},
        },
    )
    before = state.read_bytes()
    report = json.loads(diagnostics.capture(api, RuntimeError("synthetic failure")).read_text())
    assert report["nodes"]["app"]["diagnostic_error"] == "ValueError"
    cloud.get.assert_not_called()
    assert state.read_bytes() == before


@pytest.mark.parametrize("server", [None, [], "PRIVATE"])
def test_unknown_api_record_cannot_be_reported_as_owned_fault(tmp_path: Path, server: object) -> None:
    api, cloud, state = diagnostic_fixture(
        tmp_path,
        {"index_key": "app", "attributes": {"name": "otus-dz07-app", "tenant_id": "synthetic-project", "id": "own-id"}},
    )
    cloud.get.return_value = {"server": server}
    before = state.read_bytes()
    path = diagnostics.capture(api, RuntimeError("synthetic failure"))
    assert json.loads(path.read_text())["nodes"]["app"]["diagnostic_error"] == "ValueError"
    assert "PRIVATE" not in path.read_text()
    cloud.get.assert_called_once_with("compute", "/servers/own-id")
    assert state.read_bytes() == before
