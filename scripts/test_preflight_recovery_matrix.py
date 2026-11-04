"""Placement discovery must preserve retained networks and reject unknown capabilities."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import selectel_staged as staged
import test_staged_backend as fixtures
from cloud_contract import Capability, CapabilityStatus

ROOT = Path(__file__).resolve().parents[1]


class PreflightRecoveryMatrix(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(dir=ROOT / ".tools")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.helper = fixtures.StagedBackendTests()
        self.backend, self.clouds = self.helper.backend(self.root)

    def test_unsupported_pools_are_skipped_but_retained_pool_cannot_disappear(self) -> None:
        b = self.backend
        b.policy["pools"] = ["auto"]
        b.cloud = Mock(catalog=self.helper.catalog(["ru-9", "ru-3"]))
        unsupported = {"ownership_tags": Capability(CapabilityStatus.UNSUPPORTED, "API extension unavailable")}
        supported = {"ownership_tags": Capability(CapabilityStatus.SUPPORTED, "API extension present")}
        b.check_complete_capacity = Mock()
        with (
            patch.object(staged, "discover_capabilities", side_effect=[unsupported, supported]),
            patch.object(
                staged.pools,
                "candidates",
                side_effect=lambda config, credentials, **kwargs: [
                    self.helper.choice("ru-3", next(iter(config["profiles"])))
                ],
            ),
            patch.object(staged, "reject_cloud_resources"),
            patch("selectel_staged_audit.audit_namespace"),
            patch.object(
                b.global_router,
                "compatible_regions",
                create=True,
                side_effect=lambda pools, retained, accept: accept(pools),
            ),
        ):
            b.preflight()
        self.assertEqual(b.policy["pools"], ["ru-3"])
        self.assertEqual(set(b.journal["pool_subnets"]), {"ru-9", "ru-3"})
        b.policy["pools"] = ["auto"]
        with (
            patch.object(staged, "discover_capabilities", return_value=unsupported),
            self.assertRaisesRegex(ValueError, "No complete compatible"),
        ):
            b.preflight()
        b.journal["stages"]["network-ru-9"] = {"candidate": {"region": "ru-9"}, "status": "active"}
        b.cloud.catalog = self.helper.catalog(["ru-3"])
        b.policy["pools"] = ["auto"]
        with self.assertRaisesRegex(ValueError, "Saved network pool"):
            b.resolve_pools()
        b.cloud.catalog = self.helper.catalog(["ru-9", "ru-3"])
        b.journal["pool_subnets"]["ru-9"] = "10.77.0.0/24"
        b.policy["pools"] = ["auto"]
        with (
            patch.object(staged, "discover_capabilities", return_value=unsupported),
            self.assertRaisesRegex(ValueError, "capabilities changed"),
        ):
            b.preflight()

    def test_existing_network_subnet_conflict_is_fatal_and_empty_new_pools_are_rejected(self) -> None:
        b = self.backend
        for cloud in self.clouds.values():
            cloud.get = Mock(return_value={"subnets": [{"cidr": "10.0.0.0/8", "network_id": "foreign"}]})
        capabilities = {"ownership_tags": Capability(CapabilityStatus.SUPPORTED, "synthetic")}
        with (
            patch.object(staged, "discover_capabilities", return_value=capabilities),
            self.assertRaisesRegex(ValueError, "No complete compatible"),
        ):
            b.preflight()
        b.policy["pools"] = ["ru-9"]
        b.journal["stages"]["network-ru-9"] = {
            "candidate": {"region": "ru-9"},
            "status": "active",
            "output": {"network_id": "owned"},
        }
        with (
            patch.object(staged, "discover_capabilities", return_value=capabilities),
            self.assertRaisesRegex(ValueError, "existing network"),
        ):
            b.preflight()
        b.global_router.preflight.assert_not_called()

    def test_completed_stand_does_not_rediscover_flavors_or_request_new_capacity(self) -> None:
        b = self.backend
        b.journal["stages"] = {name: {"status": "active"} for name in b.desired["nodes"]}
        b.journal["stages"]["network-ru-9"] = {"candidate": {"region": "ru-9"}, "status": "active"}
        b.check_complete_capacity = Mock()
        b.path.parent.mkdir()
        b.path.write_text("{}")
        with (
            patch.object(
                staged,
                "discover_capabilities",
                return_value={"ownership_tags": Capability(CapabilityStatus.SUPPORTED, "synthetic")},
            ),
            patch.object(staged.pools, "candidates", side_effect=AssertionError("No allocation for an active stand")),
        ):
            b.preflight()
        self.assertEqual(b.choices, {"app": [], "es": []})
        b.region.assert_called_once_with("ru-9")
        b.global_router.preflight.assert_not_called()
        b.api.save.assert_called_once()

    def test_unconfirmed_flavor_and_quota_do_not_hide_valid_remaining_candidate(self) -> None:
        b = self.backend
        candidates = [self.helper.choice(pool, "app") for pool in ("ru-9", "ru-3")]
        b.desired["nodes"] = {"app": b.desired["nodes"]["app"]}
        b.choices = {}
        original = self.clouds["ru-9"].get
        self.clouds["ru-9"].get = lambda service, path: (
            {"flavor": {"availability_zones": None}} if path.startswith("/flavors/") else original(service, path)
        )
        b.check_complete_capacity = Mock()
        with (
            patch.object(
                staged,
                "discover_capabilities",
                return_value={"ownership_tags": Capability(CapabilityStatus.SUPPORTED, "synthetic")},
            ),
            patch.object(staged, "reject_cloud_resources"),
            patch("selectel_staged_audit.audit_namespace"),
            patch.object(staged.pools, "candidates", return_value=candidates),
        ):
            b.preflight()
        self.assertEqual([c["region"] for c in b.choices["app"]], ["ru-3"])
        self.assertEqual(b.policy["pools"], ["ru-3"])
        b.global_router.preflight.assert_called_once()

    def test_unknown_stage_module_rejected_before_writing_configuration(self) -> None:
        b = self.backend
        b.store.configure = Mock()
        with self.assertRaisesRegex(ValueError, "Unknown Selectel"):
            b.configure_stage("es1", "foreign-provider", {})
        b.store.configure.assert_not_called()
        for kind in ("network", "node", "global", "connection"):
            b.configure_stage("synthetic", kind, {"owner": "synthetic"})
            call = b.store.configure.call_args
            provider = "selectel" if kind in ("global", "connection") else "openstack"
            self.assertEqual(set(call.kwargs["providers"]), {provider})
            self.assertEqual(call.args[1], self.root / "terraform-selectel/staged" / kind)
