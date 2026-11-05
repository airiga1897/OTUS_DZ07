"""Reproduce ES validation with only the node-scoped settings staged in production."""

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from jinja2 import Environment, StrictUndefined

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import observability
import topology
from cloud_contract import StandDescription
from observability import NodeServiceSettings
from remote import validate_filebeat

ROOT = Path(__file__).resolve().parents[1]


class FilebeatValidationTests(unittest.TestCase):
    def settings(self, name: str) -> NodeServiceSettings:
        desired = topology.load(ROOT)
        lab: StandDescription = {
            "private_ips": {n: "10.77.0." + str(v["host"]) for n, v in desired["nodes"].items()},
            "node_roles": {n: v["role"] for n, v in desired["nodes"].items()},
        }
        passwords = {name: "synthetic-" + name for name in ("elastic", "shipper", "kibana", "kibana_encryption")}
        return observability.settings(lab, passwords, name, topology.services(ROOT))

    def test_es_and_app_validate_without_portal_settings(self) -> None:
        env = Environment(undefined=StrictUndefined)
        env.filters["to_json"] = json.dumps
        template = env.from_string((ROOT / "ansible/roles/filebeat/templates/filebeat.yml.j2").read_text())
        for name in ("app", "es1", "es2", "es3"):
            with self.subTest(node=name), tempfile.TemporaryDirectory(dir=ROOT / ".tools") as folder:
                base = Path(folder)
                node = self.settings(name)
                (base / "elk-settings.json").write_text(json.dumps(node))
                config = base / "staging.yml"
                config.write_text(template.render(dz07=node))
                parsed = yaml.safe_load(config.read_text())
                self.assertEqual(parsed["output.elasticsearch"]["password"], node["secrets"]["shipper"])
                services = {item["fields"]["service"]["name"] for item in parsed["filebeat.inputs"]}
                self.assertIn("system", services)
                self.assertIn("elasticsearch" if name.startswith("es") else "postgresql", services)
                with (
                    patch.object(validate_filebeat, "BASE", base),
                    patch(
                        "subprocess.run",
                        return_value=subprocess.CompletedProcess([], 0, "Config OK\n"),
                    ) as run,
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(validate_filebeat.main(str(config)), 0)
                self.assertFalse((base / "settings.json").exists())
                self.assertEqual((base / "filebeat-validation.log").read_text(), "Config OK\n")
                self.assertEqual(run.call_args.args[0][-1], str(config))
                self.assertIn("--strict.perms=false", run.call_args.args[0])

    def test_failed_config_keeps_exit_code_and_redacts_node_secrets(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tools") as folder:
            base = Path(folder)
            node = self.settings("es1")
            (base / "elk-settings.json").write_text(json.dumps(node))
            diagnostic = "Invalid setting: " + " ".join(node["secrets"].values())
            output = io.StringIO()
            with (
                patch.object(validate_filebeat, "BASE", base),
                patch("subprocess.run", return_value=subprocess.CompletedProcess([], 2, diagnostic)),
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(validate_filebeat.main("staging.yml"), 2)
            saved = (base / "filebeat-validation.log").read_text()
            self.assertIn("Invalid setting", saved)
            for secret in node["secrets"].values():
                self.assertNotIn(secret, saved)
                self.assertNotIn(secret, output.getvalue())

    def test_missing_node_settings_fails_before_running_filebeat(self) -> None:
        with (
            tempfile.TemporaryDirectory(dir=ROOT / ".tools") as folder,
            patch.object(validate_filebeat, "BASE", Path(folder)),
            patch("subprocess.run") as run,
        ):
            with self.assertRaises(FileNotFoundError):
                validate_filebeat.main("staging.yml")
            run.assert_not_called()
