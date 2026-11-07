"""Explicit optional ES checks and append-only es4 request; ordinary cycles stay compact."""

import copy
import json
import logging
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol, TypedDict, cast

import topology
from cloud_contract import StandDescription
from local_runtime import checked_tree
from placement_config import PlacementPolicy, checked
from runtime_ports import FilePort, OperationPort, RetainedLifecyclePort
from selectel_api import json_object, json_text
from stage_journal import StageJournal
from stage_store import JOURNAL, atomic_json, operation
from verification_types import VerificationReport

LOGGER = logging.getLogger(__name__)
MARKER = "es-failover.json"


class OptionalSettings(TypedDict):
    es4: topology.Node
    restore_attempts: int
    restore_delay_seconds: int


def settings(root: Path) -> OptionalSettings:
    value = json.loads((root / "config/optional-elasticsearch.json").read_text())
    if (
        not isinstance(value, dict)
        or set(value) != {"es4", "restore_attempts", "restore_delay_seconds"}
        or not isinstance(value["es4"], dict)
        or set(value["es4"]) != {"host", "role"}
        or value["es4"]["role"] != "es"
        or type(value["es4"]["host"]) is not int
        or not 2 <= value["es4"]["host"] <= 254
        or type(value["restore_attempts"]) is not int
        or not 1 <= value["restore_attempts"] <= 5
        or type(value["restore_delay_seconds"]) is not int
        or not 0 <= value["restore_delay_seconds"] <= 30
    ):
        raise ValueError("Invalid optional Elasticsearch settings")
    return cast(OptionalSettings, value)


def desired(root: Path, base: topology.Topology) -> topology.Topology:
    path = root / ".local" / JOURNAL
    checked_tree(path, root.resolve())
    if not path.exists():
        return base
    journal = json.loads(path.read_text())
    record = journal.get("optional_es4")
    if record is None:
        return base
    expected = copy.deepcopy(base)
    expected["nodes"]["es4"] = settings(root)["es4"]
    if record != {"base_topology": base, "requested_node": "es4"} or journal["topology"] != expected:
        raise ValueError("Saved es4 request conflicts with the base topology; preserve recovery data")
    topology.validate_extension(base, expected)
    return expected


class PowerClient(Protocol):
    def set_power(self, name: str, server_id: str, *, running: bool, prefix: str) -> None: ...


class FailoverBackend(Protocol):
    journal: StageJournal

    def completed(self, name: str) -> bool: ...
    def region(self, region: str) -> PowerClient: ...
    def save(self) -> None: ...
    def validate_existing(self) -> None: ...


class EsController(RetainedLifecyclePort, Protocol):
    def verify(self, failover: bool = False) -> VerificationReport: ...


def _count(value: object) -> int:
    """Preserve range() acceptance of bool; reject values range() would reject."""
    if isinstance(value, int):
        return value
    raise TypeError("object cannot be interpreted as an integer")


def node_order(policy: object, journal: StageJournal, target: topology.Topology) -> None:
    """Extend only the explicitly retained request, never the base configuration."""
    optional = journal.get("optional_es4")
    if optional is not None:
        checked_policy: PlacementPolicy = checked(policy)
        base = optional["base_topology"]
        if topology.validate_extension(base, target) != {"es4"} or set(
            checked_policy["sequential_placement"]["node_order"]
        ) != set(base["nodes"]):
            raise ValueError("Invalid saved optional node order")
        order = checked_policy["sequential_placement"]["node_order"]
        checked_policy["sequential_placement"]["node_order"] = [*order, "es4"]


def require_no_failover(api: FilePort) -> None:
    path = api.LOCAL / MARKER
    checked_tree(path, Path(api.ROOT).resolve())
    if path.exists():
        raise ValueError("Restore the saved ES operation with verify --es-failover before continuing")


def add_es4(api: EsController) -> None:
    from ansible_operation import pending
    from staged_runtime import backend_factory, deploy
    from test_cycle import require_access

    require_access(api)
    with operation(api.LOCAL):
        require_no_failover(api)
        backend = backend_factory(api, cleanup=True)
        journal = backend.journal
        if not journal.get("configured") or journal.get("destroy_requested"):
            raise ValueError("add-es4 requires a configured existing stand")
        if journal.get("optional_es4") is None:
            if pending(api):
                raise ValueError("Finish the saved Ansible operation before requesting es4")
            base = copy.deepcopy(journal["topology"])
            if base != topology.load(api.ROOT):
                raise ValueError("The saved stand differs from the base topology")
            target = copy.deepcopy(base)
            target["nodes"]["es4"] = settings(api.ROOT)["es4"]
            topology.validate_extension(base, target)
            backend.validate_existing()
            from verification import snapshot

            snapshot(api, api.lab_output())
            journal["optional_es4"] = {"base_topology": base, "requested_node": "es4"}
            journal["topology"] = target
            backend.save()
        # Write-ahead request persists even if allocation/configuration fails.
        deploy(api)
        api.verify(False)
        api.save(
            api.ROOT / "docs/es4-result.json",
            {"success": True, "node": "es4", "existing_ids_and_documents_checked": True, "logs_checked": True},
        )


def binding(backend: FailoverBackend, lab: StandDescription, name: str) -> dict[str, object]:
    node = backend.journal["stages"][name]["output"]
    if node["id"] != lab["server_ids"][name] or node["region"] != lab["node_regions"][name]:
        raise ValueError("ES power target differs from the saved node identity")
    return {
        "deployment_id": backend.journal["deployment_id"],
        "scope": backend.journal["scope"],
        "namespace": backend.journal["namespace"],
        "node": name,
        "server_id": node["id"],
        "region": node["region"],
    }


def require_healthy_membership(api: OperationPort, health: Mapping[str, object]) -> None:
    if health.get("status") != "green" or health.get("number_of_nodes") != len(
        topology.elastic_names(api.lab_output())
    ):
        raise RuntimeError("Full saved Elasticsearch membership is unconfirmed")


def _power_target(value: object) -> str:
    return json_text(value, "ES power target differs from the saved node identity")


def restore(
    api: OperationPort | None,
    backend: FailoverBackend,
    request: Mapping[str, object],
    options: Mapping[str, object],
) -> dict[str, object] | None:
    from observability import command

    limit = _count(options["restore_attempts"])
    if limit <= 0:
        return None
    if api is None:
        raise TypeError("restore requires an API client")
    attempt = 0
    while True:
        try:
            backend.region(_power_target(request["region"])).set_power(
                _power_target(request["node"]),
                _power_target(request["server_id"]),
                running=True,
                prefix=api.PREFIX,
            )
            health = command(api, "health")
            result: dict[str, object] = {"health": health}
            require_healthy_membership(api, health)
            if request.get("probe_written"):
                result["data"] = command(api, "recovery")
            return result
        except Exception:
            if attempt + 1 == limit:
                raise
            time.sleep(_count(options["restore_delay_seconds"]))
            attempt += 1


def failover(api: EsController) -> dict[str, object]:
    from ansible_operation import pending
    from observability import command
    from staged_runtime import backend_factory
    from test_cycle import require_access

    require_access(api)
    with operation(api.LOCAL):
        path = api.LOCAL / MARKER
        checked_tree(path, Path(api.ROOT).resolve())
        backend = backend_factory(api, cleanup=True)
        lab = api.lab_output()
        options = settings(api.ROOT)
        if pending(api) or not backend.journal.get("configured") or backend.journal.get("destroy_requested"):
            raise ValueError("Finish configuration or cleanup before the ES check")
        resumed = path.exists()
        request: dict[str, object]
        if resumed:
            request = json_object(json.loads(path.read_text()), "Saved ES recovery belongs to another stand or VM")
            name_value = request["node"]
            if name_value not in topology.elastic_names(lab) or any(
                request.get(key) != value
                for key, value in binding(
                    backend, lab, json_text(name_value, "Saved ES recovery belongs to another stand or VM")
                ).items()
            ):
                raise ValueError("Saved ES recovery belongs to another stand or VM")
            name = json_text(name_value, "Saved ES recovery belongs to another stand or VM")
        else:
            server_ids = lab["server_ids"]
            if set(server_ids) != set(backend.journal["topology"]["nodes"]) or not all(
                backend.completed(node_name) for node_name in server_ids
            ):
                raise ValueError("Finish the requested topology before the ES check")
            backend.validate_existing()
            require_healthy_membership(api, command(api, "health"))
            name_value = command(api, "master")["node"]
            if (
                name_value not in topology.elastic_names(lab)
                or name_value not in topology.services(api.ROOT)["initial_masters"]
            ):
                raise ValueError("Master identity is outside own Elasticsearch masters")
            name = json_text(name_value, "Master identity is outside own Elasticsearch masters")
            request = binding(backend, lab, name)
            atomic_json(path, request, api.ROOT)
        primary: BaseException | None = None
        failure: dict[str, object] | None = None
        try:
            if not resumed:
                backend.region(_power_target(request["region"])).set_power(
                    name,
                    _power_target(request["server_id"]),
                    running=False,
                    prefix=api.PREFIX,
                )
                failure = command(api, "failure")
                new_master = json_object(failure["new_master"], "Elasticsearch master did not change")
                if new_master["node"] == name:
                    raise RuntimeError("Elasticsearch master did not change")
                if new_master["node"] not in topology.services(api.ROOT)["initial_masters"]:
                    raise RuntimeError("Replacement master is outside the saved master set")
                request["probe_written"] = True
                atomic_json(path, request, api.ROOT)
        except BaseException as error:
            primary = error
        finally:
            report: dict[str, object] = {
                "success": False,
                "restored": False,
                "resumed_recovery": resumed,
                "node": name,
                "pool": request["region"],
            }
            if primary is not None:
                report["error_type"] = type(primary).__name__
            try:
                recovery = restore(api, backend, request, options)
                report.update(restored=True, success=primary is None, recovery=recovery)
                path.unlink()
            except BaseException as error:
                report["success"] = False
                report["restore_error_type"] = type(error).__name__
                if primary is None:
                    primary = error
            try:
                api.save(api.ROOT / "docs/elastic-failover.json", {**report, "failure": failure})
            except Exception:
                LOGGER.error("Cannot save ES evidence; original outcome preserved")
        if primary is not None:
            raise primary
        return report


def verify_failover(api: EsController) -> dict[str, object]:
    with operation(api.LOCAL):
        result = failover(api)
        return {"failover": result, "verification": api.verify(False)}
