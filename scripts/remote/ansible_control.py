"""Prepare the Linux Ansible controller on the lab's application VM."""

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ControllerSettings:
    version: str
    venv: Path


def load_settings(root: Path) -> ControllerSettings:
    document = json.loads((root / "config/stack.json").read_text(encoding="utf-8"))
    config = document.get("ansible") if isinstance(document, dict) else None
    if not isinstance(config, dict) or any(not isinstance(config.get(key), str) for key in ("version", "venv")):
        raise ValueError("Invalid Ansible controller settings")
    return ControllerSettings(config["version"], Path(config["venv"]))


def main(settings: ControllerSettings | None = None) -> None:
    settings = settings if settings is not None else load_settings(Path(__file__).resolve().parents[2])
    os.environ["DEBIAN_FRONTEND"] = "noninteractive"
    python = settings.venv / "bin/python"
    if not python.exists():
        subprocess.run(["apt-get", "update", "-qq"], check=True)
        subprocess.run(["apt-get", "install", "-y", "-qq", "python3-venv", "openssh-client"], check=True)
        subprocess.run(["python3", "-m", "venv", str(settings.venv)], check=True)
    probe = subprocess.run(
        [str(python), "-c", 'from importlib.metadata import version; print(version("ansible-core"))'],
        capture_output=True,
        text=True,
    )
    if probe.returncode or probe.stdout.strip() != settings.version:
        subprocess.run([str(python), "-m", "pip", "install", "ansible-core==" + settings.version], check=True)


if __name__ == "__main__":
    main()
