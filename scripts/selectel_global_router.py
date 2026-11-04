"""Read-only Global Router discovery and ownership/deletion audit."""

from collections.abc import Callable, Collection, Mapping, Sequence
from typing import cast
from urllib.parse import urlencode

from credentials import schema as auth_schema
from selectel_api import identity_scope, request_json, subject_token

ENDPOINT = "https://api.selectel.ru/naas/v1"
type RouterID = str | int


def response_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("Unknown Global Router response object")
    return cast(dict[str, object], value)


def response_rows(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise ValueError("Unknown Global Router collection response")
    return [response_object(row) for row in value]


def identifier(value: object) -> RouterID:
    if type(value) not in (str, int) or value == "":
        raise ValueError("Unknown Global Router identifier")
    return cast(RouterID, value)


def group_ids(value: object) -> set[RouterID]:
    return {identifier(row.get("id")) for row in response_rows(value)}


def tags(row: Mapping[str, object]) -> list[str]:
    value = row.get("tags", [])
    if not isinstance(value, list) or any(not isinstance(tag, str) for tag in value):
        raise ValueError("Unknown Global Router ownership tags")
    return cast(list[str], value)


def quota_limit(row: Mapping[str, object]) -> int:
    limit = row.get("limit")
    if type(limit) is not int or limit < -1:
        raise ValueError("Unknown Global Router quota limit")
    return limit


def matches_namespace(row: Mapping[str, object], prefix: str) -> bool:
    name = row.get("name", "")
    if not isinstance(name, str):
        raise ValueError("Unknown Global Router resource name")
    return name.startswith(prefix) or any(tag.startswith(prefix + "-") for tag in tags(row))


class GlobalRouterCapacityError(ValueError):
    """A known quota shortage, distinct from an unknown/failed API response."""


class GlobalRouterPlacementUnavailable(ValueError):
    """A confirmed placement mismatch; transport/schema failures remain distinct."""


class GlobalRouter:
    def __init__(self, credentials: Mapping[str, str]) -> None:
        self.credentials = credentials
        self.token: str | None = None
        self._zone_catalog: list[dict[str, object]] | None = None

    def authenticate(self) -> None:
        credentials = self.credentials
        body = {
            "auth": {
                "identity": {
                    "methods": ["password"],
                    "password": {
                        "user": {
                            "name": credentials["username"],
                            "password": credentials["password"],
                            "domain": {"name": credentials["domain_name"]},
                        }
                    },
                },
                "scope": {"domain": {"name": credentials["domain_name"]}},
            }
        }
        result, headers = request_json(auth_schema()["auth_url"] + "/auth/tokens", payload=body)
        domain = identity_scope(result).get("domain")
        if not isinstance(domain, dict) or domain.get("name") != credentials["domain_name"]:
            raise ValueError("Global Router account scope unconfirmed")
        self.token = subject_token(headers)

    def get(self, path: str) -> object:
        if not path.startswith("/") or ".." in path:
            raise ValueError("Invalid Global Router API path")
        if self.token is None:
            self.authenticate()
        return request_json(ENDPOINT + path, token=self.token)[0]

    def listing(self, kind: str) -> list[dict[str, object]]:
        if kind not in ("routers", "networks", "subnets", "zones", "quotas"):
            raise ValueError("Unexpected Global Router collection")
        cached: list[dict[str, object]] | None = getattr(self, "_zone_catalog", None)
        if kind == "zones" and cached is not None:
            from copy import deepcopy

            return deepcopy(cached)
        result: list[dict[str, object]] = []
        seen: set[RouterID] = set()
        for page in range(1, 1001):
            query: dict[str, int | str] = {"page": page, "limit": 200}
            if kind == "zones":
                query["show_groups"] = "true"
            data = response_rows(self.get("/" + kind + "?" + urlencode(query)))
            ids = {identifier(item.get("id")) for item in data}
            if len(ids) != len(data) or ids & seen:
                raise ValueError("Duplicate/cyclic Global Router pagination")
            seen.update(ids)
            result.extend(data)
            if len(data) < 200:
                if kind == "zones":
                    from copy import deepcopy

                    self._zone_catalog = deepcopy(result)
                return result
        raise ValueError("Global Router pagination limit exceeded")

    def compatible_regions(
        self,
        regions: Sequence[str],
        retained: Collection[str] = (),
        accept: Callable[[list[str]], bool] | None = None,
    ) -> list[str]:
        """Select one actual VPC zone group, preferring the requested pool order."""
        zones = self.listing("zones")
        groups: dict[str, set[RouterID]] = {}
        for region in regions:
            matches = [z for z in zones if z.get("name") == region and z.get("service") == "vpc"]
            if not matches or (
                len(matches) == 1 and (matches[0].get("enable") is False or matches[0].get("allow_create") is False)
            ):
                print(region + ": Global Router VPC zone unavailable; skipped", flush=True)
                continue
            if (
                len(matches) != 1
                or matches[0].get("enable") is not True
                or not isinstance(matches[0].get("groups"), list)
            ):
                raise ValueError("Unknown Global Router zone capabilities: " + region)
            groups[region] = group_ids(matches[0]["groups"])
            if not groups[region]:
                print(region + ": no Global Router zone group; skipped", flush=True)
        allowed = set.union(*groups.values()) if groups else set()
        for region in retained:
            if region not in groups:
                raise ValueError("Saved network has no confirmed Global Router zone: " + region)
            allowed &= groups[region]
        if not allowed:
            raise GlobalRouterPlacementUnavailable("No compatible Global Router zone group")
        if len({type(group) for group in allowed}) > 1:
            raise ValueError("Unknown mixed Global Router group identifier types")
        ordered = sorted(
            allowed,
            key=lambda group: (
                next(i for i, r in enumerate(regions) if group in groups.get(r, set())),
                -sum(group in g for g in groups.values()),
                group,
            ),
        )
        for selected in ordered:
            result = [r for r in regions if selected in groups.get(r, set())]
            if accept is not None and not accept(result):
                print(", ".join(result) + ": zone group cannot place the remaining stand; skipped", flush=True)
                continue
            for region in groups:
                if region not in result:
                    print(region + ": incompatible Global Router zone group; skipped", flush=True)
            return result
        raise GlobalRouterPlacementUnavailable("No zone group can place the complete stand")

    def preflight(
        self, regions: list[str], prefix: str, saved_router: str | None = None, max_networks: int | None = None
    ) -> None:
        zones = self.listing("zones")
        common_groups = None
        for region in regions:
            matches = [z for z in zones if z.get("name") == region and z.get("service") == "vpc"]
            if (
                len(matches) != 1
                or matches[0].get("enable") is not True
                or matches[0].get("allow_create", True) is not True
            ):
                raise ValueError("Global Router VPC zone is unavailable or unknown: " + region)
            groups = group_ids(matches[0].get("groups", []))
            common_groups = groups if common_groups is None else common_groups & groups
        if not common_groups:
            raise ValueError("A common Global Router zone group is unsupported or unconfirmed")
        routers = self.listing("routers")
        if not saved_router and any(matches_namespace(row, prefix) for row in routers):
            raise ValueError("Global Router namespace exists outside this deployment")
        quotas = self.listing("quotas")
        limits = (
            [quota_limit(q) for q in quotas if q.get("name") == "routers" and q.get("scope") == "account_id"]
            if not saved_router
            else []
        )
        if not saved_router and len(limits) != 1:
            raise ValueError("Global Router quota unavailable")
        if not saved_router and limits[0] != -1 and len(routers) + 1 > limits[0]:
            raise GlobalRouterCapacityError("Global Router quota insufficient")
        networks = self.listing("networks")
        subnets = self.listing("subnets")
        self.check_attachment_quotas(quotas, networks, subnets, regions, saved_router, max_networks)

    def check_attachment_quotas(
        self,
        quotas: Sequence[Mapping[str, object]],
        networks: Sequence[Mapping[str, object]],
        subnets: Sequence[Mapping[str, object]],
        regions: Sequence[str],
        router_id: str | None,
        max_networks: int | None = None,
    ) -> None:
        own_networks = {identifier(n["id"]) for n in networks if router_id and n.get("router_id") == router_id}
        own_subnets = {identifier(s["id"]) for s in subnets if s.get("network_id") in own_networks}
        for name, rows, own in [("networks", networks, own_networks), ("subnets", subnets, own_subnets)]:
            matching = [
                q
                for q in quotas
                if q.get("name") == name
                and (
                    q.get("scope") == "account_id"
                    or (router_id and q.get("scope") == "router_id" and q.get("scope_value") == router_id)
                )
            ]
            if not matching:
                raise ValueError("Global Router quota is unknown before allocation: " + name)
            for quota in matching:
                used = len(rows) if quota["scope"] == "account_id" else len(own)
                needed = (
                    max(0, min(len(regions), max_networks) - len(own))
                    if max_networks is not None
                    else max(0, len(regions) - len(own))
                )
                if needed:
                    limit = quota_limit(quota)
                    if limit != -1 and used + needed > limit:
                        raise GlobalRouterCapacityError("Insufficient Global Router quota: " + name)

    def assert_owned(
        self, kind: str, identifier: str, owner: str, expected_project: str | None = None
    ) -> dict[str, object]:
        item = response_object(self.get("/" + kind + "/" + identifier))
        if item.get("id") != identifier or owner not in tags(item):
            raise ValueError("Global Router ownership unconfirmed")
        if expected_project is not None and item.get("project_id") != expected_project:
            raise ValueError("Global Router attachment belongs to another project")
        return item

    def assert_absent(self, identifiers: dict[str, set[str]], owner: str) -> None:
        # Collection discovery succeeded independently; 404 is never used as proof.
        for kind in ("subnets", "networks", "routers"):
            if any(item["id"] in identifiers.get(kind, set()) or owner in tags(item) for item in self.listing(kind)):
                raise RuntimeError("Own Global Router resources remain: " + kind)
