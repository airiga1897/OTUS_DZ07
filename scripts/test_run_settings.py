"""Non-secret settings, provider discovery and retained-network lifecycle tests."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import run_settings
import pool_selection
from selectel_global_router import GlobalRouter
from selectel_staged import Backend, validate_policy

ROOT = Path(__file__).resolve().parents[1]


class RunSettingsTests(unittest.TestCase):
    def test_delivery_contains_example_but_not_local_settings(self):
        import tarfile
        from s3_artifacts import source_archive
        from remote.fetch_artifacts import extract
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);self.fixture(root)
            (root/'.env').write_text('LOCAL_ONLY=not-shipped')
            destination=root/'source.tar.gz';source_archive(root,destination)
            with tarfile.open(destination) as archive:
                self.assertIn('.env.example',archive.getnames())
                self.assertNotIn('.env',archive.getnames())
                self.assertTrue(all(b'not-shipped' not in archive.extractfile(name).read() for name in archive.getnames()))
            target=root/'received';target.mkdir()
            extract(destination,target)
            self.assertEqual((target/'.env.example').read_bytes(),(root/'.env.example').read_bytes())
            self.assertFalse((target/'.env').exists())

    def fixture(self, root):
        (root/'config').mkdir()
        (root/'.env.example').write_bytes((ROOT/'.env.example').read_bytes())
        (root/'config/placement.json').write_bytes((ROOT/'config/placement.json').read_bytes())

    def test_defaults_local_file_and_session_precedence(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); self.fixture(root)
            self.assertTrue(run_settings.load(root,environ={})['require_distinct_hosts'])
            (root/'.env').write_text('REQUIRE_DISTINCT_HOSTS=false\nES_VCPUS=4\n')
            value=run_settings.load(root,environ={})
            self.assertFalse(value['require_distinct_hosts'])
            self.assertEqual(value['profiles']['es']['vcpus'],4)
            self.assertTrue(run_settings.load(root,environ={'REQUIRE_DISTINCT_HOSTS':'true'})['require_distinct_hosts'])

    def test_invalid_values_and_credentials_are_rejected_without_echoing_values(self):
        cases=['OS_PASSWORD=never-print-this','never-print-this','REQUIRE_DISTINCT_HOSTS=maybe',
               'ES_VCPUS=0','ES_RAM_MB=1024','BOOT_DISK_GB=1',
               'SELECTEL_PREFERRED_POOLS=ru-7','ES_VCPUS=2\nES_VCPUS=4',
               'SELECTEL_PREFERRED_POOLS=$(command)']
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); self.fixture(root)
            for text in cases:
                with self.subTest(text=text):
                    (root/'.env').write_text(text)
                    with self.assertRaises(ValueError) as error:run_settings.load(root,environ={})
                    self.assertNotIn('never-print-this',str(error.exception))

    def test_json_does_not_duplicate_run_parameters(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); self.fixture(root)
            value=run_settings.placement(root)
            self.assertEqual(value['pools'],['auto'])
            self.assertEqual(value['preferred_pools'],['ru-2','ru-9','ru-3'])
            path=root/'config/placement.json';raw=json.loads(path.read_text());raw['boot_disk_gb']=20
            path.write_text(json.dumps(raw))
            with self.assertRaises(ValueError):run_settings.placement(root)

    def test_subnet_allocation_is_stable_private_and_nonoverlapping(self):
        policy=run_settings.placement(ROOT); assigned={}
        for pool in ('ru-9','ru-3','ru-8','kz-1','gis-1'):
            assigned[pool]=run_settings.pool_subnet(policy,pool,assigned)
        self.assertEqual(len(set(assigned.values())),len(assigned))
        self.assertEqual(assigned['ru-9'],'10.77.0.0/24')
        self.assertEqual(run_settings.pool_subnet(policy,'ru-8'),assigned['ru-8'])

    def test_saved_auth_region_works_even_if_local_run_settings_are_invalid(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);self.fixture(root);(root/'.local').mkdir()
            (root/'.env').write_text('REQUIRE_DISTINCT_HOSTS=invalid')
            (root/'.local/staged-deployment.json').write_text(json.dumps({'run_settings':{'profiles':{},'boot_disk_gb':20},
                'bootstrap_region':'ru-3','pool_subnets':{'ru-3':'10.78.0.0/24'}}))
            self.assertEqual(pool_selection.policy(root,'placement.json','context.json')['region'],'ru-3')

    def test_cycle_records_diagnostic_mode_and_rejects_invalid_settings_before_auth(self):
        import test_cycle
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);self.fixture(root);local=root/'.local';local.mkdir()
            (root/'.env').write_text('REQUIRE_DISTINCT_HOSTS=false')
            def save(path,value):
                path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value))
            api=SimpleNamespace(ROOT=root,LOCAL=local,_TOPOLOGY_OVERRIDE=None,save=save,cleanup=Mock())
            with patch('test_cycle.reject_existing'),patch('test_cycle.stages',return_value={'success':True}):
                test_cycle.run(api)
            report=json.loads((root/'docs/test-cycle-result.json').read_text())
            self.assertFalse(report['require_distinct_hosts'])
            self.assertTrue(report['diagnostic_colocation_allowed'])
            (local/'test-cycle.json').unlink()
            (root/'.env').write_text('REQUIRE_DISTINCT_HOSTS=invalid')
            with patch('test_cycle.reject_existing') as guard,self.assertRaises(ValueError):
                test_cycle.run(api)
            guard.assert_not_called();self.assertFalse((local/'test-cycle.json').exists())

    def test_existing_host_policy_cannot_change_but_cleanup_still_uses_saved_context(self):
        b=Backend.__new__(Backend);b.api=SimpleNamespace(PREFIX='synthetic');b.credentials={'project_id':'project'}
        b.policy=run_settings.placement(ROOT);b.desired={'nodes':{}}
        b.journal={'provider':'selectel','scope':{'project_id':'project'},'project_id':'project',
            'namespace':'synthetic','deployment_id':'a'*32,'stages':{},'topology':b.desired,
            'address_policy':b.policy['sequential_placement']['addresses'],
            'pool_subnets':dict(b.policy['sequential_placement']['pool_subnets'])}
        b.journal['run_settings']=run_settings.snapshot(b.policy)
        b.validate_journal()
        b.policy['preferred_pools'].reverse()
        b.validate_journal()
        b.policy['require_distinct_hosts']=False
        with self.assertRaises(ValueError):b.validate_journal()
        b.validate_journal(cleanup=True)


class ProviderDiscoveryTests(unittest.TestCase):
    def test_diagnostic_verification_records_same_host_without_claiming_separation(self):
        from selectel_staged_audit import verify_networks
        nodes={name:{'id':name,'region':'ru-3','port_id':name+'-port','private_ip':'10.78.0.'+str(i),
                     'roles':['es']} for i,name in enumerate(('es1','es2','es3'),31)}
        b=SimpleNamespace(journal={'require_distinct_hosts':False,'stages':{
            'network-ru-3':{'status':'pending','output':{'network_id':'net','security_group_id':'sg','server_group_id':'group'}}}},
            result=Mock(return_value=SimpleNamespace(nodes=nodes)))
        def get(service,path):
            path=path.split('?',1)[0]
            if path.startswith('/servers/'):return {'server':{'hostId':'same-physical-host'}}
            if path.startswith('/v2.0/ports/'):
                name=path.rsplit('/',1)[1].removesuffix('-port')
                return {'port':{'port_security_enabled':True,'security_groups':['sg'],'network_id':'net',
                                'fixed_ips':[{'ip_address':nodes[name]['private_ip']}]}}
            if path=='/v2.0/floatingips':return {'floatingips':[]}
            if path=='/os-server-groups':return {'server_groups':[{'id':'group','policies':['anti-affinity'],'members':[]}]}
            raise AssertionError(path)
        b.region=Mock(return_value=SimpleNamespace(get=get))
        report=verify_networks(b)
        self.assertEqual(report['es_distinct_hosts'],1)
        self.assertFalse(report['physical_host_separation_verified'])
        b.journal['require_distinct_hosts']=True
        with self.assertRaises(RuntimeError):verify_networks(b)
    def test_preferences_do_not_exclude_other_catalog_pools(self):
        catalog=[{'type':kind,'endpoints':[{'region':r,'interface':'public','url':'https://example.invalid/'+kind}
                  for r in ('ru-9','ru-3','ru-7','ru-8','kz-1')]} for kind in pool_selection.SERVICES]
        config={'pools':['auto'],'preferred_pools':['ru-9','ru-3'],'excluded_pools':['ru-7']}
        values=pool_selection.ordered_pools(config,catalog)
        self.assertEqual(values[:2],['ru-9','ru-3'])
        self.assertEqual(set(values),{'ru-9','ru-3','ru-8','kz-1'})

    def test_zone_group_selection_retains_existing_networks(self):
        client=GlobalRouter.__new__(GlobalRouter)
        zones=[{'name':r,'service':'vpc','enable':True,'groups':[{'id':g}]} for r,g in
               [('ru-9','one'),('ru-3','one'),('ru-8','two'),('kz-1','two')]]
        with patch.object(client,'listing',return_value=zones):
            self.assertEqual(client.compatible_regions(['ru-9','ru-3','ru-8','kz-1']),['ru-9','ru-3'])
            self.assertEqual(client.compatible_regions(['ru-9','ru-3','ru-8','kz-1'],{'ru-8'}),['ru-8','kz-1'])
            with self.assertRaises(ValueError):client.compatible_regions(['ru-9','ru-8'],{'ru-9','ru-8'})
            self.assertEqual(client.compatible_regions(['ru-9','ru-8'],accept=lambda regions:'ru-8' in regions),['ru-8'])

    def test_unknown_zone_group_shape_is_not_an_unsupported_candidate(self):
        client=GlobalRouter.__new__(GlobalRouter)
        with patch.object(client,'listing',return_value=[{'name':'ru-9','service':'vpc','enable':True}]):
            with self.assertRaises(ValueError):client.compatible_regions(['ru-9'])


class DelayedConnectionTests(unittest.TestCase):
    def test_untracked_server_group_member_blocks_network_cleanup(self):
        from selectel_staged_audit import assert_stage_owned
        kind='openstack_compute_servergroup_v2';entry={'type':kind,'attrs':{'id':'group','name':'owned-group'}}
        b=SimpleNamespace(owner='owner',credentials={'project_id':'project'},desired={'nodes':{}},
            journal={'stages':{'network-ru-9':{'status':'active','candidate':{'region':'ru-9'}}}},
            store=SimpleNamespace(entries=Mock(return_value=[entry])))
        group={'id':'group','name':'owned-group','members':['foreign-vm']}
        with patch('selectel_staged_audit.inventory',return_value={kind:{'group':group}}):
            with self.assertRaises(ValueError):assert_stage_owned(b,'network-ru-9')
            group['members']=[]
            assert_stage_owned(b,'network-ru-9')

    def backend(self):
        b=Backend.__new__(Backend)
        b.desired={'nodes':{'app':{'role':'app'},'es1':{'role':'es'}}}
        b.journal={'stages':{'app':{'status':'pending','candidate':{'region':'ru-9'}},
                             'network-ru-9':{'status':'active','output':{'network_id':'network'}}}}
        b._temporary_networks={'network-ru-9'}
        b.save=Mock();b.connect_pool=Mock()
        b.store=SimpleNamespace(identities=Mock(return_value={'vm':'one'}),state=Mock(return_value={'lineage':'line'}))
        return b

    def test_connection_is_attempted_only_after_persisting_active_vm_identity(self):
        b=self.backend()
        def fail(pool):
            self.assertEqual(b.journal['stages']['app']['status'],'active')
            self.assertEqual(b.journal['stages']['app']['identities'],{'vm':'one'})
            b.save.assert_called()
            raise RuntimeError('attachment failed')
        b.connect_pool.side_effect=fail
        with self.assertRaises(RuntimeError):b.finish('app')
        self.assertEqual(b.journal['stages']['app']['status'],'active')

    def test_unused_new_network_is_pruned_but_a_successful_vm_protects_it(self):
        b=self.backend(); b.cleanup_attempt=Mock()
        b.store.identities.side_effect=lambda n: {'vm':'one'} if n=='es1' else {}
        b.journal['stages']['es1']={'candidate':{'region':'ru-9'},'status':'active'}
        b.prune_unused_network('ru-9');b.cleanup_attempt.assert_not_called()
        b.store.identities.side_effect=lambda n:{}
        b.prune_unused_network('ru-9');b.cleanup_attempt.assert_called_once_with('network-ru-9')

    def test_preexisting_saved_network_is_never_pruned_as_a_new_attempt(self):
        b=self.backend();b._temporary_networks=set();b.cleanup_attempt=Mock()
        b.prune_unused_network('ru-9');b.cleanup_attempt.assert_not_called()

    def test_vm_preparation_does_not_create_global_resources(self):
        b=self.backend();b.completed=Mock(return_value=False)
        b.choices={};b.journal['stages']['network-ru-9']['candidate']={'region':'ru-9'}
        candidate={'region':'ru-9','network_availability_zone':'ru-9a','router_availability_zone':'ru-9a'}
        with patch('selectel_staged_capacity.require_network_capacity'),patch('selectel_staged.place') as place:
            b.ensure_pool(candidate)
            self.assertEqual([c.args[1] for c in place.call_args_list],['network-ru-9'])
        b.connect_pool.assert_not_called()

    def test_audited_network_exhaustion_allows_other_pool_but_unknown_errors_do_not(self):
        from staged_deploy import PlacementExhausted
        b=self.backend();b.completed=Mock(return_value=False);b.choices={}
        candidate={'region':'ru-9','network_availability_zone':'ru-9a','router_availability_zone':'ru-9a'}
        b.store.identities.return_value={}
        with patch('selectel_staged_capacity.require_network_capacity'):
            with patch('selectel_staged.place',side_effect=PlacementExhausted('audited empty')):
                with self.assertRaises(pool_selection.CandidateUnavailable):b.ensure_pool(candidate)
            with patch('selectel_staged.place',side_effect=RuntimeError('unknown API state')):
                with self.assertRaisesRegex(RuntimeError,'unknown API state'):b.ensure_pool(candidate)

    def test_retry_active_vm_retries_only_configuration_and_connection(self):
        b=self.backend();record=b.journal['stages']['app']
        record.update(status='active',kind='node',inputs={'same':'saved'})
        b.configure_stage=Mock();b.store.apply=Mock(return_value={'id':'one'})
        with patch('selectel_staged_audit.assert_stage_owned'):
            b.reapply('app')
        b.store.apply.assert_called_once_with('app',unchanged=True)
        b.connect_pool.assert_called_once_with('ru-9')

    def test_full_destroy_never_triggers_extra_network_pruning(self):
        b=self.backend();b.journal['destroy_requested']=True;b.prune_unused_network=Mock()
        with tempfile.TemporaryDirectory() as folder:
            b.store.entries=Mock(return_value=[])
            b.store.folder=Mock(return_value=Path(folder)/'app')
            b.store.identities.return_value={}
            with patch('selectel_staged_audit.assert_stage_owned'),patch('selectel_staged_audit.assert_stage_absent'):
                b.cleanup_attempt('app')
            b.prune_unused_network.assert_not_called()
            self.assertEqual(b.journal['stages']['app']['status'],'deleted')
