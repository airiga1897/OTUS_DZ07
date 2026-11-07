"""Actual API ownership, placement and deletion checks for one retained stage."""

import ipaddress
import time
import urllib.error
import urllib.parse
from collections.abc import Callable, Mapping
from functools import partial
from typing import TYPE_CHECKING, Protocol

from operator_network import allowed_cidrs

if TYPE_CHECKING:
    from selectel_staged import Backend

from progress_logging import get_logger, timed
from selectel_api import json_int, json_object, json_rows, json_text, json_texts
from selectel_reports import DeletionAudit, NetworkVerification, StageFaults
from stage_journal import StageRecord
from terraform_state_types import StateEntry

LOGGER = get_logger(__name__)

GLOBAL = {
    "selectel_global_router_router_v1": "routers",
    "selectel_global_router_vpc_network_v1": "networks",
    "selectel_global_router_vpc_subnet_v1": "subnets",
}
COLLECTIONS = {
    "openstack_compute_instance_v2": ("compute", "/servers/detail", "servers"),
    "openstack_compute_servergroup_v2": ("compute", "/os-server-groups", "server_groups"),
    "openstack_blockstorage_volume_v3": ("volumev3", "/volumes/detail", "volumes"),
    "openstack_networking_port_v2": ("network", "/v2.0/ports", "ports"),
    "openstack_networking_network_v2": ("network", "/v2.0/networks", "networks"),
    "openstack_networking_subnet_v2": ("network", "/v2.0/subnets", "subnets"),
    "openstack_networking_router_v2": ("network", "/v2.0/routers", "routers"),
    "openstack_networking_secgroup_v2": ("network", "/v2.0/security-groups", "security_groups"),
    "openstack_networking_secgroup_rule_v2": ("network", "/v2.0/security-group-rules", "security_group_rules"),
    "openstack_networking_floatingip_v2": ("network", "/v2.0/floatingips", "floatingips"),
}
RELATIONSHIPS = {
    "openstack_networking_router_interface_v2",
    "openstack_networking_subnet_route_v2",
    "openstack_networking_floatingip_associate_v2",
}


class PageClient(Protocol):
    endpoints: dict[str, str]

    def get(self, service: str, path: str, payload: object = None) -> Mapping[str, object]: ...


def objects(cloud: PageClient, service: str, path: str, key: str) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    seen: set[str] = set()
    url: str | None = path + "?limit=200"
    while url:
        if url in seen:
            raise ValueError("Cyclic OpenStack pagination")
        seen.add(url)
        page = cloud.get(service, url)
        values = json_rows(page[key], "Unknown OpenStack collection shape")
        result.extend(values)
        links = [
            json_text(link["href"], "Unsafe OpenStack pagination URL")
            for link in json_rows(page.get(key + "_links", []), "Collection completeness is unconfirmed")
            if link.get("rel") == "next"
        ]
        if len(links) > 1 or (len(values) >= 200 and not links):
            raise ValueError("Collection completeness is unconfirmed")
        url = None
        if links:
            endpoint = urllib.parse.urlsplit(cloud.endpoints[service])
            following = urllib.parse.urlsplit(urllib.parse.urljoin(cloud.endpoints[service] + "/", links[0]))
            if (
                following.scheme != "https"
                or following.netloc != endpoint.netloc
                or not following.path.startswith(endpoint.path.rstrip("/") + "/")
            ):
                raise ValueError("Unsafe OpenStack pagination URL")
            url = following.path[len(endpoint.path.rstrip("/")) :] + "?" + following.query
    return result


def inventory(backend: "Backend", name: str, entries: list[StateEntry]) -> dict[str, dict[str, dict[str, object]]]:
    cloud = None
    found: dict[str, dict[str, dict[str, object]]] = {}
    from selectel_intent import validate as validate_intent

    intent = validate_intent(backend, name)
    kinds = {e["type"] for e in entries} | set(intent["resource_types"] if intent else [])
    for kind in kinds:
        if kind in COLLECTIONS:
            if cloud is None:
                cloud = backend.region(
                    json_text(backend.journal["stages"][name]["candidate"]["region"], "Stage pool is unconfirmed")
                )
            service, url, key = COLLECTIONS[kind]
            found[kind] = {
                json_text(row["id"], "Unknown OpenStack resource identity"): row
                for row in objects(cloud, service, url, key)
            }
            # The collection proves API support; a documented GET by saved ID
            # distinguishes actual absence from a record outside a collection page.
            for entry in entries:
                if entry["type"] != kind:
                    continue
                identifier = json_text(entry["attrs"]["id"], "Unknown stage resource identity")
                if identifier in found[kind]:
                    continue
                direct = url.removesuffix("/detail") + "/" + identifier
                try:
                    item = json_object(cloud.get(service, direct)[key[:-1]], "Unknown OpenStack response object")
                    found[kind][identifier] = item
                except urllib.error.HTTPError as error:
                    if error.code != 404:
                        raise
        elif kind in GLOBAL:
            found[kind] = {
                json_text(row["id"], "Unknown Global Router resource identity"): row
                for row in backend.global_router.listing(GLOBAL[kind])
            }
        elif kind not in RELATIONSHIPS:
            raise ValueError("Unknown stage resource cannot be audited: " + kind)
    return found


def audit_namespace(backend: "Backend", pool: str) -> None:
    from selectel_adapter import namespace_match

    cloud = backend.region(pool)
    for service, path, key in COLLECTIONS.values():
        for item in objects(cloud, service, path, key):
            if namespace_match(item, backend.api.PREFIX):
                raise RuntimeError("Deployment namespace is not empty in " + pool)


def assert_stage_owned(
    backend: "Backend", name: str, active: bool = False, missing_access_ids: frozenset[str] = frozenset()
) -> None:
    record = backend.journal["stages"][name]
    if record.get("lineage") and backend.store.state(name).get("lineage") != record["lineage"]:
        raise ValueError("Stage state lineage changed")
    if active and "identities" in record and backend.store.identities(name) != record["identities"]:
        raise ValueError("Saved stage identities differ from state")
    entries = backend.store.entries(name)
    found = inventory(backend, name, entries)
    from selectel_intent import check_unrecorded

    check_unrecorded(backend, name, entries, found)
    for entry in entries:
        kind, attrs = entry["type"], entry["attrs"]
        if kind in RELATIONSHIPS:
            continue  # The associated objects are independently checked below/in their own stage.
        item = found[kind].get(json_text(attrs["id"], "Unknown stage resource identity"))
        if item is None:
            if kind == "openstack_networking_secgroup_rule_v2" and attrs["id"] in missing_access_ids:
                continue
            known_deleted = record["status"] == "deleting" and any(
                e["type"] == kind and e["attrs"].get("id") == attrs["id"] for e in record.get("audit_entries", [])
            )
            if not active and known_deleted:
                continue
            raise ValueError("Stage resource missing in API; explicit recovery required")
        if kind in GLOBAL:
            if backend.owner not in json_texts(item.get("tags", [])):
                raise ValueError("Global resource tag mismatch")
            if kind == "selectel_global_router_vpc_subnet_v1":
                # The API can return project_id=null for a subnet. Its project
                # is then proven through the tagged parent VPC network.
                project = backend.credentials["project_id"]
                parents = found.get("selectel_global_router_vpc_network_v1")
                if parents is None:
                    parents = {
                        json_text(p["id"], "Unknown Global Router resource identity"): p
                        for p in backend.global_router.listing("networks")
                    }
                network_id = item.get("network_id")
                parent = parents.get(network_id, {}) if isinstance(network_id, str) else {}
                if (
                    item.get("project_id") not in (None, "", project)
                    or item.get("network_id") != attrs.get("network_id")
                    or item.get("os_subnet_id") != attrs.get("os_subnet_id")
                    or parent.get("project_id") != project
                    or backend.owner not in json_texts(parent.get("tags", []))
                ):
                    raise ValueError("Global subnet parent/project mismatch")
            elif (
                kind == "selectel_global_router_vpc_network_v1"
                and item.get("project_id") != backend.credentials["project_id"]
            ):
                raise ValueError("Global attachment project mismatch")
        else:
            if attrs.get("region") not in (None, "", backend.journal["stages"][name]["candidate"]["region"]):
                raise ValueError("Stage pool mismatch")
            tenant = item.get("tenant_id", item.get("project_id"))
            # Nova server groups and Cinder volumes omit tenant in project-scoped lists.
            if tenant is not None and tenant != backend.credentials["project_id"]:
                raise ValueError("Stage project mismatch")
            if attrs.get("name") and item.get("name") != attrs["name"]:
                raise ValueError("Stage resource name mismatch")
            if (
                kind in ("openstack_compute_instance_v2", "openstack_blockstorage_volume_v3")
                and json_object(item.get("metadata", {}), "VM or volume deployment marker mismatch").get("dz07_owner")
                != backend.owner
            ):
                raise ValueError("VM or volume deployment marker mismatch")
            if kind in (
                "openstack_networking_network_v2",
                "openstack_networking_router_v2",
            ) and backend.owner not in json_texts(item.get("tags", [])):
                raise ValueError("Network deployment marker mismatch")
            if kind == "openstack_networking_secgroup_v2" and item.get("description") != backend.owner:
                raise ValueError("Security group deployment marker mismatch")
            if kind == "openstack_compute_servergroup_v2":
                known: set[str] = set()
                for node, specification in backend.desired["nodes"].items():
                    saved = backend.journal["stages"].get(node, {})
                    if (
                        specification["role"] != "es"
                        or saved.get("candidate", {}).get("region") != record["candidate"]["region"]
                    ):
                        continue
                    node_entries = backend.store.entries(node) + saved.get("audit_entries", [])
                    known.update(
                        json_text(e["attrs"]["id"], "Unknown stage resource identity")
                        for e in node_entries
                        if e["type"] == "openstack_compute_instance_v2"
                    )
                members = item.get("members")
                if (
                    not isinstance(members, list)
                    or not set(json_texts(members, "Unknown VM membership in the deployment server group")) <= known
                ):
                    raise ValueError("Unknown VM membership in the deployment server group")
        if active and kind in GLOBAL and item.get("status") != "ACTIVE":
            raise RuntimeError("Global Router object is not ACTIVE")
        if active and kind == "openstack_compute_instance_v2":
            candidate = backend.journal["stages"][name]["candidate"]
            if (
                item.get("status") != "ACTIVE"
                or item.get("OS-EXT-AZ:availability_zone") != candidate["availability_zone"]
            ):
                raise RuntimeError("VM state or segment is unconfirmed")


def assert_stage_absent(backend: "Backend", name: str) -> None:
    entries = backend.journal["stages"][name].get("audit_entries", [])
    deadline = time.monotonic() + 90
    while True:
        found = inventory(backend, name, entries)
        from selectel_intent import check_unrecorded

        check_unrecorded(backend, name, entries, found)
        remaining = [
            e["attrs"]["id"] for e in entries if e["type"] in found and e["attrs"].get("id") in found[e["type"]]
        ]
        if not remaining:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("Stage resources remain in API after Terraform deletion")
        time.sleep(2)


def faults(backend: "Backend", name: str) -> StageFaults:
    result: StageFaults = {"stage": name, "nodes": {}, "read_only": True}
    entries = backend.store.entries(name)
    if any(e["type"] in GLOBAL for e in entries):
        assert_stage_owned(backend, name)
    for entry in entries:
        if entry["type"] in GLOBAL:
            kind, attrs = entry["type"], entry["attrs"]
            try:
                item = backend.global_router.get(
                    "/"
                    + GLOBAL[kind]
                    + "/"
                    + urllib.parse.quote(json_text(attrs["id"], "Unknown stage resource identity"), safe="")
                )
                if not isinstance(item, dict):
                    raise ValueError("Unknown Global Router diagnostic response")
                if item.get("id") != attrs["id"] or backend.owner not in item.get("tags", []):
                    raise ValueError("Global diagnostic ownership mismatch")
                result.setdefault("global_router", {})[kind] = {
                    k: item[k]
                    for k in (
                        "status",
                        "created_at",
                        "updated_at",
                        "fault",
                        "error",
                        "error_message",
                        "last_error",
                        "details",
                    )
                    if k in item
                }
            except (OSError, ValueError, KeyError) as error:
                result.setdefault("global_router", {})[kind] = {
                    "diagnostic_error": type(error).__name__,
                    "http_status": getattr(error, "code", None),
                }
            continue
        if entry["type"] != "openstack_compute_instance_v2":
            continue
        cloud = backend.region(
            json_text(backend.journal["stages"][name]["candidate"]["region"], "Stage pool is unconfirmed")
        )
        server = json_object(
            cloud.get(
                "compute",
                "/servers/" + json_text(entry["attrs"]["id"], "Unknown stage resource identity"),
            )["server"]
        )
        if (
            server.get("name") != backend.api.PREFIX + "-" + name
            or server.get("tenant_id") != backend.credentials["project_id"]
        ):
            raise ValueError("Fault diagnostic ownership mismatch")
        result["nodes"][name] = {
            key: server.get(key)
            for key in (
                "status",
                "fault",
                "OS-EXT-AZ:availability_zone",
                "OS-EXT-STS:vm_state",
                "OS-EXT-STS:task_state",
            )
        }
    return result


def verify_admin_ingress(backend: "Backend") -> None:
    """Check saved network policies without refreshing Terraform or contacting the VMs."""
    stages = backend.journal["stages"]
    networks = [
        record for name, record in stages.items() if name.startswith("network-") and record["status"] == "active"
    ]
    if not networks:
        raise RuntimeError("В контексте стенда нет активных сетей для проверки правил доступа")
    app_region = json_text(stages["app"]["candidate"]["region"], "Application pool is unconfirmed")
    if not any(record["candidate"]["region"] == app_region for record in networks):
        raise RuntimeError("В контексте стенда нет активной сети ВМ app")
    missing = []
    for record in networks:
        region = json_text(record["candidate"]["region"], "Stage pool is unconfirmed")
        rules = owned_access_rules(backend, record, region)
        saved_cidrs = json_texts(record["inputs"]["admin_cidrs"], "Unknown admin networks")
        # The signed route can extend the application pool without changing other pools.
        if region == app_region:
            saved_cidrs = saved_cidrs + backend.journal["admin_cidrs"]
        cidrs = allowed_cidrs([], saved_cidrs)
        if not cidrs:
            raise RuntimeError("Список разрешённых IPv4 пуст в " + region)
        port = json_int(record["inputs"]["kibana_port"], "Unknown Kibana port")
        missing.extend(missing_admin_rules(region, cidrs, port, rules))
    for rule in missing:
        LOGGER.error("Отсутствует входящее правило %s", rule)
    if missing:
        raise RuntimeError(f"Selectel API: отсутствуют обязательные правила доступа ({len(missing)})")
    LOGGER.info("Selectel API подтвердил отдельные правила IPv4 /32 для SSH и Kibana в %d пулах", len(networks))


def owned_access_rules(backend: "Backend", record: StageRecord, region: str) -> list[dict[str, object]]:
    group_id = record["output"]["security_group_id"]
    group = json_object(backend.region(region).get("network", "/v2.0/security-groups/" + group_id)["security_group"])
    if (
        group.get("id") != group_id
        or group.get("description") != backend.owner
        or group.get("tenant_id", group.get("project_id")) not in (None, backend.credentials["project_id"])
    ):
        raise RuntimeError("Принадлежность группы безопасности стенду не подтверждена в " + region)
    return json_rows(group["security_group_rules"])


def missing_admin_rules(region: str, cidrs: list[str], kibana_port: int, rules: list[dict[str, object]]) -> list[str]:
    actual = [
        (rule.get("remote_ip_prefix"), rule.get("port_range_min"), rule.get("port_range_max"))
        for rule in rules
        if rule.get("direction") == "ingress"
        and rule.get("ethertype") == "IPv4"
        and rule.get("protocol") == "tcp"
        and rule.get("remote_group_id") is None
        and rule.get("remote_address_group_id") is None
        and type(rule.get("port_range_min")) is int
        and type(rule.get("port_range_max")) is int
    ]
    return [
        f"{region} | {service} TCP/{port} | источник {cidr}"
        for cidr in cidrs
        for service, port in (("SSH", 22), ("Kibana", kibana_port))
        if (cidr, port, port) not in actual
    ]


def verify_networks(backend: "Backend") -> NetworkVerification:
    nodes = backend.result().nodes
    require_distinct = backend.journal.get("require_distinct_hosts", True)
    physical = []
    for name, node in nodes.items():
        cloud = backend.region(node["region"])
        server = json_object(cloud.get("compute", "/servers/" + node["id"])["server"])
        network = backend.journal["stages"]["network-" + node["region"]]["output"]
        port = json_object(cloud.get("network", "/v2.0/ports/" + node["port_id"])["port"])
        if (
            port.get("port_security_enabled") is not True
            or port.get("security_groups") != [network["security_group_id"]]
            or port.get("network_id") != network["network_id"]
            or not any(ip["ip_address"] == node["private_ip"] for ip in json_rows(port["fixed_ips"]))
        ):
            raise RuntimeError("Private VM port filtering/address unconfirmed")
        if name != "app" and any(
            f.get("port_id") == node["port_id"] for f in objects(cloud, "network", "/v2.0/floatingips", "floatingips")
        ):
            raise RuntimeError("An internal Elasticsearch node has a public address")
        if "es" in node["roles"]:
            groups = objects(cloud, "compute", "/os-server-groups", "server_groups")
            matches = [g for g in groups if g["id"] == network["server_group_id"]]
            if len(matches) != 1 or matches[0].get("policies") != ["anti-affinity"]:
                raise RuntimeError("Owned Elasticsearch placement group unconfirmed")
            member = node["id"] in json_texts(matches[0].get("members", []))
            if require_distinct and (not member or not server.get("hostId")):
                raise RuntimeError("Elasticsearch hard anti-affinity unconfirmed")
            if not require_distinct and member:
                raise RuntimeError("Diagnostic VM unexpectedly belongs to the hard placement group")
            if server.get("hostId"):
                physical.append((node["region"], json_text(server["hostId"])))
    if require_distinct and (len(physical) != 3 or len(set(physical)) != 3):
        raise RuntimeError("Three physically separate Elasticsearch nodes required")
    for name, record in backend.journal["stages"].items():
        if record["status"] != "active":
            continue
        assert_stage_owned(backend, name, active=True)
        if name.startswith("network-"):
            cloud = backend.region(json_text(record["candidate"]["region"], "Stage pool is unconfirmed"))
            net = json_object(cloud.get("network", "/v2.0/networks/" + record["output"]["network_id"])["network"])
            if (
                net.get("shared") is not False
                or net.get("router:external") is not False
                or net.get("port_security_enabled") is not True
            ):
                raise RuntimeError("Private network isolation unconfirmed")
            subnet = json_object(cloud.get("network", "/v2.0/subnets/" + record["output"]["subnet_id"])["subnet"])
            peer_cidrs = json_texts(record["inputs"]["peer_cidrs"], "Unknown peer networks")
            gateway = json_int(record["inputs"]["global_gateway_host"], "Unknown gateway host")
            route_pairs = {(cidr, str(ipaddress.ip_network(record["output"]["cidr"])[gateway])) for cidr in peer_cidrs}
            if route_pairs != {(r["destination"], r["nexthop"]) for r in json_rows(subnet.get("host_routes", []))}:
                raise RuntimeError("Global Router subnet routes unconfirmed")
            routers = objects(cloud, "network", "/v2.0/routers", "routers")
            routers = [r for r in routers if r.get("name") == record["inputs"]["name"]]
            if len(routers) != 1 or any(
                set(json_texts(item.get("availability_zone_hints", []))) != {zone}
                or set(json_texts(item.get("availability_zones", []))) != {zone}
                for item, zone in (
                    (net, record["inputs"]["network_zone"]),
                    (routers[0], record["inputs"]["router_zone"]),
                )
            ):
                raise RuntimeError("Actual network/router scheduling domains unconfirmed")
            group = json_object(
                cloud.get("network", "/v2.0/security-groups/" + record["output"]["security_group_id"])["security_group"]
            )
            actual: set[tuple[object, object, object, object, object, object]] = {
                (
                    r.get("protocol"),
                    r.get("port_range_min"),
                    r.get("port_range_max"),
                    r.get("remote_ip_prefix"),
                    r.get("remote_group_id"),
                    r.get("ethertype"),
                )
                for r in json_rows(group["security_group_rules"])
                if r["direction"] == "ingress"
            }
            peer_cidrs = json_texts(record["inputs"]["peer_cidrs"], "Unknown peer networks")
            admin_cidrs = json_texts(record["inputs"]["admin_cidrs"], "Unknown admin networks")
            kibana_port = json_int(record["inputs"]["kibana_port"], "Unknown Kibana port")
            expected: set[tuple[object, object, object, object, object, object]] = {
                (None, None, None, cidr, None, "IPv4")
                for cidr in [json_text(record["inputs"]["cidr"], "Unknown network CIDR"), *peer_cidrs]
            }
            expected.add(("tcp", 80, 80, "0.0.0.0/0", None, "IPv4"))
            expected |= {("tcp", port, port, cidr, None, "IPv4") for port in (22, kibana_port) for cidr in admin_cidrs}
            if actual != expected:
                raise RuntimeError("Actual network ingress differs from saved policy")
    return {
        "success": True,
        "es_distinct_hosts": len(set(physical)) if len(physical) == 3 else None,
        "require_distinct_hosts": require_distinct,
        "physical_host_separation_verified": require_distinct,
        "private_ports_filtered": True,
        "global_routes_confirmed": True,
    }


def cleanup_order(backend: "Backend") -> list[str]:
    names = list(backend.journal["stages"])
    nodes = [n for n in names if n in backend.desired["nodes"]]
    ordered = list(reversed(nodes)) + [n for n in reversed(names) if n.startswith("connection-")]
    ordered += [n for n in reversed(names) if n.startswith("network-")]
    return ordered + [n for n in names if n == "global"]


def audit_deletion(backend: "Backend") -> DeletionAudit:
    from progress_logging import phase
    from selectel_scope_audit import read_independent, workers
    from selectel_staged_audit import assert_stage_absent, audit_namespace

    for name in backend.journal["stages"]:
        LOGGER.info("Аудит очистки: проверка отсутствия этапа %s через state и API", name)
        assert_stage_absent(backend, name)
    regions = sorted(
        {
            json_text(stage["candidate"]["region"], "Stage pool is unconfirmed")
            for name, stage in backend.journal["stages"].items()
            if name != "global"
        }
    )
    tasks: list[tuple[str, Callable[[], None]]] = [(pool, partial(audit_namespace, backend, pool)) for pool in regions]
    if any(name == "global" or name.startswith("connection-") for name in backend.journal["stages"]):
        tasks.append(("global-router", lambda: backend.global_router.assert_absent({}, backend.owner)))
    with phase(LOGGER, "Аудит сохранённой области стенда и Глобального роутера"):
        read_independent(tasks, workers(backend.api.ROOT))
    return {"pools_audited": regions, "global_router_remaining": 0, "audit_complete": True}


@timed("Удаление и подтверждение очистки этапа")
def cleanup_attempt(backend: "Backend", name: str) -> None:
    from selectel_staged_audit import assert_stage_absent, assert_stage_owned

    record = backend.journal["stages"][name]
    LOGGER.info("Удаление этапа %s: начало проверки принадлежности", name)
    assert_stage_owned(backend, name, active=False)
    record["audit_entries"] = record.get("audit_entries") or backend.store.entries(name)
    record["status"] = "deleting"
    backend.save()
    if record.get("kind") and record.get("inputs"):
        backend.configure_stage(name, record["kind"], record["inputs"])
    if not (backend.store.folder(name) / "main.tf.json").exists():
        if backend.store.identities(name):
            raise ValueError("Stage configuration missing while resources remain")
    else:
        LOGGER.info("Удаление этапа %s: начало Terraform apply", name)
        backend.store.apply(name, destroy=True)
    LOGGER.info("Удаление этапа %s: начало подтверждения через state и API", name)
    assert_stage_absent(backend, name)
    LOGGER.info("Удаление этапа %s: подтверждено", name)
    record["status"] = "deleted"
    backend.save()
    if name in backend.desired["nodes"] and not backend.journal.get("destroy_requested"):
        backend.prune_unused_network(json_text(record["candidate"]["region"], "Stage pool is unconfirmed"))


def prune_unused_network(backend: "Backend", pool: str) -> None:
    network = "network-" + pool
    if network not in getattr(backend, "_temporary_networks", set()):
        return
    for node in backend.desired["nodes"]:
        record = backend.journal["stages"].get(node, {})
        if record.get("candidate", {}).get("region") == pool and backend.store.identities(node):
            return
    connection = "connection-" + pool
    if connection in backend.journal["stages"] and backend.journal["stages"][connection]["status"] != "deleted":
        backend.cleanup_attempt(connection)
    if backend.journal["stages"][network]["status"] != "deleted":
        backend.cleanup_attempt(network)
    backend._temporary_networks.discard(network)
