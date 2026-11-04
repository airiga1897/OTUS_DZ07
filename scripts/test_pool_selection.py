"""Pool discovery and audited fallback use synthetic catalog/state only."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from cloud_contract import TerraformError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import pool_selection as pools

ROOT = Path(__file__).resolve().parents[1]


class PoolSelectionTests(unittest.TestCase):
    def test_disk_priority_and_reserve_allow_ru3a_without_weakening_network_filter(self) -> None:
        config = {
            "pools": ["ru-3"],
            "segment": None,
            "disk_classes": ["basicssd", "universal"],
            "boot_disk_gb": 20,
            "image_name": "Ubuntu",
            "image_checksums": ["a" * 32],
            "flavor_name_template": "SL1.{vcpus}-{ram_mb}",
            "profiles": {"db": {"vcpus": 2, "ram_mb": 4096}},
        }
        catalog = [
            {
                "type": kind,
                "endpoints": [{"interface": "public", "region": "ru-3", "url": "https://ru-3.invalid/" + kind}],
            }
            for kind in pools.SERVICES
        ]
        for preferred in (False, True):

            def request(url, *, preferred=preferred, **kwargs):
                if "images?" in url:
                    return {
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
                    }, {}
                if "flavors/detail" in url:
                    return {"flavors": [{"id": "flavor", "name": "SL1.2-4096", "vcpus": 2, "ram": 4096, "disk": 0}]}, {}
                if url.endswith("/types"):
                    names = ["basicssd.ru-3b", "universal.ru-3a"] + (["basicssd.ru-3a"] if preferred else [])
                    return {
                        "volume_types": [
                            {"name": name, "extra_specs": {"RESKEY:availability_zones": name.split(".")[-1]}}
                            for name in names
                        ]
                    }, {}
                if url.endswith("/v2.0/availability_zones"):
                    return {
                        "availability_zones": [
                            {"name": "ru-3a", "state": "available", "resource": r} for r in ["network", "router"]
                        ]
                    }, {}
                return {
                    "availabilityZoneInfo": [
                        {"zoneName": z, "zoneState": {"available": True}} for z in ["ru-3a", "ru-3b"]
                    ]
                }, {}

            with (
                patch.object(pools, "authenticate", return_value=("token", catalog)),
                patch.object(pools, "request_json", side_effect=request),
            ):
                choices = list(pools.candidates(config, {}))
            self.assertEqual([c["availability_zone"] for c in choices], ["ru-3a"] * (2 if preferred else 1) + ["ru-3b"])
            self.assertEqual(choices[0]["volume_type"], ("basicssd" if preferred else "universal") + ".ru-3a")
            mapped = {
                **config,
                "network_placement": {
                    "ru-3b": {
                        "network": "ru-3a",
                        "router": "ru-3a",
                        "evidence_url": "https://docs.selectel.ru/infrastructure/locations/",
                    }
                },
            }
            with (
                patch.object(pools, "authenticate", return_value=("token", catalog)),
                patch.object(pools, "request_json", side_effect=request),
            ):
                mapped_choices = list(pools.candidates(mapped, {}))
            self.assertEqual(
                [c["availability_zone"] for c in mapped_choices], ["ru-3a"] * (2 if preferred else 1) + ["ru-3b"]
            )
            self.assertEqual(mapped_choices[-1]["volume_type"], "basicssd.ru-3b")
            self.assertEqual(mapped_choices[-1]["network_availability_zone"], "ru-3a")

    def test_preferred_segments_order_without_disabling_fallback(self) -> None:
        self.assertEqual(
            pools.ordered_zones({"preferred_segments": ["ru-9b"]}, ["ru-9a", "ru-9b", "ru-9c"]),
            ["ru-9b", "ru-9a", "ru-9c"],
        )
        self.assertEqual(pools.ordered_zones({"preferred_segments": ["ru-9a"]}, ["ru-3b", "ru-3a"]), ["ru-3a", "ru-3b"])
        with self.assertRaises(ValueError):
            pools.ordered_zones({"preferred_segments": ["invalid"]}, [])

    def test_excluded_pool_is_never_restored_by_explicit_or_auto_selection(self) -> None:
        catalog = [
            {
                "type": kind,
                "endpoints": [
                    {"interface": "public", "region": region, "url": "https://" + region + ".invalid/" + kind}
                    for region in ["ru-3", "ru-7", "ru-9"]
                ],
            }
            for kind in pools.SERVICES
        ]
        config = {"pools": ["ru-3", "ru-7", "ru-9", "auto"], "excluded_pools": ["ru-7"]}
        self.assertEqual(pools.ordered_pools(config, catalog), ["ru-3", "ru-9"])
        config["pools"] = ["auto"]
        self.assertEqual(pools.ordered_pools(config, catalog), ["ru-9", "ru-3"])
        config["excluded_pools"] = ["not-a-pool"]
        with self.assertRaises(ValueError):
            pools.ordered_pools(config, catalog)

    def test_image_name_does_not_replace_verified_content_checksum(self) -> None:
        verified = {
            "name": "Ubuntu",
            "visibility": "public",
            "status": "active",
            "os_distro": "ubuntu",
            "checksum": "a" * 32,
        }
        newer = dict(verified, checksum="b" * 32, created_at="2099-01-01")
        missing = {k: v for k, v in verified.items() if k != "checksum"}
        config = {
            "image_name": "Ubuntu",
            "image_checksums": ["a" * 32],
            "boot_disk_gb": 20,
            "profiles": {"app": {"ram_mb": 2048}},
        }
        self.assertEqual(pools.compatible_images([newer, missing, verified], config), [verified])
        config["image_checksums"] = "invalid"
        with self.assertRaises(ValueError):
            pools.compatible_images([verified], config)

    def test_image_catalog_reads_later_pages_and_rejects_foreign_next_link(self) -> None:
        first = {"images": [{"name": "other"}], "next": "/v2/images?marker=next"}
        second = {"images": [{"name": "Ubuntu"}]}
        with patch.object(pools, "request_json", side_effect=[(first, {}), (second, {})]) as request:
            self.assertEqual(
                [i["name"] for i in pools.image_catalog("https://images.invalid/v2", "token")], ["other", "Ubuntu"]
            )
            self.assertEqual(request.call_count, 2)
        with (
            patch.object(
                pools, "request_json", return_value=({"images": [], "next": "https://foreign.invalid/v2/images"}, {})
            ),
            self.assertRaises(ValueError),
        ):
            pools.image_catalog("https://images.invalid/v2", "token")

    def test_image_pagination_preserves_the_catalog_service_prefix(self) -> None:
        first = {"images": [], "next": "/v2/images?marker=next"}
        with patch.object(
            pools, "request_json", side_effect=[(first, {}), ({"images": [{"name": "Ubuntu"}]}, {})]
        ) as request:
            pools.image_catalog("https://images.invalid/image/v2", "token")
        self.assertEqual(request.call_args.args[0], "https://images.invalid/image/v2/images?marker=next")

    def test_catalog_discovers_only_complete_russian_pools_and_segments(self) -> None:
        config = {
            "pools": ["ru-7", "auto"],
            "segment": None,
            "disk_class": "basicssd",
            "boot_disk_gb": 20,
            "image_name": "Ubuntu",
            "image_checksums": ["a" * 32],
            "flavor_name_template": "SL1.{vcpus}-{ram_mb}",
            "profiles": {"db": {"vcpus": 2, "ram_mb": 4096}},
        }
        catalog = [
            {
                "type": kind,
                "endpoints": [
                    {"interface": "public", "region": region, "url": "https://" + region + ".invalid/" + kind}
                    for region in ["ru-7", "ru-9", "kz-1"]
                ],
            }
            for kind in pools.SERVICES
        ]

        def request(url, **kwargs):
            pool = "ru-7" if "ru-7." in url else "ru-9"
            if "images?" in url:
                return {
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
                }, {}
            if "flavors/detail" in url:
                return {"flavors": [{"id": "flavor", "name": "SL1.2-4096", "vcpus": 2, "ram": 4096, "disk": 0}]}, {}
            if url.endswith("/types"):
                return {
                    "volume_types": [
                        {"name": "basicssd." + pool + x, "extra_specs": {"RESKEY:availability_zones": pool + x}}
                        for x in ["a", "b"]
                    ]
                }, {}
            if url.endswith("/v2.0/availability_zones"):
                return {
                    "availability_zones": [
                        {"name": pool + "a", "state": "available", "resource": r} for r in ["network", "router"]
                    ]
                }, {}
            return {
                "availabilityZoneInfo": [
                    {"zoneName": pool + "a", "zoneState": {"available": True}},
                    {"zoneName": pool + "b", "zoneState": {"available": True}},
                ]
            }, {}

        with (
            patch.object(pools, "authenticate", return_value=("token", catalog)),
            patch.object(pools, "request_json", side_effect=request),
        ):
            choices = list(pools.candidates(config, {}))
        self.assertEqual([c["region"] for c in choices], ["ru-7", "ru-7", "ru-9", "ru-9"])
        self.assertEqual(choices[2]["availability_zone"], "ru-9a")
        self.assertEqual(choices[2]["flavor_ids"], {"db": "flavor"})

    def test_capacity_fallback_follows_successful_cleanup(self) -> None:
        events = []

        def apply():
            events.append("apply")
            if events.count("apply") == 1:
                events.append("state-and-api-empty")
                raise pools.CapacityRetry()
            return "active"

        result = pools.run_candidates(
            [1, 2], lambda c: events.append("select-" + str(c)), lambda: events.append("prepare"), apply
        )
        self.assertEqual(result, "active")
        self.assertLess(events.index("state-and-api-empty"), events.index("select-2"))

    def test_cleanup_failure_blocks_next_pool_and_preserves_inputs(self) -> None:
        choose = Mock()
        with self.assertRaisesRegex(RuntimeError, "API audit"):
            pools.run_candidates([1, 2], choose, Mock(), Mock(side_effect=RuntimeError("API audit failed")))
        choose.assert_called_once_with(1)

    def test_quota_failure_skips_without_apply(self) -> None:
        prepare = Mock(side_effect=[pools.CandidateUnavailable("quota"), None])
        apply = Mock(return_value="active")
        self.assertEqual(pools.run_candidates([1, 2], Mock(), prepare, apply), "active")
        apply.assert_called_once()

    def test_selection_preserves_keys_and_rejects_unconfirmed_attempt(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tools") as d:
            root = Path(d)
            local = root / ".local"
            local.mkdir()
            (root / "terraform-selectel").mkdir()
            (local / "key").write_text("synthetic")
            pools.select(root, {"region": "ru-7"}, "project", "context.json")
            pools.phase(root, "creating")
            with self.assertRaises(ValueError):
                pools.select(root, {"region": "ru-9"}, "project", "context.json")
            pools.phase(root, "destroyed")
            pools.select(root, {"region": "ru-9"}, "project", "context.json")
            self.assertEqual((local / "key").read_text(), "synthetic")
            self.assertEqual(pools.selected(root)["region"], "ru-9")

    def test_configuration_error_is_not_retried_as_capacity_failure(self) -> None:
        self.assertFalse(pools.scheduler_unavailable(RuntimeError("configuration"), ROOT, Mock(), "otus-dz07"))
        self.assertTrue(pools.scheduler_unavailable(TerraformError("NoValidHost"), ROOT, Mock(), "otus-dz07"))

    def test_shared_quota_usage_and_reservations_are_subtracted(self) -> None:
        pools.capacity({"limit": 10, "used": 6, "reserved": 1}, 3)
        with self.assertRaises(pools.CandidateUnavailable):
            pools.capacity({"limit": 10, "in_use": 6, "reserved": 1}, 4)
        pools.capacity({"limit": -1, "in_use": 100, "reserved": 20}, 100)

    def test_own_prefix_outside_state_is_not_adopted_or_deleted(self) -> None:
        cloud = Mock()
        cloud.get.side_effect = [{"servers": [{"name": "otus-dz07-db1"}]}, {"networks": []}]
        with self.assertRaises(ValueError):
            pools.reject_orphans(cloud, "otus-dz07", [])

    def test_repeat_deploy_counts_only_missing_compute_and_disks(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tools") as d:
            root = Path(d)
            (root / "terraform-selectel").mkdir()
            resources = [
                {
                    "mode": "managed",
                    "type": "openstack_compute_instance_v2",
                    "instances": [{"index_key": "db1", "attributes": {}}],
                },
                {
                    "mode": "managed",
                    "type": "openstack_blockstorage_volume_v3",
                    "instances": [{"attributes": {"size": 20}}],
                },
            ]
            (root / "terraform-selectel/terraform.tfstate").write_text(json.dumps({"resources": resources}))
            base = {
                "cores": 4,
                "ram": 8192,
                "instances": 2,
                "disk_gb": 40,
                "volumes": 2,
                "port": 5,
                "security_group_rule": 5,
            }
            actual = pools.remaining_demand(root, base, {"db1": {"vcpus": 2, "ram_mb": 4096}})
            self.assertEqual(
                (actual["cores"], actual["ram"], actual["instances"], actual["disk_gb"], actual["volumes"]),
                (2, 4096, 1, 20, 1),
            )


if __name__ == "__main__":
    unittest.main()
