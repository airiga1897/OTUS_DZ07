"""Desired PostgreSQL topology; DCS membership changes require separate migration."""
import ipaddress
import json
from pathlib import Path
import re
from stack_config import load as stack_settings


def load(root: Path) -> dict:
    value = json.loads((root / 'config/topology.json').read_text(encoding='utf-8'))
    validate(value)
    if not set(services(root)['initial_masters']) <= set(value['nodes']):
        raise ValueError('DZ07 requires three separate Elasticsearch masters')
    return value


def database_names(lab: dict) -> list[str]:
    names = sorted(n for n in lab['private_ips'] if lab.get('node_roles', {}).get(n, 'db' if n.startswith('db') else '') == 'db')
    return names or ['app']


def validate(value: dict) -> None:
    nodes, voters = value['nodes'], value['etcd_members']
    if nodes.get('app') != {'host': 10, 'role': 'app'}:
        raise ValueError('The existing app address and role are immutable')
    databases = {n for n, v in nodes.items() if v['role'] == 'db'}
    if not databases:
        required = {'app', 'es1', 'es2', 'es3'}
        if (voters or not required <= set(nodes) <= required | {'es4'}
                or any(nodes[n]['role'] != 'es' for n in set(nodes) - {'app'})):
            raise ValueError('Compact topology requires three ES masters and only optional es4, without etcd')
        hosts = [v['host'] for v in nodes.values()]
        if len(set(hosts)) != len(hosts) or any(type(h) is not int or not 2 <= h <= 254 for h in hosts):
            raise ValueError('Node addresses must be unique usable /24 host offsets')
        return
    if not 3 <= len(databases) <= stack_settings()['postgresql']['max_nodes']:
        raise ValueError('Topology requires 3..10 PostgreSQL nodes')
    if any(v['role'] not in ('app', 'db', 'es', 'kibana') for v in nodes.values()):
        raise ValueError('Unsupported node role')
    elastic = {n for n, v in nodes.items() if v['role'] == 'es'}
    if elastic and (not {'es1', 'es2', 'es3'} <= elastic or nodes.get('kibana', {}).get('role') != 'kibana'):
        raise ValueError('Retain three Elasticsearch masters and separate Kibana')
    if len(set(voters)) != len(voters) or len(voters) < 3 or len(voters) % 2 != 1 or not set(voters) <= databases:
        raise ValueError('etcd requires an odd, unique subset of database nodes')
    hosts = [v['host'] for v in nodes.values()]
    if len(set(hosts)) != len(hosts) or any(type(h) is not int or not 2 <= h <= 254 for h in hosts):
        raise ValueError('Node addresses must be unique usable /24 host offsets')
    if any(not re.fullmatch(r'db[1-9][0-9]*', n) for n in databases):
        raise ValueError('Database names must have the form db1, db2, ...')


def validate_extension(previous: dict, desired: dict) -> set[str]:
    validate(previous)
    validate(desired)
    if previous['etcd_members'] != desired['etcd_members']:
        raise ValueError('Changing etcd membership requires a separate migration')
    if any(desired['nodes'].get(n) != v for n, v in previous['nodes'].items()):
        raise ValueError('Existing nodes cannot be removed, renamed or readdressed')
    return set(desired['nodes']) - set(previous['nodes'])


def from_lab(lab: dict) -> dict:
    return {'nodes': {n: {'host': int(ipaddress.ip_address(ip)) % 256,
                         'role': lab.get('node_roles', {}).get(n, 'app' if n == 'app' else 'kibana' if n == 'kibana' else 'es' if n.startswith('es') else 'db')}
                      for n, ip in lab['private_ips'].items()},
            'etcd_members': lab.get('etcd_members', ['db1', 'db2', 'db3'] if any(n.startswith('db') for n in lab['private_ips']) else [])}


def assert_preserved(before: dict, after: dict) -> None:
    if any(after.get(key) != value for key, value in before.items()):
        raise RuntimeError('Existing infrastructure identities changed')


def services(root: Path) -> dict:
    return json.loads((root / 'config/services.json').read_text())


def elastic_names(lab: dict) -> list[str]:
    return sorted(n for n in lab['private_ips'] if lab.get('node_roles', {}).get(n, 'es' if n.startswith('es') else '') == 'es')
