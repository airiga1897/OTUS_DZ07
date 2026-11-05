"""Explicit Glance MD5 allowlist and independent image-property requirements."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pool_selection as pools
import run_settings
import test_catalog_rejection_paths as catalogs
from placement_config import PlacementPolicy

ROOT = Path(__file__).resolve().parents[1]


def call_unchecked(function: object, *args: object) -> None:
    if not callable(function):
        raise AssertionError("expected callable")
    function(*args)


class GlanceImagePolicy(unittest.TestCase):
    def policy(self) -> PlacementPolicy:
        value = run_settings.placement(ROOT)
        value["boot_disk_gb"] = 20
        value["profiles"] = {"app": {"ram_mb": 8192}}
        return value

    def image(self) -> dict[str, object]:
        policy = self.policy()
        return {
            "id": "synthetic-image",
            "name": policy["image_name"],
            "checksum": policy["image_checksums"][0],
            "status": "active",
            "visibility": "public",
            "os_distro": "ubuntu",
            "min_disk": 5,
            "min_ram": 512,
            "architecture": "x86_64",
        }

    def test_each_explicit_build_is_accepted_without_merging_other_properties(self) -> None:
        config = self.policy()
        self.assertEqual(len(config["image_checksums"]), 2)
        for digest in config["image_checksums"]:
            image = dict(self.image(), checksum=digest)
            with self.subTest(md5=digest):
                self.assertEqual(pools.compatible_images([image], config), [image])

    def test_unknown_missing_or_uppercase_digest_is_rejected(self) -> None:
        config = self.policy()
        for digest in ("0" * 32, None, config["image_checksums"][0].upper(), "a" * 64):
            with self.subTest(digest=digest):
                self.assertEqual(pools.compatible_images([dict(self.image(), checksum=digest)], config), [])

    def test_known_digest_cannot_override_wrong_name_or_properties(self) -> None:
        config = self.policy()
        wrong_values: dict[str, list[object]] = {
            "name": ["Ubuntu", "Ubuntu 24.04 LTS 64-bit extra"],
            "visibility": ["private", None],
            "status": ["queued", "deactivated", None],
            "os_distro": ["debian", None],
            "architecture": ["aarch64"],
            "min_disk": [21],
            "min_ram": [16384],
        }
        for digest in config["image_checksums"]:
            for field, values in wrong_values.items():
                for value in values:
                    with self.subTest(field=field, value=value, digest=digest):
                        image = dict(self.image(), checksum=digest, **{field: value})
                        self.assertEqual(pools.compatible_images([image], config), [])
                        self.assertIn(field, pools.image_rejections(image, config))

    def test_allowlist_configuration_fails_closed(self) -> None:
        for value in (None, [], "a" * 32, [None], ["invalid"], ["a" * 32, "a" * 32]):
            invalid = dict(self.policy(), image_checksums=value)
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "Glance MD5"):
                call_unchecked(pools.compatible_images, [], invalid)
        kept = self.policy()
        del kept["image_checksums"]
        with self.assertRaises(ValueError):
            pools.compatible_images([self.image()], kept)

    def test_excluded_pools_never_reach_service_or_image_requests(self) -> None:
        config, catalog, values = catalogs.CatalogRejectionPaths().fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env.example").write_bytes((ROOT / ".env.example").read_bytes())
            defaults = run_settings.load(root, environ={})
        config["pools"] = ["auto"]
        config["excluded_pools"] = defaults["excluded_pools"]
        config["preferred_pools"] = defaults["preferred_pools"]
        expanded: list[dict[str, object]] = []
        for service in catalog:
            if type(service) is not dict:
                raise AssertionError("expected catalog service")
            mutable = cast(dict[str, object], copy.deepcopy(service))
            endpoints = mutable["endpoints"]
            if type(endpoints) is not list:
                raise AssertionError("expected service endpoints")
            rows = cast(list[object], endpoints)
            rows.extend(
                {"interface": "public", "region": pool, "url": "https://excluded.invalid/" + pool}
                for pool in ("ru-4", "ru-5", "ru-7")
            )
            expanded.append(mutable)
        catalog = tuple(expanded)
        fixture = catalogs.CatalogRejectionPaths()
        choices, _ = fixture.choices(config, catalog, values)
        self.assertEqual({c["region"] for c in choices}, {"ru-9"})
        self.assertTrue(
            {"ru-4", "ru-5", "ru-7", "kz-1", "ke-1", "gis-1", "gis-2", "uz-1", "uz-2"}
            <= set(defaults["excluded_pools"])
        )

    def test_saved_pool_and_image_identity_are_not_reselected_from_new_preferences(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / ".local").mkdir()
            (root / "config/placement.json").write_bytes((ROOT / "config/placement.json").read_bytes())
            (root / ".env.example").write_bytes((ROOT / ".env.example").read_bytes())
            saved = {"region": "ru-3", "availability_zone": "ru-3b", "image_id": "existing-image-id"}
            (root / ".local/placement.json").write_text(json.dumps(saved))
            with patch.dict("os.environ", {}, clear=True):
                selected = pools.policy(root, "placement.json", "context.json")
            self.assertEqual(selected["region"], saved["region"])
            self.assertEqual(selected["availability_zone"], saved["availability_zone"])
            self.assertEqual(selected["image_id"], saved["image_id"])
