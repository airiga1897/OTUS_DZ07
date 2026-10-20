"""Конфигурация Ubuntu: ES на всех узлах, web/db/lb как источники."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request

BASE = Path('/home/otus/dz07')
C = json.loads((BASE / 'settings.json').read_text())
NAME, IPS, S = C['name'], C['ips'], C['secrets']
os.environ['DEBIAN_FRONTEND'] = 'noninteractive'


def run(*args, **kw):
    return subprocess.run(args, check=True, **kw)


def write(path, content, mode=0o644):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(mode)


def package(product):
    installed = subprocess.run(['dpkg-query', '-W', '-f=${Version}', product], capture_output=True, text=True)
    if installed.returncode == 0 and installed.stdout.strip() == C['version']:
        return
    filename = product + '-' + C['version'] + '-amd64.deb'
    directory = 'beats/filebeat' if product == 'filebeat' else product
    url = 'https://artifacts.elastic.co/downloads/' + directory + '/' + filename
    destination = Path('/var/tmp') / filename
    cache = BASE / 'packages'
    if (cache / 'manifest.json').exists():
        destination = cache / filename
        checksum = json.loads((cache / 'manifest.json').read_text())[filename]
        if not destination.exists() or hashlib.file_digest(destination.open('rb'), 'sha512').hexdigest() != checksum:
            urllib.request.urlretrieve('http://' + IPS['es1'] + ':18080/' + filename, destination)
    else:
        urllib.request.urlretrieve(url, destination)
        checksum = urllib.request.urlopen(url + '.sha512', timeout=30).read().decode().split()[0]
    digest = hashlib.file_digest(destination.open('rb'), 'sha512').hexdigest()
    if digest != checksum:
        raise RuntimeError('Package checksum mismatch: ' + product)
    run('dpkg', '-i', str(destination))
    # Keep public packages available until all peers finish installation.


def elastic():
    package('elasticsearch')
    dest = Path('/etc/elasticsearch/certs')
    dest.mkdir(exist_ok=True)
    for path in (BASE / 'certs').iterdir():
        shutil.copy(path, dest / path.name)
    run('chown', '-R', 'root:elasticsearch', str(dest))
    run('chmod', '750', str(dest))
    for path in dest.iterdir():
        path.chmod(0o640)
    # Bootstrap must never be reintroduced into an already formed node.
    existing = Path('/etc/elasticsearch/elasticsearch.yml')
    old = existing.read_text() if existing.exists() else ''
    formed = (BASE / 'formed').exists()
    if 'cluster.name: otus-dz07' in old and 'cluster.initial_master_nodes' not in old:
        formed = True
    config = f'''cluster.name: otus-dz07
node.name: {NAME}
node.roles: [master, data, ingest]
network.host: {IPS[NAME]}
discovery.seed_hosts: {json.dumps(list(IPS.values()))}
path.data: /var/lib/elasticsearch
path.logs: /var/log/elasticsearch
xpack.security.enabled: true
xpack.security.autoconfiguration.enabled: false
xpack.security.transport.ssl.enabled: true
xpack.security.transport.ssl.verification_mode: full
xpack.security.transport.ssl.key: certs/node.key
xpack.security.transport.ssl.certificate: certs/node.crt
xpack.security.transport.ssl.certificate_authorities: [certs/ca.crt]
xpack.security.http.ssl.enabled: true
xpack.security.http.ssl.key: certs/node.key
xpack.security.http.ssl.certificate: certs/node.crt
xpack.security.http.ssl.certificate_authorities: [certs/ca.crt]
'''
    if not formed:
        config += 'cluster.initial_master_nodes: [es1, es2, es3]\n'
    write(existing, config, 0o640)
    run('chown', 'root:elasticsearch', str(existing))
    write('/etc/elasticsearch/jvm.options.d/heap.options', '-Xms2g\n-Xmx2g\n')
    write('/etc/sysctl.d/90-otus-es.conf', 'vm.max_map_count=1048576\n')
    run('sysctl', '--system')
    # Generated bootstrap password is replaced by the persisted elastic password at setup.
    run('/usr/share/elasticsearch/bin/elasticsearch-keystore', 'add', '-x', '-f', 'bootstrap.password',
        input=S['elastic'] + '\n', text=True)
    run('systemctl', 'enable', 'elasticsearch')
    run('systemctl', 'restart', 'elasticsearch')


def database():
    run('apt-get', 'install', '-y', '-qq', 'postgresql-16')
    write('/etc/postgresql/16/main/conf.d/dz07.conf', f"listen_addresses = '{IPS['es2']},127.0.0.1'\nlog_connections = on\nlog_disconnections = on\nlog_min_duration_statement = 0\n")
    hba = Path('/etc/postgresql/16/main/pg_hba.conf')
    rule = f"host otus otus {IPS['es1']}/32 scram-sha-256\n"
    if rule not in hba.read_text():
        with hba.open('a') as f:
            f.write(rule)
    sql = f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='otus') THEN CREATE ROLE otus LOGIN; END IF; END $$; ALTER ROLE otus PASSWORD '{S['database']}';"
    run('sudo', '-u', 'postgres', 'psql', '-v', 'ON_ERROR_STOP=1',
        input="SET log_min_duration_statement=-1;\nSET log_statement='none';\n" + sql,
        text=True, stdout=subprocess.DEVNULL)
    exists = subprocess.check_output(['sudo', '-u', 'postgres', 'psql', '-Atc', "SELECT 1 FROM pg_database WHERE datname='otus'"])
    if not exists.strip():
        run('sudo', '-u', 'postgres', 'createdb', '-O', 'otus', 'otus')
    run('systemctl', 'restart', 'postgresql')


def web():
    run('apt-get', 'install', '-y', '-qq', 'nginx', 'python3-venv', 'python3-dev', 'build-essential', 'libpcre3-dev')
    app = Path('/opt/otus-dz07')
    shutil.copytree(BASE / 'app', app / 'app', dirs_exist_ok=True)
    if not (app / 'venv/bin/python').exists():
        run('python3', '-m', 'venv', str(app / 'venv'))
    run(str(app / 'venv/bin/pip'), 'install', '-r', str(app / 'app/requirements.txt'), 'uWSGI==2.0.31')
    if subprocess.run(['id', 'otusapp'], capture_output=True).returncode:
        run('useradd', '--system', '--home', str(app), '--shell', '/usr/sbin/nologin', 'otusapp')
    run('install', '-d', '-o', 'otusapp', '-g', 'otusapp', '/srv/otus/static', '/srv/otus/media')
    write('/srv/otus/.ready', 'otus-dz04\n')
    env = {'DJANGO_SECRET_KEY': S['django'], 'DB_NAME': 'otus', 'DB_USER': 'otus',
           'DB_PASSWORD': S['database'], 'DB_HOST': IPS['es2'], 'REQUIRE_SHARED_MOUNT': '0',
           'SHARED_ROOT': '/srv/otus', 'NODE_NAME': 'otus-dz07-es1', 'DJANGO_ALLOWED_HOSTS': '*'}
    write('/etc/otus-dz07.env', ''.join(k + '=' + v + '\n' for k, v in env.items()), 0o600)
    for args in [('migrate', '--noinput'), ('seed_lessons',), ('collectstatic', '--noinput')]:
        run(str(app / 'venv/bin/python'), str(app / 'app/manage.py'), *args, env={**os.environ, **env})
    run('chown', '-R', 'otusapp:otusapp', '/srv/otus')
    write('/etc/systemd/system/otus-dz07.service', '''[Unit]
After=network.target
[Service]
User=otusapp
Group=otusapp
WorkingDirectory=/opt/otus-dz07/app
EnvironmentFile=/etc/otus-dz07.env
ExecStart=/opt/otus-dz07/venv/bin/uwsgi --http-socket 127.0.0.1:8080 --module config.wsgi:application --master --processes 2 --threads 2 --enable-threads --need-app --die-on-term
Restart=on-failure
[Install]
WantedBy=multi-user.target
''')
    Path('/etc/nginx/sites-enabled/default').unlink(missing_ok=True)
    write('/etc/nginx/conf.d/otus.conf', '''server {
 listen 8081;
 server_name _;
 access_log /var/log/nginx/otus-access.log;
 error_log /var/log/nginx/otus-error.log;
 location / { proxy_pass http://127.0.0.1:8080; proxy_set_header Host $host; }
 location /static/ { alias /srv/otus/static/; }
}''')
    run('nginx', '-t')
    run('systemctl', 'daemon-reload')
    run('systemctl', 'enable', '--now', 'otus-dz07')
    run('systemctl', 'restart', 'otus-dz07', 'nginx')


def loadbalancer():
    run('apt-get', 'install', '-y', '-qq', 'haproxy', 'rsyslog')
    write('/etc/rsyslog.d/49-otus-haproxy.conf', 'local0.* /var/log/haproxy.log\n& stop\n')
    write('/etc/haproxy/haproxy.cfg', f'''global
    log /dev/log local0
    user haproxy
    group haproxy
    daemon
defaults
    log global
    mode http
    option httplog
    timeout connect 5s
    timeout client 30s
    timeout server 30s
frontend web
    bind *:80
    default_backend portal
backend portal
    server web1 {IPS['es1']}:8081 check
''')
    run('haproxy', '-c', '-f', '/etc/haproxy/haproxy.cfg')
    run('systemctl', 'restart', 'rsyslog', 'haproxy')
    package('kibana')
    dest = Path('/etc/kibana/ca.crt')
    shutil.copy(BASE / 'certs/ca.crt', dest)
    write('/etc/kibana/kibana.yml', f'''server.host: "0.0.0.0"
server.port: 5601
server.ssl.enabled: false
elasticsearch.hosts: {json.dumps(['https://' + ip + ':9200' for ip in IPS.values()])}
elasticsearch.username: "kibana_system"
elasticsearch.password: "{S['kibana']}"
elasticsearch.ssl.certificateAuthorities: ["/etc/kibana/ca.crt"]
xpack.encryptedSavedObjects.encryptionKey: "{S['django']}"
xpack.security.encryptionKey: "{S['django']}"
xpack.reporting.encryptionKey: "{S['django']}"
''', 0o640)
    run('chown', 'root:kibana', '/etc/kibana/kibana.yml')


def filebeat():
    package('filebeat')
    shutil.copy(BASE / 'certs/ca.crt', '/etc/filebeat/ca.crt')
    inputs = [{'type': 'journald', 'id': 'system-journal',
               'fields': {'service': {'name': 'system'}}, 'fields_under_root': True}]
    sources = [('elasticsearch', ['/var/log/elasticsearch/*.log', '/var/log/elasticsearch/*_server.json'])] + {'es1': [('nginx', '/var/log/nginx/otus-*.log')],
               'es2': [('postgresql', '/var/log/postgresql/postgresql-*.log')],
               'es3': [('haproxy', '/var/log/haproxy.log')]}[NAME]
    for service, path in sources:
        inputs.append({'type': 'filestream', 'id': service, 'paths': path if isinstance(path, list) else [path],
                       'fields': {'service': {'name': service}}, 'fields_under_root': True})
    cfg = {'filebeat.inputs': inputs, 'processors': [{'add_host_metadata': {}}],
           'output.elasticsearch': {'hosts': ['https://' + ip + ':9200' for ip in IPS.values()],
                                    'loadbalance': True, 'username': 'otus_shipper', 'password': S['shipper'],
                                    'ssl.certificate_authorities': ['/etc/filebeat/ca.crt'],
                                    'index': 'logs-otus-%{+yyyy.MM.dd}', 'pipeline': 'otus-logs'},
           'setup.ilm.enabled': False, 'setup.template.enabled': False,
           'queue.disk': {'max_size': '512MB'}}
    write('/etc/filebeat/filebeat.yml', json.dumps(cfg, indent=2), 0o600)
    run('filebeat', 'test', 'config', '-c', '/etc/filebeat/filebeat.yml')


def generate():
    run('logger', '-t', 'otus-dz07', 'OTUS_DZ07 verification marker ' + NAME)
    if NAME == 'es2':
        run('sudo', '-u', 'postgres', 'psql', '-c', "SELECT 'OTUS_DZ07 database verification';", stdout=subprocess.DEVNULL)
    if NAME == 'es3':
        statuses = {}
        for path in ['/', '/health/ready', '/missing-otus-dz07'] * 12:
            try:
                with urllib.request.urlopen('http://127.0.0.1' + path, timeout=15) as response:
                    body = response.read()
                    statuses[path] = response.status
                    if path == '/' and b'OTUS' not in body:
                        raise RuntimeError('Unexpected portal response')
            except urllib.error.HTTPError as error:
                if path != '/missing-otus-dz07' or error.code != 404:
                    raise
                statuses[path] = error.code
        print(json.dumps({'http_statuses': statuses}))


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else 'install'
    if action == 'cache':
        run('systemd-run', '--collect', '--unit=otus-dz07-package-cache', '--uid=otus',
            '/usr/bin/python3', '-m', 'http.server', '18080', '--bind', IPS['es1'], '--directory', str(BASE / 'packages'))
    elif action == 'generate':
        generate()
    elif action == 'finalize':
        path = Path('/etc/elasticsearch/elasticsearch.yml')
        write(path, '\n'.join(line for line in path.read_text().splitlines() if not line.startswith('cluster.initial_master_nodes:')) + '\n', 0o640)
        run('chown', 'root:elasticsearch', str(path))
        write(BASE / 'formed', 'formed\n', 0o600)
        if NAME == 'es1':
            web()
        run('systemctl', 'enable', 'filebeat')
        run('systemctl', 'restart', 'filebeat')
        if NAME == 'es3':
            run('systemctl', 'enable', 'kibana')
            run('systemctl', 'restart', 'kibana')
    else:
        run('apt-get', 'update', '-qq')
        run('apt-get', 'install', '-y', '-qq', 'ca-certificates', 'curl')
        elastic()
        if NAME == 'es2':
            database()
        if NAME == 'es3':
            loadbalancer()
        filebeat()


if __name__ == '__main__':
    main()
