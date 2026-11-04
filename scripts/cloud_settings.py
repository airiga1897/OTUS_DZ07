"""Select an implemented adapter without changing an existing state backend."""

import json
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict, cast

from selectel_ports import SelectelFunctions

if TYPE_CHECKING:
    from runtime_ports import StagedControllerPort
    from selectel_staged import Backend


class CloudIdentity(TypedDict):
    provider: Literal["selectel"]
    terraform_directory: Literal["terraform-selectel"]


def load(root: Path) -> CloudIdentity:
    value = json.loads((root / "config/cloud.json").read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("provider") != "selectel"
        or value.get("terraform_directory") != "terraform-selectel"
    ):
        raise ValueError("Only Selectel is implemented; another cloud requires an independent stand and state")
    return cast(CloudIdentity, value)


def adapter() -> SelectelFunctions:
    """Only the implemented provider is registered; no implicit cross-cloud fallback."""
    load(Path(__file__).resolve().parents[1])
    import selectel_adapter

    return selectel_adapter


def staged_backend(api: "StagedControllerPort", cleanup: bool = False) -> "Backend":
    return adapter().staged_backend(api, cleanup=cleanup)


def terraform_root(root: Path) -> Path:
    settings = load(Path(__file__).resolve().parents[1])
    return root / settings["terraform_directory"]


def sequential_enabled(root: Path) -> bool:
    return adapter().sequential_enabled(root)
