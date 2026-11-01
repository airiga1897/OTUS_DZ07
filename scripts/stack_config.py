"""Non-secret configuration of this clone's PostgreSQL deployment."""
import json
import hashlib
import ipaddress
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def load(root: Path = ROOT) -> dict:
    value = json.loads((root / 'config/stack.json').read_text(encoding='utf-8'))
    if not re.fullmatch('[a-z][a-z0-9-]+', value['name_prefix']):
        raise ValueError('Invalid resource namespace')
    subnet = ipaddress.ip_network(value['subnet_cidr'])
    if subnet.version != 4 or subnet.prefixlen != 24:
        raise ValueError('This topology uses IPv4 /24 host offsets')
    for path in [*value['paths'].values(), value['ansible']['venv']]:
        if not re.fullmatch('/[a-zA-Z0-9_./-]+', path) or '..' in path.split('/'):
            raise ValueError('Server paths must be absolute and contain no shell metacharacters')
    pg = value['postgresql']
    if not 3 <= pg['max_nodes'] <= min(pg['max_wal_senders'], pg['max_replication_slots']):
        raise ValueError('Desired node limit exceeds initial replication capacity')
    return value


def digest(value: dict) -> str:
    services = json.loads((ROOT / 'config/services.json').read_text())
    return hashlib.sha256(json.dumps({'stack': value, 'observability': services}, sort_keys=True).encode()).hexdigest()
