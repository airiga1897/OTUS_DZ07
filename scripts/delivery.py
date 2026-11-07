"""The explicit package delivery policy for this clone."""

import json
from pathlib import Path
from typing import Literal

from artifact_types import DeliveryManifest

DeliveryMode = Literal["internet", "cached", "s3", "release"]
DELIVERY_MODES: tuple[DeliveryMode, ...] = ("internet", "cached", "s3", "release")


class ArtifactUnavailable(RuntimeError):
    pass


def mode(root: Path) -> DeliveryMode:
    path = root / "config/services.json"
    document = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not isinstance(document, dict):
        raise ValueError("Invalid artifact_delivery configuration document")
    selected = document.get("artifact_delivery", "cached")
    if not isinstance(selected, str) or selected not in DELIVERY_MODES:
        raise ValueError("Unknown artifact_delivery mode")
    return selected


def internet(root: Path) -> bool:
    return mode(root) in ("internet", "s3", "release")


def prepare(root: Path) -> DeliveryManifest | None:
    selected = mode(root)
    if selected == "release":
        from release_artifacts import prepare as prepare_release

        return prepare_release(root)
    if selected == "s3":
        from s3_artifacts import prepare as prepare_s3

        return prepare_s3(root)
    return None
