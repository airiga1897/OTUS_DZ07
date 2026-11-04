"""Extract only structured guest-network evidence from owned VM console output."""

import json
from datetime import UTC, datetime


class BootstrapNetworkError(RuntimeError):
    """Current owned guest explicitly failed its first-boot IPv4 check."""


def require_bootstrap_network(api, lab) -> None:
    evidence = console_evidence(api, lab)
    if any(
        node.get("guest_evidence_found") is True and node.get("expected_private_ipv4_present") is False
        for node in evidence["nodes"].values()
    ):
        raise BootstrapNetworkError("Guest IPv4 bootstrap failed in the selected placement")


def summary(record: dict, expected: str) -> dict:
    checks = record.get("checks", {})
    result = {"check_returncodes": {name: item.get("returncode") for name, item in checks.items()}}
    try:
        addresses = json.loads(checks["addresses"]["stdout"])
        result["expected_private_ipv4_present"] = any(
            address.get("family") == "inet" and address.get("local") == expected
            for interface in addresses
            for address in interface.get("addr_info", [])
        )
    except (KeyError, TypeError, ValueError):
        result["expected_private_ipv4_present"] = None
    return result


def console_evidence(api, lab: dict) -> dict:
    from stage_store import deployment_exists

    staged = deployment_exists(api.ROOT)
    if staged:
        from staged_runtime import backend

        provider = backend(api)
    else:
        cloud = api.cloud()
    summaries = {}
    for name, server_id in lab["server_ids"].items():
        try:
            if staged:
                output = provider.console_output(lab, name)
            else:
                node_cloud = cloud.read_only_pool(lab["node_regions"][name]) if lab.get("node_regions") else cloud
                output = node_cloud.get(
                    "compute", "/servers/" + server_id + "/action", {"os-getConsoleOutput": {"length": 1000}}
                )["output"]
            records = []
            for line in output.splitlines():
                # cloud-init may prefix the line with a timestamp.
                if "DZ07_NETWORK_DIAGNOSTICS=" in line:
                    value = json.loads(line.split("DZ07_NETWORK_DIAGNOSTICS=", 1)[1])
                    records.append(value)
            result = {"guest_evidence_found": bool(records), "records": records}
            summaries[name] = {
                "guest_evidence_found": bool(records),
                **(summary(records[-1], lab.get("private_ips", {}).get(name)) if records else {}),
            }
        except Exception as error:
            # No raw console, exception message, tokens or credentials are persisted.
            result = {"guest_evidence_found": False, "error_type": type(error).__name__}
            summaries[name] = result
        api.save(api.RUN / ("network-console-" + name + ".json"), result)
    report = {
        "time_utc": datetime.now(UTC).isoformat(),
        "source": "owned_guest_console",
        "root_cause_confirmed": False,
        "nodes": summaries,
    }
    api.save(api.ROOT / "docs/network-diagnostics-result.json", report)
    return report
