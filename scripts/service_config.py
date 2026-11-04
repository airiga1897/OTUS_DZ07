"""Clone-local service configuration, checked before inventory generation."""

import json
from pathlib import Path
from typing import NotRequired, TypedDict, cast

from delivery import DELIVERY_MODES, DeliveryMode


class LogSource(TypedDict):
    service: str
    paths: NotRequired[list[str]]
    journal_units: NotRequired[list[str]]


class LogSources(TypedDict):
    all: list[LogSource]
    roles: dict[str, list[LogSource]]


class ServiceSettings(TypedDict):
    artifact_delivery: DeliveryMode
    elastic_download_base: str
    elastic_version: str
    elastic_heap: str
    initial_masters: list[str]
    kibana_port: int
    kibana_host: str
    logs: LogSources


def _string_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _source(value: object) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("service"), str)
        and all(key not in value or _string_list(value[key]) for key in ("paths", "journal_units"))
    )


def _sources(value: object) -> bool:
    return isinstance(value, list) and all(_source(item) for item in value)


def _logs(value: object) -> bool:
    if not isinstance(value, dict) or not _sources(value.get("all")):
        return False
    roles = value.get("roles")
    return isinstance(roles, dict) and all(_sources(records) for records in roles.values())


def _document(root: Path) -> object:
    value: object = json.loads((root / "config/services.json").read_text(encoding="utf-8"))
    return value


def _version(value: object) -> str:
    version = value.get("elastic_version") if isinstance(value, dict) else None
    if not isinstance(version, str):
        raise ValueError("Invalid service Elastic version")
    return version


def _port(value: object) -> int:
    port = value.get("kibana_port") if isinstance(value, dict) else None
    if type(port) is not int:
        raise ValueError("Invalid service Kibana port")
    return port


def elastic_version(root: Path) -> str:
    return _version(_document(root))


def kibana_port(root: Path) -> int:
    return _port(_document(root))


def load(root: Path) -> ServiceSettings:
    value = _document(root)
    strings = ("artifact_delivery", "elastic_download_base", "elastic_version", "elastic_heap", "kibana_host")
    if not isinstance(value, dict) or any(not isinstance(value.get(key), str) for key in strings):
        raise ValueError("Invalid service configuration fields")
    if value["artifact_delivery"] not in DELIVERY_MODES:
        raise ValueError("Invalid service delivery mode")
    _port(value)
    _version(value)
    if not _string_list(value.get("initial_masters")) or not _logs(value.get("logs")):
        raise ValueError("Invalid service master list or log sources")
    return cast(ServiceSettings, value)
