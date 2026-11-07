"""Explicit runtime operations: storage."""

import json
import secrets
from pathlib import Path

from cloud_contract import validate_binding
from credentials import values_for_schema
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from progress_logging import get_logger
from runtime_ports import FilePort, RedactionPort, StoragePort

LOGGER = get_logger(__name__)


def save(api: FilePort, path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_relative_to(api.ROOT / "docs"):
        import privacy

        value = privacy.sanitize(value)
    text = json.dumps(value, ensure_ascii=False, indent=2)
    if path.is_relative_to(api.ROOT / "docs"):
        text = api.redact(text)
    path.write_text(text, encoding="utf-8")


def redact(api: RedactionPort, text: str) -> str:
    values = values_for_schema(api._AUTH_CONFIG, api.AUTH_SCHEMA)
    secrets_path = api.LOCAL / "secrets.json"
    if secrets_path.exists():
        values += list(json.loads(secrets_path.read_text()).values())
    for value in sorted({v for v in values if isinstance(v, str) and v}, key=len, reverse=True):
        text = text.replace(value, "<redacted>")
    import privacy

    return privacy.redact(text)


def bind_context(api: StoragePort, config: dict[str, str]) -> None:
    path = api.LOCAL / "context.json"
    from selectel_adapter import binding

    expected = binding(config, api.PREFIX, api.placement().get("availability_zone"))
    from local_runtime import validate_state

    validate_state(api.ROOT, config, api.PREFIX)
    if path.exists():
        validate_binding(json.loads(path.read_text()), expected)
    api.save(path, expected)


def auth_config(api: StoragePort) -> dict[str, str]:
    if api._AUTH_CONFIG is not None:
        return api._AUTH_CONFIG
    config = api.load_credentials()
    from stage_store import deployment_exists as exists

    if exists(api.ROOT):
        from staged_runtime import read_journal

        journal = read_journal(api.ROOT)
        if journal["project_id"] != config["project_id"] or journal["namespace"] != api.PREFIX:
            raise ValueError("Sequential deployment ownership mismatch")
    else:
        api.bind_context(config)
    api._AUTH_CONFIG = config
    return config


def prepare_identity(api: FilePort) -> Path:
    api.LOCAL.mkdir(exist_ok=True)
    key, pub = api.LOCAL / "otus_dz07", api.LOCAL / "otus_dz07.pub"
    if key.exists() != pub.exists():
        raise ValueError("Incomplete existing key pair")
    if not key.exists():
        private = Ed25519PrivateKey.generate()
        key.write_bytes(
            private.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()
            )
        )
        pub.write_bytes(
            private.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
            + b" otus-dz07\n"
        )
    secrets_path = api.LOCAL / "secrets.json"
    if not secrets_path.exists():
        api.save(
            secrets_path,
            {
                k: secrets.token_urlsafe(30)
                for k in [
                    "postgres",
                    "app",
                    "django",
                    "api",
                    "elastic",
                    "shipper",
                    "kibana",
                    "kibana_encryption",
                ]
            },
        )
    return secrets_path
