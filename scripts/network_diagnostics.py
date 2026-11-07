"""Extract only structured guest-network evidence from owned VM console output."""

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TypedDict

from cloud_contract import StandDescription
from runtime_ports import ConfigurationPort
from selectel_api import json_object, json_text


class ConsoleCheck(TypedDict, total=False):
    returncode: int | None
    stdout: str


class ConsoleRecord(TypedDict, total=False):
    checks: dict[str, ConsoleCheck]


class NodeConsoleEvidence(TypedDict, total=False):
    guest_evidence_found: bool
    records: list[dict[str, object]]
    error_type: str
    check_returncodes: dict[str, object]
    expected_private_ipv4_present: bool | None


class ConsoleEvidenceReport(TypedDict):
    time_utc: str
    source: str
    root_cause_confirmed: bool
    nodes: dict[str, NodeConsoleEvidence]


class BootstrapNetworkError(RuntimeError):
    """Current owned guest explicitly failed its first-boot IPv4 check."""


def require_bootstrap_network(api: ConfigurationPort, lab: StandDescription) -> None:
    evidence = console_evidence(api, lab)
    if any(
        node.get("guest_evidence_found") is True and node.get("expected_private_ipv4_present") is False
        for node in evidence["nodes"].values()
    ):
        raise BootstrapNetworkError("Guest IPv4 bootstrap failed in the selected placement")


def summary(record: Mapping[str, object], expected: str | None) -> NodeConsoleEvidence:
    checks = json_object(record.get("checks", {}), "Invalid guest diagnostics")
    result: NodeConsoleEvidence = {
        "check_returncodes": {
            name: json_object(item, "Invalid guest diagnostics").get("returncode") for name, item in checks.items()
        }
    }
    try:
        addresses_output = json_object(checks["addresses"], "Invalid guest diagnostics")
        addresses = json.loads(json_text(addresses_output["stdout"], "Invalid guest diagnostics"))
        result["expected_private_ipv4_present"] = any(
            address.get("family") == "inet" and address.get("local") == expected
            for interface in addresses
            for address in interface.get("addr_info", [])
        )
    except (KeyError, TypeError, ValueError):
        result["expected_private_ipv4_present"] = None
    return result


def console_evidence(api: ConfigurationPort, lab: StandDescription) -> ConsoleEvidenceReport:
    from stage_store import deployment_exists

    staged = deployment_exists(api.ROOT)
    if staged:
        from staged_runtime import backend

        provider = backend(api)
    else:
        cloud = api.cloud()
    summaries: dict[str, NodeConsoleEvidence] = {}
    for name, server_id in lab["server_ids"].items():
        try:
            output: str
            if staged:
                output = provider.console_output(lab, name)
            else:
                node_cloud = cloud.read_only_pool(lab["node_regions"][name]) if lab.get("node_regions") else cloud
                output = json_text(
                    node_cloud.get(
                        "compute", "/servers/" + server_id + "/action", {"os-getConsoleOutput": {"length": 1000}}
                    )["output"],
                    "Console output is unconfirmed",
                )
            records: list[dict[str, object]] = []
            for line in output.splitlines():
                # cloud-init may prefix the line with a timestamp.
                if "DZ07_NETWORK_DIAGNOSTICS=" in line:
                    value = json.loads(line.split("DZ07_NETWORK_DIAGNOSTICS=", 1)[1])
                    records.append(json_object(value, "Guest diagnostics record is unconfirmed"))
            result: NodeConsoleEvidence = {"guest_evidence_found": bool(records), "records": records}
            summaries[name] = {
                "guest_evidence_found": bool(records),
                **(summary(records[-1], lab.get("private_ips", {}).get(name)) if records else {}),
            }
        except Exception as error:
            # No raw console, exception message, tokens or credentials are persisted.
            result = {"guest_evidence_found": False, "error_type": type(error).__name__}
            summaries[name] = result
        api.save(api.RUN / ("network-console-" + name + ".json"), result)
    report: ConsoleEvidenceReport = {
        "time_utc": datetime.now(UTC).isoformat(),
        "source": "owned_guest_console",
        "root_cause_confirmed": False,
        "nodes": summaries,
    }
    api.save(api.ROOT / "docs/network-diagnostics-result.json", report)
    return report
