"""Provider postchecks verify actual placement and filtering, not catalog names."""

import copy
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import selectel_adapter as adapter
from cloud_contract import InfrastructureNode, InfrastructureResult, StandDescription


class ProviderPostcheckMatrix(unittest.TestCase):
    def network_fixture(self) -> tuple[InfrastructureResult, dict[str, dict[str, object]]]:
        nodes: dict[str, InfrastructureNode] = {
            "app": {
                "roles": ["app"],
                "id": "app",
                "private_ip": "10.77.0.10",
                "public_ip": "8.8.8.8",
                "volume_id": "av",
                "port_id": "ap",
            },
            "es1": {"roles": ["es"], "id": "es1", "private_ip": "10.77.0.31", "volume_id": "ev", "port_id": "ep"},
        }
        result = InfrastructureResult(
            "selectel",
            {"project_id": "synthetic"},
            "otus-dz07",
            {"region": "ru-9", "zone": "ru-9a"},
            nodes,
            "10.77.0.0/24",
        )

        def rule(
            protocol: str | None, port: int | None, cidr: str | None = None, group: str | None = None
        ) -> dict[str, object]:
            return {
                "direction": "ingress",
                "ethertype": "IPv4",
                "protocol": protocol,
                "port_range_min": port,
                "port_range_max": port,
                "remote_ip_prefix": cidr,
                "remote_group_id": group,
            }

        data: dict[str, dict[str, object]] = {
            "/v2.0/security-groups": {
                "security_groups": [
                    {
                        "name": "otus-dz07",
                        "id": "sg",
                        "tenant_id": "synthetic",
                        "security_group_rules": [
                            rule(None, None, group="sg"),
                            rule("tcp", 80, "0.0.0.0/0"),
                            rule("tcp", 22, "8.8.8.8/32"),
                            rule("tcp", 5601, "8.8.8.8/32"),
                        ],
                    }
                ]
            },
            "/v2.0/routers": {
                "routers": [
                    {"name": "otus-dz07", "availability_zones": ["ru-9a"], "availability_zone_hints": ["ru-9a"]}
                ]
            },
            "/v2.0/networks/net": {
                "network": {
                    "shared": False,
                    "router:external": False,
                    "port_security_enabled": True,
                    "availability_zones": ["ru-9a"],
                    "availability_zone_hints": ["ru-9a"],
                }
            },
            "/v2.0/floatingips": {"floatingips": []},
        }
        for name, node in nodes.items():
            data["/v2.0/ports/" + node["port_id"]] = {
                "port": {
                    "name": "otus-dz07-" + name,
                    "network_id": "net",
                    "port_security_enabled": True,
                    "security_groups": ["sg"],
                    "fixed_ips": [{"ip_address": node["private_ip"]}],
                }
            }
        return result, data

    def test_network_checks_each_isolation_and_identity_constraint(self) -> None:
        result, original = self.network_fixture()
        data = copy.deepcopy(original)
        cloud = Mock()

        def response(service: str, path: str) -> dict[str, object]:
            return data[path]

        cloud.get.side_effect = response
        self.assertTrue(adapter.check_network(cloud, result, ["8.8.8.8/32"])["success"])
        for mutation in (
            "no-sg",
            "duplicate-sg",
            "foreign-sg",
            "ingress",
            "name",
            "address",
            "network",
            "shared",
            "external",
            "unfiltered",
            "no-router",
            "public-es",
        ):
            data = copy.deepcopy(original)
            groups_value = data["/v2.0/security-groups"]["security_groups"]
            if not isinstance(groups_value, list):
                raise AssertionError("expected security groups")
            groups = groups_value
            port_value = data["/v2.0/ports/ep"]["port"]
            if not isinstance(port_value, dict):
                raise AssertionError("expected port")
            port = port_value
            if mutation == "no-sg":
                groups.clear()
            elif mutation == "duplicate-sg":
                groups.append(copy.deepcopy(groups[0]))
            elif mutation == "foreign-sg":
                groups[0]["tenant_id"] = "foreign"
            elif mutation == "ingress":
                groups[0]["security_group_rules"].pop()
            elif mutation == "name":
                port["name"] = "foreign"
            elif mutation == "address":
                port["fixed_ips"] = [{"ip_address": "10.77.0.99"}]
            elif mutation == "network":
                port["network_id"] = "different"
            elif mutation == "shared":
                network = data["/v2.0/networks/net"]["network"]
                if not isinstance(network, dict):
                    raise AssertionError("expected network")
                network["shared"] = True
            elif mutation == "external":
                network = data["/v2.0/networks/net"]["network"]
                if not isinstance(network, dict):
                    raise AssertionError("expected network")
                network["router:external"] = True
            elif mutation == "unfiltered":
                network = data["/v2.0/networks/net"]["network"]
                if not isinstance(network, dict):
                    raise AssertionError("expected network")
                network["port_security_enabled"] = False
            elif mutation == "no-router":
                data["/v2.0/routers"]["routers"] = []
            elif mutation == "public-es":
                data["/v2.0/floatingips"]["floatingips"] = [{"port_id": "ep"}]
            with self.subTest(mutation=mutation), self.assertRaises(RuntimeError):
                adapter.check_network(cloud, result, ["8.8.8.8/32"])

    def test_elastic_placement_guards_reject_weak_groups_and_same_hosts(self) -> None:
        names = ("app", "es1", "es2", "es3")
        value: StandDescription = {
            "server_ids": {name: name + "-id" for name in names},
            "private_ips": {name: "10.77.0." + str(10 + i) for i, name in enumerate(names)},
            "node_roles": {name: ("es" if name.startswith("es") else "app") for name in names},
        }
        original = {
            name: {
                "id": name + "-id",
                "name": "otus-dz07-" + name,
                "status": "ACTIVE",
                "OS-EXT-AZ:availability_zone": "ru-9a",
                "hostId": "host-" + name,
            }
            for name in names
        }
        groups = [
            {
                "name": "otus-dz07-" + role,
                "policies": ["anti-affinity"],
                "members": [name + "-id" for name in names if name.startswith(role)],
            }
            for role in ("es",)
        ]
        data = copy.deepcopy(original)
        current_groups = copy.deepcopy(groups)

        def get(service: str, path: str) -> dict[str, object]:
            if path == "/os-server-groups":
                return {"server_groups": current_groups}
            return {"server": data[path.rsplit("/", 1)[1].removesuffix("-id")]}

        api = SimpleNamespace(
            cloud=Mock(return_value=Mock(get=get)),
            PREFIX="otus-dz07",
            ROOT=Path("synthetic"),
            save=Mock(),
            auth_config=lambda: {"region": "ru-9"},
            placement=lambda: {"availability_zone": "ru-9a"},
        )
        adapter.check_placement(api, value)
        self.assertEqual(api.save.call_count, 1)
        for mutation in ("status", "zone", "es-policy", "es-host", "missing-host"):
            data = copy.deepcopy(original)
            current_groups = copy.deepcopy(groups)
            api.save.reset_mock()
            if mutation == "status":
                data["app"]["status"] = "ERROR"
            elif mutation == "zone":
                data["app"]["OS-EXT-AZ:availability_zone"] = "ru-3b"
            elif mutation == "es-policy":
                current_groups[0]["policies"] = []
            elif mutation == "es-host":
                data["es2"]["hostId"] = data["es1"]["hostId"]
            elif mutation == "missing-host":
                data["es1"].pop("hostId")
            with self.subTest(mutation=mutation), self.assertRaises(RuntimeError):
                adapter.check_placement(api, value)
