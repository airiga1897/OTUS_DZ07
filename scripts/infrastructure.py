"""Terraform boundary: topology inputs, append-only plan and resource creation."""

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING

from cloud_settings import terraform_root
from plan_diagnostics import PlannedResource
from topology import Topology

if TYPE_CHECKING:
    from runtime import Runtime


def validate_existing_identity(change: PlannedResource, existing: Mapping[object, object]) -> None:
    key = (change.get("type"), change.get("name"), str(change.get("index", "")))
    if key in existing and (change["change"].get("before") or {}).get("id") != existing[key]:
        raise ValueError("Existing resource identity changed before apply: " + change["address"])


def validate_node_creation(change: PlannedResource, allowed: set[str]) -> None:
    from cloud_settings import adapter

    adapter().validate_node_creation(change, allowed)


def apply(api: "Runtime", desired: Topology, allowed_new_nodes: set[str] | None = None) -> None:
    variables = terraform_root(api.ROOT) / "terraform.tfvars.json"
    if variables.exists():
        value = json.loads(variables.read_text(encoding="utf-8"))
        from stack_config import load

        settings = load()
        value.setdefault("name_prefix", settings["name_prefix"])
        value.setdefault("subnet_cidr", settings["subnet_cidr"])
        value.update(desired)
        api.save(variables, value)
    api.preflight()
    plan = api.checked_plan() if allowed_new_nodes is None else api.checked_plan(allowed_new_nodes=allowed_new_nodes)
    api.pools.phase(api.ROOT, "creating")
    api.tf("apply", "-input=false", "-no-color", str(plan))
