"""Lifecycle ДЗ07. No implicit destroy; only its own YC Terraform state."""
import argparse
import base64
import concurrent.futures
import datetime
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tarfile
import threading
import time
import urllib.error
import urllib.request

import paramiko
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa, ed25519
from cryptography.x509.oid import NameOID
from yc_preflight import collect

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / '.local'
YC = Path(os.environ.get('OTUS_YC', str(ROOT.parent / 'OTUS_DZ04/.tools/yc.exe')))
CONFIG = Path(os.environ.get('OTUS_YC_CONFIG', str(ROOT.parent / 'OTUS_DZ04/.local/yc-config.yaml')))
TF = Path(os.environ.get('OTUS_TERRAFORM', str(ROOT / '.tools/terraform.exe')))
IPS = {'es1': '10.77.1.10', 'es2': '10.77.2.10', 'es3': '10.77.3.10'}
ZONES = dict(zip(IPS, ['a', 'd', 'e']))


def save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')


def yc(*args):
    result = subprocess.run([str(YC), '--no-browser', '--config', str(CONFIG),
                             '--format', 'json', *args], capture_output=True, timeout=60)
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors='replace')[-2000:])
    return json.loads(result.stdout or 'null')


def tf(*args, capture=False):
    env = dict(os.environ)
    env['TF_CLI_CONFIG_FILE'] = str(ROOT / 'terraform.rc')
    env['YC_TOKEN'] = subprocess.check_output(
        [str(YC), '--no-browser', '--config', str(CONFIG), 'iam', 'create-token'], timeout=60).decode().strip()
    result = subprocess.run([str(TF), '-chdir=' + str(ROOT / 'terraform'), *args],
                            env=env, capture_output=True, timeout=1200)
    LOCAL.mkdir(exist_ok=True)
    (LOCAL / ('tf-' + args[0] + '.log')).write_bytes(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError((result.stdout + result.stderr).decode(errors='replace')[-3000:])
    if capture:
        return result.stdout.decode()
    print('Terraform ' + args[0] + ': OK', flush=True)


def preflight():
    report = collect(YC, CONFIG)
    cloud = next(c for c in report['clouds'] if c['id'] == 'b1gi39a7q0m4a0f0fc36')
    limits = {q['quota_id']: q for s in cloud['quota_limits'].values() for q in s['quota_limits']}
    required = {'compute.instances.count': 3, 'compute.disks.count': 3,
                'vpc.networks.count': 1, 'vpc.subnets.count': 3,
                'vpc.securityGroups.count': 1, 'vpc.externalAddresses.count': 3}
    for node, zone in ZONES.items():
        suffix = '.ru-central1-' + zone
        required['compute.instanceCores.count' + suffix] = 2
        required['compute.instanceMemory.size' + suffix] = 8 * 2**30
        required['compute.hddDisks.size' + suffix] = (40 if node == 'es2' else 20) * 2**30
    for key, needed in required.items():
        quota = limits[key]
        if quota['limit'] - quota.get('usage', 0) < needed:
            raise RuntimeError('Not enough free quota: ' + key)
    save(ROOT / 'docs/yc-quota-result.json', {'checked_at_utc': report['checked_at_utc'],
         'cloud_name': cloud['name'], 'required': required,
         'quotas': {k: limits[k] for k in required}, 'fits': True})
    return cloud


def certificates():
    certdir = LOCAL / 'certs'
    if certdir.exists():
        for name in ['ca.crt', 'ca.key'] + [n + ext for n in IPS for ext in ['.crt', '.key']]:
            if not (certdir / name).exists():
                raise RuntimeError('Incomplete certificate set; do not silently rotate CA')
        return
    certdir.mkdir()
    now = datetime.datetime.now(datetime.timezone.utc)
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'OTUS DZ07 CA')])
    ca = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
          .public_key(key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=365))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .sign(key, hashes.SHA256()))
    def write_key(path, private):
        path.write_bytes(private.private_bytes(serialization.Encoding.PEM,
                         serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    write_key(certdir / 'ca.key', key)
    (certdir / 'ca.crt').write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    for name, ip in IPS.items():
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cert = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
                .issuer_name(subject).public_key(private.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=180))
                .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(ip)), x509.DNSName(name)]), False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
                .sign(key, hashes.SHA256()))
        write_key(certdir / (name + '.key'), private)
        (certdir / (name + '.crt')).write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def prepare():
    cloud = preflight()
    LOCAL.mkdir(exist_ok=True)
    key = LOCAL / 'otus_dz07'
    if not key.exists():
        private = ed25519.Ed25519PrivateKey.generate()
        key.write_bytes(private.private_bytes(serialization.Encoding.PEM,
                        serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()))
        key.with_suffix('.pub').write_bytes(private.public_key().public_bytes(
            serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH))
    if not key.with_suffix('.pub').exists():
        raise RuntimeError('Missing public key')
    if not (LOCAL / 'secrets.json').exists():
        save(LOCAL / 'secrets.json', {k: secrets.token_urlsafe(30) for k in
                                    ['elastic', 'shipper', 'kibana', 'django', 'database']})
    certificates()
    ip = urllib.request.urlopen('https://api.ipify.org', timeout=15).read().decode().strip()
    ipaddress.IPv4Address(ip)
    path = ROOT / 'terraform/terraform.tfvars.json'
    previous = json.loads(path.read_text()).get('admin_cidrs', []) if path.exists() else []
    save(path, {'cloud_id': cloud['id'], 'folder_id': 'b1gc5tbdq8nn335pl2pr',
                'admin_cidrs': list(dict.fromkeys(previous + [ip + '/32']))})
    tf('init', '-input=false', '-no-color')
    tf('fmt')
    tf('validate', '-no-color')


def plan(destroy=False):
    path = LOCAL / ('destroy.tfplan' if destroy else 'create.tfplan')
    tf('plan', '-input=false', '-no-color', *(['-destroy'] if destroy else []), '-out=' + str(path))
    data = json.loads(tf('show', '-json', str(path), capture=True))
    for change in data.get('resource_changes', []):
        if change.get('mode') == 'data':
            continue
        actions = change['change']['actions']
        if actions not in (['no-op'], ['delete'] if destroy else ['create']):
            raise RuntimeError('Unexpected mutation: ' + change['address'])
        value = change['change'].get('before' if destroy else 'after') or {}
        if not value.get('name', '').startswith('otus-dz07'):
            raise RuntimeError('Foreign resource in plan')
    return path


def output():
    return json.loads(tf('output', '-json', 'lab', capture=True))


def trust(lab):
    lines = []
    for name, node in lab.items():
        for _ in range(60):
            data = yc('compute', 'instance', 'get-serial-port-output', node['id'])
            text = data.get('contents', '')
            section = re.search(r'BEGIN SSH HOST KEY KEYS.*?END SSH HOST KEY KEYS', text, re.S)
            match = re.search(r'ssh-ed25519\s+([A-Za-z0-9+/=]+)', section.group()) if section else None
            if match:
                break
            time.sleep(3)
        else:
            raise RuntimeError('No host key in YC serial output: ' + name)
        paramiko.Ed25519Key(data=base64.b64decode(match[1]))
        lines.append(node['public_ip'] + ' ssh-ed25519 ' + match[1])
    (LOCAL / 'known_hosts').write_text('\n'.join(lines) + '\n')


def connect(node):
    client = paramiko.SSHClient()
    client.load_host_keys(str(LOCAL / 'known_hosts'))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    client.connect(node['public_ip'], port=22, username='otus', key_filename=str(LOCAL / 'otus_dz07'),
                   look_for_keys=False, allow_agent=False, timeout=20)
    return client


def remote(client, command, label, stdin=None):
    channel = client.get_transport().open_session(timeout=30)
    channel.set_combine_stderr(True)
    channel.exec_command(command)
    if stdin:
        channel.sendall(stdin)
    channel.shutdown_write()
    result = bytearray()
    deadline = time.monotonic() + 1800
    while not channel.exit_status_ready() or channel.recv_ready():
        if time.monotonic() > deadline:
            channel.close()
            raise TimeoutError(label)
        if channel.recv_ready():
            result.extend(channel.recv(65536))
        else:
            time.sleep(.1)
    (LOCAL / (label + '.log')).write_bytes(result)
    if channel.recv_exit_status():
        raise RuntimeError(label + ': ' + result.decode(errors='replace')[-2500:])
    print(label + ': OK', flush=True)
    return result.decode()


def upload_package(client, sftp, node, source, target, expected, label):
    size = source.stat().st_size
    try:
        offset = sftp.stat(target).st_size
    except OSError:
        offset = 0
    def digest():
        _, stdout, stderr = client.exec_command('sha512sum ' + target, timeout=60)
        value = stdout.read().decode().split()
        if stdout.channel.recv_exit_status() or not value:
            raise RuntimeError('Remote SHA512 command failed')
        return value[0]
    if offset == size and digest() == expected:
        print(label + ': already verified', flush=True)
        return
    if offset >= size:
        offset = 0
    if not offset:
        with sftp.open(target, 'wb'):
            pass
    starts = list(range(offset, size, 8 * 2**20))
    completed = offset
    lock = threading.Lock()
    def worker(index):
        nonlocal completed
        with connect(node) as peer:
            with peer.open_sftp() as channel:
                channel.get_channel().settimeout(90)
                with channel.open(target, 'r+b') as destination, source.open('rb') as origin:
                    destination.set_pipelined(True)
                    for start in starts[index::8]:
                        count = min(8 * 2**20, size - start)
                        origin.seek(start)
                        destination.seek(start)
                        remaining = count
                        while remaining:
                            block = origin.read(min(2**20, remaining))
                            if not block:
                                raise RuntimeError('Unexpected package EOF')
                            destination.write(block)
                            remaining -= len(block)
                        destination.flush()
                        with lock:
                            previous = completed
                            completed += count
                            if completed // (64 * 2**20) != previous // (64 * 2**20):
                                print(f'{label}: {completed // 2**20}/{size // 2**20} MiB', flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(worker, range(min(8, len(starts)))))
    if sftp.stat(target).st_size != size or digest() != expected:
        raise RuntimeError('Uploaded package SHA512 mismatch: ' + label)
    print(label + ': SHA512 verified on VM', flush=True)


def upload(name, node):
    settings = {'name': name, 'ips': IPS, 'secrets': json.loads((LOCAL / 'secrets.json').read_text()),
                'version': '8.19.0', 'public_ip': node['public_ip']}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        for folder in ['app', 'scripts/remote']:
            for path in (ROOT / folder).rglob('*'):
                if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
                    archive.add(path, arcname=path.relative_to(ROOT).as_posix())
        for src, dst in [(LOCAL / 'certs/ca.crt', 'certs/ca.crt'),
                         (LOCAL / ('certs/' + name + '.crt'), 'certs/node.crt'),
                         (LOCAL / ('certs/' + name + '.key'), 'certs/node.key')]:
            archive.add(src, arcname=dst)
        raw = json.dumps(settings).encode()
        info = tarfile.TarInfo('settings.json')
        info.size, info.mode = len(raw), 0o600
        archive.addfile(info, io.BytesIO(raw))
    with connect(node) as client:
        remote(client, 'sudo python3 -', name + '-cloud-init',
               stdin=(ROOT / 'scripts/remote/check_cloud_init.py').read_bytes())
        with client.open_sftp() as sftp:
            with sftp.open('/home/otus/dz07.tar.gz', 'wb') as f:
                f.write(buffer.getvalue())
            sftp.chmod('/home/otus/dz07.tar.gz', 0o600)
        remote(client, 'mkdir -p /home/otus/dz07/packages && chmod 700 /home/otus/dz07 && tar xzf /home/otus/dz07.tar.gz -C /home/otus/dz07', name + '-upload')
        cache = LOCAL / 'packages'
        if (cache / 'manifest.json').exists():
            products = ['elasticsearch', 'filebeat'] if name == 'es1' else (['kibana'] if name == 'es3' else [])
            with client.open_sftp() as sftp:
                sftp.put(str(cache / 'manifest.json'), '/home/otus/dz07/packages/manifest.json')
                manifest = json.loads((cache / 'manifest.json').read_text())
                for product in products:
                    filename = product + '-' + settings['version'] + '-amd64.deb'
                    target = '/home/otus/dz07/packages/' + filename
                    upload_package(client, sftp, node, cache / filename, target, manifest[filename], name + '-' + product)


def configure():
    lab = output()
    trust(lab)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(upload, name, node) for name, node in lab.items()]
        for future in futures:
            future.result()
    with connect(lab['es1']) as client:
        remote(client, 'sudo python3 /home/otus/dz07/scripts/remote/setup.py cache', 'start-package-cache')
    def install(name, node):
        with connect(node) as client:
            remote(client, 'sudo python3 /home/otus/dz07/scripts/remote/setup.py', name + '-install')
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(install, name, node) for name, node in lab.items()]
        for future in futures:
            future.result()
    with connect(lab['es1']) as client:
        remote(client, 'sudo systemctl stop otus-dz07-package-cache.service', 'stop-package-cache')
    with connect(lab['es1']) as client:
        remote(client, 'sudo python3 /home/otus/dz07/scripts/remote/api.py setup', 'cluster-setup')
    # Remove bootstrap settings on disk only after the cluster is confirmed formed.
    for name, node in lab.items():
        with connect(node) as client:
            remote(client, 'sudo python3 /home/otus/dz07/scripts/remote/setup.py finalize', name + '-finalize')
    with connect(lab['es3']) as client:
        result = json.loads(remote(client, 'sudo python3 /home/otus/dz07/scripts/remote/api.py kibana', 'kibana'))
        save(ROOT / 'docs/kibana.json', result)


def verify_kibana_http(public_ip):
    url = 'http://' + public_ip + ':5601/login'
    # Kibana can still be starting after its VM has rejoined Elasticsearch.
    for attempt in range(20):
        try:
            with urllib.request.urlopen(url, timeout=15) as response:
                if response.status != 200 or b'kibana' not in response.read().lower():
                    raise RuntimeError('Public Kibana login page unavailable')
            break
        except (urllib.error.URLError, TimeoutError):
            if attempt == 19:
                raise
            time.sleep(3)
    return {'login_page_http_status': 200, 'port': 5601, 'authentication_sent': False}


def verify():
    lab = output()
    for name, node in lab.items():
        with connect(node) as client:
            raw = remote(client, 'sudo python3 /home/otus/dz07/scripts/remote/setup.py generate', name + '-generate')
            if name == 'es3':
                save(ROOT / 'docs/http-check.json', json.loads(raw))
    with connect(lab['es1']) as client:
        raw = remote(client, 'sudo python3 /home/otus/dz07/scripts/remote/api.py verify', 'verify')
        save(ROOT / 'docs/verification.json', json.loads(raw))
    save(ROOT / 'docs/kibana-public.json', verify_kibana_http(lab['es3']['public_ip']))


def failover():
    lab = output()
    with connect(lab['es3']) as client:
        elected = json.loads(remote(client, 'sudo python3 /home/otus/dz07/scripts/remote/api.py master', 'master'))['node']
    if elected not in lab:
        raise RuntimeError('Unknown master node')
    survivor = next(name for name in lab if name != elected)
    before = yc('compute', 'instance', 'get', lab[elected]['id'])
    if before.get('name') != 'otus-dz07-' + elected:
        raise RuntimeError('Foreign instance')
    start = time.monotonic()
    try:
        yc('compute', 'instance', 'stop', lab[elected]['id'])
        stopped = yc('compute', 'instance', 'get', lab[elected]['id'])
        if stopped.get('status') != 'STOPPED':
            raise RuntimeError('Shutdown not confirmed')
        with connect(lab[survivor]) as client:
            result = json.loads(remote(client, 'sudo python3 /home/otus/dz07/scripts/remote/api.py failure', 'failover'))
        result.update(stopped_master=elected, elapsed_seconds=round(time.monotonic() - start, 2))
        if result['new_master']['node'] == elected:
            raise RuntimeError('Master did not change')
        save(ROOT / 'docs/failover.json', result)
    finally:
        yc('compute', 'instance', 'start', lab[elected]['id'])
    with connect(lab[survivor]) as client:
        result = json.loads(remote(client, 'sudo python3 /home/otus/dz07/scripts/remote/api.py recovery', 'recovery'))
        save(ROOT / 'docs/recovery.json', result)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'deploy', 'configure', 'verify', 'failover', 'cleanup'])
    action = parser.parse_args().action
    if action == 'prepare':
        prepare()
    elif action == 'deploy':
        preflight()
        tf('apply', '-input=false', '-no-color', str(plan()))
        configure()
        verify()
    elif action == 'configure':
        configure()
    elif action == 'verify':
        verify()
    elif action == 'failover':
        failover()
    elif action == 'cleanup':
        tf('apply', '-input=false', '-no-color', str(plan(True)))


if __name__ == '__main__':
    main()
