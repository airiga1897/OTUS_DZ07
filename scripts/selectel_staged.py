"""Selectel backend for independently retained networks, attachments and VMs."""

import ipaddress
import json
import os
import re
import uuid
from collections.abc import Callable, Iterable, Mapping
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import pool_selection as pools
import privacy
from cloud_contract import (
    ConfigurationTarget,
    InfrastructureNode,
    InfrastructureResult,
    StandDescription,
    TerraformError,
    checked_node,
)
from credentials import credential_values, load_credentials, to_environment
from local_runtime import checked_tree
from placement_config import PlacementPolicy, checked
from plan_diagnostics import PlannedResource
from progress_logging import get_logger
from run_settings import placement as placement_settings
from run_settings import snapshot as settings_snapshot
from selectel_adapter import SelectelAdapter, discover_capabilities, reject_cloud_resources
from selectel_api import json_text
from selectel_global_router import GlobalRouter
from selectel_reports import DeletionAudit, EmptyNamespaceAudit, NetworkVerification, PoolAudit
from stage_journal import StageJournal, StageOutput
from stage_journal import checked as checked_journal
from stage_store import JOURNAL, StageStore, atomic_json
from topology import Topology

if TYPE_CHECKING:
    from runtime_ports import FilePort, RootPort, StagedControllerPort
    from selectel_placement import PlacementScan

LOGGER = get_logger(__name__)


def enabled(root: Path) -> bool:
    path = Path(root) / "config/placement.json"
    return path.exists() and json.loads(path.read_text()).get("sequential_placement", {}).get("enabled") is True


def validate_policy(policy: object, desired: Topology) -> None:
    policy = checked(policy)
    config = policy["sequential_placement"]
    if set(config["node_order"]) != set(desired["nodes"]) or len(config["node_order"]) != len(desired["nodes"]):
        raise ValueError("Sequential node order must contain each desired node once")
    subnets = config["pool_subnets"]
    if set(policy["pools"]) & set(policy["excluded_pools"]):
        raise ValueError("Requested pool is explicitly excluded")
    if ("auto" not in policy["pools"] and set(subnets) != set(policy["pools"])) or set(subnets) & set(
        policy["excluded_pools"]
    ):
        raise ValueError("Pool subnet configuration is inconsistent with pool policy")
    nets = [ipaddress.ip_network(c) for c in subnets.values()]
    if any(n.version != 4 or n.prefixlen != 24 or not n.is_private for n in nets):
        raise ValueError("Sequential networks require private IPv4 /24 subnets")
    if any(a.overlaps(b) for i, a in enumerate(nets) for b in nets[i + 1 :]):
        raise ValueError("Global Router subnets must not overlap")

    slots = config["addresses"]
    reserved = [slots["cloud_gateway"], slots["global_gateway"], *slots["service_hosts"]]
    if (
        len(slots["service_hosts"]) != 2
        or len(set(reserved)) != len(reserved)
        or any(type(h) is not int or not 1 <= h <= 254 for h in reserved)
        or not 1 <= slots["dhcp_first"] <= slots["dhcp_last"] <= 254
    ):
        raise ValueError("Invalid network service address reservations")
    dhcp = set(range(slots["dhcp_first"], slots["dhcp_last"] + 1))
    if dhcp & set(reserved) or any(node["host"] in dhcp | set(reserved) for node in desired["nodes"].values()):
        raise ValueError("Node, DHCP and gateway addresses must not overlap")


def terraform_environment(credentials: Mapping[str, str], candidate: Mapping[str, object]) -> dict[str, str]:
    region = candidate.get("region", credentials["region"])
    if not isinstance(region, str):
        raise ValueError("Terraform requires a confirmed region")
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("OS_") and k not in {"INFRA_REGION", "INFRA_PROJECT_ID", "OTUS_SELECTEL_AUTH"}
    }
    env.update(to_environment(credentials))
    env.update(OS_REGION_NAME=region, OS_DOMAIN_NAME=credentials["domain_name"])
    return env


def validate_local_result(
    api: "RootPort", result: InfrastructureResult, journal: Mapping[str, object], store: StageStore
) -> None:
    for name, node in result.nodes.items():
        for entry in store.entries(name):
            attrs = entry["attrs"]
            if attrs.get("region") not in (None, "", node["region"]) or attrs.get("tenant_id") not in (
                None,
                "",
                journal["project_id"],
            ):
                raise ValueError("Foreign Selectel pool or project in local state")


def reject_shared_namespace(api: "FilePort", credentials: Mapping[str, str] | None = None) -> None:
    from credentials import load_credentials
    from selectel_global_router import GlobalRouter

    def owned(item: Mapping[str, object]) -> bool:
        name = item.get("name")
        if isinstance(name, str) and (name == api.PREFIX or name.startswith(api.PREFIX + "-")):
            return True
        tags = item.get("tags")
        return isinstance(tags, list) and any(isinstance(tag, str) and tag.startswith(api.PREFIX + "-") for tag in tags)

    client = GlobalRouter(credentials or load_credentials())
    for kind in ("routers", "networks", "subnets"):
        if any(owned(item) for item in client.listing(kind)):
            raise RuntimeError("Global Router namespace already exists; recover its context before continuing")


def audit_empty_scope(api: "FilePort", *, expanded: bool = False) -> EmptyNamespaceAudit:
    from progress_logging import phase
    from selectel_adapter import SelectelAdapter, audit_service_exclusions, reject_cloud_resources
    from selectel_scope_audit import audit_pool, read_independent, saved_scope, workers

    credentials = load_credentials()
    saved = None if expanded else saved_scope(api, credentials["project_id"])
    exclusions = audit_service_exclusions(api.ROOT) if saved is None and not expanded else {}
    cloud = SelectelAdapter(credentials)
    if saved is None:
        LOGGER.warning(
            "Данные восстановления отсутствуют или неполны: расширенный поиск по пространству имён во всех обнаруженных пулах текущего проекта"
        )
        regions = cloud.resource_regions()
    else:
        LOGGER.info("Аудит использует сохранённую область стенда: %s", ", ".join(saved))
        regions = saved
    if not regions:
        raise ValueError("Resource API audit scope is unknown")

    def check(pool: str) -> PoolAudit:
        return audit_pool(api, cloud.read_only_pool(pool), pool, reject_cloud_resources, exclusions.get(pool, ()))

    def check_shared() -> PoolAudit:
        reject_shared_namespace(api, credentials)
        return {"excluded_services": []}

    tasks: list[tuple[str, Callable[[], PoolAudit]]] = [(pool, partial(check, pool)) for pool in regions]
    tasks.append(("global-router", check_shared))
    with phase(LOGGER, "Итоговый аудит региональных и общих ресурсов"):
        checked = read_independent(tasks, workers(api.ROOT))
    excluded = {pool: checked[pool]["excluded_services"] for pool in regions if checked[pool]["excluded_services"]}
    return {
        "empty_without_context": True,
        "pools_audited": regions,
        "global_router_remaining": 0,
        "scope_source": "saved-context" if saved is not None else "expanded-service-catalog",
        "audit_complete": True,
        "audit_complete_within_policy": True,
        "service_exclusions": excluded,
    }


def recovery_search(api: "FilePort", original: BaseException) -> None:
    """Discover only; never delete with a lost/corrupted resource binding."""
    LOGGER.warning(
        "Данные восстановления недостаточны: запускается расширенный read-only поиск; удаление заблокировано"
    )
    report = {
        "success": False,
        "recovery_files_preserved": True,
        "recovery_error_type": type(original).__name__,
        "audit_complete": False,
    }
    try:
        report["read_only_search"] = audit_empty_scope(api, expanded=True)
        report["audit_complete"] = True
    except Exception as error:
        report["audit_error_type"] = type(error).__name__
    try:
        api.save(api.ROOT / "docs/cleanup-result.json", report)
    except Exception as error:
        LOGGER.error(
            "Не удалось сохранить результат read-only поиска (%s); первоначальная ошибка сохранена",
            type(error).__name__,
        )


class Backend:
    def __init__(self, api: "StagedControllerPort", cleanup: bool = False) -> None:
        self.api, self.root = api, Path(api.ROOT).resolve()
        self._auto_discovery = False
        self.path = self.root / ".local" / JOURNAL
        checked_tree(self.path, self.root)
        self.credentials = load_credentials()
        privacy.remember(credential_values(self.credentials))
        saved = checked_journal(json.loads(self.path.read_text())) if self.path.exists() else None
        self.policy = (
            placement_settings(self.root)
            if not cleanup or not saved or not saved.get("run_settings")
            else {
                **json.loads((self.root / "config/placement.json").read_text()),
                **(saved["run_settings"] or {}),
                "pools": list(saved["pool_subnets"]),
            }
        )
        self.desired = saved["topology"] if cleanup and saved else api.desired_topology()
        from elastic_options import node_order

        if not cleanup:
            node_order(self.policy, saved or {}, self.desired)
            validate_policy(self.policy, self.desired)
        if not self.path.exists():
            legacy = self.root / "terraform-selectel"
            if (
                any((self.root / ".local" / n).exists() for n in ("context.json", "placement.json", "configured.json"))
                or (legacy / "terraform.tfvars.json").exists()
                or (self.root / ".local/stages").exists()
            ):
                raise ValueError("Existing legacy context or orphan stages require separate recovery")
        self.journal: StageJournal = (
            saved
            if saved is not None
            else {
                "provider": "selectel",
                "scope": {"project_id": self.credentials["project_id"]},
                "project_id": self.credentials["project_id"],
                "namespace": api.PREFIX,
                "deployment_id": uuid.uuid4().hex,
                "stages": {},
                "configured": False,
                "run_settings": settings_snapshot(self.policy),
                "bootstrap_region": self.credentials["region"],
                "require_distinct_hosts": self.policy.get("require_distinct_hosts", True),
                "pool_subnets": self.policy["sequential_placement"]["pool_subnets"],
                "topology": self.desired,
                "address_policy": self.policy["sequential_placement"]["addresses"],
            }
        )
        self.validate_journal(cleanup)
        self.cloud = SelectelAdapter(self.credentials)
        self.global_router = GlobalRouter(self.credentials)
        from terraform_plugins import cli_path

        self.store = StageStore(
            api,
            self.journal,
            environment=lambda candidate: terraform_environment(self.credentials, candidate),
            cli_config=cli_path(self.root),
            drift_validator=self.allow_refresh,
        )
        self.choices: dict[str, list[PlacementPolicy]] = {}
        self._temporary_networks: set[str] = set()

    def prepare_tools(self) -> None:
        from terraform_plugins import prepare

        self.store.cli_config = prepare(self.api)

    def validate_journal(self, cleanup: bool = False) -> None:
        if (
            self.journal["provider"] != "selectel"
            or self.journal.get("scope") != {"project_id": self.credentials["project_id"]}
            or self.journal["project_id"] != self.credentials["project_id"]
            or self.journal["namespace"] != self.api.PREFIX
            or (not cleanup and self.journal["topology"] != self.desired)
        ):
            raise ValueError("Sequential deployment scope or topology mismatch; preserve recovery files")
        if not re.fullmatch("[0-9a-f]{32}", self.journal["deployment_id"]):
            raise ValueError("Invalid deployment identity")
        for name, record in self.journal["stages"].items():
            from selectel_intent import validate as validate_intent

            validate_intent(self, name)
            region = record["candidate"].get("region")
            allowed = set(self.journal["topology"]["nodes"]) | {"global"}
            allowed |= {"network-" + p for p in self.journal["pool_subnets"]}
            allowed |= {"connection-" + p for p in self.journal["pool_subnets"]}
            global_region = self.journal.get("bootstrap_region", self.credentials["region"])
            valid_region = region == global_region if name == "global" else region in self.journal["pool_subnets"]
            if name not in allowed or not valid_region:
                raise ValueError("Stage is outside the saved deployment scope")
        if cleanup:
            self.desired = self.journal["topology"]
            return
        if self.journal.get("require_distinct_hosts", True) != self.policy.get("require_distinct_hosts", True):
            raise ValueError("Existing physical-host policy cannot change")
        previous = self.journal.get("run_settings") or settings_snapshot(self.policy)
        current = settings_snapshot(self.policy)
        if any(
            previous.get(key) != current.get(key)
            for key in ("profiles", "boot_disk_gb", "disk_type_patterns", "require_distinct_hosts")
        ):
            raise ValueError("Existing VM and physical-host settings cannot change; preserve the current stand")
        if self.journal["address_policy"] != self.policy["sequential_placement"]["addresses"]:
            raise ValueError("Existing network address reservations cannot change")
        if any(
            self.journal["pool_subnets"].get(p) != cidr
            for p, cidr in self.policy["sequential_placement"]["pool_subnets"].items()
            if p in self.journal["pool_subnets"]
        ):
            raise ValueError("Existing pool networks cannot be readdressed")
        if not all(
            ("auto" in self.policy["pools"] or k in self.policy["pools"]) and k not in self.policy["excluded_pools"]
            for k in self.journal["pool_subnets"]
        ):
            raise ValueError("Existing deployment uses a now forbidden pool; explicit recovery required")

    @property
    def owner(self) -> str:
        return self.api.PREFIX + "-" + self.journal["deployment_id"]

    def save(self) -> None:
        # Persist the write-ahead record before every operation, including failed cleanup.
        atomic_json(self.path, self.journal, self.root)

    def region(self, region: str) -> SelectelAdapter:
        return self.cloud.read_only_pool(region)

    def allow_refresh(self, name: str, drift: PlannedResource) -> bool:
        """Accept only proven, read-only changes derived from our saved resources."""
        kind = drift["type"]
        if not kind.startswith("openstack_"):
            return False
        before, after = drift["change"].get("before") or {}, drift["change"].get("after") or {}
        changed = {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}
        from selectel_refresh import NODE_KINDS, allow_node_refresh, string_set

        if kind in NODE_KINDS and changed - {"tags", "all_tags"} and name in self.desired["nodes"]:
            return allow_node_refresh(self, name, drift)
        if kind == "openstack_blockstorage_volume_v3" and changed == {"attachment"} and name in self.desired["nodes"]:
            entries = self.store.entries(name)
            servers = [e["attrs"]["id"] for e in entries if e["type"] == "openstack_compute_instance_v2"]
            volumes = [e for e in entries if e["type"] == kind and e["address"] == drift["address"]]
            if len(servers) != 1 or len(volumes) != 1 or volumes[0]["attrs"]["id"] != after.get("id"):
                return False
            old, new = before.get("attachment") or [], after.get("attachment") or []
            # Cinder records the volume before Nova attaches it as the boot disk.
            # Refresh may discover that attachment; it must never detach it or
            # point to another VM. Planned writes are checked independently.
            return (
                isinstance(old, list)
                and isinstance(new, list)
                and len(new) == 1
                and len(old) <= 1
                and all(isinstance(a, dict) and a.get("instance_id") == servers[0] for a in old + new)
            )
        if kind == "openstack_compute_servergroup_v2" and changed == {"members"} and name.startswith("network-"):
            pool = name.removeprefix("network-")
            expected: set[object] = set()
            known: set[object] = set()
            for node, specification in self.desired["nodes"].items():
                record = self.journal["stages"].get(node, {})
                if specification["role"] != "es" or record.get("candidate", {}).get("region") != pool:
                    continue
                entries = self.store.entries(node)
                if self.journal.get("require_distinct_hosts", True):
                    expected.update(e["attrs"]["id"] for e in entries if e["type"] == "openstack_compute_instance_v2")
                known.update(
                    e["attrs"]["id"]
                    for e in entries + record.get("audit_entries", [])
                    if e["type"] == "openstack_compute_instance_v2"
                )
            new_members, old_members = string_set(after.get("members")), string_set(before.get("members"))
            return new_members == expected and old_members is not None and old_members <= known
        for key in changed:
            old, new = before.get(key), after.get(key)
            if key in ("tags", "all_tags") and old in (None, []) and new in (None, []):
                continue
            if kind == "openstack_networking_network_v2" and key == "all_tags" and name.startswith("network-"):
                pool = name.removeprefix("network-")
                attachment = self.journal["stages"].get("connection-" + pool, {}).get("output", {}).get("network_id")
                expected = {self.owner, "l3vpn-network", "l3vpn-network-" + attachment} if attachment else set()
                if (
                    expected
                    and string_set(old) in ({self.owner}, expected)
                    and string_set(new) in ({self.owner}, expected)
                ):
                    continue
            return False
        return bool(changed)

    def preflight(self) -> None:
        from selectel_placement import PlacementScan

        self._placement_scan = PlacementScan(self, discover_capabilities, reject_cloud_resources)
        self._placement_scan.start()

    def resolve_pools(self) -> None:
        from selectel_placement import resolve_pools

        return resolve_pools(self)

    def check_node_quota(self, candidate: PlacementPolicy, role: str) -> None:
        from selectel_placement import check_node_quota

        return check_node_quota(self, candidate, role)

    def check_complete_capacity(self) -> set[str]:
        from selectel_staged_capacity import require_capacity

        return require_capacity(self)

    def saved_id(self, name: str, kind: str, output_key: Literal["id", "network_id", "router_id"]) -> str | None:
        record = self.journal["stages"].get(name, {})
        if record.get("status") == "deleted":
            return None
        if record.get("output", {}).get(output_key):
            return record["output"][output_key]
        ids = [e["attrs"]["id"] for e in self.store.entries(name) if e["type"] == kind]
        if len(ids) > 1:
            raise ValueError("Ambiguous partial stage identity")
        identifier = ids[0] if ids else None
        if identifier is not None and not isinstance(identifier, str):
            raise ValueError("Partial stage identity is unconfirmed")
        return identifier

    def completed(self, name: str) -> bool:
        return bool(self.journal["stages"].get(name, {}).get("status") == "active")

    def validate_existing(self) -> None:
        if self.journal.get("destroy_requested"):
            raise ValueError("A full destroy was started; finish destroy before creating resources")
        from selectel_staged_audit import assert_stage_owned

        for name, stage in self.journal["stages"].items():
            if stage["status"] != "deleted":
                assert_stage_owned(self, name, active=stage["status"] == "active")

    def recover_attempt(self, name: str) -> None:
        if self.journal["stages"].get(name, {}).get("status") == "deleting":
            self.cleanup_attempt(name)

    def deployment_order(self) -> list[str]:
        return self.policy["sequential_placement"]["node_order"]

    def candidates_for(self, name: str) -> Iterable[PlacementPolicy]:
        scan: PlacementScan | None = getattr(self, "_placement_scan", None)
        return scan.candidates(name) if scan is not None else self.choices[self.desired["nodes"][name]["role"]]

    def pending(self, name: str) -> Mapping[str, object] | None:
        item = self.journal["stages"].get(name)
        return item["candidate"] if item and item["status"] != "deleted" else None

    def identities(self, exclude_pending: bool = False) -> dict[tuple[str, str], str]:
        return {
            (name, address): identifier
            for name, record in self.journal["stages"].items()
            if not exclude_pending or record["status"] == "active"
            for address, identifier in self.store.identities(name).items()
        }

    def assert_preserved(self, before: Mapping[tuple[str, str], str]) -> None:
        now = self.identities()
        if any(now.get(key) != identifier for key, identifier in before.items()):
            raise RuntimeError("Previously created resource identity changed")

    def begin(self, name: str, candidate: Mapping[str, object]) -> None:
        if self.store.identities(name):
            raise ValueError("Cannot change candidate with resources remaining in its stage")
        from selectel_intent import intent

        self.journal["stages"][name] = {
            "candidate": checked(candidate),
            "status": "pending",
            "intent": intent(self, name, candidate),
        }
        self.save()

    def ensure_pool(self, candidate: Mapping[str, object]) -> StageOutput:
        from selectel_stage_operations import ensure_pool

        return ensure_pool(self, candidate)

    def connect_pool(self, pool: str) -> None:
        from selectel_stage_operations import connect_pool

        return connect_pool(self, pool)

    def inputs(self, name: str) -> tuple[str, dict[str, object]]:
        from selectel_stage_operations import inputs

        return inputs(self, name)

    def configure_stage(self, name: str, kind: str, inputs: dict[str, object]) -> None:
        from selectel_stage_operations import configure_stage

        return configure_stage(self, name, kind, inputs)

    def extend_admin_access(self, lab: StandDescription, cidr: str) -> list[str]:
        from selectel_stage_operations import extend_admin_access

        return extend_admin_access(self, lab, cidr)

    def ssh_host_key(self, lab: StandDescription, name: str, cached: str | None) -> str:
        return self.region(lab["node_regions"][name]).ssh_host_key(
            name, lab["server_ids"][name], self.api.PREFIX, cached
        )

    def console_output(self, lab: StandDescription, name: str) -> str:
        output = self.region(lab["node_regions"][name]).get(
            "compute", "/servers/" + lab["server_ids"][name] + "/action", {"os-getConsoleOutput": {"length": 1000}}
        )["output"]
        return json_text(output, "Console output is unconfirmed")

    def verify_placement(self) -> NetworkVerification:
        from selectel_staged_audit import verify_networks

        return verify_networks(self)

    def cleanup_order(self) -> list[str]:
        from selectel_staged_audit import cleanup_order

        return cleanup_order(self)

    def audit_deletion(self) -> DeletionAudit:
        from selectel_staged_audit import audit_deletion

        return audit_deletion(self)

    def apply(self, name: str) -> StageOutput:
        from selectel_stage_operations import apply

        return apply(self, name)

    def finish(self, name: str) -> None:
        from selectel_stage_operations import finish

        return finish(self, name)

    def reapply(self, name: str) -> None:
        from selectel_stage_operations import reapply

        return reapply(self, name)

    def capture_fault(self, name: str) -> None:
        from selectel_staged_audit import faults

        report = {"stage": name, "read_only": True}
        try:
            report.update(faults(self, name))
        except (OSError, ValueError, KeyError, RuntimeError) as error:
            report["diagnostic_error"] = {"type": type(error).__name__, "http_status": getattr(error, "code", None)}
        try:
            logs = list(getattr(self.api, "RUN", self.root / ".local/runs").glob("stage-" + name + "-*.log"))
            if logs:
                last = max(logs, key=lambda p: p.stat().st_mtime_ns)
                checked_tree(last, self.root)
                report["terraform_error"] = self.api.redact(last.read_text())
            self.api.save(self.root / "docs/placement-faults" / (name + "-" + uuid.uuid4().hex + ".json"), report)
        except (OSError, ValueError, KeyError) as error:
            LOGGER.error("Cannot preserve stage fault: %s", type(error).__name__)

    def is_capacity_error(self, error: Exception, name: str) -> bool:
        if isinstance(error, pools.CandidateUnavailable) and not self.store.identities(name):
            return True
        if not isinstance(error, TerraformError):
            return False
        text = getattr(error, "output", "")
        from selectel_staged_audit import faults

        if name in self.desired["nodes"]:
            try:
                text += json.dumps(faults(self, name))
            except (OSError, ValueError, KeyError, RuntimeError) as diagnostic_error:
                LOGGER.error(
                    "Scheduler diagnostics unavailable (%s); preserve original error and resources",
                    type(diagnostic_error).__name__,
                )
                return False
        normalized = " ".join(text.lower().split())
        return any(
            x in normalized
            for x in ("no valid host", "novalidhost", "not enough hosts", "exhausted all retry attempts")
        )

    def cleanup_attempt(self, name: str) -> None:
        from selectel_staged_audit import cleanup_attempt

        return cleanup_attempt(self, name)

    def prune_unused_network(self, pool: str) -> None:
        from selectel_staged_audit import prune_unused_network

        return prune_unused_network(self, pool)

    def configuration_target(self) -> ConfigurationTarget:
        record = self.journal["stages"]["app"]
        if record["status"] == "deleted":
            # This marker is saved only after state and API deletion audits.
            identity = record["output"].get("id")
            if not isinstance(identity, str) or not identity:
                raise ValueError("Deleted app stage has no saved VM identity")
            return ConfigurationTarget({"server_ids": {"app": identity}}, deletion_verified=True)
        return ConfigurationTarget(self.result().application(), deletion_verified=False)

    def result(self) -> InfrastructureResult:
        nodes: dict[str, InfrastructureNode] = {
            n: checked_node(n, self.journal["stages"][n]["output"]) for n in self.desired["nodes"]
        }
        app = nodes["app"]
        result = InfrastructureResult(
            provider="selectel",
            scope={"project_id": self.credentials["project_id"]},
            namespace=self.api.PREFIX,
            placement={
                "region": app["region"],
                "zone": app["zone"],
                "node_placements": {n: {"region": v["region"], "zone": v["zone"]} for n, v in nodes.items()},
                "pool_subnets": self.journal["pool_subnets"],
            },
            nodes=nodes,
            subnet_cidr=app["subnet_cidr"],
        )
        result.validate()
        return result
