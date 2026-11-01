"""Prepare the Linux Ansible controller on the lab's application VM."""
import json
import os
from pathlib import Path
import subprocess

CONFIG = json.loads((Path(__file__).resolve().parents[2] / 'config/stack.json').read_text())['ansible']
VERSION = CONFIG['version']
VENV = Path(CONFIG['venv'])


def main() -> None:
    os.environ['DEBIAN_FRONTEND'] = 'noninteractive'
    python = VENV / 'bin/python'
    if not python.exists():
        subprocess.run(['apt-get', 'update', '-qq'], check=True)
        subprocess.run(['apt-get', 'install', '-y', '-qq', 'python3-venv', 'openssh-client'], check=True)
        subprocess.run(['python3', '-m', 'venv', str(VENV)], check=True)
    probe = subprocess.run([str(python), '-c', 'from importlib.metadata import version; print(version("ansible-core"))'],
                           capture_output=True, text=True)
    if probe.returncode or probe.stdout.strip() != VERSION:
        subprocess.run([str(python), '-m', 'pip', 'install', 'ansible-core==' + VERSION], check=True)


if __name__ == '__main__':
    main()
