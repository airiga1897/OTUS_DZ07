"""Unknown quotas and VM diagnostics cannot authorize capacity retries."""

import copy
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from cloud_contract import TerraformError
from pool_selection import CandidateUnavailable, PoolReader, capacity, scheduler_unavailable
from quota_types import checked_quota
from selectel_staged_capacity import free, with_credit


@pytest.mark.parametrize("value", [None, [], {}, {"limit": None}, {"limit": True}, {"limit": 1.0}, {"limit": -2}])
def test_invalid_api_limit_is_not_a_valid_budget(value: object) -> None:
    with pytest.raises(ValueError, match="quota API limit"):
        checked_quota(value)


@pytest.mark.parametrize("field", ["used", "in_use", "reserved"])
@pytest.mark.parametrize("value", [None, [], "1", True, -1])
def test_unknown_or_negative_usage_is_not_zero(field: str, value: object) -> None:
    quota = {"limit": 10, field: value}
    with pytest.raises(ValueError, match="quota API usage"):
        capacity(quota, 1)
    with pytest.raises(ValueError):
        free(quota)
    with pytest.raises(ValueError):
        with_credit(quota, 1)


def test_known_quota_and_owned_credits_keep_the_existing_precedence_and_reservations() -> None:
    quota = {"limit": 10, "used": 4, "reserved": 1}
    original = copy.deepcopy(quota)
    capacity(quota, 5)
    with pytest.raises(CandidateUnavailable, match="Insufficient shared quota"):
        capacity(quota, 6)
    assert free(with_credit(quota, 2)) == 7
    assert free(with_credit(quota, 20)) == 9
    assert free({"limit": 10, "in_use": 1, "used": 4, "reserved": 1}) == 8
    assert quota == original
    capacity({"limit": -1}, 10**18)
    assert free({"limit": -1}) == 10**15
    capacity({"limit": 0}, 0)
    assert checked_quota({"limit": 10}) == {"limit": 10}


def test_absent_quotas_keep_their_candidate_rejection_contract() -> None:
    with pytest.raises(CandidateUnavailable, match="Required quota unavailable"):
        capacity(None, 1)
    with pytest.raises(CandidateUnavailable, match="Required quota is unknown"):
        free(None)
    with pytest.raises(CandidateUnavailable, match="Required quota is unknown"):
        with_credit(None, 1)


def owned_state(root: Path) -> Path:
    folder = root / "terraform-selectel"
    folder.mkdir()
    path = folder / "terraform.tfstate"
    path.write_text(
        json.dumps(
            {
                "resources": [
                    {
                        "mode": "managed",
                        "type": "openstack_compute_instance_v2",
                        "instances": [{"index_key": "app", "attributes": {"id": "own", "name": "otus-dz07-app"}}],
                    }
                ]
            }
        )
    )
    return path


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        {},
        {"server": None},
        {"server": {}},
        {"server": {"name": "otus-dz07-app", "status": None}},
        {"server": {"name": "otus-dz07-app", "status": "ERROR", "fault": None}},
        {"server": {"name": "otus-dz07-app", "status": "ERROR", "fault": {"message": []}}},
    ],
)
def test_malformed_fault_response_never_becomes_a_capacity_retry(tmp_path: Path, body: object) -> None:
    path = owned_state(tmp_path)
    original = path.read_bytes()
    reader = Mock(spec_set=PoolReader)
    reader.get.return_value = body
    with pytest.raises(ValueError):
        scheduler_unavailable(TerraformError("generic failure"), tmp_path, lambda: reader, "otus-dz07")
    reader.get.assert_called_once_with("compute", "/servers/own")
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "status,fault,expected",
    [("ACTIVE", None, False), ("ERROR", {}, False), ("ERROR", {"message": "No valid host"}, True)],
)
def test_only_confirmed_capacity_faults_allow_retry(tmp_path: Path, status: str, fault: object, expected: bool) -> None:
    path = owned_state(tmp_path)
    reader = Mock(spec_set=PoolReader)
    reader.get.return_value = {"server": {"name": "otus-dz07-app", "status": status, "fault": fault}}
    assert scheduler_unavailable(TerraformError("generic failure"), tmp_path, lambda: reader, "otus-dz07") is expected
    assert path.exists()


def test_deploy_keeps_original_terraform_error_when_capacity_diagnostics_lose_connection(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import shutil
    from unittest.mock import patch

    from runtime import create_runtime

    source = Path(__file__).resolve().parents[2]
    shutil.copytree(source / "config", tmp_path / "config")
    runtime = create_runtime(tmp_path)
    runtime.LOCAL.mkdir()
    key = runtime.LOCAL / "otus_dz07"
    key.write_bytes(b"PRIVATE recovery key")
    failure = TerraformError("generic PRIVATE infrastructure failure")
    reader = Mock(spec_set=PoolReader)
    reader.get.side_effect = OSError("PRIVATE connection failure")

    def apply(api: object, desired: object, allowed_new_nodes: object = None) -> None:
        owned_state(tmp_path)
        raise failure

    with (
        patch("test_cycle.require_access"),
        patch.object(runtime, "managed_ids", return_value={}),
        patch("infrastructure.apply", side_effect=apply),
        patch.object(runtime, "cloud", return_value=reader),
        patch.object(runtime, "diagnostics"),
        patch("placement_diagnostics.preserve"),
        patch.object(runtime, "cleanup") as cleanup,
        pytest.raises(TerraformError) as caught,
    ):
        runtime.deploy(initial_creation=True)
    assert caught.value is failure
    assert json.loads((tmp_path / "terraform-selectel/terraform.tfstate").read_text())["resources"]
    assert key.read_bytes() == b"PRIVATE recovery key"
    assert "PRIVATE" not in caplog.text
    cleanup.assert_not_called()


def test_capacity_confirmation_preserves_first_diagnostic_and_survives_logger_failure(tmp_path: Path) -> None:
    from unittest.mock import patch

    import pool_selection

    owned_state(tmp_path)
    failure = TerraformError("generic failure")
    failure.__dict__["diagnostics_error_type"] = "FirstDiagnosticError"
    reader = Mock(spec_set=PoolReader)
    reader.get.side_effect = OSError("PRIVATE connection")
    with patch.object(pool_selection.LOGGER, "warning", side_effect=OSError("PRIVATE logger")):
        assert pool_selection.confirmed_capacity(failure, tmp_path, lambda: reader, "otus-dz07") is False
    assert failure.__dict__["diagnostics_error_type"] == "FirstDiagnosticError"
    assert failure.__dict__["capacity_diagnostics_error_type"] == "OSError"
    assert "PRIVATE" not in json.dumps(failure.__dict__)


def test_confirmed_host_shortage_does_not_need_a_diagnostic_api_request(tmp_path: Path) -> None:
    from pool_selection import confirmed_capacity

    factory = Mock()
    assert confirmed_capacity(TerraformError("No valid host"), tmp_path, factory, "otus-dz07") is True
    factory.assert_not_called()


def test_diagnostic_access_denial_preserves_original_error_and_logs_only_status(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import http.client
    import urllib.error

    from pool_selection import confirmed_capacity

    owned_state(tmp_path)
    failure = TerraformError("generic failure")
    reader = Mock(spec_set=PoolReader)
    reader.get.side_effect = urllib.error.HTTPError(
        "https://host.invalid/?signature=PRIVATE", 403, "PRIVATE response", http.client.HTTPMessage(), None
    )
    assert confirmed_capacity(failure, tmp_path, lambda: reader, "otus-dz07") is False
    assert failure.__dict__["diagnostics_error_type"] == "HTTPError"
    assert "HTTP 403" in caplog.text
    assert "PRIVATE" not in caplog.text
