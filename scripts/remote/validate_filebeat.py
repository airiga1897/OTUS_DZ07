"""Validate an Ansible staging file without exposing application credentials."""

import json
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]


def main(path: str) -> int:
    # observability.stage writes this least-privilege file on every node.
    # The portal's settings.json exists only on app, never on ES servers.
    secrets = json.loads((BASE / "elk-settings.json").read_text())["secrets"]
    result = subprocess.run(
        ["/usr/bin/filebeat", "test", "config", "--strict.perms=false", "-c", path],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    text = result.stdout
    for secret in sorted((s for s in secrets.values() if isinstance(s, str) and s), key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    log = BASE / "filebeat-validation.log"
    log.touch(mode=0o600, exist_ok=True)
    log.chmod(0o600)
    log.write_text(text)
    print(text)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
