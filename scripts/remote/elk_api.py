"""ES API setup and evidence collection over verified TLS."""
import base64
import datetime
import json
from pathlib import Path
import ssl
import sys
import time
import urllib.error
import urllib.request

BASE = Path('/home/otus/dz07')
C = {}
S = {}
CTX = None


def api(path, payload=None, method=None):
    auth = base64.b64encode(('elastic:' + S['elastic']).encode()).decode()
    last = None
    for ip in C['ips'].values():
        request = urllib.request.Request('https://' + ip + ':9200' + path,
                  data=json.dumps(payload).encode() if payload is not None else None,
                  headers={'Authorization': 'Basic ' + auth, 'Content-Type': 'application/json'}, method=method)
        try:
            with urllib.request.urlopen(request, context=CTX, timeout=8) as response:
                return json.load(response)
        except urllib.error.HTTPError:
            raise
        except OSError as error:
            last = error
    raise last


def wait_health(nodes=None, green=True):
    if nodes is None:
        nodes = len(C['ips'])
    for _ in range(120):
        try:
            health = api('/_cluster/health')
            if health['number_of_nodes'] == nodes and (not green or health['status'] == 'green'):
                return health
        except OSError:
            pass
        time.sleep(3)
    raise RuntimeError('Cluster health timeout')


def setup():
    wait_health()
    api('/_security/user/elastic/_password', {'password': S['elastic']}, 'POST')
    api('/_security/user/kibana_system/_password', {'password': S['kibana']}, 'POST')
    api('/_security/role/otus_shipper', {'cluster': ['monitor'], 'indices': [
        {'names': ['logs-otus-*'], 'privileges': ['auto_configure', 'create_doc']}]}, 'PUT')
    api('/_security/user/otus_shipper', {'password': S['shipper'], 'roles': ['otus_shipper']}, 'PUT')
    api('/_ingest/pipeline/otus-logs', {'description': 'OTUS service classification and nginx parsing',
        'processors': [
            {'set': {'field': 'event.dataset', 'value': '{{{service.name}}}'}},
            {'grok': {'if': "ctx.service?.name == 'nginx' && ctx.log?.file?.path?.endsWith('access.log') == true",
                      'field': 'message', 'patterns': ['%{IPORHOST:source.address} - %{DATA:user.name} \\[%{HTTPDATE:nginx.time}\\] "%{WORD:http.request.method} %{DATA:url.original} HTTP/%{NUMBER:http.version}" %{INT:http.response.status_code:int} %{INT:http.response.body.bytes:long} %{GREEDYDATA:nginx.tail}'],
                      'on_failure': [{'set': {'field': 'error.message', 'value': '{{{_ingest.on_failure_message}}}'}}]}}
        ]}, 'PUT')
    api('/_ilm/policy/otus-logs', {'policy': {'phases': {'hot': {'actions': {}},
        'delete': {'min_age': '7d', 'actions': {'delete': {}}}}}}, 'PUT')
    api('/_index_template/otus-logs', {'index_patterns': ['logs-otus-*'], 'priority': 500,
        'template': {'settings': {'number_of_shards': 1, 'number_of_replicas': 1,
                                 'index.lifecycle.name': 'otus-logs'},
                     'mappings': {'properties': {'@timestamp': {'type': 'date'},
                         'service': {'properties': {'name': {'type': 'keyword'}}},
                         'event': {'properties': {'dataset': {'type': 'keyword'}}},
                         'host': {'properties': {'name': {'type': 'keyword'}}}}}}}, 'PUT')
    print(json.dumps({'cluster_uuid': api('/')['cluster_uuid'], 'health': wait_health()}))


def source_counts():
    result = {}
    for node, sources in C['log_expectations'].items():
        for service in sources:
            query = {'bool': {'filter': [{'term': {'host.name': 'otus-dz07-' + node}},
                                          {'term': {'service.name': service}}]}}
            result[node + '/' + service] = api('/logs-otus-*/_count', {'query': query}, 'POST')['count']
    return result


def verify():
    for _ in range(90):
        try:
            result = api('/logs-otus-*/_search', {'size': 0, 'aggs': {
                'services': {'terms': {'field': 'service.name', 'size': 20}},
                'hosts': {'terms': {'field': 'host.name', 'size': max(20, len(C['ips']))}}}}, 'POST')
            services = {x['key'] for x in result['aggregations']['services']['buckets']}
            hosts = {x['key'] for x in result['aggregations']['hosts']['buckets']}
            sources = source_counts()
            if set(C['expected_services']) <= services and {'otus-dz07-' + name for name in C['log_hosts']} <= hosts and all(sources.values()):
                break
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
        time.sleep(3)
    else:
        raise RuntimeError('Missing log sources')
    samples = {}
    for service in sorted(services):
        samples[service] = api('/logs-otus-*/_search', {'size': 2, 'query': {'term': {'service.name': service}},
                              'sort': [{'@timestamp': 'desc'}]}, 'POST')['hits']['hits']
    parsed = api('/logs-otus-*/_count', {'query': {'exists': {'field': 'http.response.status_code'}}}, 'POST')
    if not parsed['count']:
        raise RuntimeError('nginx pipeline has not produced parsed HTTP status')
    print(json.dumps({'checked_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'health': wait_health(), 'cluster_uuid': api('/')['cluster_uuid'],
        'counts': result, 'source_counts': sources, 'parsed_nginx_count': parsed['count'], 'samples': samples}, ensure_ascii=False))


def master():
    api('/logs-otus-failover/_doc/before?refresh=wait_for&wait_for_active_shards=all',
        {'@timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat(),
         'message': 'OTUS_DZ07 committed before master shutdown', 'service': {'name': 'verification'}}, 'PUT')
    print(json.dumps(api('/_cat/master?format=json')[0]))


def failure():
    health = wait_health(nodes=len(C['ips']) - 1, green=False)
    before = api('/logs-otus-failover/_doc/before')
    if not before['found'] or before['_source']['message'] != 'OTUS_DZ07 committed before master shutdown':
        raise RuntimeError('Committed pre-failure document lost')
    api('/logs-otus-failover/_doc/probe?refresh=wait_for',
        {'@timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat(),
         'message': 'OTUS_DZ07 write with one ES node offline', 'service': {'name': 'verification'}}, 'PUT')
    document = api('/logs-otus-failover/_doc/probe')
    if not document['found']:
        raise RuntimeError('Failover probe lost')
    print(json.dumps({'health': health, 'before': before, 'probe': document, 'new_master': api('/_cat/master?format=json')[0]}))


def recovery():
    print(json.dumps({'health': wait_health(), 'before': api('/logs-otus-failover/_doc/before'),
                      'probe': api('/logs-otus-failover/_doc/probe')}))


def kibana():
    auth = base64.b64encode(('elastic:' + S['elastic']).encode()).decode()
    def call(path, payload=None, method=None):
        request = urllib.request.Request('http://' + C['ip'] + ':' + str(C['kibana_port']) + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={'Authorization': 'Basic ' + auth, 'Content-Type': 'application/json', 'kbn-xsrf': 'true'}, method=method)
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)
    for _ in range(120):
        try:
            status = call('/api/status')
            if status['status']['overall']['level'] == 'available':
                break
        except OSError:
            pass
        time.sleep(3)
    else:
        raise RuntimeError('Kibana not ready')
    view = call('/api/data_views/data_view', {'data_view': {'id': 'otus-logs', 'title': 'logs-otus-*',
               'timeFieldName': '@timestamp'}, 'override': True}, 'POST')
    call('/api/kibana/settings', {'changes': {'defaultIndex': 'otus-logs'}}, 'POST')
    call('/api/saved_objects/search/otus-logs?overwrite=true', {'attributes': {
        'title': 'OTUS — централизованные логи', 'columns': ['host.name', 'service.name', 'message'],
        'sort': [['@timestamp', 'desc']], 'kibanaSavedObjectMeta': {'searchSourceJSON': json.dumps({
            'indexRefName': 'kibanaSavedObjectMeta.searchSourceJSON.index', 'query': {'language': 'kuery', 'query': ''}, 'filter': []})}},
        'references': [{'name': 'kibanaSavedObjectMeta.searchSourceJSON.index', 'type': 'index-pattern', 'id': 'otus-logs'}]}, 'POST')
    dashboard = call('/api/saved_objects/dashboard/otus-logs?overwrite=true', {'attributes': {
        'title': 'OTUS DZ07 — журналы серверов', 'timeRestore': False,
        'optionsJSON': json.dumps({'useMargins': True}),
        'panelsJSON': json.dumps([{'panelIndex': '1', 'type': 'search', 'panelRefName': 'panel_1',
              'embeddableConfig': {}, 'gridData': {'x': 0, 'y': 0, 'w': 48, 'h': 20, 'i': '1'}}])},
        'references': [{'name': 'panel_1', 'type': 'search', 'id': 'otus-logs'}]}, 'POST')
    print(json.dumps({'status': status['status']['overall'], 'data_view_id': view['data_view']['id'],
                      'dashboard_id': dashboard['id']}))


def health():
    print(json.dumps(wait_health()))


def kibana_check():
    auth = base64.b64encode(('elastic:' + S['elastic']).encode()).decode()
    results = {}
    for name, path in [('status', '/api/status'), ('data_view', '/api/data_views/data_view/otus-logs'),
                       ('dashboard', '/api/saved_objects/dashboard/otus-logs')]:
        request = urllib.request.Request('http://' + C['ip'] + ':' + str(C['kibana_port']) + path,
                                         headers={'Authorization': 'Basic ' + auth, 'kbn-xsrf': 'true'})
        with urllib.request.urlopen(request, timeout=15) as response:
            results[name] = json.load(response)
    if results['status']['status']['overall']['level'] != 'available':
        raise RuntimeError('Kibana is not available')
    print(json.dumps({'status': 'available', 'data_view': results['data_view']['data_view']['id'],
                      'dashboard': results['dashboard']['id']}))


def checkpoint():
    import secrets
    path = BASE / 'scaling-checkpoint.json'
    if path.exists():
        before = json.loads(path.read_text())
        if api('/')['cluster_uuid'] != before['cluster_uuid'] or api('/otus-scaling-check/_doc/probe')['_source']['nonce'] != before['nonce']:
            raise RuntimeError('Saved Elasticsearch checkpoint no longer matches live data')
        print(json.dumps({'checkpoint_reused': True, 'nodes_before': len(before['nodes'])}))
        return
    nonce = secrets.token_hex(24)
    api('/otus-scaling-check/_doc/probe?refresh=wait_for&wait_for_active_shards=all',
        {'nonce': nonce}, 'PUT')
    result = {'cluster_uuid': api('/')['cluster_uuid'],
              'nodes': {key: node['name'] for key, node in api('/_nodes')['nodes'].items()},
              'nonce': nonce}
    path.write_text(json.dumps(result))
    print(json.dumps({'checkpoint_written': True, 'nodes_before': len(result['nodes'])}))


def preservation():
    before = json.loads((BASE / 'scaling-checkpoint.json').read_text())
    current = api('/_nodes')['nodes']
    if api('/')['cluster_uuid'] != before['cluster_uuid']:
        raise RuntimeError('Cluster UUID changed')
    if any(key not in current or current[key]['name'] != name for key, name in before['nodes'].items()):
        raise RuntimeError('An existing Elasticsearch node was replaced')
    if {node['name'] for node in current.values()} != set(C['ips']):
        raise RuntimeError('Unexpected cluster membership')
    document = api('/otus-scaling-check/_doc/probe')
    if not document.get('found') or document['_source']['nonce'] != before['nonce']:
        raise RuntimeError('Committed document was lost')
    api('/otus-scaling-check/_doc/after?refresh=wait_for&wait_for_active_shards=all', {'nonce': before['nonce']}, 'PUT')
    if api('/otus-scaling-check/_doc/after')['_source']['nonce'] != before['nonce']:
        raise RuntimeError('Write/read after configuration failed')
    print(json.dumps({'cluster_uuid_preserved': True, 'existing_node_ids_preserved': True,
                      'committed_document_preserved': True, 'health': wait_health()}))


if __name__ == '__main__':
    C = json.loads((BASE / 'elk-settings.json').read_text())
    S = C['secrets']
    CTX = ssl.create_default_context(cafile=str(BASE / 'certs/ca.crt'))
    {'setup': setup, 'verify': verify, 'master': master, 'failure': failure,
     'recovery': recovery, 'kibana': kibana, 'health': health,
     'checkpoint': checkpoint, 'preservation': preservation, 'kibana-check': kibana_check}[sys.argv[1]]()
