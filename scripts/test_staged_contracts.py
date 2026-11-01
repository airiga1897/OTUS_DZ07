"""Offline multi-pool plan, local state and API-discovery contracts."""
from dataclasses import asdict
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error

from cloud_contract import InfrastructureResult
from selectel_staged import validate_policy
from stage_store import StageStore, check_plan, operation
from staged_runtime import open_portal, output
from selectel_global_router import GlobalRouter

ROOT = Path(__file__).resolve().parents[1]


class StagePlanTests(unittest.TestCase):
    def change(self, actions, before_id='one'):
        return {'resource_changes': [{'address': 'module.stage.resource.one', 'mode': 'managed',
                 'change': {'actions': actions, 'before': {'id': before_id}, 'after': {'id': 'one'}}}]}

    def test_update_replace_delete_and_identity_changes_are_blocked(self):
        for actions in (['update'], ['delete', 'create'], ['create', 'delete'], ['delete']):
            with self.subTest(actions=actions), self.assertRaises(ValueError):
                check_plan(self.change(actions), {'module.stage.resource.one': 'one'})
        with self.assertRaises(ValueError):
            check_plan(self.change(['no-op'], 'foreign'), {'module.stage.resource.one': 'one'})

    def test_destroy_only_allows_ids_of_its_own_state(self):
        check_plan(self.change(['delete']), {'module.stage.resource.one': 'one'}, destroy=True)
        with self.assertRaises(ValueError):
            check_plan(self.change(['delete']), {}, destroy=True)
        with self.assertRaises(ValueError):
            check_plan({'resource_drift': [{'address': 'one', 'change': {'actions': ['delete']}}]}, {})

    def test_growth_cannot_delete_previous_stage_objects(self):
        check_plan(self.change(['no-op']), {'module.stage.resource.one': 'one'}, additions_only=True)
        check_plan(self.change(['create']), {}, additions_only=True)


class LocalDistributedResultTests(unittest.TestCase):
    def fixture(self, root):
        local = root / '.local'
        local.mkdir()
        (root / 'config').mkdir()
        (root / 'config/services.json').write_text('{"kibana_port":5601}')
        nodes = {}
        stages = {}
        for name, offset in [('app',10),('es1',31),('es2',32),('es3',33)]:
            pool, zone, cidr, prefix = ('ru-9','ru-9a','10.77.0.0/24','10.77.0.') if name == 'app' else ('ru-3','ru-3b','10.78.0.0/24','10.78.0.')
            node = {'id': name+'-vm', 'volume_id': name+'-disk', 'port_id': name+'-port',
                    'private_ip': prefix+str(offset), 'public_ip':'192.0.2.1' if name == 'app' else None,
                    'roles':['app' if name=='app' else 'es'], 'region':pool, 'zone':zone, 'subnet_cidr':cidr}
            nodes[name] = node
            stages[name] = {'status':'active', 'candidate':{'region':pool}, 'output':node}
            directory = local / 'stages' / name
            directory.mkdir(parents=True)
            entries = []
            for kind, field in [('compute_instance_v2','id'),('blockstorage_volume_v3','volume_id'),('networking_port_v2','port_id')]:
                entries.append({'mode':'managed','module':'module.stage','type':'openstack_'+kind,'name':'one',
                                'instances':[{'attributes':{'id':node[field], 'region':pool, 'tenant_id':'synthetic-project'}}]})
            (directory/'terraform.tfstate').write_text(json.dumps({'lineage':name,'resources':entries}))
        placement = {'region':'ru-9','zone':'ru-9a', 'pool_subnets':{'ru-9':'10.77.0.0/24','ru-3':'10.78.0.0/24'},
                     'node_placements':{n:{'region':v['region'],'zone':v['zone']} for n,v in nodes.items()}}
        result = InfrastructureResult('selectel', {'project_id':'synthetic-project'}, 'otus-dz07', placement, nodes, '10.77.0.0/24')
        (local/'infrastructure.json').write_text(json.dumps(asdict(result)))
        journal = {'provider':'selectel','scope':{'project_id':'synthetic-project'},'project_id':'synthetic-project','namespace':'otus-dz07','stages':stages}
        (local/'staged-deployment.json').write_text(json.dumps(journal))
        return SimpleNamespace(ROOT=root, LOCAL=local), result

    def test_local_open_requires_no_credentials_and_preserves_cross_pool_addresses(self):
        with tempfile.TemporaryDirectory() as folder:
            api, result = self.fixture(Path(folder))
            result.validate()
            self.assertEqual(output(api).application()['node_regions']['es2'], 'ru-3')
            opener = Mock(return_value=True)
            with patch('selectel_staged.load_credentials', side_effect=AssertionError('no auth')):
                self.assertEqual(open_portal(api, opener), 'http://192.0.2.1')
            opener.assert_called_once_with('http://192.0.2.1')

    def test_stale_disk_or_foreign_state_is_not_a_working_stand(self):
        for mutation in ('id','project','pool'):
            with tempfile.TemporaryDirectory() as folder:
                api, result = self.fixture(Path(folder))
                path = api.LOCAL/'stages/es2/terraform.tfstate'
                value = json.loads(path.read_text())
                attrs = value['resources'][1]['instances'][0]['attributes']
                attrs[{'id':'id','project':'tenant_id','pool':'region'}[mutation]] = 'foreign'
                path.write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    output(api)

    def test_wrong_subnet_is_rejected_by_the_common_contract(self):
        with tempfile.TemporaryDirectory() as folder:
            api, result = self.fixture(Path(folder))
            result.nodes['es1']['private_ip'] = '10.79.0.31'
            with self.assertRaises(ValueError):
                result.validate()

    def test_pool_subnet_overlap_and_ru7_are_rejected_before_creation(self):
        from run_settings import placement
        policy = placement(ROOT)
        desired = json.loads((ROOT/'config/topology.json').read_text())
        validate_policy(policy, desired)
        for target in ('overlap','excluded','order'):
            altered = copy.deepcopy(policy)
            if target == 'overlap':
                altered['sequential_placement']['pool_subnets']['ru-3'] = '10.77.0.0/24'
            elif target == 'excluded':
                altered['pools'].append('ru-7')
            else:
                altered['sequential_placement']['node_order'].append('app')
            with self.assertRaises(ValueError):
                validate_policy(altered, desired)

    def test_stage_paths_cannot_escape_temporary_clone(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = StageStore(SimpleNamespace(ROOT=root), {'stages':{}})
            with self.assertRaises(ValueError):
                store.folder('../other')


class GlobalRouterReadTests(unittest.TestCase):
    def client(self):
        client = GlobalRouter.__new__(GlobalRouter)
        client.credentials = {'domain_name':'synthetic-account'}
        client.token = 'synthetic'
        return client

    def test_account_scoped_auth_uses_the_single_environment_schema(self):
        credentials = dict(username='synthetic-user', password='synthetic-secret', domain_name='synthetic-account')
        with patch('selectel_global_router.request_json', return_value=({'token':{'domain':{'name':'synthetic-account'}}}, {'X-Subject-Token':'synthetic'})) as request:
            GlobalRouter(credentials).authenticate()
        body = request.call_args.kwargs['payload']['auth']
        self.assertEqual(body['scope'], {'domain':{'name':'synthetic-account'}})
        self.assertNotIn('project', body['scope'])

    def test_permission_errors_and_unknown_shapes_do_not_mean_unsupported_or_empty(self):
        client = self.client()
        with patch.object(client, 'get', return_value={'unknown':[]}):
            with self.assertRaises(ValueError): client.listing('routers')
        error = urllib.error.HTTPError('https://example.invalid', 403, 'Forbidden', {}, None)
        with patch.object(client, 'get', side_effect=error):
            with self.assertRaises(urllib.error.HTTPError): client.listing('routers')

    def test_unsupported_zone_and_quota_block_before_allocation(self):
        client = self.client()
        zones = [{'name':'ru-9','service':'vpc','enable':True,'groups':[{'id':'same-country'}]}]
        with patch.object(client,'listing', side_effect=lambda kind: zones if kind=='zones' else []):
            with self.assertRaises(ValueError): client.preflight(['ru-9','ru-3'], 'otus-dz07')
            with self.assertRaises(ValueError): client.preflight(['ru-9'], 'otus-dz07')

    def test_cross_pool_zone_groups_and_attachment_quotas_must_be_confirmed(self):
        client = self.client()
        zones = [{'name':pool,'service':'vpc','enable':True,'groups':[{'id':'ru-group'}]} for pool in ('ru-9','ru-3')]
        quotas = [{'name':name,'scope':'account_id','limit':10} for name in ('routers','networks','subnets')]
        data = {'zones':zones,'quotas':quotas,'routers':[],'networks':[],'subnets':[]}
        with patch.object(client,'listing',side_effect=lambda kind:data[kind]):
            client.preflight(['ru-9','ru-3'],'otus-dz07')
            zones[1]['groups'] = [{'id':'different-country'}]
            with self.assertRaises(ValueError): client.preflight(['ru-9','ru-3'],'otus-dz07')
            zones[1]['groups'] = [{'id':'ru-group'}]
            quotas.pop()
            with self.assertRaises(ValueError): client.preflight(['ru-9','ru-3'],'otus-dz07')

    def test_cleanup_audit_detects_saved_ids_and_orphans_with_own_tag(self):
        client = self.client()
        with patch.object(client, 'listing', return_value=[{'id':'untracked','tags':['own-run']}]):
            with self.assertRaises(RuntimeError): client.assert_absent({}, 'own-run')
        with patch.object(client, 'listing', return_value=[{'id':'foreign','tags':['foreign-run']}]):
            client.assert_absent({}, 'own-run')
