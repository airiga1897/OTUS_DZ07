"""Unavailable, malformed and exhausted Global Router discovery is never success."""

import unittest
from unittest.mock import Mock, patch

from selectel_global_router import GlobalRouter, GlobalRouterCapacityError


class GlobalRouterRejections(unittest.TestCase):
    def test_zone_listing_requests_groups_and_stops_on_last_short_page(self) -> None:
        router = GlobalRouter({})
        router.get = Mock(return_value=[])
        self.assertEqual(router.listing("zones"), [])
        self.assertIn("show_groups=true", router.get.call_args.args[0])
        router.get.assert_called_once()

    def test_exhausted_router_and_attachment_quotas_are_capacity_errors(self) -> None:
        router = GlobalRouter({})
        data = {
            "zones": [{"name": "ru-9", "service": "vpc", "enable": True, "groups": [{"id": "shared"}]}],
            "routers": [],
            "quotas": [{"name": "routers", "scope": "account_id", "limit": 0}],
        }
        router.listing = Mock(side_effect=lambda kind: data[kind])
        with self.assertRaises(GlobalRouterCapacityError):
            router.preflight(["ru-9"], "otus-dz07")
        with self.assertRaises(GlobalRouterCapacityError):
            router.check_attachment_quotas(
                [{"name": "networks", "scope": "account_id", "limit": 0}], [], [], ["ru-9"], None
            )

    def test_get_authenticates_once_before_read_only_request(self) -> None:
        router = GlobalRouter({})

        def authenticate() -> None:
            router.token = "synthetic-token"

        router.authenticate = Mock(side_effect=authenticate)
        with patch("selectel_global_router.request_json", return_value=({"id": "synthetic"}, {})) as request:
            self.assertEqual(router.get("/routers/id"), {"id": "synthetic"})
            router.get("/routers/id")
        router.authenticate.assert_called_once()
        self.assertEqual(request.call_count, 2)

    def test_unknown_collection_and_malformed_response_fail_closed(self) -> None:
        router = GlobalRouter({})
        router.get = Mock(return_value={"routers": []})
        with self.assertRaisesRegex(ValueError, "Unexpected"):
            router.listing("servers")
        with self.assertRaisesRegex(ValueError, "response"):
            router.listing("routers")

    def test_unique_full_pages_are_stopped_at_the_documented_limit(self) -> None:
        router = GlobalRouter({})
        count = 0

        def page(path):
            nonlocal count
            count += 1
            return [{"id": str(count) + "-" + str(index)} for index in range(200)]

        router.get = Mock(side_effect=page)
        with self.assertRaisesRegex(ValueError, "limit exceeded"):
            router.listing("routers")
        self.assertEqual(router.get.call_count, 1000)

    def test_preflight_rejects_foreign_namespace_and_unknown_router_quota(self) -> None:
        zones = [{"name": "ru-9", "service": "vpc", "enable": True, "groups": [{"id": "shared"}]}]
        router = GlobalRouter({})
        for rows, message in (
            ([{"id": "foreign", "name": "otus-dz07-foreign"}], "namespace"),
            ([], "quota unavailable"),
        ):
            data = {"zones": zones, "routers": rows, "quotas": []}
            router.listing = Mock(side_effect=lambda kind, *, data=data: data[kind])
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                router.preflight(["ru-9"], "otus-dz07")

    def test_unknown_attachment_quota_is_not_unlimited_capacity(self) -> None:
        router = GlobalRouter({})
        with self.assertRaisesRegex(ValueError, "unknown before allocation"):
            router.check_attachment_quotas([], [], [], ["ru-9"], None)
