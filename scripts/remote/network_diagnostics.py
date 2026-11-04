"""Bounded read-only guest evidence; no credentials or network configuration contents."""

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol, TypedDict


class CommandRunner(Protocol):
    def __call__(
        self, command: list[str], /, *, capture_output: Literal[True], text: Literal[True], timeout: int
    ) -> subprocess.CompletedProcess[str]: ...


class CommandEvidence(TypedDict, total=False):
    returncode: int
    stdout: str
    error_type: str


class NetworkEvidence(TypedDict):
    time_utc: str
    read_only: Literal[True]
    netplan_files: list[str]
    checks: dict[str, CommandEvidence]


COMMANDS = {
    "addresses": ["ip", "-j", "address", "show"],
    "routes": ["ip", "-j", "-4", "route", "show", "table", "all"],
    "links": ["ip", "-j", "link", "show"],
    "network_service": ["systemctl", "is-active", "systemd-networkd", "NetworkManager"],
    "network_status": ["networkctl", "--no-pager", "--lines=0", "status", "--all"],
    "cloud_init": ["cloud-init", "status", "--format", "json"],
    "ssh_service": ["systemctl", "is-active", "ssh.socket", "ssh.service"],
    "ssh_listener": ["ss", "-lnt", "sport", "=", ":22"],
}


def collect(runner: CommandRunner = subprocess.run, netplan: Path = Path("/etc/netplan")) -> NetworkEvidence:
    result: NetworkEvidence = {
        "time_utc": datetime.now(UTC).isoformat(),
        "read_only": True,
        "netplan_files": sorted(p.name for p in netplan.glob("*.yaml")),
        "checks": {},
    }
    for name, command in COMMANDS.items():
        try:
            run = runner(command, capture_output=True, text=True, timeout=10)
            result["checks"][name] = {"returncode": run.returncode, "stdout": run.stdout[:24000]}
        except (OSError, subprocess.TimeoutExpired) as error:
            result["checks"][name] = {"error_type": type(error).__name__}
    return result


if __name__ == "__main__":
    print("DZ07_NETWORK_DIAGNOSTICS=" + json.dumps(collect(), ensure_ascii=True))
