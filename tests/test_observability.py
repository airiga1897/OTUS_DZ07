import copy
import json
from pathlib import Path
import sys
import unittest
import tempfile
from unittest.mock import patch, MagicMock
from jinja2 import Environment, StrictUndefined
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import topology
from selectel_adapter import infrastructure_result
from render import ansible_inventory
from elk_tls import certificates


class ObservabilityTopologyTest(unittest.TestCase):
    def nodes(self):
        initial = json.loads((ROOT / 'config/topology.json').read_text())
        return initial

    def test_distinct_roles_and_additive_elasticsearch_extension(self):
        initial = self.nodes()
        topology.validate(initial)
        extended = copy.deepcopy(initial)
        extended['nodes']['es4'] = {'host': 34, 'role': 'es'}
        self.assertEqual(topology.validate_extension(initial, extended), {'es4'})
        extended = copy.deepcopy(initial)
        self.assertEqual(topology.validate_extension(extended, extended), set())
        lab = {'private_ips': {n: '10.77.0.' + str(v['host']) for n, v in initial['nodes'].items()}}
        self.assertEqual(topology.database_names(lab), ['app'])

    def test_configuration_keeps_workload_and_observability_separate(self):
        value = json.loads((ROOT / 'config/topology.json').read_text())
        roles = {n: v['role'] for n, v in value['nodes'].items()}
        self.assertEqual(sum(r == 'es' for r in roles.values()), 3)
        self.assertNotIn('kibana', roles)
        self.assertEqual(len(roles), 4)
        self.assertEqual(sum(r == 'db' for r in roles.values()), 0)
        self.assertEqual(roles['app'], 'app')

    def inventory(self, expanded=False):
        desired = self.nodes()
        if expanded:
            desired['nodes']['es4'] = {'host': 34, 'role': 'es'}
        lab = {'private_ips': {n: '10.77.0.' + str(v['host']) for n, v in desired['nodes'].items()},
               'etcd_members': desired['etcd_members'], 'app_public_ip': '192.0.2.1', 'subnet_cidr': '10.77.0.0/24',
               **{k: {n: k + n for n in desired['nodes']} for k in ('server_ids', 'volume_ids', 'port_ids')}}
        result = infrastructure_result(lab, {'project_id': 'synthetic', 'region': 'ru-9'}, 'otus-dz07', 'ru-9a')
        passwords = dict.fromkeys(('postgres', 'replicator', 'api', 'django', 'app', 'elastic', 'shipper', 'kibana', 'kibana_encryption'), 'synthetic')
        return ansible_inventory(result, passwords)['all']

    def test_inventory_distinguishes_roles_and_limits_secrets(self):
        children = self.inventory()['children']
        self.assertEqual(set(children['database']['hosts']), {'app'})
        self.assertEqual(set(children['elasticsearch']['hosts']), {'es1', 'es2', 'es3'})
        self.assertEqual(set(children['kibana']['hosts']), {'app'})
        self.assertNotIn('patroni_config', children['database']['hosts']['app'])
        self.assertEqual(set(children['frontend']['hosts']['app']['dz07']['log_hosts']), set(self.nodes()['nodes']))

    def test_log_verification_requires_each_service_on_its_own_node(self):
        from remote import elk_api
        settings = self.inventory()['children']['frontend']['hosts']['app']['dz07']
        with patch.object(elk_api, 'C', settings), patch.object(elk_api, 'api', return_value={'count': 1}) as call:
            counts = elk_api.source_counts()
        self.assertTrue(all(counts.values()))
        for source in ('app/nginx', 'app/haproxy', 'app/postgresql', 'app/django', 'app/kibana', 'es1/elasticsearch', 'app/system'):
            self.assertIn(source, counts)
        query = call.call_args_list[0].args[1]['query']['bool']['filter']
        self.assertEqual(len(query), 2)

    def test_normal_verification_never_changes_vm_power(self):
        import verification
        api = MagicMock(ROOT=ROOT, STACK={'paths': {'remote_root': '/synthetic'}})
        api.lab_output.return_value = {'private_ips': {'app': '10.77.0.10'}}
        api.remote.return_value = json.dumps({'leader': 'db1', 'success': True})
        with patch('observability.verify', return_value={'success': True}):
            result = verification.verify(api, False)
        self.assertTrue(result['logs']['success'])
        api.Cloud.assert_not_called()
        self.assertEqual(api.remote.call_args.args[2], 'verify-baseline')

    def test_extension_keeps_database_config_and_elastic_seeds_stable(self):
        before, after = self.inventory()['children'], self.inventory(True)['children']
        self.assertEqual(before['database'], after['database'])
        for name, host in before['elasticsearch']['hosts'].items():
            self.assertEqual(host['dz07']['seed_hosts'], after['elasticsearch']['hosts'][name]['dz07']['seed_hosts'])
        self.assertEqual(after['elasticsearch']['hosts']['es4']['dz07']['roles'], ['data', 'ingest'])

    def test_native_elastic_kibana_filebeat_templates_render_valid_yaml(self):
        inventory = self.inventory()
        env = Environment(undefined=StrictUndefined)
        env.filters['to_json'] = json.dumps
        for role, group, name, filename in [('elasticsearch', 'elasticsearch', 'es1', 'elasticsearch.yml'),
                                            ('filebeat', 'frontend', 'app', 'filebeat.yml'),
                                            ('kibana', 'frontend', 'app', 'kibana.yml')]:
            variables = {**inventory['vars'], **inventory['children'][group]['hosts'][name],
                         'inventory_hostname': name, 'elastic_formed': {'stat': {'exists': False}}}
            text = env.from_string((ROOT / f'ansible/roles/{role}/templates/{filename}.j2').read_text()).render(**variables)
            config = yaml.safe_load(text)
            if role == 'kibana':
                self.assertEqual(config['elasticsearch.hosts'], ['https://10.77.0.31:9200', 'https://10.77.0.32:9200', 'https://10.77.0.33:9200'])
            if role == 'filebeat':
                self.assertEqual(config['output.elasticsearch']['ssl.certificate_authorities'], ['/etc/filebeat/ca.crt'])
                self.assertTrue(any(i.get('fields', {}).get('service', {}).get('name') == 'postgresql' for i in config['filebeat.inputs']))

    def test_extension_preserves_ca_and_existing_node_certificates(self):
        with tempfile.TemporaryDirectory(dir=ROOT / '.local') as directory:
            local = Path(directory)
            addresses = {'es1': '10.77.0.31', 'es2': '10.77.0.32', 'es3': '10.77.0.33'}
            certificates(local, addresses)
            before = {p.name: p.read_bytes() for p in (local / 'certs').iterdir()}
            certificates(local, {**addresses, 'es4': '10.77.0.34'})
            self.assertTrue((local / 'certs/es4.crt').exists())
            for name, content in before.items():
                self.assertEqual((local / 'certs' / name).read_bytes(), content)
            with self.assertRaisesRegex(RuntimeError, 'identity changed'):
                certificates(local, {'es1': '10.77.0.99'})
