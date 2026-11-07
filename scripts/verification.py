"""Cluster checks and failover orchestration; does not create infrastructure."""

import json
import uuid

import topology
from cloud_contract import StandDescription
from runtime_ports import ConfigurationPort, OperationPort, ServerDeliveryPort
from verification_types import ExpansionSnapshot, VerificationReport, baseline, checked, document


def snapshot(api: ServerDeliveryPort, previous: StandDescription) -> ExpansionSnapshot:
    """Keep the canary across retries, including a partially joined replica."""
    path = api.LOCAL / "expansion-pending.json"
    if path.exists():
        return checked(json.loads(path.read_text(encoding="utf-8")))
    run_id = uuid.uuid4().hex
    with api.connect(previous) as app:
        api.upload_tree(app)
        raw = api.remote(
            app,
            "sudo python3 " + api.STACK["paths"]["remote_root"] + "/scripts/remote/verify.py baseline " + run_id,
            "expansion-before",
        )
    result: ExpansionSnapshot = {
        "run_id": run_id,
        "before": baseline(json.loads(raw)),
        "server_ids": previous["server_ids"],
    }
    if topology.elastic_names(previous):
        import observability

        observability.command(api, "checkpoint")
        result["elastic_checkpoint"] = True
    api.save(path, result)
    return result


def expansion(api: ServerDeliveryPort, value: object) -> None:
    before = checked(value)
    if not {"run_id", "before", "server_ids"} <= before.keys() or "new_record" not in before["before"]:
        raise ValueError("Incomplete verification checkpoint; preserve recovery data")
    lab = api.lab_output()
    from topology import assert_preserved

    assert_preserved(before["server_ids"], lab["server_ids"])
    with api.connect(lab) as app:
        raw = api.remote(
            app,
            "sudo python3 "
            + api.STACK["paths"]["remote_root"]
            + "/scripts/remote/verify.py expanded "
            + before["run_id"],
            "expansion-after",
        )
    result = document(json.loads(raw))
    if before.get("elastic_checkpoint"):
        import observability

        result["elasticsearch"] = observability.command(api, "preservation")
    if result["preserved_records"] != [before["before"]["new_record"]]:
        raise RuntimeError("Expansion did not verify the original canary")
    added = sorted(set(lab["server_ids"]) - set(before["server_ids"]))
    report = {"success": True, "resource_ids_preserved": True, "added_nodes": added, "verification": result}
    api.save(api.ROOT / "docs/expansion-result.json", report)
    if added:
        api.save(api.ROOT / "docs/node-addition-result.json", report)
    (api.LOCAL / "expansion-pending.json").unlink(missing_ok=True)


def verify(api: OperationPort, failover: bool = False) -> VerificationReport:
    if failover:
        raise ValueError("Use verify --es-failover for the explicit Elasticsearch check")
    lab_output, connect, remote, save = api.lab_output, api.connect, api.remote, api.save
    project_root = api.ROOT
    lab = lab_output()
    run_id = uuid.uuid4().hex
    with connect(lab) as app:
        command = "sudo python3 " + api.STACK["paths"]["remote_root"] + "/scripts/remote/verify.py "
        raw = remote(app, command + "baseline " + run_id, "verify-baseline")
        baseline_result = document(json.loads(raw))
        save(project_root / "docs/verification.json", baseline_result)
        import observability

        return {"baseline": baseline_result, "logs": observability.verify(api)}


def check_placement(api: ConfigurationPort, lab: StandDescription) -> None:
    from cloud_settings import adapter

    return adapter().check_placement(api, lab)
