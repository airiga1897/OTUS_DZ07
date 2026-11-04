"""Regressions for unconfirmed flavor disks and incomplete OpenStack pages."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pool_selection as pools
from selectel_adapter import audit_cleanup, reject_cloud_resources

ENDPOINT = "https://example.invalid/v2.1"
COLLECTION_KEYS = {
    "/servers/detail": "servers",
    "/os-server-groups": "server_groups",
    "/volumes/detail": "volumes",
    "/v2.0/networks": "networks",
    "/v2.0/subnets": "subnets",
    "/v2.0/routers": "routers",
    "/v2.0/ports": "ports",
    "/v2.0/security-groups": "security_groups",
    "/v2.0/floatingips": "floatingips",
}


def flavor_config():
    return {"flavor_name_patterns": ["SL1.4-8192"], "profiles": {"app": {"vcpus": 4, "ram_mb": 8192}}}


def flavor(**overrides):
    item = {"id": "confirmed", "name": "SL1.4-8192", "vcpus": 4, "ram": 8192, "os-flavor-access:is_public": True}
    item.update(overrides)
    return item


class UnconfirmedCatalogTests(unittest.TestCase):
    def test_missing_or_non_integer_disk_is_not_network_boot(self) -> None:
        config = flavor_config()
        confirmed = flavor(disk=0)
        self.assertEqual(pools.flavor_choices([confirmed], config)["app"][0]["id"], "confirmed")
        for disk in (False, True, 0.0, "0", 1, -1, None):
            with self.subTest(disk=disk), self.assertRaises(pools.CandidateUnavailable):
                pools.flavor_choices([flavor(id="bad", disk=disk)], config)
        with self.assertRaises(pools.CandidateUnavailable):
            pools.flavor_choices([flavor(id="missing")], config)
        hidden = flavor(id="hidden")
        chosen = pools.flavor_choices([hidden, confirmed], config)["app"]
        self.assertEqual([item["id"] for item in chosen], ["confirmed"])


def page_cloud(pages):
    cloud = Mock()
    cloud.endpoints = dict.fromkeys(("compute", "volumev3", "network"), ENDPOINT)

    def get(service, path):
        base, _, query = path.partition("?")
        key = COLLECTION_KEYS[base]
        marker = "2" if "marker=2" in query else "1"
        body = pages.get((base, marker))
        if body is None:
            return {key: []}
        return body

    cloud.get.side_effect = get
    return cloud


class CollectionCompletenessTests(unittest.TestCase):
    def test_later_page_cannot_hide_owned_resources(self) -> None:
        pages = {
            ("/servers/detail", "1"): {
                "servers": [{"name": "foreign"}],
                "servers_links": [{"rel": "next", "href": ENDPOINT + "/servers/detail?limit=200&marker=2"}],
            },
            ("/servers/detail", "2"): {"servers": [{"name": "otus-dz07-app"}]},
            ("/v2.0/floatingips", "1"): {
                "floatingips": [],
                "floatingips_links": [{"rel": "next", "href": ENDPOINT + "/v2.0/floatingips?limit=200&marker=2"}],
            },
            ("/v2.0/floatingips", "2"): {"floatingips": [{"id": "fip-owned"}]},
        }
        cloud = page_cloud(pages)
        clock = iter([0, 100])
        with (
            patch("selectel_adapter.time.monotonic", side_effect=lambda: next(clock)),
            patch("selectel_adapter.time.sleep") as sleep,
        ):
            remaining, count = audit_cleanup(cloud, "otus-dz07", {"fip-owned"})
        self.assertEqual(remaining, ["otus-dz07-app"])
        self.assertEqual(count, 1)
        sleep.assert_not_called()
        with self.assertRaises(RuntimeError):
            reject_cloud_resources(SimpleNamespace(PREFIX="otus-dz07"), cloud)

    def test_full_page_without_a_next_link_is_not_complete(self) -> None:
        pages = {("/servers/detail", "1"): {"servers": [{"name": "foreign"}] * 200}}
        cloud = page_cloud(pages)
        with self.assertRaises(ValueError):
            audit_cleanup(cloud, "otus-dz07", set())
        with self.assertRaises(ValueError):
            reject_cloud_resources(SimpleNamespace(PREFIX="otus-dz07"), cloud)
