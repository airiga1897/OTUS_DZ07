"""Confirm the operator address without widening administrative access."""

import hashlib
import hmac
import ipaddress
import json
import time
import urllib.parse
import urllib.request
from collections.abc import Sequence
from pathlib import Path

from plan_diagnostics import PlannedResource
from progress_logging import get_logger

LOGGER = get_logger(__name__)
IP_CHECK_ATTEMPTS = 3
IP_CHECK_RETRY_DELAY = 1


class OperatorIPv4MismatchError(ValueError):
    """The two discovery services repeatedly returned different public addresses."""


def read_address(url: str) -> str:
    with urllib.request.urlopen(url, timeout=15) as response:
        raw: bytes = response.read(128)
        return raw.decode("ascii").strip()


def discover_cidrs(urls: list[str]) -> list[str]:
    if len(urls) != 2 or len({urllib.parse.urlsplit(u).hostname for u in urls}) != 2:
        raise ValueError("Configure two independent public IPv4 HTTPS services")
    for url in urls:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Operator IP discovery requires HTTPS endpoints without credentials")
    first_seen: set[str] = set()
    second_seen: set[str] = set()
    for attempt in range(1, IP_CHECK_ATTEMPTS + 1):
        if attempt > 1:
            LOGGER.info(
                "Сервисы вернули разные публичные IPv4. Повторяем проверку обоих сервисов (%d/%d)",
                attempt,
                IP_CHECK_ATTEMPTS,
            )
            time.sleep(IP_CHECK_RETRY_DELAY)
        first, second = public_ipv4_cidrs(urls)
        first_seen.add(first)
        second_seen.add(second)
        if first == second or first_seen == second_seen:
            return sorted(first_seen & second_seen)
    raise OperatorIPv4MismatchError(
        f"HTTPS-сервисы не подтвердили одинаковые публичные IPv4 за {IP_CHECK_ATTEMPTS} попытки. "
        "Проверьте VPN, прокси и туннель на роутере. "
        "Переключитесь на прямой выход через провайдера или другую сеть и повторите команду."
    )


def public_ipv4_cidrs(urls: list[str]) -> list[str]:
    cidrs = []
    for url in urls:
        address = ipaddress.ip_address(read_address(url))
        if address.version != 4 or not address.is_global:
            raise ValueError("Operator discovery must return a global IPv4 address")
        cidrs.append(str(address) + "/32")
    return cidrs


def allowed_cidrs(configured: Sequence[str], observed: Sequence[str], existing: Sequence[str] = ()) -> list[str]:
    """Preserve existing access and require explicit confirmation of a changed route."""
    for cidr in [*configured, *existing, *observed]:
        network = ipaddress.ip_network(cidr, strict=True)
        if network.version != 4 or network.prefixlen != 32 or not network.network_address.is_global:
            raise ValueError("Administrative access requires public IPv4 /32 entries")
    result = list(dict.fromkeys(existing or configured or observed))
    if not set(observed).issubset(result):
        raise ValueError("Confirmed operator IPv4 is absent from admin_cidrs; explicitly review access")
    return result


def verify_ssh_source(connection: str, cidrs: Sequence[str]) -> None:
    fields = connection.split()
    if len(fields) != 4:
        raise ValueError("SSH client source could not be confirmed")
    allowed_cidrs(cidrs, [str(ipaddress.ip_address(fields[0])) + "/32"])


def signed_route_cidr(result: object, nonce: str, key: bytes) -> str:
    """Only a signed, fresh TCP source may extend this stand's allowlist."""
    if not isinstance(result, dict) or not all(isinstance(result.get(k), str) for k in ("nonce", "ip", "signature")):
        raise ValueError("Invalid route probe response")
    expected = hmac.new(key, (nonce + "|" + result["ip"]).encode(), hashlib.sha256).hexdigest()
    if result["nonce"] != nonce or not hmac.compare_digest(expected, result["signature"]):
        raise ValueError("Untrusted route probe response")
    cidr = str(ipaddress.IPv4Address(result["ip"])) + "/32"
    allowed_cidrs([], [cidr])
    return cidr


def validate_access_plan(change: PlannedResource, cidr: str, security_group_id: str | None) -> None:
    """A route confirmation may add only exact SSH/Kibana operator /32 rules."""
    after = change["change"].get("after") or {}
    port = {"ssh": 22, "kibana": 5601}.get(change.get("name", ""))
    if (
        not security_group_id
        or after.get("security_group_id") != security_group_id
        or change.get("type") != "openstack_networking_secgroup_rule_v2"
        or port is None
        or change.get("index") != cidr
        or any(
            after.get(k) != v
            for k, v in {
                "direction": "ingress",
                "ethertype": "IPv4",
                "protocol": "tcp",
                "port_range_min": port,
                "port_range_max": port,
                "remote_ip_prefix": cidr,
            }.items()
        )
    ):
        raise ValueError("Route plan may only add the confirmed SSH/22 rule")


def check_saved_access(variables_path: Path, services: list[str], configured: Sequence[str]) -> list[str]:
    """Reconfirm the current route without rewriting a running stand's rules."""
    observed = discover_cidrs(services)
    if variables_path.exists():
        existing = json.loads(variables_path.read_text())["admin_cidrs"]
        allowed_cidrs(configured, observed, existing)
    else:
        allowed_cidrs(configured, observed)
    return observed
