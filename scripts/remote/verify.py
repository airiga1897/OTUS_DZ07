"""Check committed application data, HTTP and media across repeated configuration."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.request
import urllib.error
import uuid

if __package__:
    from .runtime_config import BASE, APP, SHARED, STACK
else:
    from runtime_config import BASE, APP, SHARED, STACK

PYTHON = str(APP / 'venv/bin/python')
if __name__ == '__main__' and sys.prefix != str(APP / 'venv'):
    os.execv(PYTHON, [PYTHON, __file__, *sys.argv[1:]])
lab, passwords = {}, {}


def initialize():
    global lab, passwords
    if sys.prefix != str(APP / 'venv'):
        os.execv(PYTHON, [PYTHON, __file__, *sys.argv[1:]])
    settings = json.loads((BASE / 'settings.json').read_text())
    lab, passwords = settings['lab'], settings['passwords']


import psycopg


def cluster():
    if not any(n.startswith('db') for n in lab['private_ips']):
        with sql_connect() as conn:
            if conn.execute('SELECT pg_is_in_recovery()').fetchone()[0]:
                raise AssertionError('Standalone PostgreSQL must be writable')
        return {'app': {'role': 'primary', 'state': 'running'}}
    members = {}
    for name in sorted(n for n in lab['private_ips'] if n.startswith('db')):
        try:
            with urllib.request.urlopen('http://' + lab['private_ips'][name] + ':' + str(STACK['patroni']['api_port']) + '/patroni', timeout=2) as response:
                data = json.load(response)
            members[name] = {'role': data.get('role'), 'state': data.get('state'),
                             'timeline': data.get('timeline'), 'server_version': data.get('server_version')}
        except OSError:
            members[name] = {'state': 'unreachable'}
    return members


def sql_connect(host='127.0.0.1'):
    return psycopg.connect(host=host, port=STACK['postgresql']['port'], user='otus', password=passwords['app'],
                          dbname='otus', connect_timeout=3, options='-c statement_timeout=10000')


def check_record(conn, record):
    row = conn.execute('SELECT id,slug,body FROM knowledge_article WHERE id=%s', (record['id'],)).fetchone()
    if row != (record['id'], record['slug'], record['body']):
        raise AssertionError('Committed record is absent or changed')


def insert_record(mode):
    token = uuid.uuid4().hex
    slug = mode + '-' + token
    body = 'committed-' + uuid.uuid4().hex
    with sql_connect() as conn:
        if not conn.execute('SELECT NOT pg_is_in_recovery()').fetchone()[0]:
            raise AssertionError('PostgreSQL is not writable')
        if conn.execute("SELECT count(*) FROM knowledge_initialdatastate WHERE key='portal-initial-v1' "
                        "AND status IN ('loaded','preserved_existing')").fetchone()[0] != 1:
            raise AssertionError('Initial-data state missing')
        previous_max = conn.execute('SELECT COALESCE(MAX(id),0) FROM knowledge_article').fetchone()[0]
    output = subprocess.check_output([PYTHON, str(BASE / 'scripts/remote/portal_probe.py'), mode], text=True)
    record = json.loads(output)
    if record['id'] <= previous_max:
        raise AssertionError('Article sequence did not advance beyond existing primary keys')
    # Read through a new connection after COMMIT.
    with sql_connect() as conn:
        check_record(conn, record)
    return record


def check_http(record):
    for path in ['/', '/health/ready']:
        with urllib.request.urlopen('http://127.0.0.1' + path, timeout=5) as response:
            body = response.read()
            if response.status != 200 or not body:
                raise AssertionError('Portal unavailable')
            if path == '/' and record['slug'].encode() in body:
                raise AssertionError('Verification record appeared on the public portal')
    try:
        urllib.request.urlopen('http://127.0.0.1/articles/' + record['slug'] + '/', timeout=5).close()
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
    else:
        raise AssertionError('Verification record is publicly accessible')


def media_manifest(root=SHARED / 'media'):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file() and not p.is_symlink()}


def check_media(expected, root=SHARED / 'media'):
    actual = media_manifest(root)
    if any(actual.get(name) != digest for name, digest in expected.items()):
        raise AssertionError('Existing media was removed or changed')


def check_http_media(run_id, expected):
    name = 'probes/' + run_id + '.txt'
    with urllib.request.urlopen('http://127.0.0.1/media/' + name, timeout=5) as response:
        if response.status != 200 or hashlib.sha256(response.read()).hexdigest() != expected[name]:
            raise AssertionError('Portal media content changed')


def manifest_row(table, row):
    # Migration 0003 only changes visibility of legacy verification rows.
    # Keep checking every other field, including their committed content.
    if (table == 'knowledge_article' and len(row) == 9
            and row[1] == row[2] and row[3] == 6 and row[4] == 'proof' and row[6] == ''
            and re.fullmatch(r'(baseline|failover|recovered|expanded)-[0-9a-f]{32}', row[2])
            and re.fullmatch(r'committed-[0-9a-f]{32}', row[5])):
        return (*row[:7], False, row[8])
    return row


def data_manifest(conn):
    tables = ['knowledge_article', 'auth_user', 'auth_user_groups', 'auth_user_user_permissions']
    return {table: {str(row[0]): hashlib.sha256(json.dumps(manifest_row(table, row), default=str).encode()).hexdigest()
                    for row in conn.execute('SELECT * FROM ' + table + ' ORDER BY id').fetchall()}
            for table in tables}


def check_data(expected, conn):
    actual = data_manifest(conn)
    if any(actual[table].get(pk) != digest for table, rows in expected.items() for pk, digest in rows.items()):
        raise AssertionError('Existing application data was removed or changed')


def main():
    initialize()
    mode, run_id = sys.argv[1:3]
    previous = sys.argv[3] if len(sys.argv) > 3 else None
    if mode not in ['baseline', 'failover', 'recovered', 'expanded'] or not re.fullmatch('[0-9a-f]{32}', run_id):
        raise ValueError('Invalid verification context')
    folder = APP / 'probes'
    folder.mkdir(exist_ok=True)
    context_path = folder / (run_id + '.json')
    context = json.loads(context_path.read_text()) if context_path.exists() else {}
    if mode == 'baseline' and not context:
        media = SHARED / 'media/probes'
        media.mkdir(exist_ok=True)
        (media / (run_id + '.txt')).write_text('media-' + uuid.uuid4().hex)
        context['media'] = media_manifest()
        with sql_connect() as conn:
            context['data'] = data_manifest(conn)
        context_path.write_text(json.dumps(context))
    capacity = {}
    required = [] if mode == 'baseline' else [context['baseline']]
    if mode == 'recovered':
        if 'failover' in context:
            required.append(context['failover'])
        if 'recovered' in context:
            required.append(context['recovered'])
    record = None
    last_error = ''
    for attempt in range(150):
        members = cluster()
        leaders = [n for n, d in members.items() if d.get('role') in ['master', 'primary'] and d.get('state') == 'running']
        healthy = sum(d.get('state') == 'running' for d in members.values())
        try:
            if len(leaders) != 1 or healthy != len(members) - int(mode == 'failover'):
                raise AssertionError('Cluster membership is not ready')
            if mode == 'failover' and leaders[0] == previous:
                raise AssertionError('Leader did not change')
            with sql_connect() as conn:
                capacity = {'senders': int(conn.execute('SHOW max_wal_senders').fetchone()[0]),
                            'slots': int(conn.execute('SHOW max_replication_slots').fetchone()[0])}
                check_data(context['data'], conn)
                for preserved in required:
                    check_record(conn, preserved)
            check_media(context['media'])
            check_http_media(run_id, context['media'])
            if record is None:
                record = insert_record(mode)
            check_http(record)
            if mode in ['recovered', 'expanded']:
                for name in sorted(n for n in lab['private_ips'] if n.startswith('db')):
                    with sql_connect(lab['private_ips'][name]) as conn:
                        check_data(context['data'], conn)
                        for committed in required + [record]:
                            check_record(conn, committed)
            break
        except (AssertionError, OSError, psycopg.Error) as error:
            last_error = type(error).__name__ + ': ' + str(error)
            time.sleep(2)
    else:
        raise RuntimeError('SQL/HTTP or cluster verification failed: ' + last_error + '; members=' + json.dumps(members))
    context[mode] = record
    context_path.write_text(json.dumps(context))
    result = {'mode': mode, 'run_id': run_id, 'leader': leaders[0], 'members': members,
              'sql_and_http': True, 'application_view_write': True, 'media_preserved': True, 'committed_record_preserved': True, 'new_write_succeeded': True,
              'new_record': record, 'preserved_records': required,
              'replica_records_verified': mode in ['recovered', 'expanded'],
              'failover_record_available': 'failover' in context,
              'replication_capacity': capacity,
              'initial_data': json.loads((APP / 'initial-data-result.json').read_text())}
    print(json.dumps(result))


if __name__ == '__main__':
    main()
