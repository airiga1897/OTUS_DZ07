"""Selectel pool discovery and capacity retry; adapted from DZ05, clone-local."""

import fnmatch
import itertools
import json
import re
import urllib.error
import urllib.parse
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Protocol, cast

from catalog_cache import CatalogCache
from cloud_contract import TerraformError
from placement_config import PlacementPolicy, VMProfile, checked, required_profiles
from progress_logging import get_logger
from quota_types import checked_quota
from selectel_api import CatalogService, authenticate, json_object, request_json
from selectel_catalog_types import CatalogResponse, Flavor, flavor_rows, text, volume_rows
from selectel_catalog_types import checked as checked_catalog_response
from selectel_catalog_types import document as catalog_document
from selectel_catalog_types import rows as catalog_rows
from terraform_state_types import checked_state

LOGGER = get_logger(__name__)
SERVICES = ("compute", "image", "volumev3", "network")


class CandidateUnavailable(RuntimeError):
    pass


class CapacityRetry(RuntimeError):
    """Previous partial deployment has passed both deletion audits."""


class PoolReader(Protocol):
    def get(self, service: str, path: str) -> object: ...


class CatalogSession(Protocol):
    @property
    def token(self) -> str: ...
    @property
    def catalog(self) -> Sequence[CatalogService]: ...


def endpoints_for(catalog: Sequence[CatalogService], region: str) -> dict[str, str]:
    endpoints = {
        s["type"]: e["url"].rstrip("/")
        for s in catalog
        for e in s["endpoints"]
        if e.get("interface") == "public" and e.get("region") == region and e["url"].startswith("https://")
    }
    missing = sorted(set(SERVICES) - endpoints.keys())
    if missing:
        raise CandidateUnavailable(
            "Required services unavailable in pool " + region + ": missing public HTTPS APIs: " + ", ".join(missing)
        )
    return endpoints


def ordered_pools(config: PlacementPolicy, catalog: Sequence[CatalogService]) -> list[str]:
    excluded = set(config.get("excluded_pools", []))
    if any(not re.fullmatch(r"[a-z][a-z0-9]*-\d+", pool) for pool in excluded):
        raise ValueError("Invalid excluded pool")
    available = {
        region
        for s in catalog
        if s["type"] == "compute"
        for e in s["endpoints"]
        if e.get("interface") == "public"
        and isinstance(region := e.get("region"), str)
        and re.fullmatch(r"[a-z][a-z0-9]*-\d+", region)
    }
    if any(
        endpoint.get("interface") == "public" and not isinstance(endpoint.get("region"), str)
        for service in catalog
        if service["type"] == "compute"
        for endpoint in service["endpoints"]
    ):
        LOGGER.warning("Каталог compute: публичный endpoint без имени пула; размещение по нему не подтверждено")
    order = []
    requested_pools = config["pools"]
    if "auto" in requested_pools:
        requested_pools = list(dict.fromkeys([*config.get("preferred_pools", []), *requested_pools]))
    for requested in requested_pools:
        if requested != "auto" and requested not in available and requested not in excluded:
            LOGGER.warning(requested + ": compute pool is absent from the catalog; skipped")
        choices = (
            sorted(available, key=lambda p: (int(p.rsplit("-", 1)[1]), p), reverse=True)
            if requested == "auto"
            else [requested]
        )
        for pool in choices:
            if pool in available and pool not in excluded and pool not in order:
                try:
                    endpoints_for(catalog, pool)
                except CandidateUnavailable as error:
                    LOGGER.warning(str(error) + "; skipped")
                    continue
                order.append(pool)
    return order


def compatible_images[Image: Mapping[str, object]](images: Iterable[Image], config: object) -> list[Image]:
    raw = json_object(config, "Configure a nonempty unique list of verified Glance MD5 checksums")
    checksums = raw.get("image_checksums")
    if (
        not isinstance(checksums, list)
        or not checksums
        or any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value) for value in checksums)
        or len(set(checksums)) != len(checksums)
    ):
        raise ValueError("Configure a nonempty unique list of verified Glance MD5 checksums")
    return [image for image in images if not image_rejections(image, checked(raw))]


def image_rejections(image: Mapping[str, object], config: PlacementPolicy) -> list[str]:
    disk, ram = image.get("min_disk", 0), image.get("min_ram", 0)
    checks = [
        ("name", image.get("name") == config["image_name"]),
        ("visibility", image.get("visibility") == "public"),
        ("status", image.get("status") == "active"),
        ("os_distro", image.get("os_distro") == config.get("image_os_distro", "ubuntu")),
        ("min_disk", type(disk) is int and disk <= config["boot_disk_gb"]),
        ("min_ram", type(ram) is int and ram <= min(p["ram_mb"] for p in config["profiles"].values())),
        ("architecture", image.get("architecture", "x86_64") in ("x86_64", "amd64")),
        ("checksum", image.get("checksum") in config["image_checksums"]),
    ]
    return [key for key, accepted in checks if not accepted]


def image_skip_reason(images: Sequence[Mapping[str, object]], config: PlacementPolicy) -> str:
    named = [image for image in images if image.get("name") == config["image_name"]]
    reasons = sorted({reason for image in named for reason in image_rejections(image, config)})
    return (
        "No matching verified image: expected "
        + config["image_name"]
        + "; active catalog images="
        + str(len(images))
        + "; matching name="
        + str(len(named))
        + "; rejected fields="
        + (", ".join(reasons) if named else "name (absent)")
    )


def ordered_zones(config: PlacementPolicy, zones: Iterable[str]) -> list[str]:
    zones = list(zones)
    preferred = config.get("preferred_segments", [])
    excluded = config.get("excluded_segments", [])
    if any(not re.fullmatch(r"ru-\d+[a-z]", zone) for zone in [*preferred, *excluded]):
        raise ValueError("Invalid preferred or excluded segment")
    for zone in sorted(set(zones) & set(excluded)):
        LOGGER.warning(zone + ": explicitly excluded segment; skipped")
    return sorted(
        (zone for zone in zones if zone not in excluded),
        key=lambda zone: (preferred.index(zone) if zone in preferred else len(preferred), zone),
    )


def _image_page(value: object) -> tuple[list[dict[str, object]], str | None]:
    if not isinstance(value, dict) or not isinstance(value.get("images"), list):
        raise ValueError("Invalid Glance image catalog page")
    images = value["images"]
    if any(not isinstance(image, dict) or any(not isinstance(key, str) for key in image) for image in images):
        raise ValueError("Invalid Glance image catalog entry")
    following = value.get("next")
    if following is not None and not isinstance(following, str):
        raise ValueError("Invalid Glance image catalog continuation")
    return cast(list[dict[str, object]], images), following


def _image_order(image: Mapping[str, object]) -> tuple[str, str]:
    identity, created = image.get("id"), image.get("created_at", "")
    if not isinstance(identity, str) or not identity or not isinstance(created, str):
        raise ValueError("Invalid verified Glance image identity or timestamp")
    return created, identity


def image_catalog(endpoint: str, token: str) -> list[dict[str, object]]:
    url = endpoint.removesuffix("/v2") + "/v2/images?status=active"
    original = urllib.parse.urlsplit(url)
    seen: set[str] = set()
    images: list[dict[str, object]] = []
    while True:
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme, parsed.netloc, parsed.path) != (
            original.scheme,
            original.netloc,
            original.path,
        ) or url in seen:
            raise ValueError("Unsafe or cyclic image catalog pagination")
        seen.add(url)
        page, next_page = _image_page(request_json(url, token=token)[0])
        images.extend(page)
        if next_page:
            following = urllib.parse.urlsplit(urllib.parse.urljoin(url, next_page))
            if (following.scheme, following.netloc) != (original.scheme, original.netloc) or following.path not in (
                original.path,
                "/v2/images",
            ):
                raise ValueError("Unsafe image catalog pagination")
            url = urllib.parse.urlunsplit((original.scheme, original.netloc, original.path, following.query, ""))
        else:
            break
    return images


def network_placement(
    config: object, pool: str, zone: str | None, supported: Mapping[str, Collection[str]] | None = None
) -> dict[str, str | None]:
    config = checked(config)
    if zone is None:
        if supported is not None:
            raise CandidateUnavailable("Network placement requires a confirmed compute segment")
        return {"network_availability_zone": None, "router_availability_zone": None}
    mapping = config.get("network_placement", {}).get(zone)
    if mapping is None:
        if supported is None:
            return {"network_availability_zone": zone, "router_availability_zone": zone}
        # Selectel L2 connectivity spans segments of one pool. Scheduling names
        # for networks need not equal Nova/Cinder names; prefer an exact API
        # domain, otherwise require one unambiguous same-pool general domain.
        result: dict[str, str | None] = {}
        for resource in ("network", "router"):
            available = sorted(name for name in supported[resource] if re.fullmatch(re.escape(pool) + r"[a-z]", name))
            if zone in available:
                selected = zone
            elif len(available) == 1:
                selected = available[0]
            else:
                raise CandidateUnavailable(
                    "Network placement unconfirmed for "
                    + zone
                    + ": "
                    + resource
                    + " domains from API="
                    + repr(available)
                    + "; require an exact or unique same-pool domain"
                )
            result[resource + "_availability_zone"] = selected
        return result
    if not str(mapping.get("evidence_url", "")).startswith("https://docs.selectel.ru/"):
        raise ValueError("A network scheduling-domain mapping requires provider documentation")
    values = [zone, mapping.get("network", ""), mapping.get("router", "")]
    if any(not re.fullmatch(re.escape(pool) + r"[a-z]", value) for value in values):
        raise ValueError("Network scheduling domains must stay in the selected pool")
    return {"network_availability_zone": values[1], "router_availability_zone": values[2]}


def disks_for_zone(types: object, patterns: Sequence[str], zone: str, get: Callable[[str, str], object]) -> list[str]:
    types = volume_rows(types)
    result: list[str] = []
    observed: dict[str, str] = {}
    for pattern in patterns:
        pattern = pattern if any(c in pattern for c in ".*?") else pattern + ".*"
        for item in sorted(types, key=lambda t: t["name"]):
            if not fnmatch.fnmatchcase(item["name"], pattern):
                continue
            specs = item.get("extra_specs")
            if specs is None:
                response = get("volumev3", "/types/" + text(item.get("id")) + "/extra_specs")
                specs = checked_catalog_response(response)["extra_specs"]
            declared = specs.get("RESKEY:availability_zones") if isinstance(specs, dict) else None
            observed[item["name"]] = declared if isinstance(declared, str) and declared.strip() else "<unconfirmed>"
            if not isinstance(declared, str) or not declared.strip():
                LOGGER.warning(zone + ": disk segment binding unconfirmed for " + item["name"] + "; skipped")
                continue
            if zone in {value.strip() for value in declared.split(",")} and item["name"] not in result:
                result.append(item["name"])
    if not result:
        detail = (
            ("; matching type bindings=" + repr(observed))
            if observed
            else ("; no type matches the allowed masks; catalog types=" + repr(sorted(t["name"] for t in types)))
        )
        raise CandidateUnavailable(
            "No confirmed allowed disk type bound to " + zone + "; allowed masks=" + repr(patterns) + detail
        )
    return result


def disk_for_zone(types: object, patterns: Sequence[str], zone: str, get: Callable[[str, str], object]) -> str:
    return disks_for_zone(types, patterns, zone, get)[0]


def flavor_available_in_zone(flavor: Mapping[str, object], zone: str | None, *, defer_missing: bool = False) -> bool:
    if defer_missing and "availability_zones" not in flavor:
        return True  # The provider preflight must confirm it using flavor detail.
    zones = flavor.get("availability_zones")
    if not isinstance(zones, list) or any(not isinstance(value, str) or not value for value in zones):
        LOGGER.warning("%s: flavor segment binding unconfirmed for %s; skipped", zone, flavor.get("name", "<unnamed>"))
        return False
    return zone in zones


def flavor_choices(flavors: object, config: Mapping[str, object]) -> dict[str, list[Flavor]]:
    """Explicit name patterns, exact dimensions, network boot; no implicit family fallback."""
    pattern_values: object = config.get("flavor_name_patterns")
    if pattern_values is None:
        pattern_values = [config.get("flavor_name_template")]
    if (
        not isinstance(pattern_values, list)
        or not pattern_values
        or any(not isinstance(p, str) or not p for p in pattern_values)
    ):
        raise ValueError("Configure flavor_name_patterns")
    patterns = cast(list[str], pattern_values)
    flavors = flavor_rows(flavors)
    result: dict[str, list[Flavor]] = {}
    for role, profile in required_profiles({"profiles": config.get("profiles", {})}).items():
        matches: list[Flavor] = []
        seen: set[str] = set()
        for pattern in patterns:
            wanted = pattern.format(**profile)
            for item in sorted(flavors, key=lambda f: (f["name"], f["id"])):
                if (
                    fnmatch.fnmatchcase(item["name"], wanted)
                    and item["vcpus"] == profile["vcpus"]
                    and item["ram"] == profile["ram_mb"]
                    and type(item.get("disk")) is int
                    and item["disk"] == 0
                    and item.get("os-flavor-access:is_public", True) is True
                    and item["id"] not in seen
                ):
                    matches.append(item)
                    seen.add(item["id"])
        if not matches:
            raise CandidateUnavailable("No allowed network-boot flavor for " + role)
        result[role] = matches
    return result


def candidates(
    config: object,
    credentials: Mapping[str, str],
    *,
    session: CatalogSession | None = None,
    cache: CatalogCache | None = None,
) -> Iterator[PlacementPolicy]:
    config = checked(config)
    classes = config.get("disk_type_patterns", config.get("disk_classes", [config.get("disk_class")]))
    if (
        not isinstance(classes, list)
        or not classes
        or any(not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9.*?-]*", value) for value in classes)
        or len(set(classes)) != len(classes)
    ):
        raise ValueError("Configure unique disk_type_patterns in priority order")
    classes = cast(list[str], classes)
    if not config.get("pools") or any(
        p != "auto" and not re.fullmatch(r"[a-z][a-z0-9]*-\d+", p) for p in config["pools"]
    ):
        raise ValueError("Configure Russian pools or auto")
    segment = config.get("segment")
    if segment and not re.fullmatch(r"ru-\d+[a-z]", segment):
        raise ValueError("Invalid configured segment")
    required_profiles(config)
    LOGGER.info("Каталог размещения: начало авторизации и чтения списка пулов")
    token, catalog = (session.token, session.catalog) if session is not None else authenticate(credentials)
    for pool in ordered_pools(config, catalog):
        LOGGER.info("Пул %s: начало чтения каталогов образов, конфигураций ВМ, дисков и зон", pool)
        endpoints = endpoints_for(catalog, pool)
        try:

            def get(
                kind: str, path: str, *, pool: str = pool, endpoints: Mapping[str, str] = endpoints
            ) -> CatalogResponse:
                def load(*, endpoints: Mapping[str, str] = endpoints) -> CatalogResponse:
                    return checked_catalog_response(request_json(endpoints[kind] + path, token=token)[0])

                return cache.read((pool, kind, path), load) if cache is not None else load()

            def load_images(*, endpoints: Mapping[str, str] = endpoints) -> list[dict[str, object]]:
                return image_catalog(endpoints["image"], token)

            catalog_images = cache.read((pool, "image-catalog"), load_images) if cache is not None else load_images()
            images = compatible_images(catalog_images, config)
            if not images:
                raise CandidateUnavailable(image_skip_reason(catalog_images, config))
            images.sort(key=_image_order, reverse=True)
            flavors = get("compute", "/flavors/detail")["flavors"]
            options = flavor_choices(flavors, config)
            types = get("volumev3", "/types")["volume_types"]
            zones = get("compute", "/os-availability-zone")["availabilityZoneInfo"]
            volume_zones = {
                z["zoneName"]
                for z in get("volumev3", "/os-availability-zone")["availabilityZoneInfo"]
                if z["zoneState"]["available"]
            }
            network_zones = get("network", "/v2.0/availability_zones")["availability_zones"]
            supported = {
                r: {z["name"] for z in network_zones if z["resource"] == r and z["state"] == "available"}
                for r in ("network", "router")
            }
            for zone in ordered_zones(
                config,
                (
                    z["zoneName"]
                    for z in zones
                    if z["zoneState"]["available"] and re.fullmatch(re.escape(pool) + r"[a-z]", z["zoneName"])
                ),
            ):
                if config.get("segment") and config["segment"] != zone:
                    continue
                if zone not in volume_zones:
                    LOGGER.warning(zone + ": volume scheduling segment unavailable; skipped")
                    continue
                try:
                    domains = network_placement(config, pool, zone, supported)
                except CandidateUnavailable as error:
                    LOGGER.warning(zone + ": " + str(error) + "; skipped")
                    continue
                if (
                    domains["network_availability_zone"] not in supported["network"]
                    or domains["router_availability_zone"] not in supported["router"]
                ):
                    LOGGER.warning(
                        zone
                        + ": required network/router scheduling domain unavailable; requested="
                        + repr(domains)
                        + "; API available="
                        + repr({k: sorted(v) for k, v in supported.items()})
                        + "; skipped"
                    )
                    continue
                try:
                    disks = disks_for_zone(types, classes, zone, get)
                except CandidateUnavailable as error:
                    LOGGER.warning(zone + ": " + str(error) + "; skipped")
                    continue
                roles = list(options)
                available = [
                    [f for f in options[r] if flavor_available_in_zone(f, zone, defer_missing=True)] for r in roles
                ]
                # Known flavor bindings filter here; the provider preflight also
                # reads the selected flavor detail and rejects unknown bindings.
                for disk in disks:
                    for combination in itertools.product(*available):
                        yield checked(
                            dict(
                                region=pool,
                                availability_zone=zone,
                                volume_type=disk,
                                **domains,
                                image_id=_image_order(images[0])[1],
                                flavor_ids={r: f["id"] for r, f in zip(roles, combination, strict=False)},
                            )
                        )
        except CandidateUnavailable as error:
            LOGGER.warning(pool + ": " + str(error) + "; skipped")


def selected(root: str | Path) -> PlacementPolicy | None:
    path = Path(root) / ".local/placement.json"
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    return None if value is None else checked(value)


def policy(root: str | Path, filename: str, context_name: str) -> PlacementPolicy:
    root = Path(root)
    journal_path = root / ".local/staged-deployment.json"
    if filename == "placement.json" and journal_path.exists():
        journal = json.loads(journal_path.read_text())
        if journal.get("run_settings"):
            data = json.loads((root / "config" / filename).read_text(encoding="utf-8-sig"))
            data.update(journal["run_settings"])
            data["pools"] = list(journal["pool_subnets"])
            data["region"] = journal.get("bootstrap_region") or next(iter(data["pools"]))
            return checked(data)
    if filename == "placement.json":
        from run_settings import placement

        data = placement(root)
    else:
        data = json.loads((root / "config" / filename).read_text(encoding="utf-8-sig"))
    preferred = next(iter(data.get("preferred_pools", [])), next((p for p in data["pools"] if p != "auto"), None))
    data["region"] = preferred
    saved = selected(root)
    if saved:
        data.update(saved)
    else:
        binding = root / ".local" / context_name
        if binding.exists():
            saved_context = json.loads(binding.read_text())
            data["region"] = saved_context.get("placement", {}).get("region", saved_context.get("region"))
        variables = root / "terraform-selectel/terraform.tfvars.json"
        if variables.exists():
            values = json.loads(variables.read_text())
            data.update({k: values[k] for k in ("region", "availability_zone", "volume_type") if k in values})
    return checked(data)


def capacity(item: object, needed: int) -> None:
    if item is None:
        raise CandidateUnavailable("Required quota unavailable")
    item = checked_quota(item)
    free = item["limit"] - item.get("in_use", item.get("used", 0)) - item.get("reserved", 0)
    if item["limit"] != -1 and free < needed:
        raise CandidateUnavailable("Insufficient shared quota")


def remaining_demand(
    root: str | Path, base: Mapping[str, int], node_profiles: Mapping[str, VMProfile]
) -> dict[str, int]:
    """Count only additions; current own resources already consume shared quotas."""
    demand = dict(base)
    state = Path(root) / "terraform-selectel/terraform.tfstate"
    if not state.exists():
        return demand
    counts: dict[str, int] = {}
    for resource in checked_state(json.loads(state.read_text())).get("resources", []):
        if resource.get("mode") != "managed":
            continue
        kind = resource["type"]
        instances = resource.get("instances", [])
        counts[kind] = counts.get(kind, 0) + len(instances)
        for instance in instances:
            attrs = instance.get("attributes", {})
            if kind == "openstack_compute_instance_v2":
                index = instance.get("index_key")
                profile = node_profiles.get(index) if isinstance(index, str) else None
                if profile:
                    for key, value in [("cores", profile["vcpus"]), ("ram", profile["ram_mb"]), ("instances", 1)]:
                        demand[key] = max(0, demand[key] - value)
            if kind == "openstack_blockstorage_volume_v3":
                size = attrs.get("size", 0)
                if type(size) is not int or size < 0:
                    raise ValueError("Owned volume size is unconfirmed")
                demand["disk_gb"] = max(0, demand["disk_gb"] - size)
    for key, kind in {
        "volumes": "blockstorage_volume_v3",
        "floatingip": "networking_floatingip_v2",
        "network": "networking_network_v2",
        "subnet": "networking_subnet_v2",
        "router": "networking_router_v2",
        "security_group": "networking_secgroup_v2",
        "security_group_rule": "networking_secgroup_rule_v2",
        "port": "networking_port_v2",
    }.items():
        if key in demand:
            demand[key] = max(0, demand[key] - counts.get("openstack_" + kind, 0))
    # Neutron creates router/DHCP/default-rule objects outside explicit state entries.
    if all(
        counts.get("openstack_" + kind, 0)
        for kind in (
            "networking_network_v2",
            "networking_subnet_v2",
            "networking_router_v2",
            "networking_router_interface_v2",
        )
    ):
        demand["port"] = max(0, demand["port"] - 3)
    demand["port"] = max(0, demand["port"] - counts.get("openstack_networking_floatingip_v2", 0))
    if counts.get("openstack_networking_secgroup_v2"):
        demand["security_group_rule"] = max(0, demand["security_group_rule"] - 2)
    return demand


def reject_orphans(cloud: PoolReader, prefix: str, subnets: Iterable[Mapping[str, object]]) -> None:
    items = list(subnets)
    items += catalog_rows(catalog_document(cloud.get("compute", "/servers/detail"))["servers"])
    items += catalog_rows(catalog_document(cloud.get("network", "/v2.0/networks"))["networks"])
    for item in items:
        name = item.get("name", "")
        if not isinstance(name, str):
            raise ValueError("Resource name is unconfirmed during namespace check")
        if name == prefix or name.startswith(prefix + "-"):
            raise ValueError("Resources with own prefix exist outside managed state; inspect before deployment")


def select(root: str | Path, choice: object, project: str, context_name: str) -> None:
    root = Path(root)
    state = root / "terraform-selectel/terraform.tfstate"
    if state.exists() and any(
        r.get("mode") == "managed" and r.get("instances") for r in json.loads(state.read_text()).get("resources", [])
    ):
        raise ValueError("Cannot change pool while managed state remains")
    previous = selected(root)
    if previous and previous.get("phase") not in ("prepared", "destroyed"):
        raise ValueError("Previous deployment needs confirmed destroy before pool selection")
    context = root / ".local" / context_name
    if context.exists() and not previous:
        raise ValueError("Legacy context needs explicit destroy before selecting a new pool")
    if previous and previous["project_id"] != project:
        raise ValueError("Saved deployment belongs to another project")
    local = root / ".local"
    local.mkdir(exist_ok=True)
    from selectel_adapter import binding
    from selectel_api import json_text
    from stack_config import load

    choice = checked(choice)
    region, zone = choice.get("region"), choice.get("availability_zone")
    if not isinstance(region, str) or not region:
        raise ValueError("Selection requires a confirmed pool")
    ownership = binding({"project_id": project, "region": region}, load()["name_prefix"], zone)
    if previous:
        if previous.get("provider") != ownership["provider"] or previous.get("namespace") != ownership["namespace"]:
            raise ValueError("Previous placement belongs to another provider or namespace")
        if context.exists():
            from cloud_contract import validate_binding

            previous_region = previous.get("region")
            if not isinstance(previous_region, str) or not previous_region:
                raise ValueError("Saved placement pool is unconfirmed; preserve recovery files")
            expected = binding(
                {"project_id": project, "region": previous_region},
                json_text(ownership["namespace"], "Saved namespace is unconfirmed"),
                previous.get("availability_zone"),
            )
            validate_binding(json.loads(context.read_text()), expected)
    data = dict(choice, project_id=project, phase="prepared", provider="selectel", namespace=ownership["namespace"])
    (local / "placement.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    context.write_text(json.dumps(ownership), encoding="utf-8")


def phase(root: str | Path, value: str) -> None:
    data = selected(root)
    if data:
        data["phase"] = value
        (Path(root) / ".local/placement.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


def scheduler_unavailable(
    error: object, root: str | Path, cloud_factory: Callable[[], PoolReader], prefix: str
) -> bool:
    if not isinstance(error, TerraformError):
        return False

    def capacity(text: str) -> bool:
        normalized = " ".join(text.lower().split())
        return any(
            s in normalized
            for s in (
                "no valid host",
                "novalidhost",
                "not enough hosts",
                "exhausted all retry attempts for shelving preemptible instances to build instance",
            )
        )

    if capacity(error.output):
        return True
    path = Path(root) / "terraform-selectel/terraform.tfstate"
    if not path.exists():
        return False
    cloud = cloud_factory()
    found = False
    for resource in json.loads(path.read_text()).get("resources", []):
        if resource.get("mode") != "managed" or resource["type"] != "openstack_compute_instance_v2":
            continue
        for instance in resource.get("instances", []):
            attrs = instance["attributes"]
            expected = prefix + "-" + str(instance["index_key"])
            if attrs.get("name") != expected:
                raise ValueError("Foreign instance diagnostics refused")
            response = cloud.get("compute", "/servers/" + attrs["id"])
            if not isinstance(response, dict) or not isinstance(response.get("server"), dict):
                raise ValueError("Invalid server diagnostics response")
            server = response["server"]
            if server.get("name") != expected:
                raise ValueError("Foreign server UUID")
            status = server.get("status")
            if not isinstance(status, str):
                raise ValueError("Server diagnostics status unconfirmed")
            if status == "ERROR":
                fault = server.get("fault", {})
                if not isinstance(fault, dict) or not isinstance(fault.get("message", ""), str):
                    raise ValueError("Server diagnostics fault unconfirmed")
                found |= capacity(fault.get("message", ""))
    return found


def confirmed_capacity(
    error: BaseException, root: str | Path, cloud_factory: Callable[[], PoolReader], prefix: str
) -> bool:
    try:
        return scheduler_unavailable(error, root, cloud_factory, prefix)
    except Exception as diagnostic_error:
        try:
            name = type(diagnostic_error).__name__
            error.__dict__.setdefault("diagnostics_error_type", name)
            error.__dict__["capacity_diagnostics_error_type"] = name
            status = diagnostic_error.code if isinstance(diagnostic_error, urllib.error.HTTPError) else None
            LOGGER.warning(
                "Диагностика причины размещения недоступна (%s, HTTP %s); исходная ошибка сохранена, автоматический повтор не разрешён",
                name,
                status,
            )
        except Exception:
            pass  # Optional diagnostics must not replace the original failure.
        return False


def run_candidates[Choice, Result](
    choices: Iterable[Choice],
    select_choice: Callable[[Choice], object],
    prepare: Callable[[], object],
    apply: Callable[[], Result],
    exhausted: Callable[[], object] | None = None,
) -> Result:
    for choice in choices:
        select_choice(choice)
        try:
            prepare()
        except CandidateUnavailable as error:
            LOGGER.warning("Candidate rejected before allocation: " + str(error))
            continue  # No allocation has occurred.
        try:
            return apply()
        except CapacityRetry:
            continue  # Raised only AFTER state/API audits succeeded.
    if exhausted:
        exhausted()
    raise RuntimeError("No configured pool/segment can place the complete stand")
