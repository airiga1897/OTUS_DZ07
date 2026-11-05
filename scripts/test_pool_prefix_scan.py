"""Catalog reads follow placement demand, not an eager all-pool pass."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import selectel_staged as staged
import test_staged_backend as fixtures
from cloud_contract import Capability, CapabilityStatus
from placement_config import PlacementPolicy
from pool_selection import CandidateUnavailable
from selectel_placement import PlacementScan


def mocked(value: object) -> Mock:
    if not isinstance(value, Mock):
        raise AssertionError("Expected test double")
    return value


ROOT = Path(__file__).resolve().parents[1]


class PoolPrefixScanTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(dir=ROOT / ".tools")
        self.addCleanup(directory.cleanup)
        self.helper = fixtures.StagedBackendTests()
        self.backend, self.clouds = self.helper.backend(Path(directory.name))

    def test_shortage_in_only_checked_pool_defers_router_api_and_allows_later_pool_scan(self) -> None:
        b = self.backend
        b._auto_discovery = True
        scan = PlacementScan(b, Mock(), Mock())
        scan.eligible = ["ru-9"]
        scan.choices = b.choices
        with patch.object(b, "check_complete_capacity", side_effect=CandidateUnavailable("quota")):
            self.assertFalse(scan.combination())
        self.assertEqual(mocked(b.global_router).mock_calls, [])

    def test_router_quotas_cover_assignment_not_unused_catalog_alternatives(self) -> None:
        b = self.backend
        b._auto_discovery = False
        scan = PlacementScan(b, Mock(), Mock())
        scan.eligible = ["ru-9", "ru-3", "ru-8"]
        scan.choices = b.choices
        with patch.object(b, "check_complete_capacity", return_value={"ru-9", "ru-3"}):
            self.assertTrue(scan.combination())
        self.assertEqual(mocked(b.global_router).preflight.call_args.args[0], ["ru-9", "ru-3"])

    def test_complete_preferred_pool_defers_all_reads_of_later_pool(self) -> None:
        b = self.backend
        self.clouds["ru-9"] = fixtures.QuotaCloud("ru-9", instances=4)
        calls: list[tuple[str, str]] = []

        def candidates(config: dict[str, object], credentials: object, **kwargs: object) -> list[PlacementPolicy]:
            selected = config["pools"]
            profiles = config["profiles"]
            if not isinstance(selected, list) or not selected or not isinstance(selected[0], str):
                raise AssertionError("expected candidate pools")
            if not isinstance(profiles, dict) or not profiles:
                raise AssertionError("expected profiles")
            role = next(iter(profiles))
            if not isinstance(role, str):
                raise AssertionError("expected role name")
            calls.append((selected[0], role))
            return [self.helper.choice(selected[0], role)]

        with (
            patch.object(
                staged,
                "discover_capabilities",
                return_value={"ownership_tags": Capability(CapabilityStatus.SUPPORTED, "yes")},
            ),
            patch("pool_selection.candidates", side_effect=candidates),
            patch.object(staged, "reject_cloud_resources"),
            patch("selectel_staged_audit.audit_namespace"),
        ):
            b.preflight()
        self.assertEqual(calls, [("ru-9", "app"), ("ru-9", "es")])
        region = mocked(b.region)
        self.assertTrue(all(c.args[0] == "ru-9" for c in region.call_args_list))
        report = mocked(b.api).save.call_args.args[1]
        if not isinstance(report, dict):
            raise AssertionError("expected placement report")
        self.assertEqual(report["deferred_pools"], ["ru-3"])
        self.assertEqual(mocked(b.global_router).mock_calls, [])

    def test_candidates_are_lazy_and_deduplicated_when_extending_pool_prefix(self) -> None:
        b = self.backend
        first, second = self.helper.choice("ru-9", "app"), self.helper.choice("ru-3", "app")
        b.choices = {"app": [first]}
        scan = PlacementScan(b, Mock(), Mock())

        extend_mock = Mock()

        def extend() -> bool:
            b.choices["app"] = [first, second]
            return extend_mock.call_count == 1

        extend_mock.side_effect = extend
        with patch.object(scan, "extend", extend_mock):
            choices = scan.candidates("app")
            self.assertEqual(next(choices), first)
            extend_mock.assert_not_called()
            self.assertEqual(next(choices), second)
            self.assertEqual(list(choices), [])
            self.assertEqual(extend_mock.call_count, 2)

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
        with patch.object(b, "check_complete_capacity", Mock()) as check_complete_capacity:
            self.assertFalse(scan.combination())
            check_complete_capacity.assert_not_called()
        mocked(b.global_router).preflight.assert_not_called()
