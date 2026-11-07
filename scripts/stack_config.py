"""Non-secret configuration of this clone's PostgreSQL deployment."""

import hashlib
import ipaddress
import json
import re
from pathlib import Path
from typing import cast

from stack_types import StackSettings

ROOT = Path(__file__).resolve().parents[1]


def _internal_validate_shape(value: object) -> StackSettings:
    if not isinstance(value, dict):
        raise ValueError("Stack configuration must be an object")
    fields: dict[str, dict[str, type[str] | type[int]]] = {
        "paths": {"remote_root": str, "application": str, "shared": str},
        "postgresql": {
            "major": int,
            "port": int,
            "data_dir": str,
        },
        "ansible": {"version": str, "venv": str},
        "portal": {"uwsgi_version": str, "service_user": str, "upstream_port": int},
        "web": {"haproxy_port": int, "nginx_port": int},
    }
    if set(value) != {"name_prefix", "subnet_cidr", *fields} or any(
        type(value.get(key)) is not str for key in ("name_prefix", "subnet_cidr")
    ):
        raise ValueError("Stack namespace and subnet must be strings")
    for group, required in fields.items():
        section = value.get(group)
        if (
            not isinstance(section, dict)
            or set(section) != set(required)
            or any(type(section.get(key)) is not kind for key, kind in required.items())
        ):
            raise ValueError("Invalid stack configuration section: " + group)
    return cast(StackSettings, value)


def load(root: Path = ROOT) -> StackSettings:
    value = json.loads((root / "config/stack.json").read_text(encoding="utf-8"))
    value = _internal_validate_shape(value)
    if not re.fullmatch("[a-z][a-z0-9-]+", value["name_prefix"]):
        raise ValueError("Invalid resource namespace")
    subnet = ipaddress.ip_network(value["subnet_cidr"])
    if subnet.version != 4 or subnet.prefixlen != 24:
        raise ValueError("This topology uses IPv4 /24 host offsets")
    for path in [*value["paths"].values(), value["ansible"]["venv"]]:
        if not re.fullmatch("/[a-zA-Z0-9_./-]+", path) or ".." in path.split("/"):
            raise ValueError("Server paths must be absolute and contain no shell metacharacters")
    return value


def digest(value: StackSettings) -> str:
    services = json.loads((ROOT / "config/services.json").read_text())
    return hashlib.sha256(json.dumps({"stack": value, "observability": services}, sort_keys=True).encode()).hexdigest()
