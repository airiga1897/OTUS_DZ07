"""Exercise bounded delivery failures and retained workload-only contracts offline."""

import hashlib
import json
import runpy
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import ansible_operation
import cache_packages
import observability
import placement_diagnostics
import pool_selection
import run_settings
import terraform_plugins
import test_terraform_plugins as provider_fixtures
from cloud_contract import InfrastructureResult, TerraformError
from inventory_types import Inventory


class ReleaseReadinessGaps(unittest.TestCase):
    def test_provider_download_exhaustion_is_bounded_and_removes_work_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            api = provider_fixtures.ProviderMirrorTests().fixture(root)
            result = subprocess.CompletedProcess([], 1, b"", b"connection timed out")
            with (
                patch("subprocess.run", return_value=result) as run,
                self.assertRaises(TerraformError),
            ):
                terraform_plugins.prepare(api)
            self.assertEqual(run.call_count, 4)  # One offline check, three downloads.
            self.assertFalse(list((root / ".tools").glob("provider-check-*")))

    def test_corrupt_cached_package_is_replaced_using_verified_explicit_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "config/services.json").write_text('{"elastic_version":"8.19.0"}')
            source, cache = root / "source", root / "cache"
            source.mkdir()
            cache.mkdir()
            hashes = {}
            for product in ("filebeat", "elasticsearch", "kibana"):
                name = product + "-8.19.0-amd64.deb"
                body = product.encode()
                hashes[name] = hashlib.sha512(body).hexdigest()
                (source / name).write_bytes(body)
                (source / (name + ".sha512")).write_text(hashes[name])
                (cache / name).write_bytes(b"corrupt cached content")
            (cache / "manifest.json").write_text(json.dumps(hashes))
            with patch.multiple(cache_packages, ROOT=root, CACHE=cache), patch("prepare_ansible.prepare"):
                cache_packages.main(source)
            for name, digest in hashes.items():
                self.assertEqual(cache_packages.sha512(cache / name), digest)
            self.assertEqual(json.loads((cache / "manifest.json").read_text()), hashes)

    def test_exhausted_candidates_without_callback_cannot_allocate(self) -> None:
        allocate = Mock()
        with self.assertRaisesRegex(RuntimeError, "No configured pool"):
            pool_selection.run_candidates([], Mock(), Mock(), allocate)
        allocate.assert_not_called()

    def test_legacy_explicit_placement_contract_remains_readable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            value = {"pools": ["ru-2"], "profiles": {"app": {}}, "boot_disk_gb": 20}
            (root / "config/placement.json").write_text(json.dumps(value))
            self.assertEqual(run_settings.placement(root), value)

    def test_inventory_without_required_elasticsearch_masters_is_rejected(self) -> None:
        result = InfrastructureResult.from_dict(
            {
                "provider": "selectel",
                "scope": {"project_id": "synthetic"},
                "namespace": "otus-dz07",
                "placement": {"region": "ru-9", "zone": "ru-9a"},
                "nodes": {
                    "app": {
                        "id": "app-id",
                        "roles": ["app"],
                        "private_ip": "10.0.0.1",
                        "public_ip": "192.0.2.10",
                        "volume_id": "volume-id",
                        "port_id": "port-id",
                    }
                },
                "subnet_cidr": "10.0.0.0/24",
            }
        )
        inventory: Inventory = {"all": {"children": {}, "vars": {}}}
        with self.assertRaisesRegex(ValueError, "Elasticsearch masters"):
            observability.inventory(result, {}, inventory)

    def test_importing_manage_does_not_start_django(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with patch("django.core.management.execute_from_command_line") as execute:
            runpy.run_path(str(root / "app/manage.py"), run_name="imported_manage")
        execute.assert_not_called()

    def test_foreign_configuration_namespace_cannot_bind_an_operation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "staged-deployment.json").write_text(json.dumps({"namespace": "foreign"}))
            api = SimpleNamespace(LOCAL=root, PREFIX="otus-dz07")
            with self.assertRaisesRegex(ValueError, "namespace mismatch"):
                ansible_operation.binding(api, {"server_ids": {"app": "own-app"}})

    def test_empty_terraform_state_preserves_error_without_cloud_queries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "terraform-selectel").mkdir()
            (root / "terraform-selectel/terraform.tfstate").write_text('{"resources":[]}')
            api = SimpleNamespace(
                ROOT=root,
                LOCAL=root / ".local",
                cloud=Mock(),
                save=Mock(),
                placement=lambda: {"region": "ru-2", "availability_zone": "ru-2a"},
            )
            with patch.object(placement_diagnostics, "terraform_root", return_value=root / "terraform-selectel"):
                placement_diagnostics.capture(api, RuntimeError("deployment failed"))
            api.cloud.assert_not_called()
            report = api.save.call_args.args[1]
            self.assertEqual(report["nodes"], {})
            self.assertEqual(report["error_type"], "RuntimeError")
