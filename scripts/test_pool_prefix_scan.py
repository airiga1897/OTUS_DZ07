"""Catalog reads follow placement demand, not an eager all-pool pass."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import selectel_staged as staged
import test_staged_backend as fixtures
from cloud_contract import Capability, CapabilityStatus
from selectel_placement import PlacementScan

ROOT = Path(__file__).resolve().parents[1]


class PoolPrefixScanTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(dir=ROOT / ".tools")
        self.addCleanup(directory.cleanup)
        self.helper = fixtures.StagedBackendTests()
        self.backend, self.clouds = self.helper.backend(Path(directory.name))

    def test_complete_preferred_pool_defers_all_reads_of_later_pool(self) -> None:
        b = self.backend
        self.clouds["ru-9"] = fixtures.QuotaCloud("ru-9", instances=4)
        calls = []

        def candidates(config, credentials, **kwargs):
            pool, role = config["pools"][0], next(iter(config["profiles"]))
            calls.append((pool, role))
            return [self.helper.choice(pool, role)]

        with (
            patch.object(
                staged,
                "discover_capabilities",
                return_value={"ownership_tags": Capability(CapabilityStatus.SUPPORTED, "yes")},
            ),
            patch.object(staged.pools, "candidates", side_effect=candidates),
            patch.object(staged, "reject_cloud_resources"),
            patch("selectel_staged_audit.audit_namespace"),
        ):
            b.preflight()
        self.assertEqual(calls, [("ru-9", "app"), ("ru-9", "es")])
        self.assertTrue(all(c.args[0] == "ru-9" for c in b.region.call_args_list))
        report = b.api.save.call_args.args[1]
        self.assertEqual(report["deferred_pools"], ["ru-3"])

    def test_candidates_are_lazy_and_deduplicated_when_extending_pool_prefix(self) -> None:
        b = self.backend
        first, second = self.helper.choice("ru-9", "app"), self.helper.choice("ru-3", "app")
        b.choices = {"app": [first]}
        scan = PlacementScan(b, Mock(), Mock())

        def extend():
            b.choices["app"] = [first, second]
            return scan.extend.call_count == 1

        scan.extend = Mock(side_effect=extend)
        choices = scan.candidates("app")
        self.assertEqual(next(choices), first)
        scan.extend.assert_not_called()
        self.assertEqual(next(choices), second)
        self.assertEqual(list(choices), [])
        self.assertEqual(scan.extend.call_count, 2)

    def test_pool_checks_skip_catalogs_for_completed_roles(self) -> None:
        b = self.backend
        b.journal["stages"] = {"app": {"status": "active"}}
        scan = PlacementScan(
            b, Mock(return_value={"ownership_tags": Capability(CapabilityStatus.SUPPORTED, "yes")}), Mock()
        )
        with (
            patch("pool_selection.candidates", return_value=[]) as candidates,
            patch("selectel_staged_audit.audit_namespace"),
        ):
            scan.pool("ru-9")
        self.assertEqual(candidates.call_count, 1)
        self.assertEqual(set(candidates.call_args.args[0]["profiles"]), {"es"})

    def test_saved_unavailable_pool_and_empty_discovery_are_refused(self) -> None:
        b = self.backend
        b.policy["pools"] = []
        with self.assertRaisesRegex(ValueError, "No compatible"):
            b.resolve_pools()
        b.policy["pools"] = ["ru-3"]
        b.journal["stages"]["network-ru-9"] = {"candidate": {"region": "ru-9"}, "status": "active"}
        with self.assertRaisesRegex(ValueError, "Saved network pool"):
            b.resolve_pools()

    def test_complete_new_candidates_do_not_bypass_unchecked_retained_pool(self) -> None:
        b = self.backend
        b.journal["stages"]["network-ru-3"] = {"candidate": {"region": "ru-3"}, "status": "active"}
        scan = PlacementScan(b, Mock(), Mock())
        scan.eligible = ["ru-9"]
        scan.choices = b.choices
        b.check_complete_capacity = Mock()
        self.assertFalse(scan.combination())
        b.check_complete_capacity.assert_not_called()
        b.global_router.preflight.assert_not_called()
