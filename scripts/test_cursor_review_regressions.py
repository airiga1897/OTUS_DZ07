"""Cursor findings: isolate unknown candidates and account for ports consistently."""

import http.client
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

import pool_selection as pools
import test_staged_backend as fixtures
from cloud_contract import Capability, CapabilityStatus
from placement_config import PlacementPolicy, checked
from quota_types import QuotaRecord
from selectel_api import CatalogService
from selectel_staged import Backend
from selectel_staged_capacity import pending_credits, require_capacity, require_network_capacity

ROOT = Path(__file__).resolve().parents[1]


class CursorReviewRegressions(unittest.TestCase):
    def backend(self) -> tuple[Backend, dict[str, fixtures.QuotaCloud]]:
        folder = tempfile.TemporaryDirectory(dir=ROOT / ".tools")
        self.addCleanup(folder.cleanup)
        provider, clouds = fixtures.StagedBackendTests().backend(Path(folder.name))
        clouds["ru-3"] = fixtures.QuotaCloud("ru-3", instances=3)
        provider.choices = {
            role: [checked(fixtures.StagedBackendTests().choice(pool, role))]
            for role, pool in [("app", "ru-9"), ("es", "ru-3")]
        }
        return provider, clouds

    def test_opaque_disk_never_hides_later_confirmed_disks(self) -> None:
        opaque_values: tuple[object, ...] = (
            {},
            [],
            {"RESKEY:availability_zones": None},
            {"RESKEY:availability_zones": 1},
            {"RESKEY:availability_zones": ""},
        )
        for opaque in opaque_values:
            types = [
                {"name": "universal.opaque", "extra_specs": opaque},
                {"name": "universal.ru-9a", "extra_specs": {"RESKEY:availability_zones": "ru-9a"}},
                {"name": "basicssd.ru-9a", "extra_specs": {"RESKEY:availability_zones": "ru-9a"}},
            ]
            with self.subTest(opaque=opaque):
                get = Mock(side_effect=AssertionError("Inline specs must not request API"))
                self.assertEqual(
                    pools.disks_for_zone(types, ["universal.*", "basicssd.*"], "ru-9a", get),
                    ["universal.ru-9a", "basicssd.ru-9a"],
                )
                with self.assertRaises(pools.CandidateUnavailable):
                    pools.disks_for_zone(types[:1], ["universal.*"], "ru-9a", get)

    def test_disk_detail_unknown_is_local_but_api_errors_propagate(self) -> None:
        types = [
            {"id": "opaque", "name": "universal.opaque"},
            {"name": "basicssd.ru-9a", "extra_specs": {"RESKEY:availability_zones": "ru-9a"}},
        ]
        self.assertEqual(
            pools.disks_for_zone(
                types, ["universal.*", "basicssd.*"], "ru-9a", Mock(return_value={"extra_specs": None})
            ),
            ["basicssd.ru-9a"],
        )
        for error in (
            TimeoutError("synthetic"),
            urllib.error.HTTPError("https://example.invalid", 403, "Forbidden", http.client.HTTPMessage(), None),
        ):
            with self.subTest(error=type(error).__name__), self.assertRaises(type(error)):
                pools.disks_for_zone(types, ["universal.*", "basicssd.*"], "ru-9a", Mock(side_effect=error))

    def test_catalog_unknown_flavor_is_skipped_and_missing_binding_is_deferred(self) -> None:
        config: PlacementPolicy = {
            "pools": ["ru-9"],
            "boot_disk_gb": 20,
            "image_name": "Ubuntu",
            "image_checksums": ["a" * 32],
            "profiles": {"app": {"vcpus": 4, "ram_mb": 8192}},
            "disk_type_patterns": ["universal.*"],
            "flavor_name_patterns": ["SL*.4-8192*"],
        }
        catalog: list[CatalogService] = [
            {
                "type": kind,
                "endpoints": [{"interface": "public", "region": "ru-9", "url": "https://example.invalid/" + kind}],
            }
            for kind in pools.SERVICES
        ]
        invalid_values: tuple[object, ...] = (None, 1, "ru-9a", {"ru-9a": True}, [None])
        for invalid in invalid_values:

            def read(url: str, *, invalid: object = invalid, **kwargs: object) -> tuple[object, dict[str, str]]:
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
                if url.endswith("/flavors/detail"):
                    return {
                        "flavors": [
                            {
                                "id": "bad",
                                "name": "SL1.4-8192",
                                "vcpus": 4,
                                "ram": 8192,
                                "disk": 0,
                                "availability_zones": invalid,
                            },
                            {
                                "id": "good",
                                "name": "SL2.4-8192-AMD",
                                "vcpus": 4,
                                "ram": 8192,
                                "disk": 0,
                                "availability_zones": ["ru-9a"],
                            },
                        ]
                    }, {}
                if url.endswith("/types"):
                    return {
                        "volume_types": [
                            {"name": "universal.ru-9a", "extra_specs": {"RESKEY:availability_zones": "ru-9a"}}
                        ]
                    }, {}
                if url.endswith("/v2.0/availability_zones"):
                    return {
                        "availability_zones": [
                            {"name": "ru-9a", "state": "available", "resource": kind} for kind in ("network", "router")
                        ]
                    }, {}
                return {"availabilityZoneInfo": [{"zoneName": "ru-9a", "zoneState": {"available": True}}]}, {}

            with (
                self.subTest(invalid=invalid),
                patch.object(pools, "authenticate", return_value=("synthetic", catalog)),
                patch.object(pools, "request_json", side_effect=read),
            ):
                values = list(pools.candidates(config, {}))
                self.assertEqual([v["flavor_ids"]["app"] for v in values], ["good"])
        self.assertTrue(pools.flavor_available_in_zone({}, "ru-9a", defer_missing=True))
        self.assertFalse(pools.flavor_available_in_zone({}, "ru-9a"))
        self.assertFalse(pools.flavor_available_in_zone({"availability_zones": []}, "ru-9a"))
        self.assertFalse(pools.flavor_available_in_zone({"availability_zones": ["ru-3b"]}, "ru-9a"))

    def test_actual_preflight_continues_after_unknown_flavor_detail(self) -> None:
        invalid_values: tuple[object, ...] = (None, 1, "ru-9a", {}, [None])
        for invalid in invalid_values:
            provider, clouds = self.backend()
            original = clouds["ru-9"].get
            with patch.object(
                clouds["ru-9"],
                "get",
                side_effect=lambda service, path, *, invalid=invalid, original=original: (
                    {"flavor": {"availability_zones": invalid}}
                    if path == "/flavors/unknown"
                    else original(service, path)
                ),
            ):

                def choices(
                    config: PlacementPolicy, credentials: dict[str, str], **kwargs: object
                ) -> list[PlacementPolicy]:
                    del credentials, kwargs
                    role = next(iter(config["profiles"]))
                    if role == "es":
                        return [checked(fixtures.StagedBackendTests().choice("ru-3", role))]
                    good = checked(fixtures.StagedBackendTests().choice("ru-9", role))
                    bad = checked({**dict(good), "flavor_ids": {"app": "unknown"}})
                    return [bad, good]

                with (
                    self.subTest(invalid=invalid),
                    patch("selectel_staged.pools.candidates", side_effect=choices),
                    patch(
                        "selectel_staged.discover_capabilities",
                        return_value={"ownership_tags": Capability(CapabilityStatus.SUPPORTED, "synthetic")},
                    ),
                    patch("selectel_staged.reject_cloud_resources"),
                    patch("selectel_staged_audit.audit_namespace"),
                ):
                    provider.preflight()
                    self.assertEqual([c["flavor_ids"]["app"] for c in provider.choices["app"]], ["app-flavor"])
                    self.assertEqual(provider.choices["es"][0]["region"], "ru-3")

    def test_service_reserve_and_each_vm_port_are_budgeted_once(self) -> None:
        provider, clouds = self.backend()
        # Boundaries chosen independently: 6 service ports + app; 6 + three ES.
        clouds["ru-9"].network["port"]["limit"] = 7
        clouds["ru-3"].network["port"]["limit"] = 9
        require_capacity(provider)
        for pool, insufficient in [("ru-9", 6), ("ru-3", 8)]:
            quota = clouds[pool].network["port"]
            previous = quota["limit"]
            quota["limit"] = insufficient
            require_network_capacity(provider, pool)
            with self.subTest(pool=pool), self.assertRaises(pools.CandidateUnavailable):
                require_capacity(provider)
            quota["limit"] = previous
        # Allocated networks need only the ports for the remaining VMs.
        provider.journal["stages"] = {"network-" + pool: {"status": "active"} for pool in clouds}
        clouds["ru-9"].network["port"]["limit"] = 1
        clouds["ru-3"].network["port"]["limit"] = 3
        require_capacity(provider)

    def test_flavor_detail_api_error_is_not_treated_as_an_unavailable_candidate(self) -> None:
        provider, clouds = self.backend()
        error = urllib.error.HTTPError("https://example.invalid", 403, "Forbidden", http.client.HTTPMessage(), None)
        for cloud in clouds.values():
            original = cloud.get
            patcher = patch.object(
                cloud,
                "get",
                side_effect=lambda service, path, original=original: (
                    (_ for _ in ()).throw(error) if path.startswith("/flavors/") else original(service, path)
                ),
            )
            patcher.start()
            self.addCleanup(patcher.stop)

        def choices(config: PlacementPolicy, credentials: dict[str, str], **kwargs: object) -> list[PlacementPolicy]:
            del credentials, kwargs
            role = next(iter(config["profiles"]))
            return [checked(fixtures.StagedBackendTests().choice("ru-9" if role == "app" else "ru-3", role))]

        with (
            patch("selectel_staged.pools.candidates", side_effect=choices),
            patch(
                "selectel_staged.discover_capabilities",
                return_value={"ownership_tags": Capability(CapabilityStatus.SUPPORTED, "synthetic")},
            ),
            patch("selectel_staged.reject_cloud_resources"),
            patch("selectel_staged_audit.audit_namespace"),
            self.assertRaises(urllib.error.HTTPError),
        ):
            provider.preflight()
        save = provider.api.save
        assert isinstance(save, Mock)
        save.assert_not_called()

    def test_per_vm_check_rejects_exhausted_ports_and_app_floating_ip(self) -> None:
        provider, clouds = self.backend()
        choice = provider.choices["app"][0]
        clouds["ru-9"].network["port"]["limit"] = 0
        with self.assertRaises(pools.CandidateUnavailable):
            provider.check_node_quota(choice, "app")
        clouds["ru-9"].network["port"]["limit"] = 1
        clouds["ru-9"].network["floatingip"]["limit"] = 0
        with self.assertRaises(pools.CandidateUnavailable):
            provider.check_node_quota(choice, "app")
        provider.check_node_quota(checked(fixtures.StagedBackendTests().choice("ru-9", "es")), "es")
        clouds["ru-9"].network.pop("port")
        with self.assertRaises(pools.CandidateUnavailable):
            provider.check_node_quota(choice, "app")

    def test_resume_credits_only_saved_ports_and_floating_ips_in_their_pool(self) -> None:
        provider, clouds = self.backend()
        choice = provider.choices["app"][0]
        provider.journal["stages"]["app"] = {"status": "pending", "candidate": choice}
        with patch.object(
            provider.store,
            "entries",
            return_value=[
                {"type": "openstack_networking_port_v2", "attrs": {"id": "synthetic-port"}},
                {"type": "openstack_networking_floatingip_v2", "attrs": {"id": "synthetic-fip"}},
            ],
        ):
            for key in ("port", "floatingip"):
                quota: QuotaRecord = clouds["ru-9"].network[key]
                quota["limit"] = 1
                quota["in_use"] = 1
            self.assertEqual(pending_credits(provider, "ru-9"), {"port": 1, "floatingip": 1})
            self.assertEqual(pending_credits(provider, "ru-3"), {})
            provider.check_node_quota(choice, "app")
