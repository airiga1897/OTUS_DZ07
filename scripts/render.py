"""Генерация конфигураций Patroni/etcd без фиксированных паролей."""

from stack_config import load
from topology import database_names
from cloud_contract import InfrastructureResult

STACK = load()
RUNTIME = STACK['paths']['remote_root'] + '/.ansible-runtime'


def node_config(name, lab, passwords):
    ip = lab['private_ips'][name]
    pg, patroni, etcd = STACK['postgresql'], STACK['patroni'], STACK['etcd']
    subnet = lab.get('subnet_cidr', STACK['subnet_cidr'])
    nodes = {n: lab['private_ips'][n] for n in lab.get('etcd_members', ['db1', 'db2', 'db3'])}
    return {'name': name, 'ip': ip, 'nodes': nodes, 'etcd_member': name in nodes,
            'patroni': {
                'scope': patroni['scope'], 'namespace': '/service/', 'name': name,
                'restapi': {'listen': ip + ':' + str(patroni['api_port']), 'connect_address': ip + ':' + str(patroni['api_port']),
                            'authentication': {'username': 'patroni', 'password': passwords['api']}},
                'etcd3': {'hosts': [v + ':' + str(etcd['client_port']) for v in nodes.values()]},
                'bootstrap': {'dcs': {'ttl': 30, 'loop_wait': 5, 'retry_timeout': 5,
                    'maximum_lag_on_failover': 1048576, 'synchronous_mode': True,
                    'synchronous_mode_strict': True, 'synchronous_node_count': 1,
                    'postgresql': {'use_pg_rewind': True, 'use_slots': True,
                        'parameters': {'wal_level': 'replica', 'hot_standby': 'on',
                                       'max_wal_senders': pg['max_wal_senders'], 'max_replication_slots': pg['max_replication_slots'],
                                       'wal_keep_size': '128MB', 'password_encryption': 'scram-sha-256'}}},
                    'initdb': [{'encoding': 'UTF8'}, 'data-checksums'],
                    'pg_hba': ['local all postgres peer', 'local all all scram-sha-256',
                               'host replication replicator ' + subnet + ' scram-sha-256',
                               'host all all ' + subnet + ' scram-sha-256']},
                'postgresql': {'listen': ip + ':' + str(pg['port']), 'connect_address': ip + ':' + str(pg['port']),
                    'data_dir': pg['data_dir'], 'bin_dir': '/usr/lib/postgresql/' + str(pg['major']) + '/bin',
                    'authentication': {'superuser': {'username': 'postgres', 'password': passwords['postgres']},
                                       'replication': {'username': 'replicator', 'password': passwords['replicator']}},
                    'parameters': {'unix_socket_directories': '/var/run/postgresql',
                                   'logging_collector': 'on', 'log_directory': '/var/log/postgresql',
                                   'log_filename': 'otus-%Y-%m-%d.log', 'log_statement': 'none'}},
                'watchdog': {'mode': 'off'}, 'tags': {'nofailover': False, 'noloadbalance': False}}}


def _application_inventory(infrastructure: InfrastructureResult, passwords: dict) -> dict:
    infrastructure.validate()
    lab = infrastructure.application()
    databases = {name: {'ansible_host': lab['private_ips'][name],
                       'patroni_config': node_config(name, lab, passwords)['patroni']}
                 for name in database_names(lab) if name != 'app'}
    return {'all': {'vars': {'stack': STACK, 'ansible_user': 'otus', 'ansible_port': 22,
                            'ansible_python_interpreter': '/usr/bin/python3',
                            'ansible_ssh_private_key_file': RUNTIME + '/key',
                            'ansible_ssh_common_args': '-o StrictHostKeyChecking=yes -o IdentitiesOnly=yes '
                                '-o UserKnownHostsFile=' + RUNTIME + '/known_hosts'},
                    'children': {'database': {'hosts': databases},
                                 'etcd_voters': {'hosts': {n: {} for n in lab['etcd_members']}},
                                 'frontend': {'hosts': {'app': {'ansible_connection': 'local',
                                     'portal_env': {'DJANGO_SECRET_KEY': passwords['django'],
                                         'DB_NAME': 'otus', 'DB_USER': 'otus', 'DB_PASSWORD': passwords['app'],
                                         'DB_HOST': '127.0.0.1', 'DB_PORT': str(STACK['postgresql']['port']),
                                         'DJANGO_ALLOWED_HOSTS': lab['app_public_ip'] + ',localhost,127.0.0.1',
                                         'REQUIRE_SHARED_MOUNT': '0', 'SHARED_ROOT': STACK['paths']['shared'],
                                         'NODE_NAME': STACK['name_prefix'] + '-app'}}}}}}}


def ansible_inventory(infrastructure: InfrastructureResult, passwords: dict) -> dict:
    from observability import inventory
    value = _application_inventory(infrastructure, passwords)
    if not infrastructure.application()['etcd_members']:
        value['all']['children']['database'] = {'hosts': {'app': {'postgres_password': passwords['postgres']}}}
    return inventory(infrastructure, passwords, value)


if __name__ == '__main__':
    from pathlib import Path
    import yaml
    root = Path(__file__).resolve().parents[1]
    lab = {'private_ips': {'app': '10.77.0.10', 'db1': '10.77.0.21', 'db2': '10.77.0.22', 'db3': '10.77.0.23'}}
    placeholder = {key: 'REPLACE_WITH_GENERATED_SECRET' for key in ['postgres', 'replicator', 'api']}
    (root / 'config/patroni.example.yml').write_text(yaml.safe_dump(node_config('db1', lab, placeholder)['patroni'], sort_keys=False), encoding='utf-8')
