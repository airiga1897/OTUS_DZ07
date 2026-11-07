"""Public allowlisted results of Selectel ownership and placement checks."""

from typing import Literal, NotRequired, TypedDict


class StageFaults(TypedDict):
    stage: str
    nodes: dict[str, dict[str, object]]
    read_only: Literal[True]
    global_router: NotRequired[dict[str, dict[str, object]]]


class NetworkVerification(TypedDict):
    success: bool
    es_distinct_hosts: int | None
    require_distinct_hosts: bool
    physical_host_separation_verified: bool
    private_ports_filtered: bool
    global_routes_confirmed: bool


class DeletionAudit(TypedDict):
    pools_audited: list[str]
    global_router_remaining: int
    audit_complete: bool


class PoolAudit(TypedDict):
    excluded_services: list[str]


class EmptyNamespaceAudit(DeletionAudit):
    empty_without_context: bool
    scope_source: Literal["saved-context", "expanded-service-catalog"]
    audit_complete_within_policy: bool
    service_exclusions: dict[str, list[str]]
