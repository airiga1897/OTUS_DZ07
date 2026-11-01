"""ELK configuration and acceptance checks, independent of provider API types."""
import json
import shlex
from pathlib import Path

import topology


def settings(lab, passwords, name, services):
    ips = {n: lab['private_ips'][n] for n in topology.elastic_names(lab)}
    masters = services['initial_masters']
    role = lab['node_roles'][name]
    secret_names = ('elastic', 'shipper', 'kibana', 'kibana_encryption') if role in ('app', 'kibana') else ('elastic', 'shipper') if role == 'es' else ('shipper',)
    return {'name': name, 'ips': ips if role != 'db' else {n: ips[n] for n in masters}, 'endpoints': {n: ips[n] for n in masters},
            'roles': (['master', 'data', 'ingest'] if name in masters else ['data', 'ingest']) if role == 'es' else [],
            'ip': lab['private_ips'][name], 'seed_hosts': [ips[n] for n in masters], 'initial_masters': masters,
            'version': services['elastic_version'], 'heap': services['elastic_heap'], 'kibana_port': services['kibana_port'],
            'elastic_download_base': services['elastic_download_base'],
            'log_sources': services['logs']['all'] + services['logs']['roles'].get(role, []),
            'secrets': {k: passwords[k] for k in secret_names},
            'expected_services': sorted({'system'} | {s['service'] for n in lab['private_ips'] for s in services['logs']['roles'].get(lab['node_roles'][n], [])}),
            'log_hosts': list(lab['private_ips']) if role in ('app', 'kibana') else [name],
            'log_expectations': {n: ['system'] + [s['service'] for s in services['logs']['roles'].get(lab['node_roles'][n], [])]
                                 for n in lab['private_ips']} if role in ('app', 'kibana') else {}}


def inventory(result, passwords, value):
    lab = result.application()
    if not topology.elastic_names(lab):
        return value  # The imported PostgreSQL role unit tests use workload-only contracts.
    services = topology.services(Path(__file__).resolve().parents[1])
    children = value['all']['children']
    for name, node in result.nodes.items():
        role = lab['node_roles'][name]
        group = {'app': 'frontend', 'db': 'database', 'es': 'elasticsearch', 'kibana': 'kibana'}[role]
        host = children.setdefault(group, {'hosts': {}})['hosts'].setdefault(name, {})
        host.update(ansible_host=node['private_ip'], dz07=settings(lab, passwords, name, services), dz07_pending=True)
    if services.get('kibana_host') == 'app':
        children['kibana'] = {'hosts': {'app': {}}}
    value['all']['vars'].update(dz07_bootstrap=True, dz07_existing_count=len(services['initial_masters']), dz07_kibana_port=services['kibana_port'])
    return value


def stage(api, app, lab, passwords):
    services = topology.services(api.ROOT)
    base = api.STACK['paths']['remote_root']
    from elk_tls import certificates
    certificates(api.LOCAL, {n: lab['private_ips'][n] for n in topology.elastic_names(lab)})
    for name in lab['server_ids']:
        def put_node(client):
            if name != 'app':
                api.remote(client, 'sudo python3 -', 'cloud-init-' + name,
                           stdin=(api.ROOT / 'scripts/remote/check_cloud_init.py').read_text())
                api.upload_tree(client, name)
            api.remote(client, 'install -d -m 700 ' + base + '/certs ' + base + '/packages', 'private-settings-' + name)
            api.put(client, base + '/elk-settings.json', json.dumps(settings(lab, passwords, name, services)))
            api.put(client, base + '/certs/ca.crt', (api.LOCAL / 'certs/ca.crt').read_bytes())
            if name in topology.elastic_names(lab):
                for suffix in ('crt', 'key'):
                    api.put(client, base + '/certs/node.' + suffix, (api.LOCAL / ('certs/' + name + '.' + suffix)).read_bytes())
            if name == 'es1' and services['artifact_delivery'] == 'cached':
                from artifact_transport import cached_packages
                cached_packages(api, client)
            elif name == 'es1' and services['artifact_delivery'] == 'internet':
                api.remote(client, 'python3 ' + base + '/scripts/remote/download_packages.py', 'official-package-download')
        if name == 'app':
            put_node(app)
        else:
            with api.connect(lab, name, app) as client:
                put_node(client)
    with api.connect(lab, 'es1', app) as client:
        manifest = api.remote(client, 'cat ' + base + '/packages/manifest.json', 'verified-package-manifest')
    lock = json.loads((api.ROOT / 'config/elastic-artifacts.json').read_text())
    if json.loads(manifest) != lock:
        raise ValueError('Remote package manifest differs from pinned hashes')
    api.put(app, base + '/packages/manifest.json', manifest, mode=0o644)
    for name in lab['server_ids']:
        if name != 'app':
            with api.connect(lab, name, app) as client:
                api.put(client, base + '/packages/manifest.json', manifest, mode=0o644)


def package_cache(api, app, lab, *, start, remote=None):
    unit = api.PREFIX + '-package-cache.service'
    with api.connect(lab, 'es1', app) as client:
        if start:
            command = ('sudo systemctl is-active --quiet ' + unit + ' || sudo systemd-run --collect --unit=' +
                api.PREFIX + '-package-cache --uid=otus /usr/bin/python3 -m http.server 18080 --bind ' +
                lab['private_ips']['es1'] + ' --directory ' + api.STACK['paths']['remote_root'] + '/packages')
        else:
            command = 'sudo systemctl stop ' + unit
        (remote or api.remote)(client, command, 'package-cache-start' if start else 'package-cache-stop')


def command(api, action, name='app'):
    lab = api.lab_output()
    with api.connect(lab) as front:
        if name == 'app':
            raw = api.remote(front, 'sudo python3 ' + api.STACK['paths']['remote_root'] + '/scripts/remote/elk_api.py ' + action, 'elk-' + action)
        else:
            with api.connect(lab, name, front) as client:
                raw = api.remote(client, 'sudo python3 ' + api.STACK['paths']['remote_root'] + '/scripts/remote/elk_api.py ' + action, 'elk-' + action)
    return json.loads(raw)


def verify(api):
    lab = api.lab_output()
    with api.connect(lab) as front:
        for name in lab['server_ids']:
            if name == 'app':
                api.remote(front, 'logger -t otus-dz07 OTUS_DZ07_verification_app', 'log-marker-app')
                statement = "DO $$ BEGIN RAISE LOG 'OTUS_DZ07 database verification'; END $$;"
                api.remote(front, 'sudo -u postgres psql -v ON_ERROR_STOP=1 -c ' + shlex.quote(statement), 'sql-log-app')
                continue
            with api.connect(lab, name, front) as client:
                api.remote(client, 'logger -t otus-dz07 OTUS_DZ07_verification_' + name, 'log-marker-' + name)
                if name in topology.database_names(lab):
                    statement = "DO $$ BEGIN RAISE LOG 'OTUS_DZ07 database verification'; END $$;"
                    api.remote(client, 'sudo -u postgres psql -v ON_ERROR_STOP=1 -c ' + shlex.quote(statement), 'sql-log-' + name)
    result = command(api, 'verify')
    result['kibana'] = command(api, 'kibana-check', name=topology.services(api.ROOT)['kibana_host'])
    api.save(api.ROOT / 'docs/logs-verification.json', result)
    return result


def failover(api):
    from elastic_options import failover as check
    return check(api)
