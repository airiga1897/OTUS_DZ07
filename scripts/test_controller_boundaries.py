"""Real controller helpers with local files and mocked infrastructure transports."""

import io
import json
import shutil
import tarfile
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import stack_config
import topology
from test_runtime_support import lab

ROOT = Path(__file__).resolve().parents[1]


class ControllerBoundaries(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory(dir=ROOT / ".tools")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.local = self.root / ".local"
        self.local.mkdir()
        (self.root / "terraform-selectel").mkdir()
        shutil.copytree(ROOT / "config", self.root / "config")
        shutil.copy(ROOT / ".env.example", self.root / ".env.example")
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(
            patch.multiple(
                lab, ROOT=self.root, LOCAL=self.local, RUN=self.local / "runs", _AUTH_CONFIG=None, _TEST_CYCLE_ID=None
            )
        )
        self.config = {
            "project_id": "synthetic",
            "region": "ru-9",
            "username": "synthetic",
            "password": "synthetic",
            "domain_name": "synthetic",
        }
        self.choice = {
            "region": "ru-9",
            "availability_zone": "ru-9a",
            "image_id": "image",
            "volume_type": "universal.ru-9a",
            "flavor_ids": {"app": "app-flavor", "es": "es-flavor"},
            "boot_disk_gb": 20,
            "admin_cidrs": [],
            "operator_ip_services": ["https://example.invalid"],
        }

    def test_authentication_binding_cache_and_foreign_context_rejection(self) -> None:
        with (
            patch.object(lab, "load_credentials", return_value=self.config) as credentials,
            patch.object(lab, "placement", return_value=self.choice),
        ):
            self.assertEqual(lab.auth_config(), self.config)
            self.assertEqual(lab.auth_config(), self.config)
        credentials.assert_called_once()
        self.assertTrue((self.local / "context.json").exists())
        with (
            patch.object(lab, "_AUTH_CONFIG", None),
            patch.object(lab, "load_credentials", return_value=self.config),
            patch.object(lab, "placement", return_value=self.choice),
        ):
            original = (self.local / "context.json").read_bytes()
            for context in (
                {"project_id": "foreign", "namespace": lab.PREFIX},
                {"project_id": "synthetic", "namespace": "foreign"},
            ):
                (self.local / "staged-deployment.json").write_text(json.dumps(context))
                with self.assertRaises(ValueError):
                    lab.auth_config()
                self.assertEqual((self.local / "context.json").read_bytes(), original)
            (self.local / "staged-deployment.json").write_text(
                json.dumps({"project_id": "synthetic", "namespace": lab.PREFIX})
            )
            self.assertEqual(lab.auth_config(), self.config)
        with (
            patch("selectel_adapter.authenticate", return_value=("synthetic-token", [])),
            patch.object(lab, "load_credentials", return_value=self.config),
        ):
            self.assertEqual(lab.cloud(bind=False).config, self.config)

    def test_prepare_reuses_local_identity_and_explicit_operator_addresses(self) -> None:
        services = json.loads((self.root / "config/services.json").read_text())
        services["artifact_delivery"] = "cached"
        (self.root / "config/services.json").write_text(json.dumps(services))
        with (
            patch.object(lab, "preflight"),
            patch.object(lab, "cloud"),
            patch.object(lab.pools, "selected", return_value=self.choice),
            patch.object(lab, "placement", return_value=self.choice),
            patch("operator_network.discover_cidrs", return_value=["8.8.8.8/32"]),
            patch.object(lab, "tf") as terraform,
        ):
            lab.prepare()
            key = (self.local / "otus_dz07").read_bytes()
            secrets = (self.local / "secrets.json").read_bytes()
            variables = self.root / "terraform-selectel/terraform.tfvars.json"
            value = json.loads(variables.read_text())
            self.assertEqual(value["admin_cidrs"], ["8.8.8.8/32"])
            value["admin_cidrs"] = ["1.1.1.1/32", "8.8.8.8/32"]
            variables.write_text(json.dumps(value))
            lab.prepare()
            self.assertEqual((self.local / "otus_dz07").read_bytes(), key)
            self.assertEqual((self.local / "secrets.json").read_bytes(), secrets)
            self.assertEqual(json.loads(variables.read_text())["admin_cidrs"], value["admin_cidrs"])
            self.assertEqual(
                [call.args[0] for call in terraform.call_args_list], ["init", "validate", "init", "validate"]
            )
        with (
            patch.object(lab, "preflight"),
            patch.object(lab, "cloud"),
            patch.object(lab.pools, "selected", return_value=None),
            self.assertRaisesRegex(ValueError, "discover"),
        ):
            lab.prepare()

    def test_signed_host_keys_are_cached_by_uuid_on_staged_path(self) -> None:
        lab.prepare_identity()
        key = (self.local / "otus_dz07.pub").read_text().split()[1]
        (self.local / "staged-deployment.json").write_text("{}")
        provider = SimpleNamespace(ssh_host_key=Mock(return_value=key))
        value = {
            "server_ids": {"app": "uuid", "es1": "es-uuid"},
            "private_ips": {"app": "10.77.0.10", "es1": "10.77.0.31"},
            "app_public_ip": "8.8.8.8",
        }
        with (
            patch("staged_runtime.backend", return_value=provider),
            patch.object(lab, "cloud", side_effect=AssertionError("No legacy auth")),
        ):
            lab.trust(value)
            lab.trust(value)
        self.assertEqual(provider.ssh_host_key.call_args.args[-1], key)
        self.assertIn("10.77.0.10,8.8.8.8 ssh-ed25519", (self.local / "known_hosts").read_text())

    def test_binary_upload_preserves_mode_and_skips_private_artifacts(self) -> None:
        services = json.loads((self.root / "config/services.json").read_text())
        services["artifact_delivery"] = "cached"
        (self.root / "config/services.json").write_text(json.dumps(services))
        for folder in ("app", "ansible", "scripts/remote"):
            (self.root / folder).mkdir(parents=True, exist_ok=True)
        for name in (
            "app/source.py",
            "app/.env",
            "app/db.sqlite3",
            "app/secret.key",
            "app/media/private.txt",
            "app/__pycache__/module.pyc",
        ):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("synthetic")
        with patch.object(lab, "put") as put, patch.object(lab, "remote"):
            lab.upload_tree(Mock())
        with tarfile.open(fileobj=io.BytesIO(put.call_args.args[2])) as archive:
            names = archive.getnames()
            self.assertIn("app/source.py", names)
            for name in (
                "app/.env",
                "app/db.sqlite3",
                "app/secret.key",
                "app/media/private.txt",
                "app/__pycache__/module.pyc",
            ):
                self.assertNotIn(name, names)
            self.assertTrue(all(entry.mode == 0o644 for entry in archive.getmembers()))
        client = MagicMock()
        lab.put(client, "/synthetic", b"binary", mode=0o640)
        client.open_sftp.return_value.__enter__.return_value.chmod.assert_called_once_with("/synthetic", 0o640)
        with (
            patch.object(Path, "is_symlink", lambda path: path == self.root / "app/source.py"),
            patch.object(lab, "put") as upload,
            self.assertRaisesRegex(ValueError, "links"),
        ):
            lab.upload_tree(Mock())
        upload.assert_not_called()

    def test_staged_delegation_and_best_effort_diagnostics_do_not_mutate_cloud(self) -> None:
        (self.local / "staged-deployment.json").write_text("{}")
        provider = Mock(verify_placement=Mock(return_value={"success": True}))
        with (
            patch("staged_runtime.backend", return_value=provider),
            patch("staged_runtime.deploy") as deploy,
            patch("staged_runtime.managed_ids", return_value={"owned": "id"}),
            patch("staged_runtime.cleanup") as cleanup,
        ):
            self.assertEqual(lab.managed_ids(), {"owned": "id"})
            lab.deploy()
            lab.cleanup(False)
            self.assertTrue(lab.check_placement({})["success"])
        deploy.assert_called_once()
        cleanup.assert_called_once_with(lab, False)
        (self.local / "staged-deployment.json").unlink()
        (self.root / "scripts/remote").mkdir(parents=True)
        (self.root / "scripts/remote/network_diagnostics.py").write_text('print("synthetic")')
        connection = MagicMock()
        with (
            patch.object(lab, "lab_output", return_value={"private_ips": {"db1": "10.77.0.21"}}),
            patch("network_diagnostics.console_evidence"),
            patch.object(lab, "connect", side_effect=[connection, OSError("unreachable")]),
            patch.object(lab, "remote") as remote,
        ):
            lab.diagnostics()
        self.assertEqual(remote.call_count, 2)

    def test_normal_deploy_preserves_configured_stack_and_canary_on_repeat(self) -> None:
        (self.local / "configured.json").write_text(json.dumps({"stack_sha256": stack_config.digest(lab.STACK)}))
        previous = {
            "server_ids": {"app": "id"},
            "private_ips": {"app": "10.77.0.10"},
            "node_roles": {"app": "app"},
        }
        canary = {"server_ids": {"app": "id"}}
        with (
            patch.object(lab, "managed_ids", return_value={"owned": "id"}),
            patch.object(lab, "lab_output", return_value=previous),
            patch.object(lab.pools, "selected", return_value={"phase": "active"}),
            patch("topology.validate_extension"),
            patch("verification.snapshot", return_value=canary),
            patch("infrastructure.apply") as apply,
            patch.object(lab, "configure") as configure,
            patch("verification.expansion") as verify,
            patch.object(lab.pools, "phase"),
        ):
            lab.deploy()
        configure.assert_called_once_with(existing=True)
        verify.assert_called_once_with(lab, canary)
        self.assertEqual(apply.call_args.kwargs["allowed_new_nodes"], set(topology.load(self.root)["nodes"]) - {"app"})

    def test_selected_candidates_and_preserved_stands_follow_separate_paths(self) -> None:
        with (
            patch.object(lab, "load_credentials", return_value=self.config),
            patch.object(lab.pools, "select") as select,
        ):
            lab.select_candidate(self.choice)
        select.assert_called_once_with(self.root, self.choice, "synthetic", "context.json")
        with (
            patch.object(lab, "load_credentials", return_value=self.config),
            patch.object(lab.pools, "candidates", return_value=iter([self.choice])),
        ):
            self.assertEqual(list(lab.deployment_candidates()), [self.choice])
        with (
            patch("cloud_settings.sequential_enabled", return_value=False),
            patch.object(lab, "managed_ids", return_value={}),
            patch.object(lab.pools, "selected", return_value=None),
            patch.object(lab, "deployment_candidates", return_value=[self.choice]),
            patch.object(lab, "select_candidate"),
            patch.object(lab, "prepare"),
            patch.object(lab, "deploy") as deploy,
        ):
            lab.prepare_selected()
            lab.deploy_selected()
        deploy.assert_called_once_with(initial_creation=True)
        (self.local / "configured.json").write_text("{}")
        with (
            patch("cloud_settings.sequential_enabled", return_value=False),
            patch.object(lab, "managed_ids", return_value={}),
            patch.object(lab.pools, "selected", return_value=None),
            self.assertRaisesRegex(ValueError, "recovery"),
        ):
            lab.deploy_selected()
