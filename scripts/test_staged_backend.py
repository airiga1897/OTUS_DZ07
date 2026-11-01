"""Synthetic API catalogs and quota budgets for the Selectel staged adapter."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error

from selectel_staged import Backend
from run_settings import placement as placement_settings
from cloud_contract import Capability, CapabilityStatus
from selectel_staged_capacity import pending_credits, require_capacity
from pool_selection import CandidateUnavailable

ROOT = Path(__file__).resolve().parents[1]


class QuotaCloud:
    def __init__(self, pool, instances=2):
        self.pool = pool
        zone = pool + ('b' if pool == 'ru-3' else 'a')
        self.zone = zone
        self.compute = {'az_quotas':{zone:{'instances':self.q(instances),'cores':self.q(instances*4),'ram':self.q(instances*8192)}},
                        'server_groups':self.q(5),'server_group_members':self.q(10)}
        self.volumes = {'gigabytes':self.q(1000),'volumes':self.q(20),'gigabytes_universal.'+zone:self.q(1000)}
        self.network = {k:self.q(100) for k in ('network','subnet','router','security_group','security_group_rule','port','floatingip')}

    @staticmethod
    def q(limit): return {'limit':limit,'in_use':0,'reserved':0}

    def get(self, service, path):
        if service=='compute' and '/os-quota-sets/' in path: return {'quota_set':self.compute}
        if service=='volumev3': return {'quota_set':self.volumes}
        if service=='network' and '/quotas/' in path: return {'quota':self.network}
        if path=='/v2.0/subnets': return {'subnets':[]}
        if path.startswith('/flavors/'): return {'flavor':{'availability_zones':[self.zone]}}
        raise AssertionError((service,path))


class StagedBackendTests(unittest.TestCase):
    def catalog(self, regions):
        return [{'type':service,'endpoints':[{'region':r,'interface':'public','url':'https://example.invalid/'+service}
                 for r in regions]} for service in ('compute','image','volumev3','network')]

    def test_auto_discovery_tries_a_nonpreferred_group_that_can_place_the_whole_stand(self):
        from selectel_global_router import GlobalRouter
        with tempfile.TemporaryDirectory() as folder:
            provider, clouds=self.backend(Path(folder))
            clouds['ru-8']=QuotaCloud('ru-8',instances=4)
            provider.policy['pools']=['auto']
            provider.cloud=SimpleNamespace(catalog=self.catalog(['ru-9','ru-8']))
            global_api=GlobalRouter.__new__(GlobalRouter)
            payload={'zones':[{'name':r,'service':'vpc','enable':True,'groups':[{'id':r}]} for r in ('ru-9','ru-8')],
                     'routers':[],'networks':[],'subnets':[],
                     'quotas':[{'id':k,'name':k,'scope':'account_id','limit':10} for k in ('routers','networks','subnets')]}
            global_api.listing=Mock(side_effect=lambda kind:payload[kind]);provider.global_router=global_api
            def choices(config,credentials):
                role=next(iter(config['profiles']));result=[]
                for region in config['pools']:
                    item=self.choice(region,role)
                    if region=='ru-8':
                        item.update(availability_zone='ru-8a',network_availability_zone='ru-8a',router_availability_zone='ru-8a',volume_type='universal.ru-8a')
                    result.append(item)
                return result
            with patch('selectel_staged.pools.candidates',side_effect=choices),patch('selectel_staged.discover_capabilities',return_value={'ownership_tags':Capability(CapabilityStatus.SUPPORTED,'synthetic')}),patch('selectel_staged.reject_cloud_resources'),patch('selectel_staged_audit.audit_namespace'):
                provider.preflight()
            self.assertEqual(provider.policy['pools'],['ru-8'])
            self.assertEqual(set(provider.journal['pool_subnets']),{'ru-8'})
            self.assertTrue(all(c['region']=='ru-8' for choices in provider.choices.values() for c in choices))

    def test_saved_pool_universe_does_not_expand_when_catalog_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            provider,_=self.backend(Path(folder));provider.policy['pools']=['auto']
            provider.path.parent.mkdir();provider.path.write_text('{}')
            provider.cloud=SimpleNamespace(catalog=self.catalog(['ru-9','ru-3','ru-8']))
            before=copy.deepcopy(provider.journal['pool_subnets'])
            with patch('selectel_staged.discover_capabilities',return_value={'ownership_tags':Capability(CapabilityStatus.SUPPORTED,'synthetic')}):
                provider.resolve_pools()
            self.assertEqual(provider.policy['pools'],['ru-9','ru-3'])
            self.assertEqual(provider.journal['pool_subnets'],before)

    def test_unknown_auto_pool_capability_aborts_instead_of_enabling_it(self):
        with tempfile.TemporaryDirectory() as folder:
            provider,_=self.backend(Path(folder));provider.policy['pools']=['auto']
            provider.cloud=SimpleNamespace(catalog=self.catalog(['ru-9']))
            with patch('selectel_staged.discover_capabilities',return_value={'ownership_tags':Capability(CapabilityStatus.UNKNOWN,'API failure')}):
                with self.assertRaises(ValueError):provider.resolve_pools()
            provider.api.save.assert_not_called()

    def backend(self, root):
        provider = Backend.__new__(Backend)
        provider.root, provider.path = root, root/'.local/staged-deployment.json'
        (root/'config').mkdir()
        (root/'config/cloud.json').write_text('{"required_capabilities":[]}')
        provider.policy = placement_settings(ROOT)
        provider.policy['pools'] = ['ru-9','ru-3']
        provider.desired = json.loads((ROOT/'config/topology.json').read_text())
        provider.credentials = {'project_id':'synthetic','region':'ru-9'}
        provider.journal = {'stages':{},'admin_cidrs':['8.8.8.8/32'],
                            'pool_subnets':provider.policy['sequential_placement']['pool_subnets']}
        provider.api = SimpleNamespace(PREFIX='otus-dz07',save=Mock())
        provider.store = SimpleNamespace(entries=Mock(return_value=[]))
        clouds = {pool:QuotaCloud(pool) for pool in ('ru-9','ru-3')}
        provider.region = Mock(side_effect=lambda pool:clouds[pool])
        provider.global_router = SimpleNamespace(preflight=Mock())
        provider.choices = {role:[self.choice(pool,role) for pool in ('ru-9','ru-3')] for role in ('app','es')}
        return provider, clouds

    def choice(self, pool, role):
        zone = 'ru-9a' if pool=='ru-9' else 'ru-3b'
        return {'region':pool,'availability_zone':zone,'volume_type':'universal.'+zone,
                'image_id':'verified-image','flavor_ids':{role:role+'-flavor'},
                'network_availability_zone':'ru-9a' if pool=='ru-9' else 'ru-3a',
                'router_availability_zone':'ru-9a' if pool=='ru-9' else 'ru-3a'}

    def test_capacity_can_be_satisfied_by_a_combination_of_pools(self):
        with tempfile.TemporaryDirectory() as folder:
            provider, clouds = self.backend(Path(folder))
            require_capacity(provider)
            clouds['ru-3'].compute['az_quotas']['ru-3b']['instances']['limit'] = 1
            with self.assertRaises(CandidateUnavailable): require_capacity(provider)

    def test_unknown_quotas_and_network_capacity_do_not_pass(self):
        for kind in ('compute','volume','network'):
            with tempfile.TemporaryDirectory() as folder:
                provider, clouds = self.backend(Path(folder))
                for cloud in clouds.values():
                    if kind=='compute': cloud.compute['az_quotas'][cloud.zone].pop('ram')
                    if kind=='volume': cloud.volumes.pop('gigabytes')
                    if kind=='network': cloud.network['port']['limit'] = 0
                with self.assertRaises(CandidateUnavailable): require_capacity(provider)

    def test_active_nodes_need_no_new_quota(self):
        with tempfile.TemporaryDirectory() as folder:
            provider, clouds = self.backend(Path(folder))
            provider.journal['stages'] = {n:{'status':'active'} for n in provider.desired['nodes']}
            provider.region.side_effect = AssertionError('No allocation quota needed')
            require_capacity(provider)

    def test_pending_owned_resources_are_credited_in_their_own_pool_and_zone(self):
        with tempfile.TemporaryDirectory() as folder:
            provider, clouds = self.backend(Path(folder))
            provider.journal['stages']['es2'] = {'status':'pending','candidate':self.choice('ru-3','es')}
            provider.store.entries.return_value = [
                {'type':'openstack_compute_instance_v2','attrs':{'id':'partial-vm'}},
                {'type':'openstack_blockstorage_volume_v3','attrs':{'id':'partial-volume','size':20}}]
            credits = pending_credits(provider,'ru-3')
            self.assertEqual(credits[('ru-3b','ram')],8192)
            self.assertEqual(credits['gigabytes'],20)
            self.assertEqual(pending_credits(provider,'ru-9'),{})

    def test_role_catalogs_are_independent_and_flavor_zone_binding_is_checked(self):
        with tempfile.TemporaryDirectory() as folder:
            provider, clouds = self.backend(Path(folder))
            clouds['ru-3'] = QuotaCloud('ru-3',instances=3)
            def choices(config,credentials):
                role = next(iter(config['profiles']))
                return [self.choice('ru-9',role)] if role=='app' else [self.choice('ru-3',role)]
            with patch('selectel_staged.pools.candidates',side_effect=choices), patch('selectel_staged.discover_capabilities',return_value={'ownership_tags':Capability(CapabilityStatus.SUPPORTED,'synthetic')}), patch('selectel_staged.reject_cloud_resources'), patch('selectel_staged_audit.audit_namespace'):
                provider.preflight()
            self.assertEqual(provider.choices['app'][0]['region'],'ru-9')
            self.assertEqual(provider.choices['es'][0]['region'],'ru-3')
            self.assertEqual(provider.global_router.preflight.call_count,1)

    def test_api_failure_is_not_silently_treated_as_an_empty_catalog(self):
        with tempfile.TemporaryDirectory() as folder:
            provider, clouds = self.backend(Path(folder))
            error = urllib.error.HTTPError('https://example.invalid',403,'Forbidden',{},None)
            provider.region.side_effect = error
            with self.assertRaises(urllib.error.HTTPError): provider.preflight()
            provider.api.save.assert_not_called()

    def test_partial_stage_id_is_recovered_from_state_when_outputs_were_not_written(self):
        with tempfile.TemporaryDirectory() as folder:
            provider, clouds = self.backend(Path(folder))
            provider.store.entries.return_value = [{'type':'selectel_global_router_router_v1','attrs':{'id':'retained-router'}}]
            self.assertEqual(provider.saved_id('global','selectel_global_router_router_v1','router_id'),'retained-router')
