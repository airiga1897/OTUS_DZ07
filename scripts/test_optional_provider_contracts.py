"""Real provider constructor supports saved optional topology and cleanup independently."""

import copy
import unittest
from typing import cast
from unittest.mock import Mock, patch

import selectel_staged
import test_selectel_retained_contracts as fixtures
from selectel_adapter import infrastructure_result
from topology import Topology


class OptionalProviderContracts(unittest.TestCase):
    def test_cleanup_can_read_retired_database_topology_without_configuring_it(self) -> None:
        helper = fixtures.RetainedSelectelContracts()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        backend = helper.backend
        retired_topology: object = {
            "nodes": {
                "app": {"host": 10, "role": "app"},
                **{f"db{i}": {"host": 20 + i, "role": "db"} for i in range(1, 4)},
            },
            "etcd_members": ["db1", "db2", "db3"],
        }
        journal = backend.journal
        if type(journal) is not dict:
            raise AssertionError("expected saved journal")
        saved_journal = cast(dict[str, object], journal)
        saved_journal["topology"] = retired_topology
        backend.journal["stages"]["db1"] = {"status": "pending", "candidate": {"region": "ru-9"}}
        backend.save()
        saved = backend.path.read_bytes()
        desired_topology = Mock(side_effect=AssertionError("cleanup must use saved scope"))
        helper.api.desired_topology = desired_topology
        cleanup = selectel_staged.Backend(helper.api, cleanup=True)
        self.assertEqual(cleanup.desired, backend.journal["topology"])
        self.assertIn("db1", cleanup.cleanup_order())
        self.assertEqual(backend.path.read_bytes(), saved)
        desired_topology.assert_not_called()
        if not isinstance(cleanup.cloud, Mock):
            self.fail("Expected mocked cloud adapter")
        if not isinstance(cleanup.global_router, Mock):
            self.fail("Expected mocked global router")
        cleanup.cloud.get.assert_not_called()
        cleanup.global_router.listing.assert_not_called()

    def test_cleanup_uses_saved_five_node_topology_without_new_optional_settings(self) -> None:
        with patch("privacy._values", set()):
            helper = fixtures.RetainedSelectelContracts()
            helper.setUp()
            self.addCleanup(helper.doCleanups)
            backend = helper.backend
            base: Topology = copy.deepcopy(backend.desired)
            backend.journal["topology"] = copy.deepcopy(base)
            backend.journal["topology"]["nodes"]["es4"] = {"host": 34, "role": "es"}
            backend.journal["optional_es4"] = {"base_topology": base, "requested_node": "es4"}
            backend.save()
            (helper.root / "config/optional-elasticsearch.json").unlink()
            cleanup = selectel_staged.Backend(helper.api, cleanup=True)
            self.assertIn("es4", cleanup.desired["nodes"])
            self.assertEqual(cleanup.journal["deployment_id"], backend.journal["deployment_id"])

    def test_provider_contract_retains_explicit_roles_instead_of_inferring_from_names(self) -> None:
        output = {
            "server_ids": {"app": "app-id", "storage": "data-id"},
            "node_roles": {"app": "app", "storage": "es"},
            "private_ips": {"app": "10.77.0.10", "storage": "10.77.0.34"},
            "volume_ids": {"app": "app-disk", "storage": "data-disk"},
            "port_ids": {"app": "app-port", "storage": "data-port"},
            "app_public_ip": "8.8.8.8",
            "subnet_cidr": "10.77.0.0/24",
        }
        result = infrastructure_result(output, {"project_id": "synthetic", "region": "ru-9"}, "otus-dz07", "ru-9a")
        self.assertEqual(result.nodes["storage"]["roles"], ["es"])
