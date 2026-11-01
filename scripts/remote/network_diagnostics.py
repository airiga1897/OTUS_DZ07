"""Bounded read-only guest evidence; no credentials or network configuration contents."""
import json
from datetime import datetime, timezone
from pathlib import Path
import subprocess


COMMANDS = {
    'addresses': ['ip', '-j', 'address', 'show'],
    'routes': ['ip', '-j', '-4', 'route', 'show', 'table', 'all'],
    'links': ['ip', '-j', 'link', 'show'],
    'network_service': ['systemctl', 'is-active', 'systemd-networkd', 'NetworkManager'],
    'network_status': ['networkctl', '--no-pager', '--lines=0', 'status', '--all'],
    'cloud_init': ['cloud-init', 'status', '--format', 'json'],
    'ssh_service': ['systemctl', 'is-active', 'ssh.socket', 'ssh.service'],
    'ssh_listener': ['ss', '-lnt', 'sport', '=', ':22'],
}


def collect(runner=subprocess.run, netplan=Path('/etc/netplan')) -> dict:
    result = {'time_utc': datetime.now(timezone.utc).isoformat(), 'read_only': True,
              'netplan_files': sorted(p.name for p in netplan.glob('*.yaml')), 'checks': {}}
    for name, command in COMMANDS.items():
        try:
            run = runner(command, capture_output=True, text=True, timeout=10)
            result['checks'][name] = {'returncode': run.returncode, 'stdout': run.stdout[:24000]}
        except (OSError, subprocess.TimeoutExpired) as error:
            result['checks'][name] = {'error_type': type(error).__name__}
    return result


if __name__ == '__main__':
    print('DZ07_NETWORK_DIAGNOSTICS=' + json.dumps(collect(), ensure_ascii=True))
