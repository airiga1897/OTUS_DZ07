"""Catalog rejection diagnostics must not turn unknown compatibility into success."""

import copy
import unittest
from unittest.mock import patch

import pool_selection as pools


class CatalogRejectionPaths(unittest.TestCase):
    def fixture(self):
        config = {
            "pools": ["ru-9"],
            "segment": None,
            "disk_type_patterns": ["universal.*", "basicssd.*"],
            "boot_disk_gb": 20,
            "image_name": "Ubuntu",
            "image_checksums": ["a" * 32],
            "flavor_name_template": "SL1.{vcpus}-{ram_mb}",
            "profiles": {"app": {"vcpus": 2, "ram_mb": 4096}},
        }
        catalog = [
            {
                "type": kind,
                "endpoints": [{"interface": "public", "region": "ru-9", "url": "https://catalog.invalid/" + kind}],
            }
            for kind in pools.SERVICES
        ]
        values = {
            "images": {
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
            "flavors": {"flavors": [{"id": "flavor", "name": "SL1.2-4096", "vcpus": 2, "ram": 4096, "disk": 0}]},
            "types": {
                "volume_types": [{"name": "universal.ru-9a", "extra_specs": {"RESKEY:availability_zones": "ru-9a"}}]
            },
            "compute": {"availabilityZoneInfo": [{"zoneName": "ru-9a", "zoneState": {"available": True}}]},
            "volumev3": {"availabilityZoneInfo": [{"zoneName": "ru-9a", "zoneState": {"available": True}}]},
            "network": {
                "availability_zones": [
                    {"name": "ru-9a", "state": "available", "resource": r} for r in ("network", "router")
                ]
            },
        }
        return config, catalog, values

    def choices(self, config, catalog, values):
        def request(url, **kwargs):
            if "images?" in url:
                key = "images"
            elif url.endswith("/flavors/detail"):
                key = "flavors"
            elif url.endswith("/types"):
                key = "types"
            elif "/network/" in url:
                key = "network"
            else:
                key = "compute" if "/compute/" in url else "volumev3"
            return copy.deepcopy(values[key]), {}

        with (
            patch.object(pools, "authenticate", return_value=("synthetic", catalog)),
            patch.object(pools, "request_json", side_effect=request),
            self.assertLogs(pools.LOGGER, "INFO") as output,
        ):
            result = list(pools.candidates(config, {}))
        return result, "\n".join(output.output)

    def test_no_matching_image_reports_reason_and_yields_nothing(self) -> None:
        config, catalog, values = self.fixture()
        values["images"]["images"] = []
        result, message = self.choices(config, catalog, values)
        self.assertEqual(result, [])
        self.assertIn("name (absent)", message)

    def test_missing_disk_or_volume_segment_does_not_yield_candidate(self) -> None:
        for missing in ("types", "volumev3"):
            config, catalog, values = self.fixture()
            values[missing][next(iter(values[missing]))] = []
            result, message = self.choices(config, catalog, values)
            self.assertEqual(result, [])
            self.assertIn("allowed masks" if missing == "types" else "volume scheduling segment", message)

    def test_unconfirmed_network_mapping_is_diagnosed_per_segment(self) -> None:
        config, catalog, values = self.fixture()
        values["network"]["availability_zones"] = []
        result, message = self.choices(config, catalog, values)
        self.assertEqual(result, [])
        self.assertIn("Network placement unconfirmed for ru-9a", message)

    def test_explicit_but_unavailable_network_domain_is_not_accepted(self) -> None:
        config, catalog, values = self.fixture()
        config["network_placement"] = {
            "ru-9a": {
                "network": "ru-9b",
                "router": "ru-9b",
                "evidence_url": "https://docs.selectel.ru/infrastructure/locations/",
            }
        }
        result, message = self.choices(config, catalog, values)
        self.assertEqual(result, [])
        self.assertIn("requested=", message)
        self.assertIn("API available=", message)

    def test_explicit_segment_filter_never_falls_back_to_another_segment(self) -> None:
        config, catalog, values = self.fixture()
        config["segment"] = "ru-9b"
        result, _ = self.choices(config, catalog, values)
        self.assertEqual(result, [])

    def test_overlapping_allowed_masks_do_not_duplicate_disk_candidates(self) -> None:
        types = [{"name": "universal.ru-9a", "extra_specs": {"RESKEY:availability_zones": "ru-9a"}}]
        self.assertEqual(pools.disks_for_zone(types, ["universal.*", "*.ru-9a"], "ru-9a", None), ["universal.ru-9a"])
