"""Complete CLI orchestration with synthetic retained resources and mock APIs."""
from contextlib import ExitStack
import copy
import ipaddress
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from cloud_contract import InfrastructureResult
from pool_selection import TerraformError
import staged_runtime
import stack_config
import topology

ROOT = Path(__file__).resolve().parents[1]


class MemoryBackend:
    """A deterministic provider boundary, not a substitute for a cloud trial."""
    def prepare_tools(self):
        pass  # Provider installation is tested separately without cloud resources.
    def __init__(self, api):
        self.api = api
        self.desired = topology.load(ROOT)
        from run_settings import placement
        self.policy = placement(ROOT)
        self.journal = {'stages':{}, 'configured':False, 'admin_cidrs':[],
                        'pool_subnets':{'ru-9':'10.77.0.0/24','ru-3':'10.78.0.0/24'}}
        self.path = api.LOCAL/'staged-deployment.json'
        self.events, self.objects, self.attempts = [], {}, {}
        self.owner = 'otus-dz07-synthetic'
        self.choices = {role:[{'region':'ru-9','availability_zone':'ru-9a'},
                              {'region':'ru-3','availability_zone':'ru-3b'}] for role in ('app','es')}
        self.preflight = Mock()
        self.global_router = SimpleNamespace(assert_absent=Mock())
        self.cloud = Mock()
        self.cloud.get.return_value = {'floatingips':[]}

    def region(self, pool): return self.cloud
    def save(self): self.api.save(self.path, self.journal)
    def completed(self, name): return self.journal['stages'].get(name,{}).get('status') == 'active'
    def pending(self, name): return None
    def identities(self, exclude_pending=False):
        return {n:i for n,i in self.objects.items() if not exclude_pending or self.completed(n)}
    def assert_preserved(self, before):
        if any(self.objects.get(n) != i for n,i in before.items()): raise RuntimeError('Identity changed')
    def begin(self, name, candidate):
        self.journal['stages'][name] = {'candidate':candidate,'status':'pending'}
    def apply(self, name):
        self.events.append(('apply',name,self.journal['stages'][name]['candidate']['region']))
        self.attempts[name] = self.attempts.get(name,0)+1
        self.objects[name] = name+'-id-'+str(self.attempts[name])
        if name == 'es2' and self.attempts[name] == 1: raise TerraformError('No valid host was found')
    def capture_fault(self, name): self.events.append(('fault',name))
    def is_capacity_error(self, error, name): return isinstance(error, TerraformError)
    def cleanup_attempt(self, name):
        self.events.append(('cleanup',name))
        self.objects.pop(name,None)
        self.journal['stages'][name]['status'] = 'deleted'
    def finish(self, name): self.journal['stages'][name]['status'] = 'active'
    def reapply(self, name): self.events.append(('repeat',name))
    def validate_existing(self):
        from selectel_staged import Backend
        return Backend.validate_existing(self)
    def recover_attempt(self, name):
        from selectel_staged import Backend
        return Backend.recover_attempt(self, name)
    def deployment_order(self):
        from selectel_staged import Backend
        return Backend.deployment_order(self)
    def candidates_for(self, name):
        from selectel_staged import Backend
        return Backend.candidates_for(self, name)
    def cleanup_order(self):
        from selectel_staged import Backend
        return Backend.cleanup_order(self)
    def audit_deletion(self):
        from selectel_staged import Backend
        return Backend.audit_deletion(self)
    def result(self):
        nodes = {}
        for name,v in self.desired['nodes'].items():
            candidate = self.journal['stages'][name]['candidate']
            pool, zone = candidate['region'], candidate['availability_zone']
            cidr = self.journal['pool_subnets'][pool]
            nodes[name] = {'id':self.objects[name],'volume_id':name+'-disk','port_id':name+'-port',
                'roles':[v['role']],'private_ip':str(ipaddress.ip_network(cidr)[v['host']]),
                'public_ip':'192.0.2.1' if name=='app' else None, 'region':pool,'zone':zone,'subnet_cidr':cidr}
        placement = {'region':nodes['app']['region'],'zone':nodes['app']['zone'],
                     'pool_subnets':self.journal['pool_subnets'],
                     'node_placements':{n:{'region':v['region'],'zone':v['zone']} for n,v in nodes.items()}}
        return InfrastructureResult('selectel',{'project_id':'synthetic-project'},'otus-dz07',placement,nodes,nodes['app']['subnet_cidr'])


class RetainedLifecycleTest(unittest.TestCase):
    def fixture(self, root):
        local = root/'.local'
        local.mkdir()
        (root/'config').mkdir()
        (root/'config/services.json').write_text((ROOT/'config/services.json').read_text())
        api = SimpleNamespace(ROOT=root,LOCAL=local,PREFIX='otus-dz07',STACK=stack_config.load(),
                              _TEST_CYCLE_ID=None,prepare_identity=Mock(),configure=Mock())
        def save(path,value):
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text(json.dumps(value))
        api.save = save
        provider = MemoryBackend(api)
        api.lab_output = lambda: provider.result().application()
        return api, provider

    def patches(self, provider):
        stack = ExitStack()
        stack.enter_context(patch('staged_runtime.Backend',return_value=provider))
        stack.enter_context(patch('operator_network.discover_cidrs',return_value=['8.8.8.8/32']))
        stack.enter_context(patch('delivery.prepare'))
        stack.enter_context(patch('selectel_staged_audit.assert_stage_owned'))
        return stack

    def test_failed_es_attempt_keeps_app_and_other_nodes_then_repeat_preserves_all_ids(self):
        with tempfile.TemporaryDirectory() as folder:
            api, provider = self.fixture(Path(folder))
            with self.patches(provider), patch('verification.snapshot',return_value={'canary':'saved'}) as snapshot, patch('verification.expansion') as preservation:
                staged_runtime.deploy(api)
                first = dict(provider.objects)
                self.assertEqual(len(first),4)
                self.assertEqual(provider.journal['stages']['es2']['candidate']['region'],'ru-3')
                self.assertEqual([e for e in provider.events if e[0]=='cleanup'], [('cleanup','es2')])
                staged_runtime.deploy(api)
                self.assertEqual(provider.objects,first)
                self.assertEqual(sum(e[0]=='apply' for e in provider.events),5)
                self.assertEqual(sum(e[0]=='repeat' for e in provider.events),4)
                snapshot.assert_called_once()
                preservation.assert_called_once_with(api,{'canary':'saved'})
                self.assertEqual([c.kwargs['existing'] for c in api.configure.call_args_list],[False,True])

    def test_configuration_error_does_not_destroy_created_nodes(self):
        with tempfile.TemporaryDirectory() as folder:
            api, provider = self.fixture(Path(folder))
            api.configure.side_effect = RuntimeError('configuration failed')
            with self.patches(provider), self.assertRaisesRegex(RuntimeError,'configuration failed'):
                staged_runtime.deploy(api)
            self.assertEqual(len(provider.objects),4)
            self.assertTrue(provider.path.exists())
            self.assertFalse(provider.journal['configured'])

    def test_destroy_audits_api_before_local_cleanup_and_preserves_files_on_api_failure(self):
        for fails in (False,True):
            with tempfile.TemporaryDirectory() as folder:
                api, provider = self.fixture(Path(folder))
                with self.patches(provider): staged_runtime.deploy(api)
                if fails: provider.global_router.assert_absent.side_effect = RuntimeError('audit failed')
                with self.patches(provider), patch('selectel_staged_audit.assert_stage_absent') as audit, patch('selectel_adapter.audit_cleanup',return_value=([],0)), patch('selectel_staged_audit.audit_namespace'), patch('artifact_lifecycle.release') as release, patch('local_runtime.cleanup_runtime') as local:
                    if fails:
                        with self.assertRaisesRegex(RuntimeError,'audit failed'): staged_runtime.cleanup(api)
                        local.assert_not_called()
                        release.assert_not_called()
                        self.assertTrue(provider.path.exists())
                    else:
                        staged_runtime.cleanup(api)
                        self.assertEqual(audit.call_count,4)
                        local.assert_called_once_with(api.ROOT)
                        release.assert_called_once_with(api.ROOT)
                        self.assertTrue(provider.journal['cleanup_verified'])
                    self.assertFalse(provider.objects)
