"""Destructive, explicitly selected test cycle, exclusively owning a fresh stand."""

import json
import logging
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from cloud_settings import terraform_root
from lifecycle_types import CycleReport, DiagnosticField
from runtime_ports import CycleAccessPort, CycleProgressPort, FilePort, OperationPort, ServerDeliveryPort
from selectel_staged_audit import PageClient
from topology import Topology, assert_preserved
from verification_types import ExpansionSnapshot, VerificationReport

LOGGER = logging.getLogger(__name__)


class ReadOnlyCloudPort(Protocol):
    def resource_regions(self) -> list[str]: ...
    def read_only_pool(self, region: str) -> PageClient: ...


class CycleStagePort(ServerDeliveryPort, OperationPort, CycleProgressPort, Protocol):
    def desired_topology(self) -> Topology: ...
    def deploy_selected(self) -> None: ...
    def managed_ids(self) -> dict[object, str]: ...
    def deploy(self) -> None: ...
    def verify(self, failover: bool = False) -> VerificationReport: ...


class CycleRunPort(CycleStagePort, CycleAccessPort, Protocol):
    ROOT: Path
    _TOPOLOGY_OVERRIDE: Topology | None

    def cloud(self, bind: bool = False) -> ReadOnlyCloudPort: ...
    def cleanup(self) -> None: ...


def assert_empty(root: str | Path) -> None:
    """Read-only local guard also used before purging reusable artifacts."""
    from local_runtime import checked_tree

    root = Path(root).resolve()
    checked_tree(root / ".local", root)
    for directory in root.glob("terraform*"):
        if not directory.is_dir():
            continue
        checked_tree(directory, root)
        for path in directory.glob("*.tfstate*"):
            if path.name.endswith(".lock.info"):
                raise ValueError("Unfinished Terraform operation")
            state = json.loads(path.read_text())
            if state.get("resources") or state.get("outputs"):
                raise ValueError("Non-empty state; operation refused")
        if list(directory.glob("*.tfplan")):
            raise ValueError("Unfinished Terraform operation")
    stages = root / ".local/stages"
    if stages.exists() and any(stages.rglob("*.tfstate*")):
        raise ValueError("Sequential stage states require confirmed cleanup before test-cycle")
    pending = (
        "staged-deployment.json",
        "context.json",
        "selectel-context.json",
        "placement.json",
        "configured.json",
        "cleanup-fips.json",
        "expansion-pending.json",
        "test-cycle.json",
        "es-failover.json",
    )
    if any((root / ".local" / name).exists() for name in pending):
        raise ValueError("Existing deployment or unfinished operation")


def require_access(api: CycleAccessPort, cleanup: bool = False) -> None:
    marker = api.LOCAL / "test-cycle.json"
    if marker.exists():
        value = json.loads(marker.read_text(encoding="utf-8"))
        if value["run_id"] != api._TEST_CYCLE_ID and not (cleanup and value.get("status") == "cleanup_failed"):
            raise RuntimeError("An unfinished test-cycle owns this clone; preserve its recovery files")


def reject_existing(api: CycleRunPort) -> None:
    """Read local ownership before authentication and all cloud namespaces before writes."""
    try:
        assert_empty(api.ROOT)
    except ValueError as error:
        raise RuntimeError(str(error)) from error
    if api.managed_ids():
        raise RuntimeError("test-cycle requires an empty stand")
    for state in api.ROOT.glob("terraform*/terraform.tfstate*"):
        from local_runtime import checked_tree

        checked_tree(state, api.ROOT.resolve())
        data = json.loads(state.read_text())
        if data.get("outputs") or any(r.get("instances") for r in data.get("resources", [])):
            raise RuntimeError("test-cycle refuses a nonempty state or backup")
    pending = [
        "context.json",
        "placement.json",
        "configured.json",
        "cleanup-fips.json",
        "expansion-pending.json",
        "test-cycle.json",
    ]
    if (
        any((api.LOCAL / name).exists() for name in pending)
        or (terraform_root(api.ROOT) / "terraform.tfvars.json").exists()
    ):
        raise RuntimeError("test-cycle refuses an existing context or unfinished operation")
    cloud = api.cloud(bind=False)
    for region in cloud.resource_regions():
        reject_cloud_resources(api, cloud.read_only_pool(region))


def reject_cloud_resources(api: FilePort, cloud: PageClient) -> None:
    from cloud_settings import adapter

    adapter().reject_cloud_resources(api, cloud)


def check_original(api: CycleStagePort, snapshot: Mapping[str, object] | ExpansionSnapshot) -> None:
    from verification import expansion

    expansion(api, snapshot)


def progress(api: CycleProgressPort, stage: str) -> None:
    report = getattr(api, "_CYCLE_PROGRESS", None)
    if isinstance(report, dict):
        if report["stage"] != "starting":
            report["completed_steps"].append(report["stage"])
        report["stage"] = stage
        report["steps"].append(stage)
        api.save(api.ROOT / "docs/test-cycle-result.json", report)


def stages(api: CycleStagePort) -> CycleReport:
    from verification import snapshot

    initial = api.desired_topology()
    if "es4" in initial["nodes"]:
        raise ValueError("Optional es4 is excluded from test-cycle")
    progress(api, "initial_deploy")
    api.deploy_selected()
    before = api.managed_ids()
    progress(api, "control_data_and_media")
    canary = snapshot(api, api.lab_output())
    import observability

    observability.command(api, "checkpoint")
    progress(api, "repeat_deploy")
    api.deploy()
    assert_preserved(before, api.managed_ids())
    check_original(api, canary)
    if api.managed_ids() != before:
        raise RuntimeError("Repeated deploy changed resource identities or added nodes")
    observability.command(api, "preservation")
    progress(api, "application_and_logs")
    api.verify(False)
    progress(api, "stages_completed")
    return {
        "success": True,
        "existing_ids_preserved": True,
        "repeat_deploy_verified": True,
        "database_nodes": 1,
        "elasticsearch_nodes": sum(n["role"] == "es" for n in initial["nodes"].values()),
        "elasticsearch_documents_preserved": True,
        "application_and_logs_verified": True,
        "original_data_and_media_preserved": True,
    }


def run(api: CycleRunPort) -> None:
    from stage_store import operation

    with operation(api.LOCAL):
        return _run_locked(api)


def _run_locked(api: CycleRunPort) -> None:
    cycle_policy = None
    if (api.ROOT / "config/placement.json").exists():
        from run_settings import placement

        cycle_policy = placement(api.ROOT)
    reject_existing(api)
    # Snapshot configuration in memory: the repository topology is never rewritten.
    api.LOCAL.mkdir(parents=True, exist_ok=True)
    marker = api.LOCAL / "test-cycle.json"
    run_id = uuid.uuid4().hex
    with marker.open("x", encoding="utf-8") as stream:
        json.dump({"run_id": run_id, "status": "running"}, stream)
    api._TEST_CYCLE_ID = run_id
    original = api._TOPOLOGY_OVERRIDE
    (api.ROOT / "docs").mkdir(parents=True, exist_ok=True)
    result: CycleReport = {
        "run_id": run_id,
        "started_utc": datetime.now(UTC).isoformat(),
        "success": False,
        "stages_completed": False,
        "state_and_api_cleanup_verified": False,
        "steps": [],
        "completed_steps": [],
        "stage": "starting",
    }
    if cycle_policy is not None:
        result["require_distinct_hosts"] = cycle_policy.get("require_distinct_hosts", True)
        result["diagnostic_colocation_allowed"] = not result["require_distinct_hosts"]
    api._CYCLE_PROGRESS = result
    try:
        api.save(api.ROOT / "docs/test-cycle-result.json", result)
        result.update(stages(api))
        result["stages_completed"] = True
    except BaseException as error:
        result["success"] = False
        result["error_type"] = type(error).__name__
        result["failed_stage"] = result["stage"]
        context = getattr(error, "api_error_context", None)
        if isinstance(context, dict):
            result["api_error_context"] = context
        fields: tuple[DiagnosticField, ...] = (
            "terraform_stage",
            "terraform_operation",
            "failure_evidence",
            "diagnostics_error_type",
        )
        for key in fields:
            detail = getattr(error, key, None)
            if isinstance(detail, str):
                result[key] = detail
        raise
    finally:
        api._TOPOLOGY_OVERRIDE = original
        try:
            require_access(api)
            api.cleanup()
            result["state_and_api_cleanup_verified"] = True
        except BaseException as error:
            result["success"] = False
            result["cleanup_error_type"] = type(error).__name__
            cleanup_evidence = getattr(error, "failure_evidence", None)
            if isinstance(cleanup_evidence, str):
                result["cleanup_failure_evidence"] = cleanup_evidence
            api.save(marker, {"run_id": run_id, "status": "cleanup_failed"})
            LOGGER.error("Test cleanup failed: preserve state, context and keys; retry destroy")
            raise
        finally:
            api._TEST_CYCLE_ID = None
            api._CYCLE_PROGRESS = None
            result["finished_utc"] = datetime.now(UTC).isoformat()
            api.save(api.ROOT / "docs/test-cycle-result.json", result)
