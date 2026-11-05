"""Provider boundaries and fail-closed discovery; synthetic data only."""

import copy
import http.client
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from typing import TypedDict, cast
from unittest.mock import Mock, patch

from cloud_contract import (
    CapabilityStatus,
    InfrastructurePlacement,
    InfrastructureResult,
    require_capabilities,
    validate_binding,
)
from selectel_adapter import SelectelAdapter, audit_cleanup, binding, check_network, discover_capabilities, preflight


class NodeDocument(TypedDict, total=False):
    roles: list[str]
    id: str
    private_ip: str
    public_ip: str
    volume_id: str
    port_id: str


class ResultDocument(TypedDict):
    provider: str
    scope: dict[str, str]
    namespace: str
    placement: dict[str, str]
    subnet_cidr: str
    nodes: dict[str, NodeDocument]


def mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(type(key) is not str for key in value):
        raise AssertionError(value)
    return cast(dict[str, object], value)


def sequence(value: object) -> list[object]:
    if not isinstance(value, list):
        raise AssertionError(value)
    return cast(list[object], value)


def placement_from(value: object) -> InfrastructurePlacement:
    raw = mapping(value)
    region, zone = raw.get("region"), raw.get("zone")
    if not isinstance(region, str) or not isinstance(zone, str):
        raise AssertionError(value)
    placement: InfrastructurePlacement = {"region": region, "zone": zone}
    for key in ("network_zone", "router_zone"):
        if key not in raw:
            continue
        item = raw[key]
        if item is not None and not isinstance(item, str):
            raise AssertionError(value)
        if key == "network_zone":
            placement["network_zone"] = item if isinstance(item, str) else None
        else:
            placement["router_zone"] = item if isinstance(item, str) else None
    return placement


class PortabilityTests(unittest.TestCase):
    def documents(self, network: list[str] | None, compute: list[str] | None, path: str) -> dict[str, object]:
        if path == "/extensions":
            return {"extensions": [{"alias": alias} for alias in (compute or [])]}
        if path == "/v2.0/extensions":
            return {"extensions": [{"alias": alias} for alias in (network or [])]}
        if path == "/os-server-groups":
            return {"server_groups": []}
        if path == "/v2.0/security-groups":
            return {"security_groups": []}
        raise AssertionError(path)

    def cloud(self, network: list[str] | None = None, compute: list[str] | None = None) -> Mock:
        cloud = Mock()

        def get(service: str, path: str, payload: object = None) -> dict[str, object]:
            return self.documents(network, compute, path)

        cloud.get.side_effect = get
        return cloud

    def test_declared_required_properties_and_missing_extension(self) -> None:
        cloud = self.cloud(
            ["security-group", "port-security", "network_availability_zone", "router_availability_zone"],
            ["os-server-groups"],
        )
        found = discover_capabilities(cloud)
        require_capabilities(found, ["network_isolation", "hard_anti_affinity", "placement_hints"])
        self.assertEqual(found["network_isolation"].status, CapabilityStatus.SUPPORTED)
        found = discover_capabilities(self.cloud())
        self.assertEqual(found["hard_anti_affinity"].status, CapabilityStatus.UNSUPPORTED)
        with self.assertRaises(ValueError):
            require_capabilities(found, ["network_isolation"])

    def test_staged_ownership_requires_declared_network_router_tags(self) -> None:
        for aliases, expected in (
            (["standard-attr-tag"], CapabilityStatus.SUPPORTED),
            (["tag", "tag-ext"], CapabilityStatus.SUPPORTED),
            ([], CapabilityStatus.UNSUPPORTED),
        ):
            result = discover_capabilities(self.cloud(aliases))
            self.assertEqual(result["ownership_tags"].status, expected)

    def test_staged_backend_factory_delegates_to_the_selected_provider(self) -> None:
        import cloud_settings

        provider = Mock()
        api = Mock()
        with patch("cloud_settings.adapter", return_value=provider):
            self.assertIs(cloud_settings.staged_backend(api, cleanup=True), provider.staged_backend.return_value)
        provider.staged_backend.assert_called_once_with(api, cleanup=True)

    def test_404_discovery_is_unknown_but_declared_operation_error_propagates(self) -> None:
        error = urllib.error.HTTPError("https://example.invalid", 404, "Not found", http.client.HTTPMessage(), None)
        cloud = self.cloud()
        cloud.get.side_effect = error
        found = discover_capabilities(cloud)
        self.assertEqual(found["network_isolation"].status, CapabilityStatus.UNKNOWN)
        with self.assertRaises(ValueError):
            require_capabilities(found, ["network_isolation"])
        network_aliases = ["security-group", "port-security"]
        compute_aliases = ["os-server-groups"]
        cloud = self.cloud(network_aliases, compute_aliases)

        def get(service: str, path: str, payload: object = None) -> dict[str, object]:
            if path == "/os-server-groups":
                raise error
            return self.documents(network_aliases, compute_aliases, path)

        cloud.get.side_effect = get
        with self.assertRaises(urllib.error.HTTPError):
            discover_capabilities(cloud)

    def test_permission_and_network_failures_are_not_success(self) -> None:
        for error in [
            urllib.error.HTTPError("https://example.invalid", 403, "Forbidden", http.client.HTTPMessage(), None),
            TimeoutError(),
        ]:
            cloud = self.cloud()
            cloud.get.side_effect = error
            with self.assertRaises(type(error)):
                discover_capabilities(cloud)

    def test_unknown_or_unsupported_property_blocks_preflight_before_quotas(self) -> None:
        from types import SimpleNamespace

        from pool_selection import CandidateUnavailable

        root = Path(__file__).resolve().parents[1]
        api = SimpleNamespace(
            ROOT=root,
            STACK={},
            PREFIX="otus-dz07",
            cloud=Mock(),
            placement=Mock(),
            desired_topology=Mock(),
            managed_ids=Mock(),
            save=Mock(),
        )
        with patch("selectel_adapter.discover_capabilities", return_value={}), self.assertRaises(CandidateUnavailable):
            preflight(api)
        api.cloud.return_value.get.assert_not_called()
        api.save.assert_not_called()

    def test_power_checks_name_and_scope_before_mutation(self) -> None:
        cloud = SelectelAdapter.__new__(SelectelAdapter)
        cloud.config = {"project_id": "own"}
        with patch.object(
            cloud, "get", Mock(return_value={"server": {"name": "otus-dz05-db1", "tenant_id": "own"}})
        ) as get:
            with self.assertRaises(ValueError):
                cloud.set_power("db1", "vm", running=False, prefix="otus-dz07")
            self.assertEqual(get.call_count, 1)
        with patch.object(
            cloud, "get", Mock(return_value={"server": {"name": "otus-dz07-db1", "tenant_id": "foreign"}})
        ) as get:
            with self.assertRaises(ValueError):
                cloud.set_power("db1", "vm", running=False, prefix="otus-dz07")
            self.assertEqual(get.call_count, 1)

    def result(self) -> ResultDocument:
        return {
            "provider": "selectel",
            "scope": {"project_id": "synthetic"},
            "namespace": "otus-dz07",
            "placement": {"region": "ru-7", "zone": "ru-7a"},
            "subnet_cidr": "10.77.0.0/24",
            "nodes": {
                "app": {
                    "roles": ["app"],
                    "id": "app-id",
                    "private_ip": "10.77.0.10",
                    "public_ip": "192.0.2.1",
                    "volume_id": "v1",
                    "port_id": "p1",
                },
                "db1": {
                    "roles": ["es"],
                    "id": "db-id",
                    "private_ip": "10.77.0.21",
                    "volume_id": "v2",
                    "port_id": "p2",
                },
            },
        }

    def test_contract_validates_roles_addresses_and_unique_ids(self) -> None:
        result = InfrastructureResult.from_dict(self.result())
        self.assertEqual(result.application()["node_roles"]["db1"], "es")
        for field, value in [("id", "app-id"), ("private_ip", "10.77.0.10"), ("roles", [])]:
            bad = copy.deepcopy(self.result())
            node = bad["nodes"]["db1"]
            if field == "id" and isinstance(value, str):
                node["id"] = value
            elif field == "private_ip" and isinstance(value, str):
                node["private_ip"] = value
            elif field == "roles" and isinstance(value, list):
                roles = [item for item in value if isinstance(item, str)]
                if len(roles) != len(value):
                    raise AssertionError(value)
                node["roles"] = roles
            else:
                raise AssertionError((field, value))
            with self.assertRaises(ValueError):
                InfrastructureResult.from_dict(bad)

    def test_provider_scope_namespace_and_placement_cannot_switch(self) -> None:
        saved = self.result()
        own: dict[str, object] = {
            "provider": saved["provider"],
            "scope": saved["scope"],
            "namespace": saved["namespace"],
            "placement": saved["placement"],
        }
        validate_binding(own, own)
        updates: tuple[tuple[str, object], ...] = (
            ("provider", "yc"),
            ("scope", {"project_id": "foreign"}),
            ("namespace", "otus-dz05"),
            ("placement", {"region": "ru-9", "zone": "ru-9a"}),
        )
        for field, value in updates:
            other = {**own, field: value}
            with self.assertRaises(ValueError):
                validate_binding(own, other)

    def test_network_postcheck_detects_weakened_filtering_and_wrong_zone(self) -> None:
        result = InfrastructureResult.from_dict(self.result())

        def rule(
            protocol: str | None, low: int | None, high: int | None, cidr: str | None, group: str | None
        ) -> dict[str, object]:
            return {
                "direction": "ingress",
                "ethertype": "IPv4",
                "protocol": protocol,
                "port_range_min": low,
                "port_range_max": high,
                "remote_ip_prefix": cidr,
                "remote_group_id": group,
            }

        data: dict[str, object] = {
            "/v2.0/security-groups": {
                "security_groups": [
                    {
                        "name": "otus-dz07",
                        "id": "sg",
                        "security_group_rules": [
                            rule(None, None, None, None, "sg"),
                            rule("tcp", 80, 80, "0.0.0.0/0", None),
                            rule("tcp", 22, 22, "192.0.2.2/32", None),
                            rule("tcp", 5601, 5601, "192.0.2.2/32", None),
                        ],
                    }
                ]
            },
            "/v2.0/routers": {
                "routers": [
                    {"name": "otus-dz07", "availability_zone_hints": ["ru-7a"], "availability_zones": ["ru-7a"]}
                ]
            },
            "/v2.0/networks/net": {
                "network": dict(
                    shared=False,
                    port_security_enabled=True,
                    **{"router:external": False},
                    availability_zone_hints=["ru-7a"],
                    availability_zones=["ru-7a"],
                )
            },
            "/v2.0/floatingips": {"floatingips": []},
        }
        for name, node in result.nodes.items():
            data["/v2.0/ports/" + node["port_id"]] = {
                "port": {
                    "name": "otus-dz07-" + name,
                    "network_id": "net",
                    "port_security_enabled": True,
                    "security_groups": ["sg"],
                    "fixed_ips": [{"ip_address": node["private_ip"]}],
                }
            }
        cloud = Mock()
        cloud.get.side_effect = lambda service, path: data[path]
        self.assertTrue(check_network(cloud, result, ["192.0.2.2/32"])["success"])
        port = mapping(mapping(data["/v2.0/ports/p2"])["port"])
        port["port_security_enabled"] = False
        with self.assertRaises(RuntimeError):
            check_network(cloud, result, ["192.0.2.2/32"])
        port["port_security_enabled"] = True
        router = mapping(sequence(mapping(data["/v2.0/routers"])["routers"])[0])
        router["availability_zones"] = ["ru-7b"]
        with self.assertRaises(RuntimeError):
            check_network(cloud, result, ["192.0.2.2/32"])
        from dataclasses import replace

        result = replace(
            result,
            placement=placement_from(
                binding({"project_id": "synthetic", "region": "ru-3"}, "otus-dz07", "ru-3b")["placement"]
            ),
        )
        network = mapping(mapping(data["/v2.0/networks/net"])["network"])
        for resource in (router, network):
            resource.update(availability_zone_hints=["ru-3a"], availability_zones=["ru-3a"])
        self.assertTrue(check_network(cloud, result, ["192.0.2.2/32"])["success"])
        router["availability_zones"] = ["ru-3b"]
        with self.assertRaises(RuntimeError):
            check_network(cloud, result, ["192.0.2.2/32"])

    def test_cleanup_audit_does_not_match_neighbor_namespace_and_propagates_errors(self) -> None:
        cloud = Mock()
        cloud.get.return_value = {
            "servers": [{"name": "otus-dz05-db1"}, {"name": "otus-dz070-db1"}],
            "server_groups": [],
            "volumes": [],
            "networks": [],
            "subnets": [],
            "routers": [],
            "ports": [],
            "security_groups": [],
            "floatingips": [],
        }
        self.assertEqual(audit_cleanup(cloud, "otus-dz07", set()), ([], 0))
        cloud.get.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            audit_cleanup(cloud, "otus-dz07", set())

    def test_foreign_context_prevents_local_open_and_preserves_recovery_files(self) -> None:
        from local_runtime import open_portal, validate_state

        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / ".local") as directory:
            root = Path(directory)
            (root / ".local").mkdir()
            (root / "terraform-selectel").mkdir()
            result = self.result()
            state: dict[str, object] = {
                "resources": [
                    {
                        "mode": "managed",
                        "type": "openstack_compute_instance_v2",
                        "instances": [
                            {
                                "index_key": "app",
                                "attributes": {
                                    "id": "app-id",
                                    "name": "otus-dz07-app",
                                    "tenant_id": "synthetic",
                                    "region": "ru-7",
                                },
                            }
                        ],
                    }
                ],
                "outputs": {"infrastructure": {"value": result}},
            }
            (root / "terraform-selectel/terraform.tfstate").write_text(json.dumps(state))
            context: dict[str, object] = {
                "provider": "yc",
                "scope": result["scope"],
                "namespace": result["namespace"],
                "placement": result["placement"],
            }
            (root / ".local/context.json").write_text(json.dumps(context))
            key = root / ".local/key"
            key.write_text("synthetic")
            with self.assertRaises(ValueError):
                validate_state(root, {"project_id": "synthetic", "region": "ru-7"}, "otus-dz07")
            opener = Mock()
            with self.assertRaises(ValueError):
                open_portal(root, 6, opener)
            opener.assert_not_called()
            self.assertTrue(key.exists())

    def test_foreign_provider_state_blocks_cycle_and_local_cleanup(self) -> None:
        from types import SimpleNamespace

        import test_cycle
        from local_runtime import cleanup_runtime

        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / ".local") as directory:
            root = Path(directory)
            (root / ".local").mkdir()
            (root / "terraform-selectel").mkdir()
            (root / "terraform-other").mkdir()
            state = {"resources": [{"mode": "managed", "instances": [{"attributes": {"id": "foreign"}}]}]}
            (root / "terraform-other/terraform.tfstate").write_text(json.dumps(state))
            key = root / ".local/key"
            key.write_text("synthetic")
            api = SimpleNamespace(ROOT=root, LOCAL=root / ".local", managed_ids=Mock(return_value={}), cloud=Mock())
            with self.assertRaises(RuntimeError):
                test_cycle.reject_existing(api)
            api.cloud.assert_not_called()
            with self.assertRaises(ValueError):
                cleanup_runtime(root)
            self.assertTrue(key.exists())

    def test_pool_selection_never_overwrites_foreign_provider_context(self) -> None:
        import pool_selection

        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / ".local") as directory:
            root = Path(directory)
            (root / ".local").mkdir()
            (root / "terraform-selectel").mkdir()
            saved = {
                "project_id": "synthetic",
                "provider": "other",
                "region": "ru-7",
                "phase": "prepared",
                "namespace": "otus-dz07",
            }
            saved_result = self.result()
            context: dict[str, object] = {
                "provider": "other",
                "scope": saved_result["scope"],
                "namespace": saved_result["namespace"],
                "placement": saved_result["placement"],
            }
            (root / ".local/placement.json").write_text(json.dumps(saved))
            path = root / ".local/context.json"
            path.write_text(json.dumps(context))
            original = path.read_bytes()
            with self.assertRaises(ValueError):
                pool_selection.select(
                    root, {"region": "ru-7", "availability_zone": "ru-7a"}, "synthetic", "context.json"
                )
            self.assertEqual(path.read_bytes(), original)

    def test_collectstatic_runs_as_application_user_without_ansible_temp_acl(self) -> None:
        import yaml

        root = Path(__file__).resolve().parents[1]
        tasks = yaml.safe_load((root / "ansible/roles/portal/tasks/main.yml").read_text())
        task = next(t for t in tasks if t["name"] == "Collect static assets")
        self.assertNotIn("become_user", task)
        self.assertEqual(
            task["ansible.builtin.command"]["argv"][:4],
            ["/usr/sbin/runuser", "-u", "{{ stack.portal.service_user }}", "--"],
        )
