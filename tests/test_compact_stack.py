"""Four-server deployment contracts; no provider calls or real state."""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml
from jinja2 import Environment, StrictUndefined

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import topology
from render import ansible_inventory
from selectel_adapter import infrastructure_result

ROOT = Path(__file__).resolve().parents[1]


class CompactStackTests(unittest.TestCase):
    def test_bootstrap_http_probe_stops_before_haproxy_can_bind_public_port(self) -> None:
        plays = yaml.safe_load((ROOT / "ansible/site.yml").read_text())
        frontend = next(
            play for play in plays if play.get("hosts") == "frontend" and "haproxy" in play.get("roles", [])
        )
        tasks = list(frontend.get("pre_tasks", []))
        for role in frontend["roles"]:
            tasks.extend(yaml.safe_load((ROOT / "ansible/roles" / role / "tasks/main.yml").read_text()))
        services = [
            task["ansible.builtin.systemd_service"] for task in tasks if "ansible.builtin.systemd_service" in task
        ]
        stopped = next(
            i
            for i, item in enumerate(services)
            if item["name"].endswith("-route.service") and item.get("state") == "stopped"
        )
        started = next(
            i for i, item in enumerate(services) if item["name"] == "haproxy" and item.get("state") == "started"
        )
        self.assertLess(stopped, started)

    def inventory(self):
        desired = topology.load(ROOT)
        nodes = desired["nodes"]
        lab = {
            "private_ips": {n: "10.77.0." + str(v["host"]) for n, v in nodes.items()},
            "app_public_ip": "192.0.2.1",
            "subnet_cidr": "10.77.0.0/24",
            **{k: {n: k + n for n in nodes} for k in ("server_ids", "volume_ids", "port_ids")},
        }
        result = infrastructure_result(lab, {"project_id": "synthetic", "region": "ru-9"}, "otus-dz07", "ru-9a")
        passwords = dict.fromkeys(
            ("postgres", "django", "app", "elastic", "shipper", "kibana", "kibana_encryption"), "synthetic"
        )
        return ansible_inventory(result, passwords)["all"]

    def test_same_app_hosts_database_frontend_and_kibana(self) -> None:
        children = self.inventory()["children"]
        for group in ("database", "frontend", "kibana"):
            self.assertEqual(set(children[group]["hosts"]), {"app"})
        self.assertEqual(set(children["elasticsearch"]["hosts"]), {"es1", "es2", "es3"})
        self.assertNotIn("etcd_voters", children)
        self.assertNotIn("postgres_password", children["elasticsearch"]["hosts"]["es1"])

    def test_filebeat_labels_all_services_and_filters_journal_units(self) -> None:
        inventory = self.inventory()
        env = Environment(undefined=StrictUndefined)
        env.filters["to_json"] = json.dumps
        variables = {**inventory["vars"], **inventory["children"]["frontend"]["hosts"]["app"]}
        config = yaml.safe_load(
            env.from_string((ROOT / "ansible/roles/filebeat/templates/filebeat.yml.j2").read_text()).render(**variables)
        )
        inputs = {v["fields"]["service"]["name"]: v for v in config["filebeat.inputs"]}
        self.assertEqual(set(inputs), {"system", "nginx", "haproxy", "postgresql", "django", "kibana"})
        self.assertEqual(inputs["django"]["include_matches"]["match"], ["_SYSTEMD_UNIT=otus-dz07.service"])
        self.assertEqual(inputs["kibana"]["include_matches"]["match"], ["_SYSTEMD_UNIT=kibana.service"])

    def test_standalone_verification_reads_postgresql_without_patroni(self) -> None:
        from remote import verify

        conn = MagicMock()
        conn.__enter__.return_value.execute.return_value.fetchone.return_value = (False,)
        with (
            patch.object(verify, "lab", {"private_ips": {"app": "10.77.0.10"}}),
            patch.object(verify, "sql_connect", return_value=conn),
            patch.object(verify.urllib.request, "urlopen") as http,
        ):
            self.assertIsNone(verify.check_database())
        http.assert_not_called()

    def test_source_expectations_require_application_and_kibana_logs(self) -> None:
        expected = self.inventory()["children"]["frontend"]["hosts"]["app"]["dz07"]["log_expectations"]
        self.assertEqual(set(expected), {"app", "es1", "es2", "es3"})
        self.assertEqual(set(expected["app"]), {"system", "nginx", "haproxy", "postgresql", "django", "kibana"})

    def test_database_setup_does_not_reinitialize_existing_pgdata(self) -> None:
        tasks = yaml.safe_load((ROOT / "ansible/roles/postgresql/tasks/main.yml").read_text())
        creation = next(t for t in tasks if t["name"] == "Create cluster only when absent")
        self.assertEqual(creation["when"], "not pg_cluster.stat.exists")
        self.assertFalse(any("initdb" in str(t) or "DROP DATABASE" in str(t) for t in tasks))
