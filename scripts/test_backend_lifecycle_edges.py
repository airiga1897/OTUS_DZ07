"""Retained stages reject ambiguous identities and preserve deletion evidence."""

import ipaddress
import json
import unittest
from unittest.mock import Mock, patch

import selectel_stage_operations as stage_operations
import test_selectel_retained_contracts as fixtures
from cloud_contract import StandDescription
from plan_diagnostics import PlannedResource


class BackendLifecycleEdges(unittest.TestCase):
    def setUp(self) -> None:
        helper = fixtures.RetainedSelectelContracts()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        self.b = helper.backend

    def test_ambiguous_or_deleted_partial_identity_cannot_be_adopted(self) -> None:
        b = self.b
        with (
            patch.object(
                b.store,
                "entries",
                return_value=[{"type": "vm", "attrs": {"id": "one"}}, {"type": "vm", "attrs": {"id": "two"}}],
            ),
            self.assertRaisesRegex(ValueError, "Ambiguous"),
        ):
            b.saved_id("es1", "vm", "id")
        b.journal["stages"]["es1"] = {"status": "deleted", "output": {"id": "old"}}
        self.assertIsNone(b.saved_id("es1", "vm", "id"))
        with (
            patch.object(b.store, "identities", return_value={"vm": "owned"}),
            self.assertRaisesRegex(ValueError, "resources remaining"),
        ):
            b.begin("es1", {"region": "ru-3"})
        self.assertEqual(b.journal["stages"]["es1"]["status"], "deleted")

    def test_pending_and_identity_filters_cannot_hide_preserved_resource_replacement(self) -> None:
        b = self.b
        b.journal["stages"] = {
            "app": {"status": "active", "candidate": {"region": "ru-9"}},
            "es1": {"status": "pending", "candidate": {"region": "ru-3"}},
            "es2": {"status": "deleted", "candidate": {"region": "ru-3"}},
        }
        with patch.object(b.store, "identities", side_effect=lambda n: {"vm": n + "-id"} if n != "es2" else {}):
            self.assertEqual(b.identities(exclude_pending=True), {("app", "vm"): "app-id"})
            self.assertEqual(b.pending("es1"), {"region": "ru-3"})
            self.assertIsNone(b.pending("es2"))
            self.assertIsNone(b.pending("unknown"))
            b.assert_preserved({("app", "vm"): "app-id"})
            with self.assertRaisesRegex(RuntimeError, "identity changed"):
                b.assert_preserved({("app", "vm"): "replacement"})

    def test_connection_uses_live_pool_set_and_same_global_identity(self) -> None:
        b = self.b
        b.journal["stages"] = {
            "global": {"status": "active", "output": {"router_id": "router"}},
            "connection-ru-3": {"status": "active", "candidate": {"region": "ru-3"}},
            "connection-ru-8": {"status": "deleted", "candidate": {"region": "ru-8"}},
        }
        preflight = Mock()
        place = Mock()
        with patch.object(b.global_router, "preflight", preflight), patch.object(stage_operations, "place", place):
            b.connect_pool("ru-9")
        self.assertEqual(preflight.call_args.args, (["ru-3", "ru-9"], b.api.PREFIX, "router"))
        self.assertEqual([c.args[1] for c in place.call_args_list], ["global", "connection-ru-3", "connection-ru-9"])

    def populate_node_outputs(self) -> None:
        b = self.b
        for n, node in b.desired["nodes"].items():
            pool = "ru-9" if n == "app" else "ru-3"
            cidr = b.journal["pool_subnets"][pool]
            b.journal["stages"][n] = {
                "status": "active",
                "output": {
                    "id": n + "-id",
                    "volume_id": n + "-volume",
                    "port_id": n + "-port",
                    "roles": [node["role"]],
                    "private_ip": str(ipaddress.ip_network(cidr)[node["host"]]),
                    "public_ip": "8.8.8.8" if n == "app" else None,
                    "region": pool,
                    "zone": pool + "a",
                    "subnet_cidr": cidr,
                },
            }

    def test_actual_result_has_private_per_node_network_binding(self) -> None:
        self.populate_node_outputs()
        b = self.b
        result = b.result()
        self.assertEqual(result.application()["node_regions"]["es1"], "ru-3")
        self.assertEqual(len(result.nodes), 4)
        b.journal["stages"]["es1"]["output"]["id"] = "app-id"
        with self.assertRaises(ValueError):
            b.result()

    def test_configuration_target_uses_only_audited_deleted_app_identity(self) -> None:
        self.populate_node_outputs()
        target = self.b.configuration_target()
        self.assertFalse(target.deletion_verified)
        self.assertEqual(target.lab["server_ids"]["app"], "app-id")
        app = self.b.journal["stages"]["app"]
        app["status"] = "deleted"
        self.b.journal["stages"]["es1"] = {"status": "deleted"}
        target = self.b.configuration_target()
        self.assertTrue(target.deletion_verified)
        self.assertEqual(target.lab, {"server_ids": {"app": "app-id"}})
        output = json.loads(json.dumps(app["output"]))
        output["id"] = None
        app["output"] = output
        with self.assertRaisesRegex(ValueError, "saved VM identity"):
            self.b.configuration_target()

    def test_global_diagnostics_delegation_is_scoped_to_saved_node_pool(self) -> None:
        b = self.b
        value: StandDescription = {"node_regions": {"es1": "ru-3"}, "server_ids": {"es1": "owned"}}
        cloud = Mock()
        cloud.get.return_value = {"output": "synthetic console"}
        with patch.object(b, "region", return_value=cloud) as region:
            self.assertEqual(b.console_output(value, "es1"), "synthetic console")
            b.ssh_host_key(value, "es1", "cached-key")
        self.assertEqual([c.args[0] for c in region.call_args_list], ["ru-3", "ru-3"])
        cloud.ssh_host_key.assert_called_once_with("es1", "owned", b.api.PREFIX, "cached-key")

    def test_audit_refuses_untracked_named_or_owner_marked_floating_ip(self) -> None:
        b = self.b
        b.journal["stages"] = {"es1": {"candidate": {"region": "ru-9"}}}
        with patch.object(b.global_router, "assert_absent", create=True):
            for objects in (
                [{"name": b.api.PREFIX + "-app"}],
                [{"description": b.owner}],
                [{"metadata": {"dz07_owner": b.owner}}],
                [{"tags": [b.owner]}],
            ):
                with (
                    patch("selectel_staged_audit.assert_stage_absent"),
                    patch("selectel_staged_audit.objects", return_value=objects),
                    self.assertRaisesRegex(RuntimeError, "namespace is not empty"),
                ):
                    b.audit_deletion()

    def test_network_cleanup_reuses_saved_inputs_and_only_marks_deleted_after_api_absence(self) -> None:
        b = self.b
        name = "network-ru-9"
        b.journal["stages"][name] = {
            "candidate": {"region": "ru-9"},
            "status": "active",
            "kind": "network",
            "inputs": {"name": "own"},
        }
        folder = b.store.folder(name)
        folder.mkdir(parents=True)
        (folder / "main.tf.json").write_text("{}")
        with (
            patch.object(b.store, "entries", return_value=[]),
            patch.object(b.store, "apply") as apply,
            patch.object(b, "configure_stage") as configure_stage,
            patch("selectel_staged_audit.assert_stage_owned"),
            patch("selectel_staged_audit.assert_stage_absent", side_effect=OSError("API unavailable")),
            self.assertRaises(OSError),
        ):
            b.cleanup_attempt(name)
        self.assertEqual(b.journal["stages"][name]["status"], "deleting")
        apply.assert_called_once_with(name, destroy=True)
        configure_stage.assert_called_once_with(name, "network", {"name": "own"})

    def test_unsupported_and_unchanged_refresh_are_not_accepted(self) -> None:
        b = self.b
        for kind in ("selectel_global_router_router_v1", "openstack_networking_network_v2"):
            change: PlannedResource = {
                "address": "network-ru-9",
                "type": kind,
                "change": {"actions": ["update"], "before": {"id": "same"}, "after": {"id": "same"}},
            }
            self.assertFalse(b.allow_refresh("network-ru-9", change))
        b.journal["stages"] = {"global": {"candidate": {"region": "ru-9"}, "status": "pending"}}
        with (
            patch.object(b, "inputs", return_value=("global", {"name": "saved"})),
            patch.object(b, "configure_stage"),
            patch.object(b.store, "apply", return_value={"router_id": "new"}),
            patch("selectel_staged_audit.assert_stage_owned"),
        ):
            self.assertEqual(b.apply("global"), {"router_id": "new"})
