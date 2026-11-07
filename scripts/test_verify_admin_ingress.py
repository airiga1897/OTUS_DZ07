"""Verify the Selectel ingress checks with isolated stage data and API responses."""

import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import runtime_application

FIRST = "1.1.1.1/32"
SECOND = "8.8.8.8/32"


def rule(cidr: str, port: int) -> dict[str, object]:
    return {
        "direction": "ingress",
        "ethertype": "IPv4",
        "protocol": "tcp",
        "remote_ip_prefix": cidr,
        "port_range_min": port,
        "port_range_max": port,
    }


class VerifyAdminIngressTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / ".tools")
        self.addCleanup(folder.cleanup)
        self.api = Mock(ROOT=Path(folder.name))
        self.local = self.api.ROOT / ".local"
        self.api.LOCAL = self.local
        self.local.mkdir()
        (self.local / "staged-deployment.json").touch()
        (self.local / "infrastructure.json").touch()
        self.groups = {
            pool: {
                "id": "group-" + pool,
                "description": "stand-owner",
                "project_id": "project",
                "security_group_rules": [rule(cidr, port) for cidr in (FIRST, SECOND) for port in (22, 5601)],
            }
            for pool in ("ru-2", "ru-3")
        }
        self.backend = Mock(
            owner="stand-owner",
            credentials={"project_id": "project"},
            journal={
                "admin_cidrs": [FIRST, SECOND],
                "stages": {"app": {"candidate": {"region": "ru-2"}}}
                | {
                    "network-" + pool: {
                        "status": "active",
                        "candidate": {"region": pool},
                        "output": {"security_group_id": group["id"]},
                        "inputs": {"admin_cidrs": [FIRST, SECOND], "kibana_port": 5601},
                    }
                    for pool, group in self.groups.items()
                },
            },
        )
        self.clouds = {
            pool: Mock(get=Mock(return_value={"security_group": group})) for pool, group in self.groups.items()
        }
        self.backend.region.side_effect = self.clouds.__getitem__
        for name, value in {
            "elastic_options.require_no_failover": None,
            "staged_runtime.backend": self.backend,
        }.items():
            patcher = patch(name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_checks_each_saved_pool_before_running_application_checks(self) -> None:
        with patch("verification.verify") as verify, self.assertLogs("otus_dz07", level="INFO") as logs:
            self.assertIs(runtime_application.verify(self.api), verify.return_value)
        verify.assert_called_once_with(self.api, False)
        for pool, cloud in self.clouds.items():
            cloud.get.assert_called_once_with("network", "/v2.0/security-groups/group-" + pool)
        self.assertIn("в 2 пулах", logs.output[-1])

    def test_reports_every_missing_rule_and_rejects_ranges_and_broader_sources(self) -> None:
        self.backend.journal["stages"]["network-ru-2"]["inputs"]["admin_cidrs"] = [FIRST]
        self.groups["ru-2"]["security_group_rules"] = [
            rule(FIRST, 22),
            rule(FIRST, 5601),
            rule(SECOND, 22),
            rule("8.8.8.0/24", 5601),
            {**rule(SECOND, 5601), "port_range_max": 5602},
        ]
        self.groups["ru-3"]["security_group_rules"] = [
            {**rule(FIRST, 22), "direction": "egress"},
            rule(FIRST, 5601),
            rule(SECOND, 22),
            rule(SECOND, 5601),
        ]
        with (
            patch("verification.verify") as verify,
            self.assertLogs("otus_dz07", level="ERROR") as logs,
            self.assertRaisesRegex(RuntimeError, r"правила доступа \(2\)"),
        ):
            runtime_application.verify(self.api)
        verify.assert_not_called()
        self.assertIn(f"ru-2 | Kibana TCP/5601 | источник {SECOND}", logs.output[0])
        self.assertIn(f"ru-3 | SSH TCP/22 | источник {FIRST}", logs.output[1])

    def test_api_denial_is_not_reported_as_a_missing_rule(self) -> None:
        self.clouds["ru-2"].get.side_effect = HTTPError("https://example.invalid", 403, "Forbidden", Message(), None)
        with patch("verification.verify") as verify, self.assertRaises(HTTPError):
            runtime_application.verify(self.api)
        verify.assert_not_called()

    def test_rejects_a_foreign_group(self) -> None:
        self.groups["ru-2"]["description"] = "different-stand"
        with patch("verification.verify") as verify, self.assertRaisesRegex(RuntimeError, "Принадлежность"):
            runtime_application.verify(self.api)
        verify.assert_not_called()

    def test_missing_network_or_allowlist_is_not_success(self) -> None:
        stages = self.backend.journal["stages"]
        stages["network-ru-3"]["inputs"]["admin_cidrs"] = []
        with self.assertRaisesRegex(RuntimeError, "Список разрешённых IPv4 пуст"):
            runtime_application.verify(self.api)
        del stages["network-ru-2"]
        with self.assertRaisesRegex(RuntimeError, "нет активной сети ВМ app"):
            runtime_application.verify(self.api)
        stages.clear()
        with self.assertRaisesRegex(RuntimeError, "нет активных сетей"):
            runtime_application.verify(self.api)

    def test_legacy_path_keeps_its_existing_verification(self) -> None:
        (self.local / "staged-deployment.json").unlink()
        with patch("verification.verify") as verify:
            runtime_application.verify(self.api)
        verify.assert_called_once_with(self.api, False)
        self.backend.region.assert_not_called()

    def test_missing_staged_result_stops_before_api_or_ssh(self) -> None:
        from cloud_contract import InfrastructureUnavailableError

        (self.local / "infrastructure.json").unlink()
        with (
            patch("verification.verify") as verify,
            self.assertRaisesRegex(InfrastructureUnavailableError, "результата"),
        ):
            runtime_application.verify(self.api)
        verify.assert_not_called()
        self.backend.region.assert_not_called()
