"""Run the real post-create network audit against synthetic Neutron/Nova replies."""

import copy
import unittest
from typing import cast
from unittest.mock import patch

from cloud_contract import InfrastructureNode, InfrastructureResult
from selectel_adapter import SelectelAdapter
from selectel_staged import Backend
from selectel_staged_audit import verify_networks
from stage_journal import StageOutput
from stage_store import StageStore
from topology import Topology


def mapping(**fields: object) -> dict[str, object]:
    return fields


def obj(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AssertionError("expected object")
    return cast(dict[str, object], value)


def rows(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise AssertionError("expected object list")
    return cast(list[dict[str, object]], value)


def server(data: dict[tuple[str, str], dict[str, object]], name: str) -> dict[str, object]:
    return obj(obj(data[("compute", "/servers/" + name)])["server"])


def fixture(
    distinct: bool = True,
) -> tuple[Backend, dict[tuple[str, str], dict[str, object]], dict[tuple[str, str, str], dict[str, object]]]:
    nodes: dict[str, InfrastructureNode] = {
        name: {
            "id": name,
            "port_id": "p-" + name,
            "volume_id": "v-" + name,
            "region": "ru-9",
            "zone": "ru-9a",
            "roles": ["app"] if name == "app" else ["es"],
            "private_ip": "10.77.0." + str(host),
        }
        for name, host in [("app", 10), ("es1", 31), ("es2", 32), ("es3", 33)]
    }
    inputs: dict[str, object] = {
        "name": "otus-dz07-net",
        "cidr": "10.77.0.0/24",
        "peer_cidrs": ["10.78.0.0/24"],
        "global_gateway_host": 2,
        "admin_cidrs": ["192.0.2.1/32"],
        "kibana_port": 5601,
        "network_zone": "ru-9a",
        "router_zone": "ru-9a",
    }
    cidr = inputs["cidr"]
    peer_value = inputs["peer_cidrs"]
    network_name = inputs["name"]
    if not isinstance(cidr, str) or not isinstance(network_name, str):
        raise AssertionError("expected network identity")
    if not isinstance(peer_value, list) or any(not isinstance(item, str) for item in peer_value):
        raise AssertionError("expected peer networks")
    peer_cidrs = cast(list[str], peer_value)
    network: StageOutput = {
        "network_id": "network",
        "subnet_id": "subnet",
        "security_group_id": "group",
        "server_group_id": "placement",
        "cidr": cidr,
    }
    rules: list[object] = []
    for prefix in (cidr, *peer_cidrs):
        rules.append(mapping(direction="ingress", ethertype="IPv4", remote_ip_prefix=prefix))
    for port, prefix in ((80, "0.0.0.0/0"), (22, "192.0.2.1/32"), (5601, "192.0.2.1/32")):
        rules.append(
            mapping(
                direction="ingress",
                ethertype="IPv4",
                protocol="tcp",
                port_range_min=port,
                port_range_max=port,
                remote_ip_prefix=prefix,
            )
        )
    rules.append(mapping(direction="egress", ethertype="IPv4"))
    network_body = mapping(
        shared=False,
        port_security_enabled=True,
        availability_zone_hints=["ru-9a"],
        availability_zones=["ru-9a"],
    )
    network_body["router:external"] = False
    members: list[object] = ["es1", "es2", "es3"] if distinct else []
    data: dict[tuple[str, str], dict[str, object]] = {
        ("network", "/v2.0/networks/network"): mapping(network=network_body),
        ("network", "/v2.0/subnets/subnet"): mapping(
            subnet=mapping(host_routes=[mapping(destination="10.78.0.0/24", nexthop="10.77.0.2")])
        ),
        ("network", "/v2.0/routers"): mapping(
            routers=[mapping(name=network_name, availability_zone_hints=["ru-9a"], availability_zones=["ru-9a"])]
        ),
        ("network", "/v2.0/security-groups/group"): mapping(security_group=mapping(security_group_rules=rules)),
        ("network", "/v2.0/floatingips"): mapping(floatingips=[]),
        ("compute", "/os-server-groups"): mapping(
            server_groups=[mapping(id="placement", policies=["anti-affinity"], members=members)]
        ),
    }
    for name, node in nodes.items():
        data[("compute", "/servers/" + name)] = mapping(server=mapping(hostId="host-" + name))
        data[("network", "/v2.0/ports/p-" + name)] = mapping(
            port=mapping(
                port_security_enabled=True,
                security_groups=["group"],
                network_id="network",
                fixed_ips=[mapping(ip_address=node["private_ip"])],
            )
        )
    pages: dict[tuple[str, str, str], dict[str, object]] = {}

    def read(service: str, path: str, payload: object = None) -> dict[str, object]:
        base, _, query = path.partition("?")
        marker = "2" if "marker=2" in query else "1"
        return copy.deepcopy(pages.get((service, base, marker), data[(service, base)]))

    cloud = SelectelAdapter.__new__(SelectelAdapter)
    cloud.endpoints = {"network": "https://example.invalid", "compute": "https://example.invalid"}
    result = InfrastructureResult(
        "selectel",
        {"project_id": "synthetic"},
        "otus-dz07",
        {"region": "ru-9", "zone": "ru-9a"},
        nodes,
        "10.77.0.0/24",
    )
    desired: Topology = {"nodes": {}}
    backend = Backend.__new__(Backend)
    backend.desired = desired
    backend.journal = {
        "require_distinct_hosts": distinct,
        "stages": {
            "network-ru-9": {
                "status": "active",
                "candidate": {"region": "ru-9"},
                "inputs": inputs,
                "output": network,
            }
        },
    }
    store = StageStore.__new__(StageStore)
    backend.store = store
    backend.credentials = {"project_id": "synthetic"}
    patch.object(cloud, "get", read).start()
    patch.object(backend, "region", return_value=cloud).start()
    patch.object(backend, "result", return_value=result).start()
    patch.object(store, "entries", return_value=[]).start()
    return backend, data, pages


class NetworkPolicyAcceptance(unittest.TestCase):
    def test_complete_policy_and_diagnostic_mode_are_distinct_contracts(self) -> None:
        backend, data, _ = fixture()
        result = verify_networks(backend)
        self.assertTrue(result["global_routes_confirmed"])
        self.assertEqual(result["es_distinct_hosts"], 3)
        backend, data, _ = fixture(False)
        for name in ("es1", "es2", "es3"):
            server(data, name).pop("hostId")
        result = verify_networks(backend)
        self.assertFalse(result["physical_host_separation_verified"])
        self.assertIsNone(result["es_distinct_hosts"])

    def test_each_private_port_property_is_mandatory(self) -> None:
        changes: tuple[dict[str, object], ...] = (
            {"port_security_enabled": False},
            {"security_groups": ["foreign"]},
            {"network_id": "foreign"},
            {"fixed_ips": []},
            {"fixed_ips": [{"ip_address": "10.77.0.99"}]},
        )
        for change in changes:
            backend, data, _ = fixture()
            obj(obj(data[("network", "/v2.0/ports/p-es1")])["port"]).update(change)
            with self.subTest(change=change), self.assertRaisesRegex(RuntimeError, "port filtering"):
                verify_networks(backend)

    def test_internal_public_address_is_rejected_even_on_a_later_page(self) -> None:
        for paginated in (False, True):
            backend, data, pages = fixture()
            floating = mapping(floatingips=[mapping(id="public", port_id="p-es1")])
            if paginated:
                pages[("network", "/v2.0/floatingips", "1")] = mapping(
                    floatingips=[],
                    floatingips_links=[mapping(rel="next", href="https://example.invalid/v2.0/floatingips?marker=2")],
                )
                pages[("network", "/v2.0/floatingips", "2")] = floating
            else:
                data[("network", "/v2.0/floatingips")] = floating
            with self.subTest(paginated=paginated), self.assertRaisesRegex(RuntimeError, "public address"):
                verify_networks(backend)

    def test_missing_or_weak_placement_group_is_rejected(self) -> None:
        variants: tuple[list[object], ...] = (
            [],
            [mapping(id="placement", policies=["soft-anti-affinity"])],
            [mapping(id="foreign")],
        )
        for groups in variants:
            backend, data, _ = fixture()
            obj(data[("compute", "/os-server-groups")])["server_groups"] = groups
            with self.subTest(groups=groups), self.assertRaisesRegex(RuntimeError, "placement group"):
                verify_networks(backend)

    def test_separation_requires_membership_host_ids_and_three_distinct_hosts(self) -> None:
        for mode in ("missing-member", "missing-host", "same-host"):
            backend, data, _ = fixture()
            if mode == "missing-member":
                rows(obj(data[("compute", "/os-server-groups")])["server_groups"])[0]["members"] = []
            if mode == "missing-host":
                server(data, "es1").pop("hostId")
            if mode == "same-host":
                server(data, "es2")["hostId"] = "host-es1"
            with self.subTest(mode=mode), self.assertRaises(RuntimeError):
                verify_networks(backend)
        backend, data, _ = fixture(False)
        rows(obj(data[("compute", "/os-server-groups")])["server_groups"])[0]["members"] = ["es1"]
        with self.assertRaisesRegex(RuntimeError, "unexpectedly belongs"):
            verify_networks(backend)

    def test_shared_external_or_unfiltered_network_is_rejected(self) -> None:
        for field in ("shared", "router:external", "port_security_enabled"):
            backend, data, _ = fixture()
            obj(obj(data[("network", "/v2.0/networks/network")])["network"])[field] = field != "port_security_enabled"
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, "isolation"):
                verify_networks(backend)

    def test_route_targets_domains_and_exact_ingress_policy_are_checked(self) -> None:
        for mode in (
            "route",
            "network-hint",
            "network-actual",
            "router-hint",
            "router-actual",
            "extra-ingress",
            "missing-ingress",
        ):
            backend, data, _ = fixture()
            if mode == "route":
                rows(obj(obj(data[("network", "/v2.0/subnets/subnet")])["subnet"])["host_routes"])[0]["nexthop"] = (
                    "10.77.0.9"
                )
            if mode.startswith("network-"):
                field = "availability_zone_hints" if mode.endswith("hint") else "availability_zones"
                obj(obj(data[("network", "/v2.0/networks/network")])["network"])[field] = ["ru-3a"]
            if mode.startswith("router-"):
                field = "availability_zone_hints" if mode.endswith("hint") else "availability_zones"
                rows(obj(data[("network", "/v2.0/routers")])["routers"])[0][field] = []
            rules = rows(
                obj(obj(data[("network", "/v2.0/security-groups/group")])["security_group"])["security_group_rules"]
            )
            if mode == "extra-ingress":
                rules.append(mapping(direction="ingress", ethertype="IPv4", remote_ip_prefix="0.0.0.0/0"))
            if mode == "missing-ingress":
                rules.pop(0)
            with self.subTest(mode=mode), self.assertRaises(RuntimeError):
                verify_networks(backend)

    def test_later_router_and_placement_pages_are_not_false_absence(self) -> None:
        backend, data, pages = fixture()
        for service, path, key in [
            ("network", "/v2.0/routers", "routers"),
            ("compute", "/os-server-groups", "server_groups"),
        ]:
            first: dict[str, object] = {
                key: [],
                key + "_links": [mapping(rel="next", href="https://example.invalid" + path + "?marker=2")],
            }
            pages[(service, path, "1")] = first
            pages[(service, path, "2")] = data[(service, path)]
        self.assertTrue(verify_networks(backend)["success"])
