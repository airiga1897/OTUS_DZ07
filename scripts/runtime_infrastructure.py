"""Explicit runtime operations: infrastructure."""

import json
import os
import subprocess
import time
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Literal, overload

import pool_selection as pools
import topology
from cloud_contract import InfrastructureResult, TerraformError, validate_binding
from cloud_settings import terraform_root
from credentials import environment_for_schema
from placement_config import PlacementPolicy
from progress_logging import get_logger
from runtime_ports import ApplicationPort, DesiredTopologyPort, RootPort, TerraformExecutionPort
from selectel_adapter import SelectelAdapter
from selectel_api import json_object, json_text, json_texts
from terraform_state_types import checked_state

if TYPE_CHECKING:
    from runtime import Runtime

LOGGER = get_logger(__name__)


@overload
def tf(api: TerraformExecutionPort, *args: str, capture: Literal[True]) -> str: ...
@overload
def tf(api: TerraformExecutionPort, *args: str, capture: Literal[False] = False) -> None: ...
@overload
def tf(api: TerraformExecutionPort, *args: str, capture: bool) -> str | None: ...


def tf(api: TerraformExecutionPort, *args: str, capture: bool = False) -> str | None:
    config = api.auth_config()
    env = dict(os.environ)
    env.update(environment_for_schema(config, api.AUTH_SCHEMA))
    for name in list(env):
        if name.startswith("TF_VAR_") or name in {
            "TF_LOG",
            "TF_LOG_CORE",
            "TF_LOG_PROVIDER",
            "TF_LOG_PATH",
            "OS_DEBUG",
            "OTUS_SELECTEL_AUTH",
        }:
            env.pop(name, None)
    env["TF_CLI_CONFIG_FILE"] = str(api.ROOT / (api.CLOUD["terraform_directory"] + ".rc"))
    env["TF_IN_AUTOMATION"] = "1"
    binary = str(api.TF)
    result = subprocess.run(
        [binary, "-chdir=" + str(terraform_root(api.ROOT)), *args],
        env=env,
        capture_output=True,
    )
    api.RUN.mkdir(parents=True, exist_ok=True)
    output = api.redact((result.stdout + result.stderr).decode(errors="replace"))
    (api.RUN / ("terraform-" + args[0] + "-" + str(time.time_ns()) + ".log")).write_text(output, encoding="utf-8")
    if result.returncode:
        print(output[-5000:], flush=True)
        raise TerraformError(output)
    if capture:
        return result.stdout.decode()
    print("Terraform " + args[0] + ": OK", flush=True)
    return None


def placement(api: RootPort) -> PlacementPolicy:
    return pools.policy(api.ROOT, "placement.json", "context.json")


def desired_topology(api: DesiredTopologyPort) -> topology.Topology:
    if api._TOPOLOGY_OVERRIDE is not None:
        return api._TOPOLOGY_OVERRIDE
    from elastic_options import desired

    return desired(api.ROOT, topology.load(api.ROOT))


def preflight(api: "Runtime") -> None:
    from selectel_adapter import preflight as check

    return check(api)


def prepare(api: "Runtime") -> None:
    api.preflight()
    api.cloud()
    choice = pools.selected(api.ROOT)
    if not choice:
        raise ValueError("Use prepare_selected to discover a complete pool/segment")
    images = [{"id": choice["image_id"]}]
    ids = choice["flavor_ids"]
    import ipaddress

    from operator_network import allowed_cidrs, discover_cidrs

    observed_cidrs = discover_cidrs(api.placement()["operator_ip_services"])
    secrets_path = api.prepare_identity()
    from elk_tls import certificates

    certificates(
        api.LOCAL,
        {
            n: str(ipaddress.ip_network(api.STACK["subnet_cidr"])[v["host"]])
            for n, v in api.desired_topology()["nodes"].items()
            if v["role"] == "es"
        },
    )
    variables_path = terraform_root(api.ROOT) / "terraform.tfvars.json"
    existing_cidrs = json.loads(variables_path.read_text()).get("admin_cidrs", []) if variables_path.exists() else []
    cidrs = allowed_cidrs(api.placement().get("admin_cidrs", []), observed_cidrs, existing_cidrs)
    api.save(
        variables_path,
        {
            "region": api.placement()["region"],
            "availability_zone": api.placement()["availability_zone"],
            "image_id": images[0]["id"],
            "volume_type": api.placement()["volume_type"],
            "flavor_ids": ids,
            "admin_cidrs": cidrs,
            "network_availability_zone": api.placement().get("network_availability_zone"),
            "router_availability_zone": api.placement().get("router_availability_zone"),
            "kibana_port": topology.services(api.ROOT)["kibana_port"],
            "route_probe_key": json.loads(secrets_path.read_text())["api"],
            "boot_disk_gb": api.placement()["boot_disk_gb"],
            "name_prefix": api.PREFIX,
            "subnet_cidr": api.STACK["subnet_cidr"],
            **api.desired_topology(),
        },
    )
    api.tf("init", "-input=false", "-no-color")
    api.tf("validate", "-no-color")
    import delivery

    delivery.prepare(api.ROOT)


def checked_plan(
    api: "Runtime",
    destroy: bool = False,
    allowed_new_nodes: set[str] | None = None,
    access_cidr: str | None = None,
) -> Path:
    before_ids = api.managed_ids()
    plan = api.LOCAL / ("destroy.tfplan" if destroy else "create.tfplan")
    api.tf("plan", "-input=false", "-no-color", *(["-destroy"] if destroy else []), "-out=" + str(plan))
    data = json.loads(api.tf("show", "-json", str(plan), capture=True))
    resources = data.get("prior_state", {}).get("values", {}).get("root_module", {}).get("resources", [])
    resources += data.get("planned_values", {}).get("root_module", {}).get("resources", [])
    for resource in resources:
        if resource.get("mode") == "managed":
            name = resource.get("values", {}).get("name")
            if name and name != api.PREFIX and not name.startswith(api.PREFIX + "-"):
                raise ValueError("Foreign resource in state/plan")
    for change in data.get("resource_changes", []):
        if change.get("mode") == "data":
            continue
        if not destroy:
            from infrastructure import validate_existing_identity

            validate_existing_identity(change, before_ids)
        actions = change["change"]["actions"]
        if access_cidr and actions != ["no-op"]:
            from operator_network import validate_access_plan

            if actions != ["create"]:
                raise ValueError("Route plan cannot alter existing resources")
            validate_access_plan(change, access_cidr, before_ids.get(("openstack_networking_secgroup_v2", "lab", "")))
        if actions not in (["no-op"], ["delete"] if destroy else ["create"]):
            raise ValueError("Unexpected mutation in plan: " + change["address"])
        if not destroy and allowed_new_nodes is not None and actions == ["create"]:
            from infrastructure import validate_node_creation

            validate_node_creation(change, allowed_new_nodes)
    return plan


def infrastructure_output(api: ApplicationPort) -> InfrastructureResult:
    from stage_store import require_context

    require_context(api.ROOT)
    from stage_store import deployment_exists as exists

    if exists(api.ROOT):
        from staged_runtime import output

        return output(api)
    result = InfrastructureResult.from_dict(json.loads(api.tf("output", "-json", "infrastructure", capture=True)))
    validate_binding(json.loads((api.LOCAL / "context.json").read_text()), result.binding())
    return result


def managed_ids(api: "Runtime") -> dict[object, str]:
    from stage_store import require_context

    require_context(api.ROOT)
    from stage_store import deployment_exists as exists

    if exists(api.ROOT):
        from staged_runtime import managed_ids as stage_ids

        return dict(stage_ids(api))
    path = terraform_root(api.ROOT) / "terraform.tfstate"
    from local_runtime import checked_tree

    checked_tree(path, api.ROOT.resolve())
    state = checked_state(json.loads(path.read_text())) if path.exists() else {}
    result: dict[object, str] = {}
    for resource in state.get("resources", []):
        if resource.get("mode") != "managed":
            continue
        for instance in resource.get("instances", []):
            attributes = instance.get("attributes", {})
            name = attributes.get("name")
            if (
                name
                and name != api.PREFIX
                and not json_text(name, "Foreign resource in DZ07 state").startswith(api.PREFIX + "-")
            ):
                raise ValueError("Foreign resource in DZ07 state")
            result[(resource["type"], resource["name"], str(instance.get("index_key", "")))] = json_text(
                attributes["id"], "Managed resource identity is unconfirmed"
            )
    return result


def cleanup(api: "Runtime", clean_local: bool = True) -> None:
    from stage_store import RecoveryContextMissing, require_context

    try:
        require_context(api.ROOT)
    except RecoveryContextMissing as error:
        from cloud_settings import adapter

        adapter().recover_stages(api, error)
        raise
    from stage_store import deployment_exists as exists

    if exists(api.ROOT):
        from staged_runtime import cleanup as cleanup_stages

        return cleanup_stages(api, clean_local)
    from cloud_settings import sequential_enabled as enabled

    if (
        enabled(api.ROOT)
        and not api.managed_ids()
        and not (terraform_root(api.ROOT) / "terraform.tfvars.json").exists()
    ):
        from staged_runtime import cleanup_empty

        return cleanup_empty(api, clean_local)
    from test_cycle import require_access

    require_access(api, cleanup=True)
    config = api.auth_config()
    from local_runtime import validate_state

    validate_state(api.ROOT, config, api.PREFIX)
    from ansible_operation import before_destroy

    before_destroy(api)
    pools.phase(api.ROOT, "cleanup_pending")
    state_path = terraform_root(api.ROOT) / "terraform.tfstate"
    before = checked_state(json.loads(state_path.read_text())) if state_path.exists() else {}
    own_fips = SelectelAdapter.cleanup_ids(before)
    scope_path = api.LOCAL / "cleanup-fips.json"
    ownership = (
        json_object(json.loads((api.LOCAL / "context.json").read_text()), "Invalid saved context binding")
        if (api.LOCAL / "context.json").exists()
        else {}
    )
    if own_fips:
        api.save(scope_path, {**ownership, "ids": sorted(own_fips)})
    elif scope_path.exists():
        scope = json_object(json.loads(scope_path.read_text()), "Invalid floating IP cleanup scope")
        validate_binding(scope, ownership)
        own_fips = set(json_texts(scope["ids"], "Invalid floating IP cleanup scope"))
    if (
        own_fips
        or any(r.get("mode") == "managed" and r.get("instances") for r in before.get("resources", []))
        or (terraform_root(api.ROOT) / "terraform.tfvars.json").exists()
    ):
        plan = api.checked_plan(True)
        api.tf("apply", "-input=false", "-no-color", str(plan))
        state = json.loads(api.tf("show", "-json", capture=True))
    else:
        state = {}
    managed = [
        r for r in state.get("values", {}).get("root_module", {}).get("resources", []) if r.get("mode") == "managed"
    ]
    if managed:
        raise RuntimeError("DZ07 resources remain in state")
    cloud = api.cloud()
    from selectel_adapter import audit_cleanup

    remaining, remaining_fips = audit_cleanup(cloud, api.PREFIX, own_fips)
    api.save(
        api.ROOT / "docs/cleanup-result.json",
        {
            "region": cloud.config["region"],
            "managed_remaining": len(managed),
            "named_remaining": remaining,
            "own_floating_ips_remaining": remaining_fips,
            "success": not remaining and not remaining_fips,
        },
    )
    if remaining or remaining_fips:
        raise RuntimeError("Named DZ07 resources remain outside state")
    pools.phase(api.ROOT, "destroyed")
    if clean_local:
        from artifact_lifecycle import release

        release(api.ROOT)
        from local_runtime import cleanup_runtime

        cleanup_runtime(api.ROOT)
    print("DZ07 cleanup verified; other labs untouched", flush=True)


def select_candidate(api: "Runtime", choice: PlacementPolicy) -> None:
    pass
    config = api.load_credentials()
    pools.select(api.ROOT, choice, config["project_id"], "context.json")
    api._AUTH_CONFIG = None


def deployment_candidates(api: "Runtime") -> Iterable[PlacementPolicy]:
    from run_settings import placement

    policy = placement(api.ROOT)
    return pools.candidates(policy, api.load_credentials())


def prepare_selected(api: "Runtime") -> None:
    return pools.run_candidates(api.deployment_candidates(), api.select_candidate, api.prepare, lambda: None)
