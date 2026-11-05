"""Offline Ansible contracts: inventory, rendered templates, guarded tasks and cleanup."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import ansible_backend
import stack_config
import topology
import yaml
from cloud_contract import InfrastructureResult
from inventory_types import Inventory
from jinja2 import Environment, StrictUndefined
from render import ansible_inventory
from selectel_adapter import infrastructure_result

ROOT = Path(__file__).resolve().parents[1]


def tasks(value: list[dict[str, object]]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for task in value:
        result.append(task)
        block = task.get("block", [])
        if isinstance(block, list):
            result.extend(tasks(block))
    return result


class AnsibleBackendTests(unittest.TestCase):
    def inventory(self, extended: bool = False) -> tuple[InfrastructureResult, dict[str, str], Inventory]:
        desired = topology.load(ROOT)
        if extended:
            desired["nodes"]["es4"] = {"host": 34, "role": "es"}
        raw_lab: dict[str, object] = {
            "private_ips": {n: "10.77.0." + str(v["host"]) for n, v in desired["nodes"].items()},
            "app_public_ip": "192.0.2.1",
            "subnet_cidr": "10.77.0.0/24",
            **{k: {n: k + n for n in desired["nodes"]} for k in ("server_ids", "volume_ids", "port_ids")},
        }
        lab = infrastructure_result(raw_lab, {"project_id": "synthetic", "region": "ru-7"}, "otus-dz07", "ru-7a")
        passwords = dict.fromkeys(
            ("postgres", "replicator", "api", "django", "app", "elastic", "shipper", "kibana", "kibana_encryption"),
            "synthetic",
        )
        return lab, passwords, ansible_inventory(lab, passwords)

    def test_es_extension_preserves_app_hostvars(self) -> None:
        before = self.inventory()[2]["all"]["children"]
        after = self.inventory(True)[2]["all"]["children"]
        self.assertNotIn("etcd_voters", after)
        for name, variables in before["database"]["hosts"].items():
            self.assertEqual(variables, after["database"]["hosts"][name])
        self.assertIn("es4", after["elasticsearch"]["hosts"])
        self.assertEqual(after["frontend"]["hosts"]["app"]["ansible_connection"], "local")

    def test_backend_preserves_durable_operation_errors_and_private_inputs(self) -> None:
        for fails in (False, True):
            with self.subTest(fails=fails), tempfile.TemporaryDirectory(dir=ROOT / ".tools") as directory:
                local = Path(directory)
                (local / "otus_dz07").write_text("synthetic-private-key")
                (local / "known_hosts").write_text("synthetic-known-host")
                api = MagicMock(LOCAL=local)
                app = MagicMock()
                lab, passwords, _ = self.inventory(True)
                inventory = ansible_inventory(lab, passwords)
                with patch(
                    "ansible_operation.configure", side_effect=RuntimeError("configuration failed") if fails else None
                ) as operation:
                    if fails:
                        with self.assertRaisesRegex(RuntimeError, "configuration failed"):
                            ansible_backend.configure(api, app, inventory)
                    else:
                        ansible_backend.configure(api, app, inventory)
                operation.assert_called_once_with(api, app, inventory)
                sftp = app.open_sftp.return_value.__enter__.return_value
                sftp.remove.assert_not_called()
                self.assertTrue((local / "otus_dz07").exists())

    def test_roles_and_handlers_are_present_and_tasks_are_valid_yaml(self) -> None:
        site = yaml.safe_load((ROOT / "ansible/site.yml").read_text())
        self.assertEqual(site[0]["roles"], ["postgresql"])
        self.assertEqual(site[1]["roles"], ["haproxy", "portal"])
        for play in site:
            for role in play.get("roles", []):
                path = ROOT / "ansible/roles" / role / "tasks/main.yml"
                definitions = list(tasks(yaml.safe_load(path.read_text())))
                self.assertTrue(definitions)
                handlers = ROOT / "ansible/roles" / role / "handlers/main.yml"
                known = {h["name"] for h in yaml.safe_load(handlers.read_text())} if handlers.exists() else set()
                for task in definitions:
                    if "notify" in task:
                        self.assertIn(task["notify"], known)

    def test_haproxy_routes_to_the_local_nginx(self) -> None:
        env = Environment(undefined=StrictUndefined)
        rendered = env.from_string((ROOT / "ansible/roles/haproxy/templates/haproxy.cfg.j2").read_text()).render(
            stack=stack_config.load(ROOT)
        )
        self.assertIn("server app 127.0.0.1:8081 check", rendered)

    def test_redeploy_applies_code_migrations_and_guarded_initial_data(self) -> None:
        portal = yaml.safe_load((ROOT / "ansible/roles/portal/tasks/main.yml").read_text())
        initialize = next(t for t in portal if t["name"] == "Apply migrations and guarded initial data")
        self.assertNotIn("when", initialize)
        self.assertTrue(initialize["no_log"])
        for name in (
            "Install portal source from this repository",
            "Install portal dependencies",
            "Install uWSGI",
            "Collect static assets",
        ):
            task = next(t for t in portal if t["name"] == name)
            self.assertNotIn("when", task)
        self.assertEqual(
            next(t for t in portal if t["name"] == "Install portal source from this repository")["notify"],
            "Restart portal",
        )
        database = yaml.safe_load((ROOT / "ansible/roles/postgresql/tasks/main.yml").read_text())
        self.assertTrue(next(t for t in database if t["name"] == "Set local bootstrap password")["no_log"])

    def test_native_templates_have_valid_jinja_syntax(self) -> None:
        env = Environment()
        for path in (ROOT / "ansible").rglob("*.j2"):
            with self.subTest(path=path.name):
                env.parse(path.read_text())
