"""Restore missing managed ingress rules without changing other infrastructure."""

import json
import urllib.error
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from operator_network import allowed_cidrs
from plan_diagnostics import PlanView
from progress_logging import get_logger
from selectel_api import json_int, json_text, json_texts
from selectel_staged_audit import assert_stage_owned, owned_access_rules
from stage_journal import StageRecord

if TYPE_CHECKING:
    from selectel_staged import Backend

LOGGER = get_logger(__name__)
RULE_TYPE = "openstack_networking_secgroup_rule_v2"
GROUP_ADDRESS = "module.stage.openstack_networking_secgroup_v2.lab"
Rule = tuple[str, str, int]


def expected_rules(record: StageRecord, cidrs: list[str]) -> dict[str, Rule]:
    inputs = record["inputs"]
    kibana = json_int(inputs["kibana_port"], "Unknown Kibana port")
    if not cidrs or not 1 <= kibana <= 65535 or kibana == 22:
        raise ValueError("Saved administrative policy is incomplete")
    return {
        f"module.stage.{RULE_TYPE}.{service}[{json.dumps(cidr)}]": (service, cidr, port)
        for cidr in cidrs
        for service, port in (("ssh", 22), ("kibana", kibana))
    }


def matches(row: Mapping[str, object], rule: Rule) -> bool:
    _, cidr, port = rule
    return all(
        row.get(key) == value
        for key, value in {
            "direction": "ingress",
            "ethertype": "IPv4",
            "protocol": "tcp",
            "remote_ip_prefix": cidr,
            "port_range_min": port,
            "port_range_max": port,
            "remote_group_id": None,
            "remote_address_group_id": None,
        }.items()
    )


def rule_ids(rules: list[dict[str, object]], expected: Mapping[str, Rule], group: str) -> dict[str, str]:
    ids = {}
    for address, rule in expected.items():
        found = [
            json_text(row.get("id"), "Unknown security rule identity")
            for row in rules
            if row.get("security_group_id") == group and matches(row, rule)
        ]
        if len(found) > 1:
            raise ValueError("Duplicate ingress rule requires explicit recovery")
        if found:
            ids[address] = found[0]
    return ids


def require_absent(backend: "Backend", region: str, identifier: str) -> None:
    try:
        backend.region(region).get("network", "/v2.0/security-group-rules/" + identifier)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return
        raise
    raise ValueError("Saved ingress rule still exists with a different policy")


def validate_plan(plan: PlanView, expected: Mapping[str, Rule], group: str) -> None:
    creates = set()
    for drift in plan.get("resource_drift", []):
        actions = drift["change"]["actions"]
        if actions != ["no-op"] and (actions != ["delete"] or drift["address"] not in expected):
            raise ValueError("Unrelated drift in access repair plan")
    for change in plan.get("resource_changes", []):
        if change.get("mode") == "data" or change["change"]["actions"] == ["no-op"]:
            continue
        address = change["address"]
        after = change["change"].get("after") or {}
        if (
            address not in expected
            or address in creates
            or change["change"]["actions"] != ["create"]
            or change["change"].get("before") is not None
            or change.get("type") != RULE_TYPE
            or (change.get("name"), change.get("index")) != expected[address][:2]
            or after.get("security_group_id") != group
            or not matches(after, expected[address])
        ):
            raise ValueError("Access repair plan may only create the missing saved ingress rules")
        creates.add(address)
    if creates != expected.keys():
        raise ValueError("Access repair plan does not create all missing rules")


def preserved(saved: Mapping[str, str], current: Mapping[str, str], targets: Mapping[str, str]) -> bool:
    return {key: value for key, value in saved.items() if key not in targets} == {
        key: value for key, value in current.items() if key not in targets
    }


def repair_network(backend: "Backend", name: str, record: StageRecord, app_cidrs: Sequence[str] = ()) -> int:
    region = json_text(record["candidate"]["region"], "Stage pool is unconfirmed")
    saved_cidrs = json_texts(record["inputs"]["admin_cidrs"], "Unknown admin networks")
    cidrs = allowed_cidrs([], [*saved_cidrs, *app_cidrs])
    expected = expected_rules(record, cidrs)
    saved = record["identities"]
    current = backend.store.identities(name)
    group = record["output"]["security_group_id"]
    if not record.get("lineage") or backend.store.state(name).get("lineage") != record["lineage"]:
        raise ValueError("Security group stage state lineage is unconfirmed")
    if not group or group != saved.get(GROUP_ADDRESS) or group != current.get(GROUP_ADDRESS):
        raise ValueError("Saved security group identity differs from Terraform state")
    actual = rule_ids(owned_access_rules(backend, record, region), expected, group)
    pending = record.get("access_repair", {})
    if not preserved(saved, current, pending) or any(
        address not in expected or saved.get(address, "") != old for address, old in pending.items()
    ):
        raise ValueError("Unrelated changes in saved network state")
    missing = {address: saved.get(address, "") for address in expected if address not in actual}
    if pending and missing.keys() - pending.keys():
        raise ValueError("Finish the saved repair before handling new drift")
    for address, identifier in actual.items():
        if current.get(address) != identifier:
            raise ValueError("Existing access rule is outside the saved state; explicit recovery required")
    for address in missing:
        if old_id := current.get(address) or saved.get(address):
            require_absent(backend, region, old_id)
    absent = frozenset(current[address] for address in missing if address in current)
    assert_stage_owned(backend, name, missing_access_ids=absent)
    if not missing and not pending and cidrs == saved_cidrs:
        return 0
    if missing:
        record["access_repair"] = pending or missing
        record["inputs"]["admin_cidrs"] = cidrs
        backend.save()
        backend.configure_stage(name, "network", record["inputs"])
        backend.store.apply(
            name,
            restore={address: current[address] for address in missing if address in current},
            validate_extra=lambda plan: validate_plan(plan, {address: expected[address] for address in missing}, group),
        )
    final = backend.store.identities(name)
    verified = rule_ids(owned_access_rules(backend, record, region), expected, group)
    if (
        not preserved(saved, final, record.get("access_repair", {}))
        or verified.keys() != expected.keys()
        or any(final.get(address) != identifier for address, identifier in verified.items())
    ):
        raise RuntimeError("Repaired access rules or preserved resource identities could not be confirmed")
    assert_stage_owned(backend, name)
    record["identities"] = final
    record["inputs"]["admin_cidrs"] = cidrs
    record.pop("access_repair", None)
    backend.save()
    return len(missing)


def repair(backend: "Backend") -> None:
    stages = backend.journal["stages"]
    if backend.journal.get("destroy_requested") or backend.journal.get("admin_access_pending"):
        raise RuntimeError("Access repair requires active network stages and no pending access change or destroy")
    app_region = json_text(stages.get("app", {}).get("candidate", {}).get("region"), "Application pool is unconfirmed")
    networks = [
        name for name, record in stages.items() if record.get("kind") == "network" and record["status"] == "active"
    ]
    if "network-" + app_region not in networks:
        raise RuntimeError("Access repair requires the application's active network stage")
    for name in networks:
        LOGGER.info("Восстановление правил доступа: проверка этапа %s", name)
        app_cidrs = backend.journal["admin_cidrs"] if name == "network-" + app_region else []
        count = repair_network(backend, name, stages[name], app_cidrs)
        LOGGER.info("Восстановление правил доступа: этап %s, создано правил %d, состояние подтверждено", name, count)
