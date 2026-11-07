"""Repair only saved /32 ingress rules, with an independently checked plan and API."""

import unittest
from email.message import Message
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from plan_diagnostics import PlanView
from selectel_access_repair import GROUP_ADDRESS, RULE_TYPE, repair_network, validate_plan
from stage_journal import StageRecord
from stage_store import check_plan

CIDR = "1.1.1.1/32"
SSH = f'module.stage.{RULE_TYPE}.ssh["{CIDR}"]'
KIBANA = f'module.stage.{RULE_TYPE}.kibana["{CIDR}"]'
NAME = "network-ru-2"


def rule(port: int, identifier: str) -> dict[str, object]:
    return {
        "id": identifier,
        "security_group_id": "own-group",
        "direction": "ingress",
        "ethertype": "IPv4",
        "protocol": "tcp",
        "remote_ip_prefix": CIDR,
        "port_range_min": port,
        "port_range_max": port,
    }


def plan() -> PlanView:
    after = rule(22, "")
    del after["id"]
    return {
        "resource_drift": [{"address": SSH, "change": {"actions": ["delete"], "before": {"id": "old-ssh"}}}],
        "resource_changes": [
            {
                "address": SSH,
                "type": RULE_TYPE,
                "name": "ssh",
                "index": CIDR,
                "change": {"actions": ["create"], "before": None, "after": after},
            }
        ],
    }


class SelectelAccessRepairTests(unittest.TestCase):
    def setUp(self) -> None:
        self.saved = {GROUP_ADDRESS: "own-group", SSH: "old-ssh", KIBANA: "keep-kibana", "network": "keep-network"}
        self.current = dict(self.saved)
        self.rules = [rule(5601, "keep-kibana")]
        self.record: StageRecord = {
            "status": "active",
            "kind": "network",
            "candidate": {"region": "ru-2"},
            "lineage": "original-lineage",
            "output": {"security_group_id": "own-group"},
            "inputs": {"admin_cidrs": [CIDR], "kibana_port": 5601},
            "identities": dict(self.saved),
        }
        self.backend = Mock(owner="owner", credentials={"project_id": "project"})
        self.backend.store.identities.side_effect = lambda _: dict(self.current)
        self.backend.store.state.return_value = {"lineage": "original-lineage"}
        self.backend.region.return_value.get.side_effect = self.get
        ownership = patch("selectel_access_repair.assert_stage_owned")
        self.ownership = ownership.start()
        self.addCleanup(ownership.stop)

    def get(self, service: str, path: str) -> dict[str, object]:
        self.assertEqual(service, "network")
        if path == "/v2.0/security-group-rules/old-ssh":
            raise HTTPError("https://example.invalid", 404, "missing", Message(), None)
        self.assertEqual(path, "/v2.0/security-groups/own-group")
        return {
            "security_group": {
                "id": "own-group",
                "description": "owner",
                "project_id": "project",
                "security_group_rules": self.rules,
            }
        }

    def test_recreates_only_the_missing_rule_and_records_new_api_identity(self) -> None:
        def apply(name: str, **kwargs: object) -> None:
            self.assertEqual(name, NAME)
            self.assertEqual(self.record["access_repair"], {SSH: "old-ssh"})
            self.assertEqual(kwargs["restore"], {SSH: "old-ssh"})
            proposed = plan()
            check_plan(proposed, self.current, additions_only=True, restore={SSH: "old-ssh"})
            validate = kwargs["validate_extra"]
            assert callable(validate)
            validate(proposed)
            self.current[SSH] = "new-ssh"
            self.rules.append(rule(22, "new-ssh"))

        self.backend.store.apply.side_effect = apply
        self.assertEqual(repair_network(self.backend, NAME, self.record), 1)
        self.assertEqual(self.record["identities"], self.saved | {SSH: "new-ssh"})
        self.assertNotIn("access_repair", self.record)
        self.assertEqual(self.backend.save.call_count, 2)
        self.ownership.assert_any_call(self.backend, NAME, missing_access_ids=frozenset({"old-ssh"}))

    def test_resumes_after_apply_without_creating_a_duplicate(self) -> None:
        self.record["access_repair"] = {SSH: "old-ssh"}
        self.current[SSH] = "new-ssh"
        self.rules.append(rule(22, "new-ssh"))
        self.assertEqual(repair_network(self.backend, NAME, self.record), 0)
        self.assertEqual(self.record["identities"][SSH], "new-ssh")
        self.backend.store.apply.assert_not_called()
        self.assertNotIn("access_repair", self.record)

    def test_rejects_untracked_equivalent_rule_and_unrelated_state_changes(self) -> None:
        self.rules.append(rule(22, "foreign-rule"))
        with self.assertRaisesRegex(ValueError, "outside the saved state"):
            repair_network(self.backend, NAME, self.record)
        self.current["network"] = "changed-network"
        with self.assertRaisesRegex(ValueError, "Unrelated"):
            repair_network(self.backend, NAME, self.record)
        self.backend.store.apply.assert_not_called()

    def test_api_confirmation_failure_preserves_old_journal_and_repair_intent(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "could not be confirmed"):
            repair_network(self.backend, NAME, self.record)
        self.assertEqual(self.record["identities"], self.saved)
        self.assertEqual(self.record["access_repair"], {SSH: "old-ssh"})

    def test_plan_rejects_unrelated_create_wide_rule_and_wrong_drift_identity(self) -> None:
        expected = {SSH: ("ssh", CIDR, 22)}
        for bad in ("extra", "wrong-prefix", "replace"):
            proposed = plan()
            change = proposed["resource_changes"][0]
            if bad == "extra":
                proposed["resource_changes"].append({"address": "other", "change": {"actions": ["create"]}})
            elif bad == "wrong-prefix":
                after = change["change"]["after"]
                assert after is not None
                after["remote_ip_prefix"] = "0.0.0.0/0"
            else:
                change["change"]["actions"] = ["delete", "create"]
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_plan(proposed, expected, "own-group")
        with self.assertRaisesRegex(ValueError, "Resource drift"):
            check_plan(plan(), self.saved, additions_only=True)
        with self.assertRaisesRegex(ValueError, "Resource drift"):
            check_plan(plan(), self.saved, additions_only=True, restore={SSH: "unrelated-rule"})
