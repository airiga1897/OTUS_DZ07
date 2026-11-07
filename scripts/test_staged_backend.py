"""Synthetic API catalogs and quota budgets for the Selectel staged adapter."""

import copy
import json
import tempfile
import unittest
import urllib.error
from collections.abc import Mapping, Sequence
from email.message import Message
from pathlib import Path
from typing import TypedDict
from unittest.mock import Mock, patch

from cloud_contract import Capability, CapabilityStatus
from placement_config import PlacementPolicy, checked
from pool_selection import CandidateUnavailable
from quota_types import QuotaRecord
from run_settings import placement as placement_settings
from selectel_api import CatalogService
from selectel_staged import Backend
from selectel_staged_capacity import pending_credits, require_capacity
from stage_store import StageStore

ROOT = Path(__file__).resolve().parents[1]


class ComputeQuotas(TypedDict):
    az_quotas: dict[str, dict[str, QuotaRecord]]
    server_groups: QuotaRecord
    server_group_members: QuotaRecord


class QuotaCloud:
    def __init__(self, pool: str, instances: int = 2) -> None:
        self.pool = pool
        zone = pool + ("b" if pool == "ru-3" else "a")
        self.zone = zone
        self.compute: ComputeQuotas = {
            "az_quotas": {
                zone: {"instances": self.q(instances), "cores": self.q(instances * 4), "ram": self.q(instances * 8192)}
            },
            "server_groups": self.q(5),
            "server_group_members": self.q(10),
        }
        self.volumes: dict[str, object] = {
            "gigabytes": self.q(1000),
            "volumes": self.q(20),
            "gigabytes_universal." + zone: self.q(1000),
        }
        self.network = {
            k: self.q(100)
            for k in ("network", "subnet", "router", "security_group", "security_group_rule", "port", "floatingip")
        }

    @staticmethod
    def q(limit: int) -> QuotaRecord:
        return {"limit": limit, "in_use": 0, "reserved": 0}

    def get(self, service: str, path: str) -> dict[str, object]:
        if service == "compute" and "/os-quota-sets/" in path:
            return {"quota_set": self.compute}
        if service == "volumev3":
            return {"quota_set": self.volumes}
        if service == "network" and "/quotas/" in path:
            return {"quota": self.network}
        if path == "/v2.0/subnets":
            return {"subnets": []}
        if path.startswith("/flavors/"):
            return {"flavor": {"availability_zones": [self.zone]}}
        raise AssertionError((service, path))


class StagedBackendTests(unittest.TestCase):
    def catalog(self, regions: Sequence[str]) -> list[CatalogService]:
        return [
            {
                "type": service,
                "endpoints": [
                    {"region": r, "interface": "public", "url": "https://example.invalid/" + service} for r in regions
                ],
            }
            for service in ("compute", "image", "volumev3", "network")
        ]

    def test_auto_discovery_tries_a_nonpreferred_group_that_can_place_the_whole_stand(self) -> None:
        from selectel_global_router import GlobalRouter

        with tempfile.TemporaryDirectory() as folder:
            provider, clouds = self.backend(Path(folder))
            clouds["ru-8"] = QuotaCloud("ru-8", instances=4)
            provider.policy["pools"] = ["auto"]
            provider.cloud = Mock(spec_set=["catalog"], catalog=self.catalog(["ru-9", "ru-8"]))
            global_api = GlobalRouter.__new__(GlobalRouter)
            payload = {
                "zones": [
                    {"name": r, "service": "vpc", "enable": True, "groups": [{"id": r}]} for r in ("ru-9", "ru-8")
                ],
                "routers": [],
                "networks": [],
                "subnets": [],
                "quotas": [
                    {"id": k, "name": k, "scope": "account_id", "limit": 10} for k in ("routers", "networks", "subnets")
                ],
            }
            listing = patch.object(global_api, "listing", side_effect=lambda kind: payload[kind])
            listing.start()
            self.addCleanup(listing.stop)
            provider.global_router = global_api

            def choices(
                config: PlacementPolicy, credentials: Mapping[str, str], **kwargs: object
            ) -> list[dict[str, object]]:
                role = next(iter(config["profiles"]))
                result: list[dict[str, object]] = []
                for region in config["pools"]:
                    item: dict[str, object] = dict(self.choice(region, role))
                    if region == "ru-8":
                        item.update(
                            availability_zone="ru-8a",
                            network_availability_zone="ru-8a",
                            router_availability_zone="ru-8a",
                            volume_type="universal.ru-8a",
                        )
                    result.append(item)
                return result

            with (
                patch("selectel_staged.pools.candidates", side_effect=choices),
                patch(
                    "selectel_staged.discover_capabilities",
                    return_value={"ownership_tags": Capability(CapabilityStatus.SUPPORTED, "synthetic")},
                ),
                patch("selectel_staged.reject_cloud_resources"),
                patch("selectel_staged_audit.audit_namespace"),
            ):
                provider.preflight()
            self.assertEqual(provider.policy["pools"], ["ru-8"])
            # CIDRs are reserved metadata, not evidence of allocated networks.
            self.assertEqual(set(provider.journal["pool_subnets"]), {"ru-9", "ru-8"})
            self.assertTrue(all(c["region"] == "ru-8" for choices in provider.choices.values() for c in choices))

    def test_saved_pool_universe_does_not_expand_when_catalog_changes(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            provider, _ = self.backend(Path(folder))
            provider.policy["pools"] = ["auto"]
            provider.path.parent.mkdir()
            provider.path.write_text("{}")
            provider.cloud = Mock(spec_set=["catalog"], catalog=self.catalog(["ru-9", "ru-3", "ru-8"]))
            before = copy.deepcopy(provider.journal["pool_subnets"])
            with patch(
                "selectel_staged.discover_capabilities",
                return_value={"ownership_tags": Capability(CapabilityStatus.SUPPORTED, "synthetic")},
            ):
                provider.resolve_pools()
            self.assertEqual(provider.policy["pools"], ["ru-9", "ru-3"])
            self.assertEqual(provider.journal["pool_subnets"], before)

    def test_unknown_auto_pool_capability_aborts_instead_of_enabling_it(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            provider, _ = self.backend(Path(folder))
            provider.policy["pools"] = ["auto"]
            provider.cloud = Mock(spec_set=["catalog"], catalog=self.catalog(["ru-9"]))
            with (
                patch(
                    "selectel_staged.discover_capabilities",
                    return_value={"ownership_tags": Capability(CapabilityStatus.UNKNOWN, "API failure")},
                ),
                self.assertRaises(ValueError),
            ):
                provider.preflight()
            self.api_save.assert_not_called()

    def backend(self, root: Path) -> tuple[Backend, dict[str, QuotaCloud]]:
        provider = Backend.__new__(Backend)
        provider.root, provider.path = root, root / ".local/staged-deployment.json"
        (root / "config").mkdir()
        (root / "config/cloud.json").write_text('{"required_capabilities":[]}')
        provider.policy = placement_settings(ROOT)
        provider.policy["pools"] = ["ru-9", "ru-3"]
        provider.desired = json.loads((ROOT / "config/topology.json").read_text())
        provider.credentials = {"project_id": "synthetic", "region": "ru-9"}
        provider.journal = {
            "stages": {},
            "admin_cidrs": ["8.8.8.8/32"],
            "pool_subnets": provider.policy["sequential_placement"]["pool_subnets"],
        }
        self.api_save = Mock()
        provider.api = Mock(ROOT=root, PREFIX="otus-dz07", save=self.api_save)
        self.entries_mock = Mock(return_value=[])
        provider.store = Mock(spec_set=StageStore, entries=self.entries_mock)
        clouds = {pool: QuotaCloud(pool) for pool in ("ru-9", "ru-3")}
        self.region_mock = Mock(side_effect=lambda pool: clouds[pool])
        region = patch.object(provider, "region", self.region_mock)
        region.start()
        self.addCleanup(region.stop)
        self.preflight_mock = Mock()
        provider.global_router = Mock(preflight=self.preflight_mock)
        provider.choices = {role: [self.choice(pool, role) for pool in ("ru-9", "ru-3")] for role in ("app", "es")}
        return provider, clouds

    def choice(self, pool: str, role: str) -> PlacementPolicy:
        zone = "ru-9a" if pool == "ru-9" else "ru-3b"
        return {
            "region": pool,
            "availability_zone": zone,
            "volume_type": "universal." + zone,
            "image_id": "verified-image",
            "flavor_ids": {role: role + "-flavor"},
            "network_availability_zone": "ru-9a" if pool == "ru-9" else "ru-3a",
            "router_availability_zone": "ru-9a" if pool == "ru-9" else "ru-3a",
        }

    def test_capacity_can_be_satisfied_by_a_combination_of_pools(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            provider, clouds = self.backend(Path(folder))
            require_capacity(provider)
            clouds["ru-3"].compute["az_quotas"]["ru-3b"]["instances"]["limit"] = 1
            with self.assertRaises(CandidateUnavailable):
                require_capacity(provider)

    def test_unknown_quotas_and_network_capacity_do_not_pass(self) -> None:
        for kind in ("compute", "volume", "network"):
            with tempfile.TemporaryDirectory() as folder:
                provider, clouds = self.backend(Path(folder))
                for cloud in clouds.values():
                    if kind == "compute":
                        cloud.compute["az_quotas"][cloud.zone].pop("ram")
                    if kind == "volume":
                        cloud.volumes.pop("gigabytes")
                    if kind == "network":
                        cloud.network["port"]["limit"] = 0
                with self.assertRaises(CandidateUnavailable):
                    require_capacity(provider)

    def test_active_nodes_need_no_new_quota(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            provider, clouds = self.backend(Path(folder))
            provider.journal["stages"] = {n: {"status": "active"} for n in provider.desired["nodes"]}
            self.region_mock.side_effect = AssertionError("No allocation quota needed")
            require_capacity(provider)

    def test_pending_owned_resources_are_credited_in_their_own_pool_and_zone(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            provider, clouds = self.backend(Path(folder))
            provider.journal["stages"]["es2"] = {"status": "pending", "candidate": checked(self.choice("ru-3", "es"))}
            self.entries_mock.return_value = [
                {"type": "openstack_compute_instance_v2", "attrs": {"id": "partial-vm"}},
                {"type": "openstack_blockstorage_volume_v3", "attrs": {"id": "partial-volume", "size": 20}},
            ]
            quota_credits = pending_credits(provider, "ru-3")
            self.assertEqual(quota_credits[("ru-3b", "ram")], 8192)
            self.assertEqual(quota_credits["gigabytes"], 20)
            self.assertEqual(pending_credits(provider, "ru-9"), {})

    def test_role_catalogs_are_independent_and_flavor_zone_binding_is_checked(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            provider, clouds = self.backend(Path(folder))
            clouds["ru-3"] = QuotaCloud("ru-3", instances=3)

            def choices(
                config: PlacementPolicy, credentials: Mapping[str, str], **kwargs: object
            ) -> list[PlacementPolicy]:
                role = next(iter(config["profiles"]))
                return [self.choice("ru-9", role)] if role == "app" else [self.choice("ru-3", role)]

            with (
                patch("selectel_staged.pools.candidates", side_effect=choices),
                patch(
                    "selectel_staged.discover_capabilities",
                    return_value={"ownership_tags": Capability(CapabilityStatus.SUPPORTED, "synthetic")},
                ),
                patch("selectel_staged.reject_cloud_resources"),
                patch("selectel_staged_audit.audit_namespace"),
            ):
                provider.preflight()
            self.assertEqual(provider.choices["app"][0]["region"], "ru-9")
            self.assertEqual(provider.choices["es"][0]["region"], "ru-3")
            self.assertEqual(self.preflight_mock.call_count, 1)

    def test_api_failure_is_not_silently_treated_as_an_empty_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            provider, clouds = self.backend(Path(folder))
            error = urllib.error.HTTPError("https://example.invalid", 403, "Forbidden", Message(), None)
            self.region_mock.side_effect = error
            with self.assertRaises(urllib.error.HTTPError):
                provider.preflight()
            self.api_save.assert_not_called()

    def test_partial_stage_id_is_recovered_from_state_when_outputs_were_not_written(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            provider, clouds = self.backend(Path(folder))
            self.entries_mock.return_value = [
                {"type": "selectel_global_router_router_v1", "attrs": {"id": "retained-router"}}
            ]
            self.assertEqual(
                provider.saved_id("global", "selectel_global_router_router_v1", "router_id"), "retained-router"
            )
