"""Canonical resource entries derived from a stage's private Terraform state."""

from typing import NotRequired, TypedDict, cast


class StateEntry(TypedDict):
    type: str
    attrs: dict[str, object]
    address: NotRequired[str]
    index: NotRequired[str | int | None]


class StateInstance(TypedDict, total=False):
    attributes: dict[str, object]
    index_key: str | int | None


class StateResource(TypedDict, total=False):
    mode: str
    type: str
    name: str
    module: str
    instances: list[StateInstance]


class TerraformState(TypedDict, total=False):
    resources: list[StateResource]
    lineage: str | None


class OutputModule(TypedDict, total=False):
    resources: list[dict[str, object]]
    child_modules: list["OutputModule"]


def object_map(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("Invalid Terraform state object")
    return cast(dict[str, object], value)


def object_rows(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise ValueError("Invalid Terraform state collection")
    return [object_map(item) for item in value]


def checked_state(value: object) -> TerraformState:
    """Validate consumed fields without requiring unrelated provider metadata."""
    state = object_map(value)
    if state.get("lineage") is not None and not isinstance(state["lineage"], str):
        raise ValueError("Invalid Terraform state lineage")
    for resource in object_rows(state.get("resources", [])):
        for key in ("mode", "type", "name", "module"):
            if key in resource and not isinstance(resource[key], str):
                raise ValueError("Invalid Terraform resource identity field")
        for instance in object_rows(resource.get("instances", [])):
            if "attributes" in instance:
                object_map(instance["attributes"])
            index = instance.get("index_key")
            if index is not None and type(index) not in (str, int):
                raise ValueError("Invalid Terraform resource index")
    return cast(TerraformState, state)


def checked_module(value: object) -> OutputModule:
    module = object_map(value)
    object_rows(module.get("resources", []))
    for child in object_rows(module.get("child_modules", [])):
        checked_module(child)
    return cast(OutputModule, module)
