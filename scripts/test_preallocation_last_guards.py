"""Placement and dispatch guards stop unsafe work before allocating resources."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import credentials
import lab
import pool_selection as pools
import selectel_staged as staged
import test_controller_boundaries as controller_fixture
import test_staged_backend as backend_fixture
from cloud_contract import Capability, CapabilityStatus
from selectel_global_router import GlobalRouter

ROOT=Path(__file__).resolve().parents[1]


class PreallocationLastGuards(unittest.TestCase):
    def test_quota_failure_in_first_pool_does_not_hide_compatible_alternative(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'.tools') as directory:
            helper=backend_fixture.StagedBackendTests();b,clouds=helper.backend(Path(directory))
            clouds['ru-9'].compute['az_quotas']['ru-9a']['cores']['limit']=0
            clouds['ru-3']=backend_fixture.QuotaCloud('ru-3',instances=4)
            def candidates(config,auth):return [helper.choice(pool,next(iter(config['profiles']))) for pool in config['pools']]
            with patch.object(staged,'discover_capabilities',return_value={'ownership_tags':Capability(CapabilityStatus.SUPPORTED,'synthetic')}), \
                    patch.object(staged.pools,'candidates',side_effect=candidates),patch.object(staged,'reject_cloud_resources'),patch('selectel_staged_audit.audit_namespace'):
                b.preflight()
            self.assertEqual(b.policy['pools'],['ru-3'])
            self.assertTrue(all(c['region']=='ru-3' for items in b.choices.values() for c in items))
            candidate=helper.choice('ru-9','es');candidate['availability_zone']='ru-9z'
            with self.assertRaisesRegex(pools.CandidateUnavailable,'quota unknown'):b.check_node_quota(candidate,'es')

    def test_disjoint_global_router_groups_cannot_split_required_roles_incompatibly(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'.tools') as directory:
            helper=backend_fixture.StagedBackendTests();b,clouds=helper.backend(Path(directory))
            b.policy['pools']=['auto'];b.cloud=Mock(catalog=helper.catalog(['ru-9','ru-3']))
            router=GlobalRouter.__new__(GlobalRouter)
            router.listing=Mock(return_value=[{'name':r,'service':'vpc','enable':True,'groups':[{'id':r}]} for r in ('ru-9','ru-3')])
            router.preflight=Mock();b.global_router=router
            def candidates(config,auth):
                role=next(iter(config['profiles']));return [helper.choice('ru-9' if role=='app' else 'ru-3',role)]
            with patch.object(staged,'discover_capabilities',return_value={'ownership_tags':Capability(CapabilityStatus.SUPPORTED,'synthetic')}), \
                    patch.object(staged.pools,'candidates',side_effect=candidates),patch.object(staged,'reject_cloud_resources'), \
                    patch('selectel_staged_audit.audit_namespace'),self.assertRaisesRegex(ValueError,'No zone group'):
                b.preflight()
            router.preflight.assert_not_called();b.api.save.assert_not_called()

    def test_missing_endpoint_or_region_cannot_be_replaced_from_supplied_configuration(self):
        schema={'auth_url':'https://identity.invalid/v3','region':'ru-9','fields':{'username':{}}}
        for changes in ({'auth_url':'https://foreign.invalid/v3'},{'region':'foreign'}):
            with patch.object(credentials,'auth_settings',return_value=schema), \
                    patch.object(credentials,'config_from_env',return_value={'username':'synthetic',**changes}),self.assertRaisesRegex(ValueError,'Unsupported'):
                credentials.load_credentials()

    def test_empty_cleanup_and_staged_commands_delegate_without_legacy_mutation(self):
        helper=controller_fixture.ControllerBoundaries();helper.setUp();self.addCleanup(helper.doCleanups)
        with patch('staged_runtime.cleanup_empty') as clean:
            lab.cleanup(False)
        clean.assert_called_once_with(lab,False)
        (helper.local/'staged-deployment.json').write_text('{}')
        with patch('staged_runtime.output',return_value='synthetic-result') as output, \
                patch('staged_runtime.discover_route',return_value=['8.8.8.8/32']) as route, \
                patch('staged_runtime.deploy') as deploy:
            self.assertEqual(lab.infrastructure_output(),'synthetic-result')
            self.assertEqual(lab.discover_route({'site_url':'http://8.8.8.8'}),['8.8.8.8/32'])
            lab.deploy_selected()
        output.assert_called_once_with(lab);deploy.assert_called_once_with(lab)
        route.assert_called_once_with(lab,{'site_url':'http://8.8.8.8'})

    def test_legacy_repeat_deploy_never_reselects_placement(self):
        helper=controller_fixture.ControllerBoundaries();helper.setUp();self.addCleanup(helper.doCleanups)
        with patch('cloud_settings.sequential_enabled',return_value=False),patch.object(lab,'managed_ids',return_value={'owned':'id'}), \
                patch.object(lab,'deploy') as deploy,patch.object(lab,'deployment_candidates') as candidates:
            lab.deploy_selected()
        deploy.assert_called_once_with();candidates.assert_not_called()
