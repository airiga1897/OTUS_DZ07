"""Read-only Global Router capability, quota and ownership edge cases."""

import unittest
from unittest.mock import Mock, patch

import selectel_global_router as gr


class RouterBoundaryContracts(unittest.TestCase):
    def client(self) -> gr.GlobalRouter:
        return gr.GlobalRouter({"username": "synthetic", "password": "synthetic", "domain_name": "synthetic"})

    def test_auth_scope_cached_token_and_invalid_paths(self) -> None:
        client = self.client()
        with (
            patch.object(gr, "request_json", return_value=({"token": {"domain": {"name": "foreign"}}}, {})),
            self.assertRaises(ValueError),
        ):
            client.authenticate()
        client.token = "synthetic-token"
        with patch.object(gr, "request_json", return_value=({"ok": True}, {})) as get:
            self.assertTrue(client.get("/zones")["ok"])
            for path in ("zones", "/zones/../networks"):
                with self.assertRaises(ValueError):
                    client.get(path)
            self.assertEqual(get.call_count, 1)

    def test_unknown_collections_shapes_duplicates_and_pagination_limit(self) -> None:
        client = self.client()
        client.get = Mock(return_value={})
        with self.assertRaises(ValueError):
            client.listing("foreign")
        with self.assertRaises(ValueError):
            client.listing("networks")
        client.get.return_value = [{"id": "same"}, {"id": "same"}]
        with self.assertRaises(ValueError):
            client.listing("networks")
        client.get.side_effect = [[{"id": str(i)} for i in range(200)], [{"id": str(i + 200)} for i in range(200)]]
        with patch.object(gr, "range", return_value=iter([1, 2]), create=True), self.assertRaises(ValueError):
            client.listing("networks")

    def test_zone_unknown_and_retained_incompatible_zone_fail_closed(self) -> None:
        client = self.client()
        for zones in [
            [],
            [{"name": "ru-3", "service": "vpc", "enable": False}],
            [{"name": "ru-3", "service": "vpc", "enable": True, "groups": []}],
            [{"name": "ru-3", "service": "vpc", "enable": True}],
        ]:
            client.listing = Mock(return_value=zones)
            with self.assertRaises(ValueError):
                client.compatible_regions(["ru-3"], retained=["ru-3"])
        zones = [{"name": "ru-3", "service": "vpc", "enable": True, "groups": [{"id": "one"}]}]
        client.listing = Mock(return_value=zones)
        with self.assertRaises(ValueError):
            client.compatible_regions(["ru-3"], accept=lambda regions: False)

    def test_owned_resources_and_api_absence_are_not_inferred_from_missing_quota(self) -> None:
        client = self.client()
        client.get = Mock(return_value={"id": "one", "tags": ["owner"], "project_id": "project"})
        self.assertEqual(client.assert_owned("networks", "one", "owner", "project")["id"], "one")
        with self.assertRaises(ValueError):
            client.assert_owned("networks", "one", "foreign", "project")
        with self.assertRaises(ValueError):
            client.assert_owned("networks", "one", "owner", "foreign")
        client.listing = Mock(return_value=[])
        client.assert_absent({}, "owner")
        client.listing.return_value = [{"id": "one", "tags": ["owner"]}]
        with self.assertRaises(RuntimeError):
            client.assert_absent({}, "owner")
        with self.assertRaises(ValueError):
            client.check_attachment_quotas([], [], [], ["ru-3"], None)

    def test_existing_and_unlimited_attachment_quotas_do_not_demand_new_capacity(self) -> None:
        client = self.client()
        quotas = [{"name": kind, "scope": "account_id", "limit": -1} for kind in ("networks", "subnets")]
        client.check_attachment_quotas(quotas, [], [], ["ru-3", "ru-9"], None)
        quotas = [
            {"name": kind, "scope": "router_id", "scope_value": "router", "limit": 1}
            for kind in ("networks", "subnets")
        ]
        client.check_attachment_quotas(
            quotas, [{"id": "net", "router_id": "router"}], [{"id": "sub", "network_id": "net"}], ["ru-3"], "router"
        )
