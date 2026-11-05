"""Additional supported contracts, with synthetic catalogs and disposable state."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import observability
import selectel_adapter as adapter
import selectel_api
import selectel_staged_audit as audit
import selectel_staged_capacity as capacity
import test_ansible_backend as ansible_fixtures
import test_network_policy_acceptance as network_fixtures
import test_staged_backend as placement_fixtures
import topology
from inventory_types import Inventory
from remote import fetch_artifacts
from selectel_staged import Backend

ROOT = Path(__file__).resolve().parents[1]


class RemainingProviderContracts(unittest.TestCase):
    def root(self) -> Path:
        folder = tempfile.TemporaryDirectory(dir=ROOT / ".tools")
        self.addCleanup(folder.cleanup)
        return Path(folder.name)

    def test_nova_skips_an_empty_compute_service_before_a_valid_catalog_entry(self) -> None:
        catalog = [
            {"type": "compute", "endpoints": []},
            {
                "type": "compute",
                "endpoints": [{"interface": "public", "region": "ru-9", "url": "https://example.invalid/compute"}],
            },
        ]
        with patch.object(selectel_api, "authenticate", return_value=("synthetic", catalog)):
            client = selectel_api.NovaClient({"region": "ru-9"})
        self.assertEqual(client.endpoint, "https://example.invalid/compute")

    def test_preflight_existing_resources_and_optional_aggregate_volume_quotas(self) -> None:
        root = self.root()
        (root / "config").mkdir()
        (root / "config/cloud.json").write_text('{"required_capabilities":[]}')
        quota = {"limit": 100000, "in_use": 0, "reserved": 0}
        placement = {
            "region": "ru-9",
            "availability_zone": "ru-9a",
            "volume_type": "universal.ru-9a",
            "boot_disk_gb": 20,
            "profiles": {"app": {"vcpus": 4, "ram_mb": 8192}},
            "admin_cidrs": [],
        }

        def get(service: str, path: str) -> dict[str, object]:
            if "subnets" in path:
                return {"subnets": []}
            if service == "compute":
                return {"quota_set": {"az_quotas": {"ru-9a": {k: dict(quota) for k in ("cores", "ram", "instances")}}}}
            if service == "volumev3":
                return {"quota_set": {"gigabytes_universal.ru-9a": dict(quota)}}
            return {
                "quota": {
                    k: dict(quota)
                    for k in (
                        "floatingip",
                        "network",
                        "subnet",
                        "router",
                        "security_group",
                        "security_group_rule",
                        "port",
                    )
                }
            }

        cloud = SimpleNamespace(config={"project_id": "synthetic"}, get=Mock(side_effect=get))
        api = SimpleNamespace(
            ROOT=root,
            STACK={"subnet_cidr": "10.77.0.0/24"},
            PREFIX="otus-dz07",
            cloud=Mock(return_value=cloud),
            placement=lambda: placement,
            desired_topology=lambda: {"nodes": {"app": {"role": "app"}}},
            managed_ids=lambda: {"own": "id"},
            save=Mock(),
        )
        with patch.object(adapter, "discover_capabilities", return_value={}):
            adapter.preflight(api)
        api.save.assert_called_once()

    def test_workload_only_placement_needs_no_cluster_group(self) -> None:
        server = {"name": "otus-dz07-app", "status": "ACTIVE", "OS-EXT-AZ:availability_zone": "ru-9a"}
        cloud = Mock()
        cloud.get.return_value = {"server": server}
        api = SimpleNamespace(
            ROOT=self.root(),
            PREFIX="otus-dz07",
            cloud=Mock(return_value=cloud),
            save=Mock(),
            auth_config=lambda: {"region": "ru-9"},
            placement=lambda: {"availability_zone": "ru-9a"},
        )
        adapter.check_placement(
            api, {"server_ids": {"app": "app-id"}, "private_ips": {"app": "10.77.0.10"}, "node_roles": {"app": "app"}}
        )
        cloud.get.assert_called_once_with("compute", "/servers/app-id")

    def test_post_create_network_audit_also_accepts_owned_active_non_network_stages(self) -> None:
        backend_like, data, pages = network_fixtures.fixture()
        backend = Backend.__new__(Backend)
        backend.__dict__.update(backend_like.__dict__)
        backend.journal["stages"]["app"] = {"status": "active"}
        with patch.object(audit, "assert_stage_owned") as owned:
            result = audit.verify_networks(backend)
        self.assertTrue(result["private_ports_filtered"])
        self.assertIn(unittest.mock.call(backend, "app", active=True), owned.call_args_list)

    def test_quota_metadata_is_not_interpreted_as_a_resource_limit(self) -> None:
        b, clouds = placement_fixtures.StagedBackendTests().backend(self.root())
        for cloud in clouds.values():
            cloud.volumes["metadata"] = "synthetic catalog annotation"
            cloud.volumes["annotation"] = {"note": "synthetic catalog annotation"}
        capacity.require_capacity(b)

    def test_non_package_host_downloads_source_without_fetching_package_archive(self) -> None:
        root = self.root()
        manifest = root / "artifact-delivery.json"
        manifest.write_text('{"source":{"url":"https://example.invalid/source"},"packages":{}}')
        (root / "artifact-node.json").write_text('{"name":"es2"}')
        with (
            patch.object(fetch_artifacts, "BASE", root),
            patch.object(fetch_artifacts, "download") as download,
            patch.object(fetch_artifacts, "extract"),
        ):
            fetch_artifacts.main()
        download.assert_called_once()
        self.assertFalse((root / "packages").exists())
        self.assertFalse(manifest.exists())

    def test_observability_does_not_assign_kibana_to_app_when_configuration_says_otherwise(self) -> None:
        result, passwords, _ = ansible_fixtures.AnsibleBackendTests().inventory()
        settings = topology.services(ROOT)
        settings["kibana_host"] = "es1"
        value: Inventory = {"all": {"vars": {}, "children": {}}}
        with patch.object(topology, "services", return_value=settings):
            actual = observability.inventory(result, passwords, value)
        self.assertNotIn("kibana", actual["all"]["children"])
        self.assertIn("elasticsearch", actual["all"]["children"])
