"""Exercise actual Selectel journal, input and apply contracts with only external IO mocked."""

import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import selectel_staged as staged
import stack_config
import topology
from placement_config import PlacementPolicy
from stage_journal import StageJournal, StageOutput

ROOT = Path(__file__).resolve().parents[1]


class RetainedSelectelContracts(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory(dir=ROOT / ".tools")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        shutil.copytree(ROOT / "config", self.root / "config")
        shutil.copyfile(ROOT / ".env.example", self.root / ".env.example")
        (self.root / ".local").mkdir()
        self.credentials = {
            "region": "ru-9",
            "project_id": "synthetic-project",
            "domain_name": "synthetic",
            "username": "synthetic-user",
            "password": "synthetic-secret",
        }
        self.api = SimpleNamespace(
            ROOT=self.root,
            LOCAL=self.root / ".local",
            PREFIX="otus-dz07",
            STACK=stack_config.load(),
            topology=topology,
            desired_topology=lambda: topology.load(ROOT),
            RUN=self.root / ".local/runs",
            redact=lambda text: text,
        )
        self.api.save = lambda path, value: (
            path.parent.mkdir(parents=True, exist_ok=True),
            path.write_text(json.dumps(value)),
        )
        for target, value in [
            ("load_credentials", self.credentials),
            ("SelectelAdapter", Mock()),
            ("GlobalRouter", Mock()),
        ]:
            setting = patch.object(staged, target, return_value=value)
            setting.start()
            self.addCleanup(setting.stop)
        self.backend = staged.Backend(self.api)

    def test_constructor_scope_and_legacy_context_are_checked_before_cloud_operations(self) -> None:
        b = self.backend
        b.save()
        self.assertEqual(staged.Backend(self.api).journal["deployment_id"], b.journal["deployment_id"])

        def foreign_provider(journal: StageJournal) -> None:
            journal["provider"] = "foreign"

        def foreign_namespace(journal: StageJournal) -> None:
            journal["namespace"] = "foreign"

        def invalid_deployment(journal: StageJournal) -> None:
            journal["deployment_id"] = "invalid"

        for key, edit in (
            ("provider", foreign_provider),
            ("namespace", foreign_namespace),
            ("deployment_id", invalid_deployment),
        ):
            data = copy.deepcopy(b.journal)
            edit(data)
            b.path.write_text(json.dumps(data))
            with self.subTest(key=key), self.assertRaises(ValueError):
                staged.Backend(self.api)
        b.path.unlink()
        (self.root / ".local/context.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "legacy"):
            staged.Backend(self.api)

    def test_account_global_router_binding_does_not_require_bootstrap_compute_pool(self) -> None:
        b = self.backend
        b.journal["pool_subnets"] = {"ru-8": "10.96.0.0/24"}
        b.journal["stages"] = {"global": {"candidate": {"region": "ru-9"}, "status": "active"}}
        b.validate_journal(cleanup=True)
        b.journal["stages"]["global"]["candidate"]["region"] = "foreign"
        with self.assertRaises(ValueError):
            b.validate_journal(cleanup=True)
        b.journal["stages"] = {"app": {"candidate": {"region": "ru-9"}}}
        with self.assertRaises(ValueError):
            b.validate_journal(cleanup=True)

    def test_each_stage_input_uses_saved_network_and_node_settings(self) -> None:
        b = self.backend
        network: StageOutput = {
            "network_id": "network",
            "subnet_id": "subnet",
            "security_group_id": "group",
            "server_group_id": "placement",
            "external_network_name": "external",
            "cidr": "10.77.0.0/24",
            "gateway_ip": "10.77.0.1",
        }
        candidate: PlacementPolicy = {
            "region": "ru-9",
            "availability_zone": "ru-9a",
            "network_availability_zone": "ru-9a",
            "router_availability_zone": "ru-9a",
            "image_id": "image",
            "flavor_ids": {"app": "flavor", "es": "flavor"},
            "volume_type": "universal.ru-9a",
        }
        b.journal["admin_cidrs"] = ["8.8.8.8/32"]
        b.journal["stages"] = {
            name: {"candidate": copy.deepcopy(candidate)}
            for name in ("global", "network-ru-9", "connection-ru-9", "es1")
        }
        b.journal["stages"]["global"]["output"] = {"router_id": "router"}
        b.journal["stages"]["network-ru-9"]["output"] = network
        (self.root / ".local/secrets.json").write_text('{"api":"synthetic-nonce-key"}')
        ensure_pool = Mock(return_value=network)
        quota = Mock()
        saved_id = Mock(return_value=None)
        with (
            patch.object(b, "ensure_pool", ensure_pool),
            patch.object(b, "check_node_quota", quota),
            patch.object(b, "saved_id", saved_id),
        ):
            for name, kind in [
                ("global", "global"),
                ("network-ru-9", "network"),
                ("connection-ru-9", "connection"),
                ("es1", "node"),
            ]:
                actual, inputs = b.inputs(name)
                self.assertEqual(actual, kind)
                if kind == "node":
                    self.assertEqual(inputs["private_ip"], "10.77.0.31")
                    self.assertTrue(inputs["require_distinct_hosts"])
                if kind == "connection":
                    self.assertEqual(inputs["os_network_id"], "network")
        quota.assert_called_once_with(candidate, "es")

    def test_write_ahead_apply_and_finish_keep_successful_vm_on_attachment_error(self) -> None:
        b = self.backend
        b.store = Mock()
        b.store.identities.return_value = {}
        b.begin("app", {"region": "ru-9"})
        stage_inputs = Mock(return_value=("node", {"same": "settings"}))
        configure_stage = Mock()
        connect_pool = Mock(side_effect=RuntimeError("attachment ERROR"))
        b.store.apply.return_value = {"id": "own-vm"}
        b.store.state.return_value = {"lineage": "lineage"}
        with (
            patch.object(b, "inputs", stage_inputs),
            patch.object(b, "configure_stage", configure_stage),
            patch("selectel_staged_audit.faults", return_value={}),
            patch("selectel_staged_audit.assert_stage_owned"),
        ):
            self.assertEqual(b.apply("app"), {"id": "own-vm"})
        b.store.identities.return_value = {"vm": "own-vm"}
        with patch.object(b, "connect_pool", connect_pool), self.assertRaises(RuntimeError):
            b.finish("app")
        saved = json.loads(b.path.read_text())["stages"]["app"]
        self.assertEqual(saved["status"], "active")
        self.assertEqual(saved["identities"], {"vm": "own-vm"})
        connect_pool.side_effect = None
        with patch.object(b, "connect_pool", connect_pool), patch("selectel_staged_audit.assert_stage_owned"):
            b.reapply("app")
        self.assertTrue(b.store.apply.call_args.kwargs["unchanged"])

    def test_saved_identity_and_preservation_guards(self) -> None:
        b = self.backend
        b.journal["stages"] = {"es1": {"candidate": {"region": "ru-9"}, "status": "pending"}}
        b.store = Mock()
        b.store.entries.return_value = [{"type": "vm", "attrs": {"id": "one"}}, {"type": "vm", "attrs": {"id": "two"}}]
        with self.assertRaises(ValueError):
            b.saved_id("es1", "vm", "id")
        b.store.identities.return_value = {"vm": "one"}
        self.assertEqual(b.identities(), {("es1", "vm"): "one"})
        with self.assertRaises(RuntimeError):
            b.assert_preserved({("es1", "vm"): "old"})
        b.journal["stages"]["es1"]["status"] = "deleted"
        self.assertIsNone(b.saved_id("es1", "vm", "id"))

    def test_invalid_mutations_of_saved_policy_are_rejected(self) -> None:
        b = self.backend
        baseline = copy.deepcopy(b.journal)

        def hosts(journal: StageJournal) -> None:
            journal["require_distinct_hosts"] = False

        def disk(journal: StageJournal) -> None:
            settings = journal.get("run_settings")
            if not isinstance(settings, dict):
                raise AssertionError("expected saved run settings")
            settings["boot_disk_gb"] = 999

        def gateway(journal: StageJournal) -> None:
            journal["address_policy"]["global_gateway"] = 99

        def subnet(journal: StageJournal) -> None:
            journal["pool_subnets"]["ru-9"] = "10.200.0.0/24"

        def foreign(journal: StageJournal) -> None:
            journal["stages"]["foreign"] = {"candidate": {"region": "ru-9"}}

        for edit in (hosts, disk, gateway, subnet, foreign):
            b.journal = copy.deepcopy(baseline)
            edit(b.journal)
            with self.assertRaises(ValueError):
                b.validate_journal()
