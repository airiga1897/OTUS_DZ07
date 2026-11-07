"""Preserve provider faults before deletion; never allocate or mutate resources."""

import contextlib
import json
import logging
import os
import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, NotRequired, Protocol, TypedDict

import privacy
from cloud_settings import terraform_root
from credentials import credential_values
from local_runtime import checked_tree
from runtime_ports import FilePort, PlacementIdentity
from selectel_adapter import SelectelAdapter

LOGGER = logging.getLogger(__name__)


class TerraformEvidencePort(Protocol):
    ROOT: Path

    def redact(self, text: str) -> str: ...


class PlacementEvidencePort(FilePort, Protocol):
    def placement(self) -> PlacementIdentity: ...
    def cloud(self) -> SelectelAdapter: ...


class PlacementFaultReport(TypedDict):
    region: str | None
    availability_zone: str | None
    error_type: str
    nodes: dict[str, dict[str, object]]
    read_only: Literal[True]
    provider_error: NotRequired[dict[str, object]]
    terraform_error: NotRequired[str]


def capture_terraform(
    api: TerraformEvidencePort,
    stage: str,
    operation: str,
    candidate: Mapping[str, object],
    output: str,
    returncode: int,
) -> str:
    """Persist the original command failure independently of disposable run logs."""
    root = Path(api.ROOT).resolve()
    error_text = api.redact(output)
    report: dict[str, object] = {
        "stage": stage,
        "operation": operation,
        "returncode": returncode,
        "run_id": getattr(api, "_TEST_CYCLE_ID", None),
        "recorded_utc": datetime.now(UTC).isoformat(),
        "placement": {
            k: candidate[k]
            for k in ("region", "availability_zone", "network_availability_zone", "router_availability_zone")
            if k in candidate
        },
        "terraform_error": error_text,
    }
    privacy.remember_application(root / ".local")
    # Query strings may contain signed URLs or opaque credentials not registered
    # in memory. Diagnostic endpoint paths remain useful without those queries.
    report["terraform_error"] = re.sub(r"(https?://[^\s?]+)\?[^\s]+", r"\1?[REDACTED]", error_text)
    public_report = privacy.sanitize(report)
    path = root / "docs/terraform-faults" / (stage + "-" + operation + "-" + uuid.uuid4().hex + ".json")
    checked_tree(path, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(public_report, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    return path.relative_to(root).as_posix()


def capture(api: PlacementEvidencePort, error: BaseException) -> Path:
    root = Path(api.ROOT).resolve()
    state_path = terraform_root(root) / "terraform.tfstate"
    checked_tree(state_path, root)
    placement = api.placement()
    report: PlacementFaultReport = {
        "region": placement["region"],
        "availability_zone": placement.get("availability_zone"),
        "error_type": type(error).__name__,
        "nodes": {},
        "read_only": True,
    }
    if state_path.exists():
        state = json.loads(state_path.read_text())
        instances = [
            i
            for r in state.get("resources", [])
            if r.get("mode") == "managed" and r.get("type") == "openstack_compute_instance_v2"
            for i in r.get("instances", [])
        ]
        if instances:
            try:
                collect_nodes(api, instances, report)
            except (OSError, ValueError, KeyError) as diagnostic_error:
                report["provider_error"] = {
                    "type": type(diagnostic_error).__name__,
                    "http_status": getattr(diagnostic_error, "code", None),
                }
    output = getattr(error, "output", None)
    if isinstance(output, str):
        report["terraform_error"] = output
    privacy.remember_application(api.LOCAL)
    path = (
        root
        / "docs/placement-faults"
        / (str(placement.get("availability_zone", "unknown")) + "-" + uuid.uuid4().hex + ".json")
    )
    api.save(path, privacy.sanitize(report))
    return path


def collect_nodes(api: PlacementEvidencePort, instances: Sequence[object], report: PlacementFaultReport) -> None:
    cloud = api.cloud()
    privacy.remember(credential_values(cloud.config))
    for instance in instances:
        if not isinstance(instance, dict) or not isinstance(instance.get("attributes", {}), dict):
            raise ValueError("Unknown compute state diagnostic shape")
        attrs, name = instance.get("attributes", {}), str(instance.get("index_key", ""))
        expected = api.PREFIX + "-" + name
        if (
            attrs.get("name") != expected
            or attrs.get("tenant_id") not in (None, "", cloud.config["project_id"])
            or attrs.get("region") not in (None, "", report["region"])
        ):
            report["nodes"][name] = {"diagnostic_error": "State ownership mismatch; query refused"}
            continue
        try:
            identifier = attrs.get("id")
            if not isinstance(identifier, str) or not identifier:
                raise ValueError("Unknown compute state diagnostic identity")
            server = cloud.get("compute", "/servers/" + identifier)["server"]
            if not isinstance(server, dict):
                raise ValueError("Unknown compute API diagnostic shape")
            if server.get("name") != expected or server.get("tenant_id") != cloud.config["project_id"]:
                report["nodes"][name] = {"diagnostic_error": "API ownership mismatch; details omitted"}
                continue
            report["nodes"][name] = {
                key: server.get(key)
                for key in (
                    "status",
                    "fault",
                    "OS-EXT-STS:vm_state",
                    "OS-EXT-STS:task_state",
                    "OS-EXT-AZ:availability_zone",
                )
            }
        except (OSError, KeyError, ValueError) as error:
            report["nodes"][name] = {
                "diagnostic_error": type(error).__name__,
                "http_status": getattr(error, "code", None),
            }


def preserve(api: PlacementEvidencePort, error: BaseException) -> Path | None:
    """A diagnostic API/storage error must never prevent the required cleanup."""
    try:
        return capture(api, error)
    except Exception as diagnostic_error:
        # Failure reporting must not replace the original infrastructure error.
        with contextlib.suppress(Exception):
            LOGGER.error(
                "Could not preserve placement fault (%s); cleanup must continue", type(diagnostic_error).__name__
            )
        return None
