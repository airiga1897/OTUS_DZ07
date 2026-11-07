"""One credential schema; Python receives credentials only from the environment."""

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import TypedDict

ROOT = Path(__file__).resolve().parents[1]


class CredentialField(TypedDict):
    env: str
    prompt: str
    secret: bool


class AuthenticationSchema(TypedDict):
    auth_url: str
    fields: dict[str, CredentialField]


class AuthenticationSettings(AuthenticationSchema):
    region: str


def _internal_field(value: object) -> CredentialField:
    if not isinstance(value, dict) or set(value) != {"env", "prompt", "secret"}:
        raise ValueError("Invalid credential field schema")
    env, prompt, secret = value["env"], value["prompt"], value["secret"]
    if (
        not isinstance(env, str)
        or not re.fullmatch(r"OS_[A-Z][A-Z_0-9]*", env)
        or not isinstance(prompt, str)
        or not prompt.strip()
        or not isinstance(secret, bool)
    ):
        raise ValueError("Invalid credential field schema")
    return {"env": env, "prompt": prompt, "secret": secret}


def schema(root: Path = ROOT) -> AuthenticationSchema:
    value = json.loads((root / "config/selectel-auth.json").read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict) or set(value) != {"auth_url", "fields"}:
        raise ValueError("Invalid authentication schema")
    url, raw_fields = value["auth_url"], value["fields"]
    if not isinstance(url, str) or not url.startswith("https://") or not isinstance(raw_fields, dict) or not raw_fields:
        raise ValueError("Invalid authentication schema")
    fields: dict[str, CredentialField] = {}
    for name, field in raw_fields.items():
        if not isinstance(name, str) or not name:
            raise ValueError("Invalid authentication field name")
        fields[name] = _internal_field(field)
    return {"auth_url": url, "fields": fields}


def auth_settings(root: Path = ROOT) -> AuthenticationSettings:
    settings = schema(root)
    from pool_selection import policy

    region = policy(root, "placement.json", "context.json")["region"]
    if not isinstance(region, str) or not region:
        raise ValueError("Invalid authentication region")
    return {**settings, "region": region}


def config_from_env(root: Path = ROOT) -> dict[str, str]:
    settings = auth_settings(root)
    return {
        "auth_url": settings["auth_url"],
        "region": settings["region"],
        **{k: os.environ.get(v["env"], "") for k, v in settings["fields"].items()},
    }


def load_credentials(root: Path = ROOT) -> dict[str, str]:
    settings = auth_settings(root)
    supplied = config_from_env(root)
    config = {"auth_url": settings["auth_url"], "region": settings["region"], **supplied}
    if any(not isinstance(config.get(k), str) or not config[k].strip() for k in settings["fields"]):
        raise ValueError("Incomplete Selectel credentials; run scripts/env.ps1")
    if config["auth_url"] != settings["auth_url"] or config["region"] != settings["region"]:
        raise ValueError("Unsupported Selectel endpoint or region")
    return config


def to_environment(config: Mapping[str, str], root: Path = ROOT) -> dict[str, str]:
    settings = auth_settings(root)
    return environment_for_schema({**config, "region": settings["region"]}, settings)


def environment_for_schema(config: dict[str, str], settings: AuthenticationSchema) -> dict[str, str]:
    return {
        **{v["env"]: config[k] for k, v in settings["fields"].items()},
        "OS_AUTH_URL": settings["auth_url"],
        "OS_REGION_NAME": config["region"],
    }


def credential_values(config: Mapping[str, str] | None, root: Path = ROOT) -> list[str]:
    return values_for_schema(config, schema(root))


def values_for_schema(config: Mapping[str, str] | None, settings: AuthenticationSchema) -> list[str]:
    return [v for k in settings["fields"] if (v := (config or {}).get(k))]
