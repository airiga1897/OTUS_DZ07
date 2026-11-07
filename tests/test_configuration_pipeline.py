"""Run actual configuration, per-node staging and Ansible orchestration against fake SSH IO."""

import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import AbstractContextManager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import paramiko

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import configuration
import stack_config
from cloud_contract import InfrastructureNode, InfrastructurePlacement, InfrastructureResult, StandDescription
from inventory_types import Inventory
from runtime_ports import OperationPort

ROOT = Path(__file__).resolve().parents[1]


def node(name: str, host: int) -> InfrastructureNode:
    record: InfrastructureNode = {
        "id": name + "-id",
        "volume_id": name + "-disk",
        "port_id": name + "-port",
        "roles": ["app" if name == "app" else "es"],
        "private_ip": "10.77.0." + str(host),
        "public_ip": "192.0.2.10" if name == "app" else None,
        "region": "ru-9",
        "zone": "ru-9a",
    }
    return record


class ConfigurationPipelineContracts(unittest.TestCase):
    files: dict[tuple[str, str], str]
    commands: list[str]
    ansible_failed: bool
    bad_manifest: bool
    clients: dict[str, MagicMock]

    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory(dir=ROOT / ".tools")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.local = self.root / ".local"
        self.local.mkdir()
        shutil.copytree(ROOT / "config", self.root / "config")
        (self.root / "scripts/remote").mkdir(parents=True)
        for name in ("network_diagnostics.py", "check_cloud_init.py"):
            shutil.copyfile(ROOT / "scripts/remote" / name, self.root / "scripts/remote" / name)
        self.passwords = {
            name: "synthetic-" + name
            for name in (
                "postgres",
                "api",
                "django",
                "app",
                "elastic",
                "shipper",
                "kibana",
                "kibana_encryption",
            )
        }
        (self.local / "secrets.json").write_text(json.dumps(self.passwords))
        (self.local / "otus_dz07").write_text("synthetic-private-key")
        (self.local / "known_hosts").write_text("synthetic-known-hosts")
        (self.local / "staged-deployment.json").write_text("{}")
        nodes = {name: node(name, host) for name, host in (("app", 10), ("es1", 31), ("es2", 32), ("es3", 33))}
        placement: InfrastructurePlacement = {"region": "ru-9", "zone": "ru-9a"}
        self.result = InfrastructureResult(
            "selectel",
            {"project_id": "synthetic-project"},
            "otus-dz07",
            placement,
            nodes,
            "10.77.0.0/24",
        )
        self.clients = {name: MagicMock(name=name) for name in nodes}
        for name, client in self.clients.items():
            client.node_name = name
        self.files = {}
        self.commands = []
        self.ansible_failed = False
        self.bad_manifest = False

        def remote(client: object, command: str, label: str = "remote", stdin: str | bytes | None = None) -> str:
            self.commands.append(label)
            if label == "ansible-configure" and self.ansible_failed:
                raise RuntimeError("synthetic Ansible failure")
            if label == "verified-package-manifest":
                return "{}" if self.bad_manifest else (ROOT / "config/elastic-artifacts.json").read_text()
            return ""

        def put(client: MagicMock, path: str, value: str, mode: int | None = None) -> None:
            node_name = client.node_name
            if not isinstance(node_name, str):
                raise AssertionError(node_name)
            self.files[(node_name, path)] = value

        def save(path: Path, value: object) -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value))

        self.api = SimpleNamespace(
            ROOT=self.root,
            LOCAL=self.local,
            PREFIX="otus-dz07",
            STACK=stack_config.load(),
            lab_output=lambda: self.result.application(),
            infrastructure_output=lambda: self.result,
            trust=Mock(),
            check_placement=Mock(),
            discover_route=Mock(),
            cloud=Mock(),
            save=save,
            connect=self.open_connection,
            remote=remote,
            put=put,
            upload_tree=Mock(),
        )

    def open_connection(
        self, value: StandDescription, name: str = "app", front: paramiko.SSHClient | None = None
    ) -> AbstractContextManager[MagicMock]:
        return contextlib.nullcontext(self.clients[name])

    def run_pipeline(self, existing: bool = False) -> None:
        def operation(api: OperationPort, app: paramiko.SSHClient | None, inventory: Inventory) -> None:
            import observability

            self.commands.append("ansible-systemd")
            # The durable backend's terminal cleanup is tested with the real
            # protocol elsewhere; this suite isolates per-node staging.
            if app is None:
                raise AssertionError(app)
            observability.package_cache(api, app, api.lab_output(), start=False, remote=api.remote)
            if self.ansible_failed:
                raise RuntimeError("synthetic Ansible failure")

        with (
            patch("network_diagnostics.require_bootstrap_network"),
            patch("ansible_operation.reserve"),
            patch("ansible_backend.configure", side_effect=operation),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            configuration.configure(self.api, existing=existing)

    def test_all_node_staging_least_privilege_and_ansible_cleanup_on_both_runs(self) -> None:
        for existing in (False, True):
            self.run_pipeline(existing)
        base = self.api.STACK["paths"]["remote_root"]
        for node_name in self.clients:
            settings = json.loads(self.files[(node_name, base + "/elk-settings.json")])
            self.assertIn("shipper", settings["secrets"])
            if node_name.startswith("es"):
                self.assertEqual(set(settings["secrets"]), {"elastic", "shipper"})
                self.assertNotIn((node_name, base + "/settings.json"), self.files)
        self.assertIn(("app", base + "/settings.json"), self.files)
        self.assertEqual(self.commands.count("ansible-systemd"), 2)
        self.assertEqual(self.commands.count("package-cache-stop"), 2)
        self.assertEqual(self.clients["app"].open_sftp.return_value.__enter__.return_value.remove.call_count, 0)

    def test_configuration_failure_stops_cache_and_removes_remote_inventory(self) -> None:
        self.ansible_failed = True
        with self.assertRaises(RuntimeError):
            self.run_pipeline()
        self.assertEqual(self.commands[-1], "package-cache-stop")
        self.assertTrue((self.local / "otus_dz07").exists())

    def test_wrong_package_manifest_blocks_ansible(self) -> None:
        self.bad_manifest = True
        with self.assertRaisesRegex(ValueError, "manifest differs"):
            self.run_pipeline()
        self.assertNotIn("ansible-systemd", self.commands)

    def test_existing_stand_ssh_failure_never_changes_route_rules(self) -> None:
        self.api.connect = Mock(side_effect=TimeoutError())
        with self.assertRaisesRegex(RuntimeError, "SSH to the existing stand"):
            self.run_pipeline(existing=True)
        self.api.discover_route.assert_not_called()

    def test_authentication_or_host_key_failure_never_changes_route_rules(self) -> None:
        errors = [paramiko.AuthenticationException("synthetic"), paramiko.BadHostKeyException("host", Mock(), Mock())]
        for error in errors:
            self.api.connect = Mock(side_effect=error)
            with self.assertRaisesRegex(RuntimeError, "authentication or host-key"):
                self.run_pipeline()
            self.api.discover_route.assert_not_called()

    def test_legacy_network_check_and_missing_ephemeral_inventory_are_safe(self) -> None:
        (self.local / "staged-deployment.json").unlink()
        (self.root / "terraform-selectel").mkdir()
        (self.root / "terraform-selectel/terraform.tfvars.json").write_text('{"admin_cidrs":["8.8.8.8/32"]}')
        self.clients["app"].open_sftp.return_value.__enter__.return_value.remove.side_effect = FileNotFoundError()
        with patch("cloud_settings.adapter") as adapter:
            adapter.return_value.check_network.return_value = {"success": True}
            self.run_pipeline()
            adapter.return_value.check_network.assert_called_once()

    def test_initial_route_failure_uses_explicit_signed_route_discovery(self) -> None:
        contexts: list[BaseException | AbstractContextManager[MagicMock]] = [
            TimeoutError(),
            contextlib.nullcontext(self.clients["app"]),
        ]
        original = self.open_connection

        def connect(
            value: StandDescription, name: str = "app", front: paramiko.SSHClient | None = None
        ) -> AbstractContextManager[object]:
            if name == "app" and contexts:
                result = contexts.pop(0)
                if isinstance(result, BaseException):
                    raise result
                return result
            return original(value, name, front)

        self.api.connect = connect
        self.run_pipeline()
        self.api.discover_route.assert_called_once()
