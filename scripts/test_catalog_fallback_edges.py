"""Full synthetic catalogs exercise skipped segments and explicit quota credits."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import pool_selection as pools
import selectel_staged_capacity as quota
import test_staged_backend as backend_fixtures
from cloud_contract import TerraformError

ROOT = Path(__file__).resolve().parents[1]


class CatalogFallbackEdges(unittest.TestCase):
    def catalog(self):
        config = {
            "pools": ["ru-9"],
            "image_name": "Ubuntu",
            "image_checksums": ["a" * 32],
            "boot_disk_gb": 20,
            "disk_type_patterns": ["universal.*"],
            "flavor_name_patterns": ["SL1.{vcpus}-{ram_mb}"],
            "profiles": {"app": {"vcpus": 4, "ram_mb": 8192}},
        }
        catalog = [
            {"type": s, "endpoints": [{"region": "ru-9", "interface": "public", "url": "https://example.invalid/" + s}]}
            for s in pools.SERVICES
        ]
        zones = [{"zoneName": "ru-9a", "zoneState": {"available": True}}]
        data = {
            "image/v2/images?status=active": {
                "images": [
                    {
                        "id": "image",
                        "name": "Ubuntu",
                        "visibility": "public",
                        "status": "active",
                        "os_distro": "ubuntu",
                        "checksum": "a" * 32,
                    }
                ]
            },
            "compute/flavors/detail": {
                "flavors": [{"id": "flavor", "name": "SL1.4-8192", "vcpus": 4, "ram": 8192, "disk": 0}]
            },
            "volumev3/types": {
                "volume_types": [{"name": "universal.ru-9a", "extra_specs": {"RESKEY:availability_zones": "ru-9a"}}]
            },
            "compute/os-availability-zone": {"availabilityZoneInfo": copy.deepcopy(zones)},
            "volumev3/os-availability-zone": {"availabilityZoneInfo": copy.deepcopy(zones)},
            "network/v2.0/availability_zones": {
                "availability_zones": [
                    {"resource": r, "state": "available", "name": "ru-9a"} for r in ("network", "router")
                ]
            },
        }

        def request(url, **kwargs):
            return data[url.removeprefix("https://example.invalid/")], {}

        return config, catalog, data, request

    def test_invalid_disk_or_flavor_catalog_does_not_select_unconfirmed_zone(self) -> None:
        for mode in ("valid", "segment", "volume-zone", "opaque-disk", "flavor-zone"):
            config, catalog, data, request = self.catalog()
            if mode == "segment":
                config["segment"] = "ru-9b"
            if mode == "volume-zone":
                data["volumev3/os-availability-zone"]["availabilityZoneInfo"] = []
            if mode == "opaque-disk":
                data["volumev3/types"]["volume_types"][0]["extra_specs"] = {}
            if mode == "flavor-zone":
                data["compute/flavors/detail"]["flavors"][0]["availability_zones"] = ["ru-3b"]
            with (
                self.subTest(mode=mode),
                patch.object(pools, "authenticate", return_value=("synthetic", catalog)),
                patch.object(pools, "request_json", side_effect=request),
            ):
                choices = list(pools.candidates(config, {}))
                self.assertEqual(bool(choices), mode == "valid")

    def test_incomplete_automatic_pool_and_invalid_flavor_patterns_are_rejected(self) -> None:
        config, catalog, data, request = self.catalog()
        catalog = [s for s in catalog if s["type"] != "image"]
        self.assertEqual(pools.ordered_pools({"pools": ["auto"], "excluded_pools": []}, catalog), [])
        for patterns in ([], None, "SL1.*", [None]):
            bad = {
                "profiles": {"app": {"vcpus": 4, "ram_mb": 8192}},
                "flavor_name_patterns": patterns,
                "flavor_name_template": "SL1.*",
            }
            if patterns is None:
                self.assertEqual(
                    len(
                        pools.flavor_choices(
                            [{"id": "f", "name": "SL1.4-8192", "vcpus": 4, "ram": 8192, "disk": 0}], bad
                        )["app"]
                    ),
                    1,
                )
            else:
                with self.assertRaises(ValueError):
                    pools.flavor_choices([], bad)

    def test_remaining_demand_credits_only_managed_known_nodes_and_implicit_service_ports(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tools") as directory:
            root = Path(directory)
            tf = root / "terraform-selectel"
            tf.mkdir()
            base = {
                "cores": 8,
                "ram": 16384,
                "instances": 2,
                "disk_gb": 40,
                "volumes": 2,
                "port": 10,
                "security_group_rule": 6,
            }
            resources = [{"mode": "data"}]
            kinds = [
                "networking_network_v2",
                "networking_subnet_v2",
                "networking_router_v2",
                "networking_router_interface_v2",
                "networking_secgroup_v2",
                "networking_floatingip_v2",
            ]
            resources += [
                {"mode": "managed", "type": "openstack_" + kind, "instances": [{"attributes": {}}]} for kind in kinds
            ]
            resources += [
                {
                    "mode": "managed",
                    "type": "openstack_compute_instance_v2",
                    "instances": [{"index_key": "app", "attributes": {}}, {"index_key": "unknown", "attributes": {}}],
                },
                {
                    "mode": "managed",
                    "type": "openstack_blockstorage_volume_v3",
                    "instances": [{"attributes": {"size": 20}}],
                },
            ]
            (tf / "terraform.tfstate").write_text(json.dumps({"resources": resources}))
            result = pools.remaining_demand(root, base, {"app": {"vcpus": 4, "ram_mb": 8192}})
            self.assertEqual(
                result,
                {
                    "cores": 4,
                    "ram": 8192,
                    "instances": 1,
                    "disk_gb": 20,
                    "volumes": 1,
                    "port": 6,
                    "security_group_rule": 4,
                },
            )

    def test_scheduler_retry_refuses_foreign_live_uuid_and_ignores_unrelated_state(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tools") as directory:
            root = Path(directory)
            tf = root / "terraform-selectel"
            tf.mkdir()
            state = {
                "resources": [
                    {"mode": "data"},
                    {
                        "mode": "managed",
                        "type": "openstack_compute_instance_v2",
                        "instances": [{"index_key": "app", "attributes": {"id": "owned", "name": "otus-dz07-app"}}],
                    },
                ]
            }
            (tf / "terraform.tfstate").write_text(json.dumps(state))
            cloud = Mock()
            cloud.get.return_value = {"server": {"name": "foreign", "status": "ERROR"}}
            with self.assertRaisesRegex(ValueError, "Foreign server UUID"):
                pools.scheduler_unavailable(TerraformError("unknown failure"), root, lambda: cloud, "otus-dz07")
            cloud.get.return_value = {
                "server": {"name": "otus-dz07-app", "status": "ERROR", "fault": {"message": "No valid host"}}
            }
            self.assertTrue(
                pools.scheduler_unavailable(TerraformError("unknown failure"), root, lambda: cloud, "otus-dz07")
            )

    def test_quota_resume_credits_ports_and_unlimited_quotas_and_checks_reserved_group(self) -> None:
        self.assertEqual(quota.free({"limit": -1}), 10**15)
        for call in (lambda: quota.free(None), lambda: quota.with_credit(None, 1)):
            with self.assertRaises(pools.CandidateUnavailable):
                call()
        with tempfile.TemporaryDirectory(dir=ROOT / ".tools") as directory:
            b, clouds = backend_fixtures.StagedBackendTests().backend(Path(directory))
            b.journal["stages"]["es1"] = {
                "status": "pending",
                "candidate": backend_fixtures.StagedBackendTests().choice("ru-9", "es"),
            }
            entries = [
                {"type": "openstack_networking_port_v2", "attrs": {}},
                {"type": "openstack_networking_floatingip_v2", "attrs": {}},
            ]
            b.store.entries.return_value = entries
            self.assertEqual(quota.pending_credits(b, "ru-9"), {"port": 1, "floatingip": 1})
            b.store.entries.return_value = [
                {"type": "openstack_networking_network_v2", "attrs": {}},
                {"type": "openstack_networking_secgroup_v2", "attrs": {}},
            ]
            self.assertEqual(quota.network_port_demand(b, "ru-9"), 0)
            quota.require_network_capacity(b, "ru-9")
            clouds["ru-9"].compute["server_group_members"] = None
            with self.assertRaises(pools.CandidateUnavailable):
                quota.require_network_capacity(b, "ru-9")
            b.journal["require_distinct_hosts"] = False
            quota.require_network_capacity(b, "ru-9")
