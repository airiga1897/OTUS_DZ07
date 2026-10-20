"""Selectel adapter; reuses DZ07 transport, certificates and remote setup."""
import argparse
import base64
import datetime
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
import urllib.request

import yc_lab as lab
from selectel_api import authenticate, request_json, config_from_env

ROOT, LOCAL = lab.ROOT, lab.LOCAL


def auth():
    path = os.environ.get('OTUS_SELECTEL_AUTH')
    if not path and (LOCAL / 'operator.json').exists():
        path = str(LOCAL / 'operator.json')
    if path:
        config = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    elif os.environ.get('OS_PASSWORD'):
        config = config_from_env()
    else:
        raise RuntimeError('Selectel operator credentials unavailable; run scripts/env.ps1 and scripts/save-operator.ps1 interactively')
    if (os.environ.get('OS_PROJECT_ID') and config['project_id'] != os.environ['OS_PROJECT_ID']) or config['region'] != 'ru-9':
        raise RuntimeError('Unexpected Selectel project/region')
    return config


class Cloud:
    def __init__(self):
        self.token, catalog = authenticate(auth())
        self.endpoints = {s['type']: e['url'].rstrip('/') for s in catalog for e in s['endpoints']
                          if e.get('interface') == 'public' and e.get('region') == 'ru-9'
                          and e['url'].startswith('https://')}

    def get(self, service, path, payload=None):
        return request_json(self.endpoints[service] + path, token=self.token, payload=payload)[0]


def tf(*args, capture=False):
    config = auth()
    env = dict(os.environ)
    for name, key in {'OS_AUTH_URL': 'auth_url', 'OS_PROJECT_ID': 'project_id',
                      'OS_REGION_NAME': 'region', 'OS_USER_DOMAIN_NAME': 'domain_name',
                      'OS_USERNAME': 'username', 'OS_PASSWORD': 'password'}.items():
        env[name] = config[key]
    env['TF_CLI_CONFIG_FILE'] = str(ROOT / 'terraform-selectel.rc')
    LOCAL.mkdir(exist_ok=True)
    # Stream into a local file so long operations remain inspectable.
    logfile = LOCAL / ('selectel-tf-' + args[0] + '.log')
    with logfile.open('wb') as output:
        result = subprocess.run([str(lab.TF), '-chdir=' + str(ROOT / 'terraform-selectel'), *args],
                                env=env, stdout=output, stderr=subprocess.STDOUT, timeout=1800)
    if result.returncode:
        raise RuntimeError(logfile.read_text(errors='replace')[-3500:])
    if capture:
        return logfile.read_text(encoding='utf-8')
    print('Selectel terraform ' + args[0] + ': OK', flush=True)


def preflight():
    cloud = Cloud()
    project = auth()['project_id']
    compute = cloud.get('compute', '/os-quota-sets/' + project + '/detail')['quota_set']['az_quotas']['ru-9a']
    volumes = cloud.get('volumev3', '/os-quota-sets/' + project + '?usage=True')['quota_set']
    network = cloud.get('network', '/v2.0/quotas/' + project + '/details.json')['quota']
    required = [(compute['cores'], 6), (compute['ram'], 24576), (compute['instances'], 3),
                (volumes['gigabytes_basicssd.ru-9a'], 80), (network['floatingip'], 3),
                (network['network'], 1), (network['subnet'], 1), (network['router'], 1),
                (network['security_group'], 1), (network['security_group_rule'], 8), (network['port'], 10)]
    for quota, needed in required:
        free = quota['limit'] - quota.get('in_use', quota.get('used', 0)) - quota.get('reserved', 0)
        if quota['limit'] != -1 and free < needed:
            raise RuntimeError('Insufficient shared Selectel quota')
    for subnet in cloud.get('network', '/v2.0/subnets')['subnets']:
        if subnet.get('ip_version') == 4 and not subnet.get('name', '').startswith('otus-dz07'):
            if ipaddress.ip_network(subnet['cidr']).overlaps(ipaddress.ip_network('10.77.0.0/16')):
                raise RuntimeError('DZ07 subnet overlaps existing Selectel subnet')
    lab.save(ROOT / 'docs/selectel-preflight.json', {'checked_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
             'compute': compute, 'ssd': volumes['gigabytes_basicssd.ru-9a'], 'network': network,
             'required': {'instances': 3, 'cores': 6, 'ram_mib': 24576, 'ssd_gb': 80, 'floating_ipv4': 3}})
    return cloud


def prepare():
    cloud = preflight()
    images = request_json(cloud.endpoints['image'].removesuffix('/v2') +
              '/v2/images?name=Ubuntu%2024.04%20LTS%2064-bit&status=active', token=cloud.token)[0]['images']
    images = [i for i in images if i.get('visibility') == 'public' and i.get('min_disk', 0) <= 20]
    flavors = [f for f in cloud.get('compute', '/flavors/detail')['flavors'] if f['name'] == 'SL1.2-8192']
    if len(images) != 1 or len(flavors) != 1:
        raise RuntimeError('Expected one image and one 2CPU/8GB flavor')
    LOCAL.mkdir(exist_ok=True)
    if not (LOCAL / 'otus_dz07').exists():
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        private = Ed25519PrivateKey.generate()
        (LOCAL / 'otus_dz07').write_bytes(private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()))
        (LOCAL / 'otus_dz07.pub').write_bytes(private.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH))
    if not (LOCAL / 'secrets.json').exists():
        lab.save(LOCAL / 'secrets.json', {k: secrets.token_urlsafe(30) for k in ['elastic', 'shipper', 'kibana', 'django', 'database']})
    lab.certificates()
    cidrs = []
    for path in [ROOT / 'terraform/terraform.tfvars.json', ROOT / 'terraform-selectel/terraform.tfvars.json',
                 ROOT.parent / 'OTUS_DZ06/terraform-selectel/terraform.tfvars.json',
                 ROOT.parent / 'OTUS_DZ05/terraform-selectel/terraform.tfvars.json']:
        if path.exists():
            try:
                previous = json.loads(path.read_text())
            except json.JSONDecodeError:
                if path.is_relative_to(ROOT):
                    raise
                continue
            cidrs.extend(previous.get('admin_cidrs', []))
    current = urllib.request.urlopen('https://api.ipify.org', timeout=15).read().decode().strip()
    ipaddress.IPv4Address(current)
    cidrs.append(current + '/32')
    cidrs = list(dict.fromkeys(cidrs))
    if any(ipaddress.ip_network(c).prefixlen != 32 for c in cidrs):
        raise RuntimeError('Operator /32 addresses required')
    lab.save(ROOT / 'terraform-selectel/terraform.tfvars.json', {'region': 'ru-9', 'availability_zone': 'ru-9a',
             'image_id': images[0]['id'], 'flavor_id': flavors[0]['id'], 'volume_type': 'basicssd.ru-9a', 'admin_cidrs': cidrs})
    tf('init', '-input=false', '-no-color')
    tf('fmt')
    tf('validate', '-no-color')


def plan(destroy=False):
    path = LOCAL / ('selectel-destroy.tfplan' if destroy else 'selectel-create.tfplan')
    tf('plan', '-input=false', '-no-color', *(['-destroy'] if destroy else []), '-out=' + str(path))
    data = json.loads(tf('show', '-json', str(path), capture=True))
    for change in data.get('resource_changes', []):
        if change.get('mode') == 'data':
            continue
        if change['change']['actions'] not in (['no-op'], ['delete'] if destroy else ['create']):
            raise RuntimeError('Unexpected mutation: ' + change['address'])
        value = change['change'].get('before' if destroy else 'after') or {}
        if value.get('name') and not value['name'].startswith('otus-dz07'):
            raise RuntimeError('Foreign resource')
        if not change['type'].startswith('openstack_'):
            raise RuntimeError('Unexpected provider')
    return path


def output():
    return json.loads(tf('output', '-json', 'lab', capture=True))


def trust(topology):
    cloud = Cloud()
    lines = []
    for name, node in topology.items():
        server = cloud.get('compute', '/servers/' + node['id'])['server']
        if server['name'] != 'otus-dz07-' + name:
            raise RuntimeError('Foreign server UUID')
        for _ in range(60):
            text = cloud.get('compute', '/servers/' + node['id'] + '/action', {'os-getConsoleOutput': {'length': 1000}})['output']
            section = re.search(r'BEGIN SSH HOST KEY KEYS.*?END SSH HOST KEY KEYS', text, re.S)
            match = re.search(r'ssh-ed25519\s+([A-Za-z0-9+/=]+)', section.group()) if section else None
            if match:
                break
            time.sleep(3)
        else:
            raise RuntimeError('No trusted host key: ' + name)
        lab.paramiko.Ed25519Key(data=base64.b64decode(match[1]))
        lines.append('[' + node['public_ip'] + ']:443 ssh-ed25519 ' + match[1])
    (LOCAL / 'known_hosts').write_text('\n'.join(lines) + '\n')


def failover():
    topology = output()
    with lab.connect(topology['es3']) as client:
        elected = json.loads(lab.remote(client, 'sudo python3 /home/otus/dz07/scripts/remote/api.py master', 'master'))['node']
    if elected not in topology:
        raise RuntimeError('Unknown master')
    survivor = next(n for n in topology if n != elected)
    node = topology[elected]
    cloud = Cloud()
    before = cloud.get('compute', '/servers/' + node['id'])['server']
    if before['name'] != 'otus-dz07-' + elected:
        raise RuntimeError('Foreign server UUID')
    start = time.monotonic()
    try:
        cloud.get('compute', '/servers/' + node['id'] + '/action', {'os-stop': None})
        for _ in range(90):
            if cloud.get('compute', '/servers/' + node['id'])['server']['status'] == 'SHUTOFF':
                break
            time.sleep(2)
        else:
            raise RuntimeError('SHUTOFF not confirmed')
        with lab.connect(topology[survivor]) as client:
            result = json.loads(lab.remote(client, 'sudo python3 /home/otus/dz07/scripts/remote/api.py failure', 'failover'))
        if result['new_master']['node'] == elected:
            raise RuntimeError('Master did not change')
        result.update(stopped_master=elected, cloud='Selectel', elapsed_seconds=round(time.monotonic() - start, 2))
        lab.save(ROOT / 'docs/failover.json', result)
    finally:
        cloud.get('compute', '/servers/' + node['id'] + '/action', {'os-start': None})
    with lab.connect(topology[survivor]) as client:
        result = json.loads(lab.remote(client, 'sudo python3 /home/otus/dz07/scripts/remote/api.py recovery', 'recovery'))
        lab.save(ROOT / 'docs/recovery.json', result)


def cleanup():
    state = json.loads(tf('show', '-json', capture=True))
    resources = state.get('values', {}).get('root_module', {}).get('resources', [])
    floating_ids = [r['values']['id'] for r in resources if r['type'] == 'openstack_networking_floatingip_v2']
    tf('apply', '-input=false', '-no-color', str(plan(True)))
    state = json.loads(tf('show', '-json', capture=True))
    managed = [r for r in state.get('values', {}).get('root_module', {}).get('resources', []) if r.get('mode') == 'managed']
    cloud = Cloud()
    remaining = []
    for service, path in [('compute', '/servers/detail'), ('compute', '/os-server-groups'),
                          ('volumev3', '/volumes/detail'), ('network', '/v2.0/networks'),
                          ('network', '/v2.0/subnets'), ('network', '/v2.0/routers'),
                          ('network', '/v2.0/ports'), ('network', '/v2.0/security-groups')]:
        for items in cloud.get(service, path).values():
            if isinstance(items, list):
                remaining += [x['name'] for x in items if isinstance(x, dict) and x.get('name', '').startswith('otus-dz07')]
    fips = cloud.get('network', '/v2.0/floatingips')['floatingips']
    own_fips = [f['id'] for f in fips if f['id'] in floating_ids]
    result = {'checked_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'managed_remaining': len(managed), 'named_remaining': remaining, 'floating_remaining': own_fips,
              'success': not managed and not remaining and not own_fips}
    lab.save(ROOT / 'docs/selectel-cleanup.json', result)
    if not result['success']:
        raise RuntimeError('Selectel cleanup incomplete')


def diagnostics():
    try:
        topology = output()
    except Exception:
        return
    for name, node in topology.items():
        try:
            with lab.connect(node) as client:
                lab.remote(client, 'sudo tail -n 100 /var/log/elasticsearch/otus-dz07.log', name + '-diagnostics')
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'deploy', 'configure', 'verify', 'failover', 'cleanup', 'test', 'resume-test'])
    action = parser.parse_args().action
    # Fail before entering a cloud lifecycle if no operator access is available.
    auth()
    # Explicitly bind the shared transport to this provider's topology and trust source.
    lab.output, lab.trust = output, trust
    if action == 'prepare':
        prepare()
    elif action in ['deploy', 'test', 'resume-test']:
        try:
            if action != 'resume-test':
                preflight()
                tf('apply', '-input=false', '-no-color', str(plan()))
            lab.configure()
            lab.verify()
            if action in ['test', 'resume-test']:
                failover()
        finally:
            if action in ['test', 'resume-test']:
                try:
                    if sys.exc_info()[0] is not None:
                        diagnostics()
                finally:
                    cleanup()
    elif action == 'configure':
        lab.configure()
    elif action == 'verify':
        lab.verify()
    elif action == 'failover':
        failover()
    else:
        cleanup()


if __name__ == '__main__':
    main()
