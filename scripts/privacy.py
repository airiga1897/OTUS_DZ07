"""Redact credentials in diagnostics and anonymize publication evidence."""
import ipaddress
import json
from pathlib import Path
import re
from typing import Iterable
from urllib.parse import quote

_values: set[str] = set()
_sensitive = {'password', 'passwd', 'token', 'access_token', 'refresh_token',
              'authorization', 'cookie', 'set-cookie', 'project_id', 'tenant_id',
              'domain_name', 'account_number', 'username', 'private_key', 'user_id',
              'api_key', 'client_secret', 'secret'}


def remember(values: Iterable[str]) -> None:
    for value in values:
        if isinstance(value, str) and value:
            _values.update((value, quote(value, safe=''), json.dumps(value)[1:-1]))


def remember_application(local: Path) -> None:
    path = local / 'secrets.json'
    if path.exists():
        remember(json.loads(path.read_text(encoding='utf-8')).values())


def redact(text: str) -> str:
    for value in sorted(_values, key=len, reverse=True):
        text = text.replace(value, '[REDACTED]')
    return re.sub(r'(?i)(\b(?:authorization|x-auth-token)\s*[:=]\s*)(?:Basic |Bearer )?[^\s,;]+',
                  r'\1[REDACTED]', text)


def _public_ip(match: re.Match) -> str:
    try:
        address = ipaddress.ip_address(match.group())
    except ValueError:
        return match.group()
    return '<public-ip>' if address.is_global else match.group()


def sanitize(value: object) -> object:
    if isinstance(value, dict):
        return {redact(str(key)): '[REDACTED]' if str(key).lower() in _sensitive else sanitize(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        value = redact(value)
        value = re.sub(r'(?i)\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b', '<resource-id>', value)
        return re.sub(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', _public_ip, value)
    return value
