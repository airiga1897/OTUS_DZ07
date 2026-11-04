"""Explicit ES power checks and expansion requests use isolated state and mock APIs."""

import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import elastic_options as options
import observability
import run_settings
import test_cycle
import topology
from ansible_operation import OperationContext
from cloud_contract import StandDescription

ROOT = Path(__file__).resolve().parents[1]


class OptionalElasticsearchTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(dir=ROOT / ".tools")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        shutil.copytree(ROOT / "config", self.root / "config")
        shutil.copyfile(ROOT / ".env.example", self.root / ".env.example")
        self.local = self.root / ".local"
        self.local.mkdir()
        self.base = topology.load(self.root)
        self.lab: StandDescription = {
            "private_ips": {n: "10.77.0." + str(v["host"]) for n, v in self.base["nodes"].items()},
            "server_ids": {n: "id-" + n for n in self.base["nodes"]},
            "node_roles": {n: v["role"] for n, v in self.base["nodes"].items()},
            "node_regions": {n: "ru-3" if n == "es2" else "ru-9" for n in self.base["nodes"]},
        }
        self.backend = Mock(unsafe=True)
        self.backend.completed.return_value = True
        self.backend.journal = {
            "deployment_id": "a" * 32,
            "scope": {"project_id": "synthetic"},
            "namespace": "otus-dz07",
            "configured": True,
            "topology": copy.deepcopy(self.base),
            "stages": {
                n: {
                    "status": "active",
                    "output": {"id": self.lab["server_ids"][n], "region": self.lab["node_regions"][n]},
                }
                for n in self.base["nodes"]
            },
        }
        self.cloud = Mock()
        self.backend.region.return_value = self.cloud

        def save(path, value) -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value))

        self.backend.save.side_effect = lambda: save(self.local / "staged-deployment.json", self.backend.journal)
        self.backend.save()
        self.api = SimpleNamespace(
            ROOT=self.root,
            LOCAL=self.local,
            PREFIX="otus-dz07",
            _TEST_CYCLE_ID=None,
            lab_output=lambda: self.lab,
            save=save,
            verify=Mock(return_value={"logs": True}),
        )

        def command(api, action):
            if action == "master":
                return {"node": "es2"}
            if action == "failure":
                return {"new_master": {"node": "es1"}}
            return {"status": "green", "number_of_nodes": 3}

        self.command = Mock(side_effect=command)
        for patcher in (
            patch("staged_runtime.backend_factory", return_value=self.backend),
            patch("observability.command", self.command),
            patch("time.sleep"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_master_uses_its_pool_and_restore_confirms_cluster(self) -> None:
        result = options.verify_failover(self.api)
        self.assertTrue(result["failover"]["restored"])
        self.assertEqual([c.args for c in self.backend.region.call_args_list], [("ru-3",), ("ru-3",)])
        self.assertEqual([c.kwargs["running"] for c in self.cloud.set_power.call_args_list], [False, True])
        self.assertTrue(all(c.args[:2] == ("es2", "id-es2") for c in self.cloud.set_power.call_args_list))
        self.api.verify.assert_called_once_with(False)
        self.assertFalse((self.local / options.MARKER).exists())
        self.assertEqual(
            [c.args[1] for c in self.command.call_args_list], ["health", "master", "failure", "health", "recovery"]
        )

    def test_no_master_change_restores_and_returns_original_error(self) -> None:
        original = self.command.side_effect
        self.command.side_effect = lambda api, action: (
            {"new_master": {"node": "es2"}} if action == "failure" else original(api, action)
        )
        with self.assertRaisesRegex(RuntimeError, "master did not change"):
            options.failover(self.api)
        self.assertTrue(self.cloud.set_power.call_args.kwargs["running"])
        self.assertFalse((self.local / options.MARKER).exists())

    def test_unrecognized_replacement_master_is_not_reported_as_success(self) -> None:
        original = self.command.side_effect
        self.command.side_effect = lambda api, action: (
            {"new_master": {"node": "foreign"}} if action == "failure" else original(api, action)
        )
        with self.assertRaisesRegex(RuntimeError, "outside the saved master set"):
            options.failover(self.api)
        self.assertTrue(self.cloud.set_power.call_args.kwargs["running"])

    def test_controller_interrupt_still_restores_before_propagating(self) -> None:
        self.cloud.set_power.side_effect = [KeyboardInterrupt(), None]
        with self.assertRaises(KeyboardInterrupt):
            options.failover(self.api)
        self.assertTrue(self.cloud.set_power.call_args.kwargs["running"])
        self.assertFalse((self.local / options.MARKER).exists())

    def test_stop_response_lost_and_restore_failure_preserve_primary_and_recovery(self) -> None:
        primary = OSError("PRIVATE-STOP")

        def fail(name, identity, **kwargs) -> None:
            raise primary if not kwargs["running"] else TimeoutError("PRIVATE-RESTORE")

        self.cloud.set_power.side_effect = fail
        with self.assertRaises(OSError) as caught:
            options.failover(self.api)
        self.assertIs(caught.exception, primary)
        self.assertEqual(self.cloud.set_power.call_count, 4)
        self.assertTrue((self.local / options.MARKER).exists())
        report = json.loads((self.root / "docs/elastic-failover.json").read_text())
        self.assertFalse(report["restored"])
        self.assertNotIn("PRIVATE", json.dumps(report))

    def test_retry_saved_recovery_never_stops_another_node(self) -> None:
        self.cloud.set_power.side_effect = TimeoutError("unavailable")
        with self.assertRaises(TimeoutError):
            options.failover(self.api)
        self.cloud.set_power.reset_mock()
        self.cloud.set_power.side_effect = None
        self.command.reset_mock()
        result = options.failover(self.api)
        self.assertTrue(result["resumed_recovery"])
        self.assertTrue(all(c.kwargs["running"] for c in self.cloud.set_power.call_args_list))
        self.assertNotIn("master", [c.args[1] for c in self.command.call_args_list])
        self.assertFalse((self.local / options.MARKER).exists())

    def test_restore_retry_and_lost_probe_do_not_trigger_duplicate_shutdown(self) -> None:
        self.cloud.set_power.side_effect = [None, TimeoutError("transient"), None]
        self.assertTrue(options.failover(self.api)["restored"])
        self.assertEqual([c.kwargs["running"] for c in self.cloud.set_power.call_args_list], [False, True, True])

    def test_restore_failure_after_successful_failure_check_is_not_success(self) -> None:
        self.cloud.set_power.side_effect = [None, OSError("offline"), OSError("offline"), OSError("offline")]
        with self.assertRaises(OSError):
            options.failover(self.api)
        record = json.loads((self.local / options.MARKER).read_text())
        self.assertTrue(record["probe_written"])
        self.cloud.set_power.side_effect = None
        self.assertTrue(options.failover(self.api)["restored"])

    def test_foreign_recovery_and_wrong_vm_binding_are_blocked_before_power(self) -> None:
        request = options.binding(self.backend, self.lab, "es2")
        for key, value in (("server_id", "foreign"), ("region", "ru-2"), ("deployment_id", "b" * 32), ("node", "app")):
            with self.subTest(key=key):
                (self.local / options.MARKER).write_text(json.dumps({**request, key: value}))
                with self.assertRaises(ValueError):
                    options.failover(self.api)
                self.cloud.set_power.assert_not_called()
        (self.local / options.MARKER).unlink()
        self.lab["node_regions"]["es2"] = "ru-9"
        with self.assertRaisesRegex(ValueError, "node identity"):
            options.failover(self.api)
        self.cloud.set_power.assert_not_called()

    def pending_configuration(self) -> OperationContext:
        return {
            "bootstrap": False,
            "finalized": False,
            "state": "running",
            "request": {
                "id": "b" * 32,
                "deployment_id": self.backend.journal["deployment_id"],
                "namespace": self.api.PREFIX,
                "app_id": self.lab["server_ids"]["app"],
                "parameters_sha256": "c" * 64,
                "timeout": 1800,
                "stop_wait": 1,
                "payload": dict.fromkeys(("inventory.json", "controls.json", "key", "known_hosts"), "d" * 64),
            },
        }

    def test_foreign_master_pending_configuration_and_incomplete_topology_are_blocked(self) -> None:
        original = self.command.side_effect
        self.command.side_effect = lambda api, action: {"node": "app"} if action == "master" else original(api, action)
        with self.assertRaisesRegex(ValueError, "outside own"):
            options.failover(self.api)
        self.command.side_effect = original
        (self.local / "ansible-operation.json").write_text(json.dumps(self.pending_configuration()))
        with self.assertRaisesRegex(ValueError, "Finish configuration"):
            options.failover(self.api)
        (self.local / "ansible-operation.json").unlink()
        self.backend.completed.return_value = False
        with self.assertRaisesRegex(ValueError, "Finish the requested topology"):
            options.failover(self.api)
        self.cloud.set_power.assert_not_called()

    def test_stale_guest_node_count_or_yellow_health_does_not_allow_shutdown(self) -> None:
        for health in ({"status": "green", "number_of_nodes": 2}, {"status": "yellow", "number_of_nodes": 3}):
            with self.subTest(health=health):
                self.command.side_effect = lambda api, action, *, health=health: health
                with self.assertRaisesRegex(RuntimeError, "membership is unconfirmed"):
                    options.failover(self.api)
                self.cloud.set_power.assert_not_called()

    def test_evidence_failure_cannot_mask_primary_error(self) -> None:
        primary = OSError("PRIVATE")
        self.cloud.set_power.side_effect = [primary, None]
        self.api.save = Mock(side_effect=PermissionError("PRIVATE-DISK"))
        with self.assertRaises(OSError) as caught:
            options.failover(self.api)
        self.assertIs(caught.exception, primary)
        self.assertTrue(self.cloud.set_power.call_args.kwargs["running"])

    def test_es4_request_is_persistent_and_idempotent_without_changing_base(self) -> None:
        original = (self.root / "config/topology.json").read_bytes()
        with patch("verification.snapshot") as snapshot, patch("staged_runtime.deploy") as deploy:
            options.add_es4(self.api)
            saved = json.loads((self.local / "staged-deployment.json").read_text())
            target = options.desired(self.root, self.base)
            self.assertEqual(topology.validate_extension(self.base, target), {"es4"})
            self.assertEqual(saved["topology"], target)
            options.add_es4(self.api)
        snapshot.assert_called_once()
        self.assertEqual(deploy.call_count, 2)
        self.assertEqual((self.root / "config/topology.json").read_bytes(), original)
        self.assertEqual(self.api.verify.call_count, 2)

    def test_es4_joins_as_data_ingest_without_altering_bootstrap_masters(self) -> None:
        lab = copy.deepcopy(self.lab)
        lab["private_ips"]["es4"] = "10.77.0.34"
        lab["node_roles"]["es4"] = "es"
        value = observability.settings(
            lab, {"elastic": "synthetic", "shipper": "synthetic"}, "es4", topology.services(self.root)
        )
        self.assertEqual(value["roles"], ["data", "ingest"])
        self.assertEqual(value["initial_masters"], ["es1", "es2", "es3"])
        self.assertEqual(len(value["ips"]), 4)
        self.assertEqual(len(value["seed_hosts"]), 3)
        self.assertEqual(set(value["secrets"]), {"elastic", "shipper"})

    def test_failed_extension_keeps_request_and_existing_records(self) -> None:
        with (
            patch("verification.snapshot"),
            patch("staged_runtime.deploy", side_effect=RuntimeError("capacity")),
            self.assertRaises(RuntimeError),
        ):
            options.add_es4(self.api)
        saved = json.loads((self.local / "staged-deployment.json").read_text())
        self.assertEqual(saved["optional_es4"]["requested_node"], "es4")
        self.assertEqual(saved["stages"], self.backend.journal["stages"])
        self.api.verify.assert_not_called()

    def test_new_extension_requires_configured_base_and_no_pending_operation(self) -> None:
        self.backend.journal["configured"] = False
        with self.assertRaisesRegex(ValueError, "configured existing"):
            options.add_es4(self.api)
        self.backend.journal["configured"] = True
        (self.local / "ansible-operation.json").write_text(json.dumps(self.pending_configuration()))
        with self.assertRaisesRegex(ValueError, "Finish the saved Ansible operation"):
            options.add_es4(self.api)
        (self.local / "ansible-operation.json").unlink()
        self.backend.journal["topology"]["nodes"]["es1"]["host"] = 39
        with self.assertRaisesRegex(ValueError, "differs from the base"):
            options.add_es4(self.api)

    def test_saved_request_and_optional_settings_are_validated(self) -> None:
        self.assertEqual(options.desired(self.root, self.base), self.base)
        self.backend.journal.update(
            optional_es4={"base_topology": self.base, "requested_node": "es4"}, topology=copy.deepcopy(self.base)
        )
        self.backend.save()
        with self.assertRaisesRegex(ValueError, "conflicts"):
            options.desired(self.root, self.base)
        self.backend.journal["topology"]["nodes"]["es4"] = options.settings(self.root)["es4"]
        self.backend.save()
        target = options.desired(self.root, self.base)
        policy = run_settings.placement(self.root)
        options.node_order(policy, self.backend.journal, target)
        self.assertEqual(policy["sequential_placement"]["node_order"][-1], "es4")
        with self.assertRaisesRegex(ValueError, "node order"):
            options.node_order(policy, self.backend.journal, target)
        path = self.root / "config/optional-elasticsearch.json"
        value = json.loads(path.read_text())
        value["restore_attempts"] = 0
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "Invalid optional"):
            options.settings(self.root)

    def test_pending_failover_blocks_extension_and_ordinary_configuration(self) -> None:
        (self.local / options.MARKER).write_text("{}")
        with self.assertRaisesRegex(ValueError, "verify --es-failover"):
            options.add_es4(self.api)
        with self.assertRaisesRegex(ValueError, "verify --es-failover"):
            options.require_no_failover(self.api)

    def test_temporary_cycle_rejects_es4_before_any_deployment(self) -> None:
        api = Mock()
        api.desired_topology.return_value = {"nodes": {"es4": {"host": 34, "role": "es"}}}
        with self.assertRaisesRegex(ValueError, "excluded from test-cycle"):
            test_cycle.stages(api)
        api.deploy_selected.assert_not_called()
