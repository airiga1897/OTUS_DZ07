"""Reviewed pool priorities and segment exclusions do not weaken compatibility."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pool_selection as pools
import run_settings
from placement_config import PlacementPolicy
from selectel_api import CatalogEndpoint, CatalogService

ROOT = Path(__file__).resolve().parents[1]


class PreferredPlacementPolicy(unittest.TestCase):
    def fixture(self, root: Path) -> None:
        (root / ".env.example").write_bytes((ROOT / ".env.example").read_bytes())
        (root / "config").mkdir()
        (root / "config/placement.json").write_bytes((ROOT / "config/placement.json").read_bytes())

    def test_verified_pool_first_and_catalog_compatible_alternatives_retained(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            with patch.dict("os.environ", {}, clear=True):
                policy = run_settings.placement(root)
        regions = (
            "ru-1",
            "ru-2",
            "ru-3",
            "ru-4",
            "ru-5",
            "ru-6",
            "ru-7",
            "ru-8",
            "ru-9",
            "kz-1",
            "ke-1",
            "gis-1",
            "uz-1",
        )
        catalog: list[CatalogService] = []
        for kind in pools.SERVICES:
            endpoints: list[CatalogEndpoint] = []
            for region in regions:
                endpoints.append({"interface": "public", "region": region, "url": "https://example.invalid/" + kind})
            catalog.append({"type": kind, "endpoints": endpoints})
        self.assertEqual(pools.ordered_pools(policy, catalog), ["ru-2", "ru-9", "ru-3", "ru-8", "ru-1"])
        self.assertEqual(policy["disk_type_patterns"], ["universal.*", "basicssd.*"])
        self.assertTrue(policy["require_distinct_hosts"])

    def test_segment_exclusion_consumes_a_generator_once_and_preserves_other_segments(self) -> None:
        source = (zone for zone in ("ru-1b", "ru-1c", "ru-1a", "ru-2b", "ru-2a"))
        selected = pools.ordered_zones(
            PlacementPolicy(preferred_segments=["ru-2a"], excluded_segments=["ru-1b"]),
            source,
        )
        self.assertEqual(selected, ["ru-2a", "ru-1a", "ru-1c", "ru-2b"])
        # Quotas are re-read, not converted into permanent segment exclusions.
        self.assertIn("ru-1a", selected)
        self.assertIn("ru-2b", selected)

    def test_segment_list_validation_and_explicit_empty_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            self.assertEqual(run_settings.load(root, environ={})["excluded_segments"], ["ru-1b"])
            self.assertEqual(
                run_settings.load(root, environ={"SELECTEL_EXCLUDED_SEGMENTS": ""})["excluded_segments"], []
            )
            for value in ("ru-1", "kz-1a", "ru-1b,ru-1b", "not-a-segment"):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    run_settings.load(root, environ={"SELECTEL_EXCLUDED_SEGMENTS": value})
        with self.assertRaises(ValueError):
            pools.ordered_zones(PlacementPolicy(excluded_segments=["invalid"]), [])

    def test_saved_preferences_and_ids_remain_authoritative(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            (root / ".local").mkdir()
            saved = {
                "region": "ru-3",
                "availability_zone": "ru-3b",
                "image_id": "existing",
                "flavor_ids": {"app": "retained"},
            }
            (root / ".local/placement.json").write_text(json.dumps(saved))
            with patch.dict("os.environ", {}, clear=True):
                policy = pools.policy(root, "placement.json", "context.json")
            self.assertEqual(policy["region"], saved["region"])
            self.assertEqual(policy["availability_zone"], saved["availability_zone"])
            self.assertEqual(policy["image_id"], saved["image_id"])
            self.assertEqual(policy["flavor_ids"], saved["flavor_ids"])
            snapshot = run_settings.snapshot(policy)
            self.assertEqual(snapshot["excluded_segments"], ["ru-1b"])
            self.assertEqual(snapshot["preferred_pools"], ["ru-2", "ru-9", "ru-3"])
