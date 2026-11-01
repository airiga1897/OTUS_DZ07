"""Retained-stage recovery must preserve scope, identities and original failures."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import pool_selection as pools
from selectel_staged import Backend, PlacementExhausted, validate_policy
import test_selectel_retained_contracts as fixtures


class BackendRecoveryContracts(unittest.TestCase):
    def fixture(self):
        helper=fixtures.RetainedSelectelContracts();helper.setUp();self.addCleanup(helper.doCleanups)
        return helper.backend

    def test_policy_rejects_invalid_scope_subnets_and_all_reservation_collisions(self):
        b=self.fixture()
        for change in ('boolean','excluded','pool-map','public','ipv6','prefix','overlap','service-count','duplicate','type','range','dhcp','node'):
            policy=copy.deepcopy(b.policy);config=policy['sequential_placement'];slots=config['addresses']
            if change=='boolean':policy['require_distinct_hosts']='false'
            if change=='excluded':policy['pools']=['ru-7']
            if change=='pool-map':policy['pools']=['ru-9']
            if change=='public':config['pool_subnets']['ru-9']='8.8.8.0/24'
            if change=='ipv6':config['pool_subnets']['ru-9']='fd00::/64'
            if change=='prefix':config['pool_subnets']['ru-9']='10.77.0.0/25'
            if change=='overlap':config['pool_subnets']['ru-3']=config['pool_subnets']['ru-9']
            if change=='service-count':slots['service_hosts']=[]
            if change=='duplicate':slots['global_gateway']=slots['cloud_gateway']
            if change=='type':slots['cloud_gateway']=True
            if change=='range':slots['dhcp_first']=255
            if change=='dhcp':slots['dhcp_first']=1
            if change=='node':slots['cloud_gateway']=10
            with self.subTest(change=change),self.assertRaises(ValueError):validate_policy(policy,b.desired)

    def test_context_cannot_readdress_retained_pools_and_tools_use_prepared_cli(self):
        b=self.fixture()
        original=copy.deepcopy(b.journal)
        for change in ('deployment-id','reserved-address','subnet','excluded'):
            b.journal=copy.deepcopy(original);b.policy=copy.deepcopy(b.policy)
            if change=='deployment-id':b.journal['deployment_id']='bad'
            if change=='reserved-address':b.journal['address_policy']={}
            if change=='subnet':b.journal['pool_subnets']={**b.journal['pool_subnets'],'ru-9':'10.99.0.0/24'}
            if change=='excluded':b.journal['pool_subnets']={**b.journal['pool_subnets'],'ru-7':'10.79.0.0/24'}
            with self.subTest(change=change),self.assertRaises(ValueError):b.validate_journal()
        b.journal=original
        prepared=b.root/'.tools/synthetic.tfrc'
        with patch('terraform_plugins.prepare',return_value=prepared):b.prepare_tools()
        self.assertEqual(b.store.cli_config,prepared)

    def test_network_scheduling_exhaustion_never_discards_a_remaining_identity(self):
        b=self.fixture();b.store=Mock();b.store.identities.return_value={}
        candidate={'region':'ru-9','network_availability_zone':'ru-9a','router_availability_zone':'ru-9a'}
        b.choices={'app':[candidate,{**candidate,'network_availability_zone':'ru-9b'}]}
        b.journal['stages']['network-ru-9']={'status':'pending'}
        with patch('selectel_staged_capacity.require_network_capacity'),patch('selectel_staged.place',side_effect=PlacementExhausted('synthetic')):
            with self.assertRaises(pools.CandidateUnavailable):b.ensure_pool(candidate)
            b.store.identities.return_value={'owned':'id'}
            with self.assertRaises(PlacementExhausted):b.ensure_pool(candidate)
        self.assertEqual(b.journal['stages']['network-ru-9']['status'],'pending')

    def test_network_choice_domains_are_deduplicated_and_retained(self):
        b=self.fixture();b.store=Mock();b.store.identities.return_value={}
        candidate={'region':'ru-9','network_availability_zone':'ru-9a','router_availability_zone':'ru-9a'}
        b.choices={'app':[candidate,dict(candidate),{**candidate,'network_availability_zone':'ru-9b'},
                          {**candidate,'region':'ru-3'}]}
        def create(backend,name,choices):
            backend.journal['stages'][name]={'status':'active','output':{'network_id':'owned'}}
        with patch('selectel_staged_capacity.require_network_capacity') as quota,patch('selectel_staged.place',side_effect=create) as place:
            self.assertEqual(b.ensure_pool(candidate),{'network_id':'owned'})
            self.assertEqual(len(place.call_args.args[2]),2)
            self.assertIn('network-ru-9',b._temporary_networks)
            quota.reset_mock();b.ensure_pool(candidate);quota.assert_not_called()

    def test_missing_stage_configuration_preserves_state_and_recovery_key(self):
        b=self.fixture();folder=b.root/'.local/stages/app';folder.mkdir(parents=True)
        key=b.root/'.local/key';key.write_text('synthetic')
        b.journal['stages']['app']={'status':'pending','candidate':{'region':'ru-9'}}
        b.store=Mock();b.store.folder.return_value=folder;b.store.identities.return_value={'owned':'vm'};b.store.entries.return_value=[]
        with patch('selectel_staged_audit.assert_stage_owned'):
            with self.assertRaisesRegex(ValueError,'configuration missing'):b.cleanup_attempt('app')
        self.assertTrue(key.exists());self.assertEqual(b.journal['stages']['app']['status'],'deleting')
        b.store.apply.assert_not_called()

    def test_pruning_removes_only_a_new_unused_pool_in_dependency_order(self):
        b=self.fixture();network='network-ru-9';connection='connection-ru-9'
        b.journal['stages']={network:{'status':'active'},connection:{'status':'active'},
                             'es1':{'candidate':{'region':'ru-9'}}}
        b._temporary_networks={network};b.store=Mock();b.store.identities.return_value={}
        b.cleanup_attempt=Mock()
        b.prune_unused_network('ru-3');b.cleanup_attempt.assert_not_called()
        b.store.identities.return_value={'owned':'vm'};b.prune_unused_network('ru-9');b.cleanup_attempt.assert_not_called()
        b.store.identities.return_value={};b.prune_unused_network('ru-9')
        self.assertEqual([call.args[0] for call in b.cleanup_attempt.call_args_list],[connection,network])
        self.assertNotIn(network,b._temporary_networks)

    def test_fault_capture_does_not_replace_an_error_when_diagnostics_or_disk_fail(self):
        b=self.fixture();b.journal['stages']['app']={'candidate':{'region':'ru-9'}}
        b.api.RUN.mkdir(parents=True);(b.api.RUN/'stage-app-apply.log').write_text('synthetic-secret')
        b.api.redact=lambda value:value.replace('synthetic-secret','[REDACTED]')
        with patch('selectel_staged_audit.faults',side_effect=TimeoutError('synthetic-secret')):b.capture_fault('app')
        saved=json.loads(next((b.root/'docs/placement-faults').glob('*.json')).read_text())
        self.assertEqual(saved['diagnostic_error']['type'],'TimeoutError');self.assertEqual(saved['terraform_error'],'[REDACTED]')
        with patch('selectel_staged_audit.faults',return_value={}),patch.object(b.api,'save',side_effect=OSError('synthetic')):
            b.capture_fault('app')

    def test_capacity_classification_does_not_mask_original_error_with_diagnostic_failure(self):
        b=self.fixture();b.store=Mock();b.store.identities.return_value={}
        self.assertTrue(b.is_capacity_error(pools.CandidateUnavailable('quota'),'app'))
        with patch('selectel_staged_audit.faults',side_effect=OSError('synthetic diagnostic outage')):
            self.assertFalse(b.is_capacity_error(RuntimeError('original configuration failure'),'app'))
            self.assertFalse(b.is_capacity_error(pools.TerraformError('ERROR without a reason'),'app'))
        with patch('selectel_staged_audit.faults',return_value={'fault':{'message':'No valid host was found'}}):
            self.assertTrue(b.is_capacity_error(pools.TerraformError('ERROR'),'app'))
        with patch('selectel_staged_audit.faults',return_value={}):
            self.assertFalse(b.is_capacity_error(pools.TerraformError('Unrelated provider error'),'app'))
