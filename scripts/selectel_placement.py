"""Check pool prefixes in preference order; extend only when placement needs it."""

import json
from collections.abc import Iterator

import pool_selection as pools
from catalog_cache import CatalogCache
from cloud_contract import CapabilityStatus, require_capabilities
from progress_logging import get_logger, phase
from run_settings import pool_subnet
from selectel_global_router import GlobalRouterCapacityError, GlobalRouterPlacementUnavailable

LOGGER = get_logger(__name__)


class PlacementScan:
    def __init__(self, backend, capabilities, reject_resources) -> None:
        self.backend = backend
        self.capabilities, self.reject_resources = capabilities, reject_resources
        self.cache = CatalogCache()
        self.index = 0
        self.eligible = []
        self.roles = sorted({n["role"] for n in backend.desired["nodes"].values()})
        self.choices = {role: [] for role in self.roles}
        self.required = json.loads((backend.root / "config/cloud.json").read_text())["required_capabilities"] + [
            "ownership_tags"
        ]

    def pending_role(self, role):
        b = self.backend
        return any(not b.completed(n) for n, node in b.desired["nodes"].items() if node["role"] == role)

    def pool(self, pool, *, catalogs=True) -> None:
        b = self.backend
        with phase(LOGGER, "Проверка пула", pool=pool):
            cloud = b.region(pool)
            found = self.cache.read(("capabilities", pool), lambda: self.capabilities(cloud))
            unknown = [k for k in self.required if k not in found or found[k].status == CapabilityStatus.UNKNOWN]
            if unknown:
                require_capabilities(found, self.required)
            unsupported = [k for k in self.required if found[k].status == CapabilityStatus.UNSUPPORTED]
            if unsupported:
                if pool in self.retained:
                    raise ValueError("Saved pool capabilities changed: " + pool)
                LOGGER.warning("%s: unsupported capabilities %s; skipped", pool, ", ".join(unsupported))
                return
            import ipaddress

            subnets = cloud.get("network", "/v2.0/subnets")["subnets"]
            wanted = ipaddress.ip_network(b.journal["pool_subnets"][pool])
            own = b.saved_id("network-" + pool, "openstack_networking_network_v2", "network_id")
            if any(ipaddress.ip_network(s["cidr"]).overlaps(wanted) and s["network_id"] != own for s in subnets):
                if own:
                    raise ValueError("Subnet conflicts with an existing network in " + pool)
                LOGGER.warning("%s: subnet conflict; skipped", pool)
                return
            if not b.path.exists():
                self.reject_resources(b.api, cloud)
                from selectel_staged_audit import audit_namespace

                audit_namespace(b, pool)
            self.eligible.append(pool)
            if not catalogs:
                return
            for role in self.roles:
                if not self.pending_role(role):
                    continue
                config = {**b.policy, "pools": [pool], "profiles": {role: b.policy["profiles"][role]}}
                with phase(LOGGER, "Каталоги и квоты кандидатов", pool=pool, role=role):
                    session = getattr(b, "cloud", None)
                    candidates = pools.candidates(config, b.credentials, session=session, cache=self.cache)
                    for candidate in candidates:
                        if candidate["region"] != pool:
                            continue
                        key = (pool, "flavor", candidate["flavor_ids"][role])
                        flavor = self.cache.read(
                            key,
                            lambda *, candidate=candidate, role=role: cloud.get(
                                "compute", "/flavors/" + candidate["flavor_ids"][role]
                            )["flavor"],
                        )
                        if not pools.flavor_available_in_zone(flavor, candidate["availability_zone"]):
                            LOGGER.warning(
                                "%s: flavor availability in the segment is unconfirmed", candidate["availability_zone"]
                            )
                            continue
                        try:
                            with phase(LOGGER, "Квоты кандидата", pool=pool, role=role):
                                b.check_node_quota(candidate, role)
                        except pools.CandidateUnavailable as error:
                            LOGGER.warning("%s/%s rejected: %s", pool, candidate["availability_zone"], error)
                            continue
                        self.choices[role].append(candidate)
            if pool not in self.retained and not any(
                c["region"] == pool for items in self.choices.values() for c in items
            ):
                self.eligible.remove(pool)

    def combination(self):
        b = self.backend
        if not self.retained <= set(self.eligible):
            return False
        if any(self.pending_role(role) and not self.choices[role] for role in self.roles):
            return False
        router = b.saved_id("global", "selectel_global_router_router_v1", "router_id")
        original = {role: list(items) for role, items in self.choices.items()}

        def accept(regions):
            b.policy["pools"] = list(regions)
            b.choices = {role: [c for c in items if c["region"] in regions] for role, items in original.items()}
            if any(self.pending_role(role) and not b.choices[role] for role in self.roles):
                return False
            try:
                with phase(LOGGER, "Полное сочетание ресурсов и квот"):
                    b.check_complete_capacity()
                    b.global_router.preflight(regions, b.api.PREFIX, router, max_networks=len(b.desired["nodes"]))
            except (pools.CandidateUnavailable, GlobalRouterCapacityError) as error:
                LOGGER.warning("Сочетание пулов отклонено: %s", error)
                return False
            return True

        if getattr(b, "_auto_discovery", False):
            try:
                b.global_router.compatible_regions(self.eligible, self.retained, accept=accept)
            except GlobalRouterPlacementUnavailable:
                return False
            return True
        return accept(self.eligible)

    @property
    def retained(self):
        return {
            s["candidate"]["region"]
            for n, s in self.backend.journal["stages"].items()
            if n.startswith("network-") and s["status"] != "deleted"
        }

    def extend(self):
        while self.index < len(self.regions):
            pool = self.regions[self.index]
            self.index += 1
            self.pool(pool)
            if self.combination():
                return True
        return False

    def start(self) -> None:
        b = self.backend
        b.resolve_pools()
        self.regions = list(b.policy["pools"])
        if not any(self.pending_role(role) for role in self.roles):
            for pool in sorted(self.retained):
                self.pool(pool, catalogs=False)
            b.policy["pools"] = sorted(self.retained)
            b.choices = {role: [] for role in self.roles}
        elif not self.extend():
            raise ValueError("No complete compatible candidates for the remaining stand")
        b.api.save(
            b.root / "docs/staged-preflight.json",
            {
                "success": True,
                "read_only": True,
                "candidate_counts": {r: len(v) for r, v in b.choices.items()},
                "global_router_discovery": True,
                "checked_pools": list(self.eligible),
                "deferred_pools": self.regions[self.index :],
            },
        )

    def candidates(self, name: str) -> Iterator[dict[str, object]]:
        seen = set()
        role = self.backend.desired["nodes"][name]["role"]
        while True:
            for candidate in self.backend.choices[role]:
                identity = json.dumps(candidate, sort_keys=True)
                if identity not in seen:
                    seen.add(identity)
                    yield candidate
            if not self.extend():
                return


def resolve_pools(backend) -> None:
    """Resolve the allowed universe from metadata; do not probe future pools."""
    backend._auto_discovery = "auto" in backend.policy["pools"]
    regions = (
        pools.ordered_pools(backend.policy, backend.cloud.catalog)
        if backend._auto_discovery
        else list(backend.policy["pools"])
    )
    if backend.path.exists():
        regions = [r for r in regions if r in backend.journal["pool_subnets"]]
    retained = {
        s["candidate"]["region"]
        for name, s in backend.journal["stages"].items()
        if name.startswith("network-") and s["status"] != "deleted"
    }
    if not retained <= set(regions):
        raise ValueError("Saved network pool is not available in the confirmed catalog")
    if not regions:
        raise ValueError("No compatible discovered pools")
    backend.policy["pools"] = regions
    if not backend.path.exists() and backend._auto_discovery:
        # Reserve deterministic CIDRs before the first network is created.
        # Catalog presence is not evidence of usable resources in these pools.
        assigned = {}
        for region in regions:
            assigned[region] = pool_subnet(backend.policy, region, assigned)
        backend.journal["pool_subnets"] = assigned


def check_node_quota(backend, candidate, role) -> None:
    cloud = backend.region(candidate["region"])
    project = backend.credentials["project_id"]
    compute = (
        cloud.get("compute", "/os-quota-sets/" + project + "/detail")["quota_set"]
        .get("az_quotas", {})
        .get(candidate["availability_zone"])
    )
    if compute is None:
        raise pools.CandidateUnavailable("Compute segment quota unknown")
    from selectel_staged_capacity import pending_credits, require_named_quota

    quota_credits = pending_credits(backend, candidate["region"])
    profile = backend.policy["profiles"][role]
    for key, needed in [("instances", 1), ("cores", profile["vcpus"]), ("ram", profile["ram_mb"])]:
        require_named_quota(
            compute.get(key),
            needed,
            role + ": compute." + key,
            quota_credits.get((candidate["availability_zone"], key), 0),
        )
    volume = cloud.get("volumev3", "/os-quota-sets/" + project + "?usage=True")["quota_set"]
    for key, needed in [
        ("gigabytes", backend.policy["boot_disk_gb"]),
        ("volumes", 1),
        ("gigabytes_" + candidate["volume_type"], backend.policy["boot_disk_gb"]),
    ]:
        require_named_quota(volume.get(key), needed, role + ": volume." + key, quota_credits.get(key, 0))
    network = cloud.get("network", "/v2.0/quotas/" + project + "/details.json")["quota"]
    for key, needed in [("port", 1), ("floatingip", int(role == "app"))]:
        if needed:
            require_named_quota(network.get(key), needed, role + ": network." + key, quota_credits.get(key, 0))
