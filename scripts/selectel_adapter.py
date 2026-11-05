"""Selectel-specific authentication, discovery, placement and resource API operations."""

import json
import re
import time
import urllib.error
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, Self

import pool_selection as pools
import topology
from cloud_contract import Capability, CapabilityStatus, InfrastructureResult
from placement_config import PlacementPolicy, VMProfile
from selectel_api import (
    authenticate,
    identifier_set,
    json_int,
    json_object,
    json_rows,
    json_text,
    json_texts,
    request_json,
    text_mapping,
)
from selectel_reports import EmptyNamespaceAudit
from stack_types import StackSettings

if TYPE_CHECKING:
    from cloud_contract import StandDescription
    from runtime_ports import ConfigurationPort, FilePort, RootPort, StagedControllerPort
    from selectel_staged import Backend
    from selectel_staged_audit import PageClient
    from stage_store import StageStore


def sequential_enabled(root: Path) -> bool:
    from selectel_staged import enabled

    return enabled(root)


def staged_backend(api: "StagedControllerPort", cleanup: bool = False) -> "Backend":
    from selectel_staged import Backend

    return Backend(api, cleanup=cleanup)


def reject_shared_namespace(api: "FilePort") -> None:
    from selectel_staged import reject_shared_namespace as reject

    return reject(api)


def audit_service_exclusions(root: Path) -> dict[str, list[str]]:
    """Explicit scope policy, never inference that an absent endpoint means empty."""
    path = Path(root) / "config/cloud.json"
    config = json.loads(path.read_text()).get("audit", {}) if path.exists() else {}
    values = config.get("service_exclusions", {})
    if not isinstance(values, dict) or set(values) - {"volumev3"}:
        raise ValueError("Invalid Selectel audit service exclusions")
    result: dict[str, list[str]] = {}
    for service, regions in values.items():
        if (
            not isinstance(regions, list)
            or any(not isinstance(r, str) or not r for r in regions)
            or len(set(regions)) != len(regions)
        ):
            raise ValueError("Invalid Selectel audit exclusion pools")
        for region in regions:
            result.setdefault(region, []).append(service)
    return result


def audit_empty_stages(api: "FilePort") -> EmptyNamespaceAudit:
    from selectel_staged import audit_empty_scope

    return audit_empty_scope(api)


def recover_stages(api: "FilePort", error: BaseException) -> None:
    from selectel_staged import recovery_search

    return recovery_search(api, error)


def validate_staged_local(
    api: "RootPort", result: InfrastructureResult, journal: Mapping[str, object], store: "StageStore"
) -> None:
    from selectel_staged import validate_local_result

    return validate_local_result(api, result, journal, store)


def binding(config: Mapping[str, str], namespace: str, zone: str | None) -> dict[str, object]:
    identity: dict[str, object] = {"region": config["region"], "zone": zone}
    policy = json.loads((Path(__file__).resolve().parents[1] / "config/placement.json").read_text())
    domains = pools.network_placement(policy, config["region"], zone)
    if domains["network_availability_zone"] != zone or domains["router_availability_zone"] != zone:
        identity.update(
            network_zone=domains["network_availability_zone"], router_zone=domains["router_availability_zone"]
        )
    return {
        "provider": "selectel",
        "scope": {"project_id": config["project_id"]},
        "namespace": namespace,
        "placement": identity,
    }


def partial_state_binding(root: Path, config: Mapping[str, str], prefix: str) -> dict[str, object]:
    """A failed apply may have no outputs; require the original preparation records."""
    from cloud_contract import validate_binding
    from cloud_settings import terraform_root
    from local_runtime import checked_tree, read

    paths = [
        root / ".local/context.json",
        root / ".local/placement.json",
        terraform_root(root) / "terraform.tfvars.json",
    ]
    for path in paths:
        checked_tree(path, root.resolve())
        if not path.is_file():
            raise ValueError("Partial state binding requires context, placement and tfvars; preserve recovery files")
    context, placement, variables = [read(p) for p in paths]
    zone = variables.get("availability_zone")
    if not isinstance(zone, str) or not zone:
        raise ValueError("Partial state binding has no segment")
    expected = binding(config, prefix, zone)
    validate_binding(context, expected)
    saved = {
        "provider": placement.get("provider"),
        "namespace": placement.get("namespace"),
        "scope": {"project_id": placement.get("project_id")},
        "placement": {"region": placement.get("region"), "zone": placement.get("availability_zone")},
    }
    saved_placement = json_object(saved["placement"], "Unknown placement binding")
    expected_placement = json_object(expected["placement"], "Unknown placement binding")
    if "network_zone" in expected_placement:
        saved_placement.update(
            network_zone=placement.get("network_availability_zone"),
            router_zone=placement.get("router_availability_zone"),
        )
        if (
            variables.get("network_availability_zone") != saved_placement["network_zone"]
            or variables.get("router_availability_zone") != saved_placement["router_zone"]
        ):
            raise ValueError("Partial state network scheduling domains conflict with saved placement")
    validate_binding(saved, expected)
    if variables.get("region") != config["region"] or variables.get("name_prefix") != prefix:
        raise ValueError("Partial state binding conflicts with Terraform variables")
    return expected


class SelectelAdapter:
    provider = "selectel"

    def __init__(self, config: Mapping[str, str]) -> None:
        self.config = config
        self.token, catalog = authenticate(self.config)
        self.catalog = catalog
        self.endpoints = {
            s["type"]: e["url"].rstrip("/")
            for s in catalog
            for e in s["endpoints"]
            if e.get("interface") == "public"
            and e.get("region") == self.config["region"]
            and e["url"].startswith("https://")
        }

    def read_only_pool(self, region: str) -> Self:
        other = type(self).__new__(type(self))
        other.config = {**self.config, "region": region}
        other.token, other.catalog = self.token, self.catalog
        other.endpoints = {
            s["type"]: e["url"].rstrip("/")
            for s in self.catalog
            for e in s["endpoints"]
            if e.get("interface") == "public" and e.get("region") == region and e["url"].startswith("https://")
        }
        return other

    def resource_regions(self) -> list[str]:
        regions: set[str] = set()
        for service in self.catalog:
            if service["type"] not in ("compute", "volumev3", "network"):
                continue
            for endpoint in service["endpoints"]:
                if endpoint.get("interface") != "public":
                    continue
                region = endpoint.get("region")
                if not isinstance(region, str) or not region:
                    raise ValueError("Resource API audit pool identity is unconfirmed")
                regions.add(region)
        return sorted(regions)

    @staticmethod
    def cleanup_ids(state: Mapping[str, object]) -> set[str]:
        identifiers: set[str] = set()
        for resource in json_rows(state.get("resources", []), "Unknown Terraform state"):
            if resource.get("mode") != "managed" or resource.get("type") != "openstack_networking_floatingip_v2":
                continue
            for item in json_rows(resource.get("instances", []), "Unknown Terraform state"):
                attributes = json_object(item.get("attributes", {}), "Unknown Terraform state")
                identifiers.update(identifier_set(attributes.get("id"), "Unknown Terraform state"))
        return identifiers

    def get(self, service: str, path: str, payload: object = None) -> dict[str, object]:
        # Keep retry policy in the shared HTTPS client, including catalog reads.
        # Mutating requests are never retried automatically.
        return json_object(
            request_json(self.endpoints[service] + path, token=self.token, payload=payload)[0],
            "Unknown OpenStack response object",
        )

    def set_power(self, name: str, server_id: str, *, running: bool, prefix: str) -> None:
        server = json_object(self.get("compute", "/servers/" + server_id)["server"])
        if server["name"] != prefix + "-" + name or server.get("tenant_id") != self.config["project_id"]:
            raise ValueError("Refusing foreign instance power action")
        self.get("compute", "/servers/" + server_id + "/action", {"os-start" if running else "os-stop": None})
        target = "ACTIVE" if running else "SHUTOFF"
        for _ in range(60):
            if json_object(self.get("compute", "/servers/" + server_id)["server"]).get("status") == target:
                return
            time.sleep(2)
        raise RuntimeError("Instance power transition unconfirmed")

    def ssh_host_key(self, name: str, server_id: str, prefix: str, cached: str | None) -> str:
        server = json_object(self.get("compute", "/servers/" + server_id)["server"])
        if server["name"] != prefix + "-" + name:
            raise ValueError("Foreign instance UUID")
        if cached is not None:
            return cached
        for _ in range(60):
            console = json_text(
                json_object(
                    self.get("compute", "/servers/" + server_id + "/action", {"os-getConsoleOutput": {"length": 1000}})
                )["output"]
            )
            section = re.search(r"BEGIN SSH HOST KEY KEYS.*?END SSH HOST KEY KEYS", console, re.S)
            match = re.search(r"ssh-ed25519\s+([A-Za-z0-9+/=]+)", section.group()) if section else None
            if match:
                return match[1]
            time.sleep(3)
        raise RuntimeError("No trusted SSH key for " + name)


def owned(name: str, prefix: str) -> bool:
    return name == prefix or name.startswith(prefix + "-")


def namespace_match(item: Mapping[str, object], prefix: str) -> bool:
    metadata, tags = item.get("metadata") or {}, item.get("tags") or []
    if not isinstance(metadata, dict) or not isinstance(tags, list):
        raise ValueError("Unknown ownership marker shape")
    markers = [item.get("name"), item.get("description"), metadata.get("dz07_owner")]
    markers += tags
    return any(isinstance(value, str) and owned(value, prefix) for value in markers)


OWNED_COLLECTIONS = (
    ("compute", "/servers/detail", "servers"),
    ("compute", "/os-server-groups", "server_groups"),
    ("volumev3", "/volumes/detail", "volumes"),
    ("network", "/v2.0/networks", "networks"),
    ("network", "/v2.0/subnets", "subnets"),
    ("network", "/v2.0/routers", "routers"),
    ("network", "/v2.0/ports", "ports"),
    ("network", "/v2.0/security-groups", "security_groups"),
)


def collection(cloud: "PageClient", service: str, path: str, key: str) -> list[dict[str, object]]:
    """Read a complete OpenStack collection; a partial page is not an empty result."""
    from selectel_staged_audit import objects

    return objects(cloud, service, path, key)


def audit_cleanup(cloud: "PageClient", prefix: str, own_fips: set[str]) -> tuple[list[str], int]:
    deadline = time.monotonic() + 90
    while True:
        remaining: list[str] = []
        for service, path, key in OWNED_COLLECTIONS:
            for item in collection(cloud, service, path, key):
                name = json_text(item.get("name", ""), "Unknown cleanup resource name; audit incomplete")
                if owned(name, prefix):
                    remaining.append(name)
        floating = collection(cloud, "network", "/v2.0/floatingips", "floatingips")
        count = sum(isinstance(item, dict) and item.get("id") in own_fips for item in floating)
        if (not remaining and not count) or time.monotonic() >= deadline:
            return remaining, count
        time.sleep(3)


def _extensions(cloud: "PageClient", service: str, path: str) -> set[str] | None:
    try:
        value = cloud.get(service, path)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        return None
    return {
        json_text(item.get("alias"), "Malformed capability discovery response")
        for item in json_rows(value.get("extensions"), "Malformed capability discovery response")
    }


def discover_capabilities(cloud: "PageClient") -> dict[str, Capability]:
    """Use Neutron extension aliases and Nova's documented server-group API."""
    network = _extensions(cloud, "network", "/v2.0/extensions")
    compute = _extensions(cloud, "compute", "/extensions")
    definitions = {
        "network_isolation": (network, {"security-group", "port-security"}),
        "placement_hints": (network, {"network_availability_zone", "router_availability_zone"}),
        "hard_anti_affinity": (compute, {"os-server-groups"}),
    }
    result = {}
    for name, (aliases, required) in definitions.items():
        status = (
            CapabilityStatus.UNKNOWN
            if aliases is None
            else (CapabilityStatus.SUPPORTED if required <= aliases else CapabilityStatus.UNSUPPORTED)
        )
        result[name] = Capability(
            status,
            "Extension discovery unavailable"
            if aliases is None
            else "Declared extensions: " + ", ".join(sorted(required)),
        )
    tags = network is not None and ("standard-attr-tag" in network or {"tag", "tag-ext"} <= network)
    status = (
        CapabilityStatus.UNKNOWN
        if network is None
        else CapabilityStatus.SUPPORTED
        if tags
        else CapabilityStatus.UNSUPPORTED
    )
    result["ownership_tags"] = Capability(status, "Neutron ownership tags on networks and routers")
    # A declared extension does not permit suppressing an operation's 404/403/timeout.
    if result["network_isolation"].status == CapabilityStatus.SUPPORTED:
        cloud.get("network", "/v2.0/security-groups")["security_groups"]
    if result["hard_anti_affinity"].status == CapabilityStatus.SUPPORTED:
        cloud.get("compute", "/os-server-groups")["server_groups"]
    return result


def infrastructure_result(
    output: Mapping[str, object], config: Mapping[str, str], namespace: str, zone: str
) -> InfrastructureResult:
    server_ids = text_mapping(output["server_ids"], "Unknown infrastructure output")
    private_ips = text_mapping(output["private_ips"], "Unknown infrastructure output")
    volume_ids = text_mapping(output["volume_ids"], "Unknown infrastructure output")
    port_ids = text_mapping(output["port_ids"], "Unknown infrastructure output")
    roles = text_mapping(output.get("node_roles", {}), "Unknown infrastructure output")
    nodes: dict[str, dict[str, object]] = {}
    for name, server_id in server_ids.items():
        role = roles.get(name)
        if role is None:
            role = "app" if name == "app" else "es" if name.startswith("es") else "unsupported"
        nodes[name] = {
            "id": server_id,
            "roles": [role],
            "private_ip": private_ips[name],
            "volume_id": volume_ids[name],
            "port_id": port_ids[name],
        }
        if name == "app":
            nodes[name]["public_ip"] = json_text(output["app_public_ip"], "Unknown infrastructure output")
    return InfrastructureResult.from_dict(
        {
            "provider": "selectel",
            "scope": {"project_id": config["project_id"]},
            "namespace": namespace,
            "placement": binding(config, namespace, zone)["placement"],
            "nodes": nodes,
            "subnet_cidr": json_text(output["subnet_cidr"], "Unknown infrastructure output"),
        }
    )


class PreflightPort(Protocol):
    ROOT: Path
    STACK: StackSettings
    PREFIX: str

    def cloud(self, bind: bool = ...) -> SelectelAdapter: ...
    def placement(self) -> PlacementPolicy: ...
    def desired_topology(self) -> topology.Topology: ...
    def managed_ids(self) -> object: ...
    def save(self, path: Path, value: object) -> None: ...


class PlacementCheckPort(Protocol):
    ROOT: Path
    PREFIX: str

    def cloud(self, bind: bool = ...) -> SelectelAdapter: ...
    def placement(self) -> PlacementPolicy: ...
    def save(self, path: Path, value: object) -> None: ...


def vm_profile(value: object) -> VMProfile:
    row = json_object(value, "Unknown VM profile")
    return {
        "vcpus": json_int(row.get("vcpus"), "Unknown VM profile"),
        "ram_mb": json_int(row.get("ram_mb"), "Unknown VM profile"),
    }


def preflight(api: PreflightPort) -> None:
    project_root, stack_settings, namespace = api.ROOT, api.STACK, api.PREFIX
    cloud_factory, placement, desired_topology = api.cloud, api.placement, api.desired_topology
    managed_ids, save = api.managed_ids, api.save
    cloud = cloud_factory()
    required = json.loads((project_root / "config/cloud.json").read_text())["required_capabilities"]
    from cloud_contract import require_capabilities

    capabilities = discover_capabilities(cloud)
    try:
        require_capabilities(capabilities, required)
    except ValueError as error:
        raise pools.CandidateUnavailable(str(error)) from error
    project = cloud.config["project_id"]
    place = placement()
    quota_set = json_object(
        cloud.get("compute", "/os-quota-sets/" + project + "/detail")["quota_set"], "Unknown quota set"
    )
    zone = json_text(place.get("availability_zone"), "Compute quotas unavailable in candidate segment")
    compute_quota = json_object(quota_set.get("az_quotas", {}), "Unknown quota set").get(zone)
    if compute_quota is None:
        raise pools.CandidateUnavailable("Compute quotas unavailable in candidate segment")
    compute = json_object(compute_quota, "Unknown quota set")
    volumes = json_object(
        cloud.get("volumev3", "/os-quota-sets/" + project + "?usage=True")["quota_set"], "Unknown quota set"
    )
    network = json_object(
        cloud.get("network", "/v2.0/quotas/" + project + "/details.json")["quota"], "Unknown quota set"
    )
    saved_profiles = json_object(place.get("profiles"), "Unknown VM profile")
    profiles = {name: vm_profile(saved_profiles[node["role"]]) for name, node in desired_topology()["nodes"].items()}
    demand = pools.remaining_demand(
        project_root,
        {
            "cores": sum(profile["vcpus"] for profile in profiles.values()),
            "ram": sum(profile["ram_mb"] for profile in profiles.values()),
            "instances": len(profiles),
            "disk_gb": json_int(place.get("boot_disk_gb"), "Unknown boot disk") * len(profiles),
            "volumes": len(profiles),
            "floatingip": 1,
            "network": 1,
            "subnet": 1,
            "router": 1,
            "security_group": 1,
            "security_group_rule": 2 + 2 * max(1, len(place.get("admin_cidrs", []))),
            "port": len(profiles) + 3,
        },
        profiles,
    )
    volume_type = json_text(place.get("volume_type"), "Unknown volume type")
    for key in ("cores", "ram", "instances"):
        pools.capacity(compute.get(key), demand[key])
    pools.capacity(volumes.get("gigabytes_" + volume_type), demand["disk_gb"])
    for key in ("gigabytes", "volumes"):
        if key in volumes:
            pools.capacity(volumes[key], demand["disk_gb" if key == "gigabytes" else "volumes"])
    for key in ("floatingip", "network", "subnet", "router", "security_group", "security_group_rule", "port"):
        pools.capacity(network.get(key), demand[key])
    subnets = json_rows(cloud.get("network", "/v2.0/subnets")["subnets"], "Unknown OpenStack collection shape")
    if not managed_ids():
        from pool_selection import reject_orphans

        reject_orphans(cloud, namespace, subnets)
    import ipaddress

    if any(
        ipaddress.ip_network(json_text(subnet["cidr"], "Unknown subnet CIDR")).overlaps(
            ipaddress.ip_network(stack_settings["subnet_cidr"])
        )
        for subnet in subnets
        if subnet.get("ip_version") == 4
        and not json_text(subnet.get("name", ""), "Unknown subnet name").startswith(namespace)
    ):
        raise pools.CandidateUnavailable("DZ07 subnet overlaps an existing subnet")
    report = {
        "region": json_text(place.get("region"), "Unknown pool"),
        "availability_zone": zone,
        "quotas_sufficient": True,
        "subnet_no_conflict": True,
        "capabilities": {name: value.status.value for name, value in capabilities.items()},
    }
    save(project_root / "docs/preflight.json", report)
    print("Free quotas checked, including concurrent project usage", flush=True)


def check_placement(api: "ConfigurationPort", lab: "StandDescription") -> None:
    cloud_factory, namespace, save, project_root = api.cloud, api.PREFIX, api.save, api.ROOT
    cloud = cloud_factory()
    expected_zone = api.placement().get("availability_zone")
    servers: dict[str, dict[str, object]] = {}
    for name, identifier in lab["server_ids"].items():
        server = json_object(cloud.get("compute", "/servers/" + identifier)["server"], "Unknown server")
        if (
            server.get("name") != namespace + "-" + name
            or server.get("status") != "ACTIVE"
            or server.get("OS-EXT-AZ:availability_zone") != expected_zone
        ):
            raise RuntimeError("Actual VM identity, state or compute segment unconfirmed")
        servers[name] = server
    elastic = topology.elastic_names(lab)
    if elastic:
        expected = {lab["server_ids"][name] for name in elastic}
        group = [
            item
            for item in json_rows(cloud.get("compute", "/os-server-groups")["server_groups"], "Unknown server groups")
            if item.get("name") == namespace + "-es"
            and set(json_texts(item.get("members", []), "Unknown server group members")) == expected
        ]
        if len(group) != 1 or group[0].get("policies") != ["anti-affinity"]:
            raise RuntimeError("Elasticsearch hard anti-affinity group unconfirmed")
        physical: list[str | None] = []
        for name in elastic:
            host = servers[name].get("hostId")
            physical.append(host if isinstance(host, str) and host else None)
        if not all(physical) or len(set(physical)) != len(elastic):
            raise RuntimeError("Elasticsearch nodes must use distinct physical hosts")
        save(
            project_root / "docs/elastic-placement.json",
            {"success": True, "nodes": len(elastic), "distinct_hosts": len(set(physical))},
        )


def check_network(cloud: "PageClient", result: InfrastructureResult, admin_cidrs: list[str]) -> dict[str, bool]:
    """Confirm actual filtering, private ports and both network/router placement."""
    zone, prefix = result.placement["zone"], result.namespace
    groups = [
        item
        for item in json_rows(
            cloud.get("network", "/v2.0/security-groups")["security_groups"], "Unknown security groups"
        )
        if item.get("name") == prefix
        and item.get("tenant_id", result.scope["project_id"]) == result.scope["project_id"]
    ]
    if len(groups) != 1:
        raise RuntimeError("Own security group identity unconfirmed")
    group = groups[0]
    actual = {
        (
            rule.get("ethertype"),
            rule.get("protocol"),
            rule.get("port_range_min"),
            rule.get("port_range_max"),
            rule.get("remote_ip_prefix"),
            rule.get("remote_group_id"),
        )
        for rule in json_rows(group["security_group_rules"], "Unknown security group rules")
        if rule["direction"] == "ingress"
    }
    expected = {("IPv4", None, None, None, None, group["id"]), ("IPv4", "tcp", 80, 80, "0.0.0.0/0", None)}
    expected |= {("IPv4", "tcp", 22, 22, cidr, None) for cidr in admin_cidrs}
    if any("es" in n["roles"] for n in result.nodes.values()):
        from topology import services

        service_port = services(Path(__file__).resolve().parents[1])["kibana_port"]
        expected |= {("IPv4", "tcp", service_port, service_port, cidr, None) for cidr in admin_cidrs}
    if actual != expected:
        raise RuntimeError("Actual ingress rules differ from required network isolation")
    networks, internal_ports = set(), set()
    for name, node in result.nodes.items():
        port = json_object(cloud.get("network", "/v2.0/ports/" + node["port_id"])["port"], "Unknown port")
        if (
            port["name"] != prefix + "-" + name
            or port.get("port_security_enabled") is not True
            or port["security_groups"] != [group["id"]]
        ):
            raise RuntimeError("Node port filtering or identity unconfirmed")
        if {address["ip_address"] for address in json_rows(port["fixed_ips"], "Unknown port addresses")} != {
            node["private_ip"]
        }:
            raise RuntimeError("Unexpected private port addresses")
        networks.add(port["network_id"])
        if "app" not in node["roles"]:
            internal_ports.add(node["port_id"])
    if len(networks) != 1:
        raise RuntimeError("Nodes do not share the required private network")
    network_id = json_text(networks.pop(), "Unknown network")
    network = json_object(cloud.get("network", "/v2.0/networks/" + network_id)["network"], "Unknown network")
    routers = [
        item
        for item in json_rows(cloud.get("network", "/v2.0/routers")["routers"], "Unknown routers")
        if item.get("name") == prefix
    ]
    if (
        network.get("shared") is not False
        or network.get("router:external") is not False
        or network.get("port_security_enabled") is not True
    ):
        raise RuntimeError("Private network isolation unconfirmed")
    expected_domains = [result.placement.get("network_zone", zone), result.placement.get("router_zone", zone)]
    if len(routers) != 1 or any(
        set(json_texts(item.get("availability_zone_hints", []), "Unknown availability zones")) != {expected}
        or set(json_texts(item.get("availability_zones", []), "Unknown availability zones")) != {expected}
        for item, expected in zip((network, *routers), expected_domains, strict=False)
    ):
        raise RuntimeError("Actual network/router placement unconfirmed")
    floating = json_rows(cloud.get("network", "/v2.0/floatingips")["floatingips"], "Unknown floating addresses")
    if any(item.get("port_id") in internal_ports for item in floating):
        raise RuntimeError("Internal server ports must not have public addresses")
    return {
        "success": True,
        "private_ports_filtered": True,
        "ingress_rules_confirmed": True,
        "internal_public_ips_absent": True,
        "network_router_zone_confirmed": True,
    }


def reject_cloud_resources(api: "FilePort", cloud: "PageClient") -> None:
    for service, url, key in OWNED_COLLECTIONS:
        if service not in cloud.endpoints:
            continue  # Audit every available service; image discovery is not needed for reads.
        try:
            items = collection(cloud, service, url, key)
        except urllib.error.HTTPError as error:
            if service != "network" or url != "/v2.0/security-groups" or error.code != 404:
                raise
            extensions = json_rows(
                cloud.get("network", "/v2.0/extensions")["extensions"], "Malformed capability discovery response"
            )
            if any(item["alias"] == "security-group" for item in extensions):
                raise
            continue  # The catalog can expose Neutron without security-group support.
        if any(isinstance(item, dict) and namespace_match(item, api.PREFIX) for item in items):
            raise RuntimeError("Existing DZ07 cloud resources require separate recovery")


def validate_node_creation(change: Mapping[str, object], allowed: set[str]) -> None:
    kinds = {
        "openstack_compute_instance_v2": "node",
        "openstack_networking_port_v2": "node",
        "openstack_blockstorage_volume_v3": "boot",
    }
    kind = change.get("type")
    index = change.get("index")
    if (
        not isinstance(kind, str)
        or kind not in kinds
        or change.get("name") != kinds[kind]
        or not isinstance(index, str)
        or index not in allowed
    ):
        address = change.get("address")
        label = address if isinstance(address, str) else "[unconfirmed address]"
        raise ValueError("Extension may only create resources of new nodes: " + label)


def openstack_type(value: object) -> str:
    if not isinstance(value, str) or not value.startswith("openstack_"):
        raise ValueError("Foreign provider in state")
    return value


def validate_managed_state(state: Mapping[str, object], config: Mapping[str, str], prefix: str) -> None:
    for resource in json_rows(state.get("resources", []), "Unknown Terraform state"):
        if resource.get("mode") != "managed" or not resource.get("instances"):
            continue
        openstack_type(resource.get("type", ""))
        for item in json_rows(resource["instances"], "Unknown Terraform state"):
            attributes = json_object(item.get("attributes", {}), "Unknown Terraform state")
            name = attributes.get("name")
            if name and name != prefix and not json_text(name, "Foreign resource in state").startswith(prefix + "-"):
                raise ValueError("Foreign resource in state")
            if attributes.get("tenant_id") and attributes["tenant_id"] != config["project_id"]:
                raise ValueError("Foreign project in state")
            if attributes.get("region") and attributes["region"] != config["region"]:
                raise ValueError("Foreign pool in state")
