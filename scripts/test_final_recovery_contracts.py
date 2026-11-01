"""Remaining provider and lifecycle failures preserve ownership and recovery data."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch
import urllib.error

import lab
import cloud_contract
import pool_selection
import selectel_adapter as adapter
import selectel_staged as staged
import staged_runtime
import test_cycle
import verification
import placement_diagnostics
import cache_packages
from remote import verify as remote_verify
import test_staged_backend as backend_fixtures
import test_staged_lifecycle as lifecycle_fixtures
import test_controller_boundaries as controller_fixtures
import test_cycle_safety as cycle_fixtures
import test_provider_postcheck_matrix as network_fixtures
import test_empty_scope_audit as empty_fixtures
import test_provider_audit as audit_fixtures
import selectel_staged_audit as audit


class FinalRecoveryContracts(unittest.TestCase):
    def setUp(self):
        secrets = patch('privacy._values', set())
        secrets.start()
        self.addCleanup(secrets.stop)

    def test_empty_scope_floating_ip_permission_error_blocks_success(self):
        helper = empty_fixtures.EmptyScopeAuditContracts()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        error = urllib.error.HTTPError('https://example.invalid', 403, 'Forbidden', {}, None)
        with patch('selectel_adapter.reject_cloud_resources'), patch.object(audit, 'objects', side_effect=error), \
                self.assertRaises(urllib.error.HTTPError) as caught:
            staged.audit_empty_scope(helper.api)
        self.assertIs(caught.exception, error)
        helper.global_api.listing.assert_not_called()

    def test_cleared_failed_node_prunes_only_its_temporary_pool_after_absence_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            backend, _ = backend_fixtures.StagedBackendTests().backend(Path(directory))
            backend.journal['stages']['app'] = {'status': 'pending', 'candidate': {'region': 'ru-9'}}
            backend.store.folder = Mock(return_value=Path(directory) / 'empty-stage')
            backend.store.identities = Mock(return_value={})
            backend.prune_unused_network = Mock()
            backend.save = Mock()
            events = []
            with patch.object(audit, 'assert_stage_owned'), \
                    patch.object(audit, 'assert_stage_absent', side_effect=lambda *args: events.append('absent')):
                backend.prune_unused_network.side_effect = lambda pool: events.append(pool)
                backend.cleanup_attempt('app')
            self.assertEqual(events, ['absent', 'ru-9'])
            self.assertEqual(backend.journal['stages']['app']['status'], 'deleted')

    def test_servergroup_membership_ignores_non_es_and_other_pool_nodes(self):
        backend, entry, item = audit_fixtures.ProviderAuditTests().backend('openstack_compute_servergroup_v2')
        backend.desired['nodes'].update(app={'role': 'app'}, es2={'role': 'es'})
        backend.journal['stages'].update(app={'candidate': {'region': 'ru-3'}}, es2={'candidate': {'region': 'ru-9'}})
        with patch.object(audit, 'inventory', return_value={entry['type']: {'owned': item}}):
            audit.assert_stage_owned(backend, 'es1')

    def test_legacy_contract_includes_etcd_role_only_for_declared_members(self):
        names = ['app', 'db1']
        output = {'server_ids': {name: name + '-id' for name in names}, 'private_ips': {'app': '10.77.0.10', 'db1': '10.77.0.11'},
            'volume_ids': {name: name + '-volume' for name in names}, 'port_ids': {name: name + '-port' for name in names},
            'etcd_members': ['db1'], 'app_public_ip': '8.8.8.8', 'subnet_cidr': '10.77.0.0/24'}
        result = adapter.infrastructure_result(output, {'project_id': 'own', 'region': 'ru-9'}, 'otus-dz07', 'ru-9a')
        self.assertEqual(result.nodes['db1']['roles'], ['db', 'etcd'])
        self.assertEqual(result.nodes['app']['roles'], ['app'])

    def test_partial_state_binding_rejects_changed_terraform_region_or_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = {'project_id': 'own', 'region': 'ru-9'}
            context = adapter.binding(config, 'otus-dz07', 'ru-9a')
            placement = {'provider': 'selectel', 'namespace': 'otus-dz07', 'project_id': 'own',
                'region': 'ru-9', 'availability_zone': 'ru-9a'}
            for name, value in (('.local/context.json', context), ('.local/placement.json', placement)):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(value))
            variables = root / 'terraform-selectel/terraform.tfvars.json'
            variables.parent.mkdir()
            for key, changed in (('region', 'ru-3'), ('name_prefix', 'foreign')):
                value = {'region': 'ru-9', 'name_prefix': 'otus-dz07', 'availability_zone': 'ru-9a', key: changed}
                variables.write_text(json.dumps(value))
                with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'conflicts with Terraform'):
                    adapter.partial_state_binding(root, config, 'otus-dz07')

    def test_legacy_preflight_missing_compute_quota_is_unknown_not_sufficient(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'config').mkdir()
            (root / 'config/cloud.json').write_text('{"required_capabilities":[]}')
            cloud = Mock(config={'project_id': 'own'})
            cloud.get.return_value = {'quota_set': {'az_quotas': {}}}
            api = SimpleNamespace(ROOT=root, STACK={}, PREFIX='otus-dz07', Cloud=lambda: cloud,
                placement=lambda: {'availability_zone': 'ru-9a'}, desired_topology=Mock(), managed_ids=Mock(), save=Mock())
            with patch.object(adapter, 'discover_capabilities', return_value={}), \
                    self.assertRaisesRegex(pool_selection.CandidateUnavailable, 'Compute quotas unavailable'):
                adapter.preflight(api)
            api.save.assert_not_called()

    def test_legacy_repeat_stack_mismatch_preserves_recovery_and_never_runs_apply(self):
        helper = controller_fixtures.ControllerBoundaries()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        (helper.local / 'configured.json').write_text('{"stack_sha256":"different"}')
        with patch('stage_store.deployment_exists', return_value=False), patch.object(lab, 'managed_ids', return_value={'app': 'same'}), \
                patch.object(lab.pools, 'selected', return_value={'phase': 'active'}), patch.object(lab, 'diagnostics'), \
                patch('placement_diagnostics.preserve'), patch.object(lab, 'cleanup') as cleanup, patch.object(lab, 'tf') as tf, \
                self.assertRaisesRegex(ValueError, 'separate migration'):
            lab.deploy()
        cleanup.assert_not_called()
        tf.assert_not_called()

    def test_package_progress_counts_verified_ranges_without_large_local_files(self):
        size = 128 * 2**20
        block = b'x' * 2**20
        offsets = []
        def open_response(request, **kwargs):
            response = MagicMock()
            response.__enter__.return_value = response
            if request.get_method() == 'HEAD':
                response.headers = {'Content-Length': str(size)}
            else:
                value = request.get_header('Range')
                start, end = map(int, value.removeprefix('bytes=').split('-'))
                offsets.append(start)
                response.status = 206
                response.headers = {'Content-Range': f'bytes {start}-{end}/{size}'}
                response.read.return_value = block
            return response
        target = MagicMock()
        output = target.open.return_value.__enter__.return_value
        with patch.object(cache_packages.urllib.request, 'urlopen', side_effect=open_response), \
                patch('sys.stdout', new_callable=io.StringIO) as printed:
            cache_packages.download('https://example.invalid/package', target)
        self.assertEqual(sorted(offsets), list(range(0, size, len(block))))
        self.assertEqual(output.write.call_count, 128)
        self.assertEqual(sum(len(call.args[0]) for call in output.write.call_args_list), size)
        self.assertIn('128/128 MiB', printed.getvalue())

    def test_verifier_exhaustion_does_not_accept_unchanged_primary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'probes').mkdir()
            run_id = 'a' * 32
            context = {'baseline': {'id': 1}, 'media': {}, 'data': {}}
            file = root / 'probes' / (run_id + '.json')
            file.write_text(json.dumps(context))
            before = file.read_bytes()
            members = {'db1': {'role': 'primary', 'state': 'running'}, 'db2': {'state': 'unreachable'}}
            with patch.object(remote_verify, 'initialize'), patch.object(remote_verify, 'APP', root), \
                    patch.object(remote_verify.sys, 'argv', ['verify.py', 'failover', run_id, 'db1']), \
                    patch.object(remote_verify, 'cluster', return_value=members) as cluster, \
                    patch.object(remote_verify, 'sql_connect') as sql, patch.object(remote_verify.time, 'sleep'), \
                    self.assertRaisesRegex(RuntimeError, 'Leader did not change'):
                remote_verify.main()
            self.assertEqual(cluster.call_count, 150)
            sql.assert_not_called()
            self.assertEqual(file.read_bytes(), before)

    def test_infrastructure_contract_rejects_unrecorded_cross_pool_subnet(self):
        node = {'roles': ['app'], 'id': 'app-id', 'volume_id': 'disk', 'port_id': 'port',
            'private_ip': '10.78.0.10', 'public_ip': '8.8.8.8', 'region': 'ru-3', 'zone': 'ru-3b', 'subnet_cidr': '10.78.0.0/24'}
        result = cloud_contract.InfrastructureResult('selectel', {'project_id': 'own'}, 'otus-dz07',
            {'region': 'ru-9', 'zone': 'ru-9a', 'pool_subnets': {}, 'node_placements': {}}, {'app': node}, '10.77.0.0/24')
        with self.assertRaisesRegex(ValueError, 'outside the saved network contract'):
            result.validate()

    def test_provider_state_skips_data_and_empty_resources_but_rejects_foreign_names(self):
        rows = [{'mode': 'data', 'type': 'arbitrary', 'instances': [{}]},
                {'mode': 'managed', 'type': 'openstack_any', 'instances': []}]
        adapter.validate_managed_state({'resources': rows}, {}, 'otus-dz07')
        rows.append({'mode': 'managed', 'type': 'openstack_compute_instance_v2',
            'instances': [{'attributes': {'name': 'other-project'}}]})
        with self.assertRaisesRegex(ValueError, 'Foreign resource'):
            adapter.validate_managed_state({'resources': rows}, {}, 'otus-dz07')

    def test_diagnostics_omit_server_details_when_live_ownership_disagrees(self):
        cloud = Mock(config={'project_id': 'own'})
        cloud.get.return_value = {'server': {'name': 'foreign', 'tenant_id': 'own', 'fault': {'message': 'PRIVATE'}}}
        api = SimpleNamespace(Cloud=lambda: cloud, PREFIX='otus-dz07')
        report = {'nodes': {}, 'region': 'ru-9'}
        placement_diagnostics.collect_nodes(api, [{'index_key': 'app', 'attributes': {'name': 'otus-dz07-app', 'id': 'own'}}], report)
        self.assertIn('API ownership mismatch', report['nodes']['app']['diagnostic_error'])
        self.assertNotIn('PRIVATE', json.dumps(report))

    def test_existing_node_scheduler_fault_is_classified_before_reapplying(self):
        backend = staged.Backend.__new__(staged.Backend)
        backend.desired = {'nodes': {'es1': {'role': 'es'}}}
        backend.store = Mock()
        with patch('selectel_staged_audit.faults', return_value={'fault': 'No valid host'}), \
                self.assertRaises(pool_selection.TerraformError):
            backend.apply('es1')
        backend.store.apply.assert_not_called()

    def test_network_verification_uses_saved_backend_instead_of_reselection(self):
        backend = staged.Backend.__new__(staged.Backend)
        with patch('selectel_staged_audit.verify_networks', return_value={'success': True}) as verify:
            self.assertEqual(backend.verify_placement(), {'success': True})
        verify.assert_called_once_with(backend)

    def test_global_router_inventory_is_read_through_its_provider(self):
        backend = SimpleNamespace(journal={'stages': {'global': {'candidate': {'region': 'ru-9'}}}},
            region=Mock(), global_router=Mock())
        backend.global_router.listing.return_value = [{'id': 'own-router'}]
        from selectel_staged_audit import inventory
        result = inventory(backend, 'global', [{'type': 'selectel_global_router_router_v1', 'attrs': {'id': 'own-router'}}])
        self.assertIn('own-router', result['selectel_global_router_router_v1'])
        backend.global_router.listing.assert_called_once_with('routers')

    def test_new_node_preservation_report_is_written_only_after_successful_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = {'id': 1}
            api = SimpleNamespace(ROOT=root, LOCAL=root, STACK={'paths': {'remote_root': '/test'}},
                lab_output=lambda: {'server_ids': {'app': 'same', 'es1': 'new'}},
                connect=lambda value: contextlib.nullcontext(object()),
                remote=Mock(return_value=json.dumps({'preserved_records': [record]})), save=Mock())
            before = {'server_ids': {'app': 'same'}, 'run_id': 'a' * 32, 'before': {'new_record': record}}
            verification.expansion(api, before)
            self.assertEqual([call.args[0].name for call in api.save.call_args_list], ['expansion-result.json', 'node-addition-result.json'])
            self.assertEqual(api.save.call_args.args[1]['added_nodes'], ['es1'])

    def test_controller_configuration_wrapper_preserves_existing_flag(self):
        with patch('configuration.configure', return_value='configured') as configure:
            self.assertEqual(lab.configure(existing=True), 'configured')
        configure.assert_called_once_with(lab, True)

    def test_provider_discovery_reports_only_public_compute_regions(self):
        cloud = adapter.SelectelAdapter.__new__(adapter.SelectelAdapter)
        cloud.catalog = [dict(type='compute', endpoints=[dict(interface='public', region='ru-9'),
            dict(interface='internal', region='ru-3')]), dict(type='image', endpoints=[dict(interface='public', region='ru-2')])]
        self.assertEqual(cloud.resource_regions(), ['ru-9'])

    def test_internal_api_permission_error_is_not_treated_as_unsupported(self):
        cloud = Mock(endpoints={'compute': 'https://example.invalid'})
        error = urllib.error.HTTPError('https://example.invalid', 403, 'Forbidden', {}, None)
        with patch.object(adapter, 'collection', side_effect=error), self.assertRaises(urllib.error.HTTPError) as caught:
            adapter.reject_cloud_resources(SimpleNamespace(PREFIX='otus-dz07'), cloud)
        self.assertIs(caught.exception, error)

    def test_foreign_cloud_provider_in_state_is_rejected_before_deletion(self):
        state = {'resources': [{'mode': 'managed', 'type': 'foreign_compute', 'instances': [{}]}]}
        with self.assertRaisesRegex(ValueError, 'Foreign provider'):
            adapter.validate_managed_state(state, {'project_id': 'own', 'region': 'ru-9'}, 'otus-dz07')

    def test_network_only_node_set_does_not_require_elastic_public_rule(self):
        result, data = network_fixtures.ProviderPostcheckMatrix().network_fixture()
        result.nodes['es1']['roles'] = ['db']
        rules = data['/v2.0/security-groups']['security_groups'][0]['security_group_rules']
        rules[:] = [rule for rule in rules if rule.get('port_range_min') != 5601]
        cloud = Mock()
        cloud.get.side_effect = lambda service, path: data[path]
        self.assertTrue(adapter.check_network(cloud, result, ['8.8.8.8/32'])['success'])

    def test_reapply_never_runs_after_stack_configuration_was_changed(self):
        with tempfile.TemporaryDirectory() as directory:
            helper = lifecycle_fixtures.RetainedLifecycleTest()
            api, backend = helper.fixture(Path(directory))
            backend.journal.update(configured=True, stack_sha256='different')
            with helper.patches(backend), patch('configuration.resume', return_value=False), \
                    self.assertRaisesRegex(ValueError, 'separate migration'):
                staged_runtime.deploy(api)
            api.configure.assert_not_called()

    def test_empty_role_candidates_fail_before_any_infrastructure_change(self):
        with tempfile.TemporaryDirectory() as directory:
            helper = backend_fixtures.StagedBackendTests()
            backend, _ = helper.backend(Path(directory))
            with patch.object(staged, 'discover_capabilities', return_value={'ownership_tags': cloud_contract.Capability(cloud_contract.CapabilityStatus.SUPPORTED, 'synthetic')}), \
                    patch.object(staged.pools, 'candidates', return_value=iter([])), \
                    patch.object(staged, 'reject_cloud_resources'), patch('selectel_staged_audit.audit_namespace'), \
                    self.assertRaisesRegex(ValueError, 'No complete compatible candidates'):
                backend.preflight()
            backend.api.save.assert_not_called()

    def test_deletion_audit_rejects_floating_ip_left_after_named_resources_disappeared(self):
        backend = staged.Backend.__new__(staged.Backend)
        backend.journal = {'deployment_id': 'synthetic', 'stages': {'app': {'candidate': {'region': 'ru-9'}}}}
        backend.api = SimpleNamespace(PREFIX='otus-dz07')
        backend.global_router = Mock(unsafe=True)
        backend.region = Mock()
        with patch('selectel_staged_audit.assert_stage_absent'), patch('selectel_adapter.audit_cleanup', return_value=([], 0)), \
                patch('selectel_staged_audit.objects', return_value=[{'description': backend.owner}]), \
                self.assertRaisesRegex(RuntimeError, 'resources remain'):
            backend.audit_deletion()

    def test_expansion_report_is_not_created_if_original_record_was_not_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            api = SimpleNamespace(ROOT=root, LOCAL=root, STACK={'paths': {'remote_root': '/test'}},
                lab_output=lambda: {'server_ids': {'app': 'same'}},
                connect=lambda value: contextlib.nullcontext(object()),
                remote=Mock(return_value='{"preserved_records":[]}'), save=Mock())
            before = {'server_ids': {'app': 'same'}, 'run_id': 'a' * 32, 'before': {'new_record': {'id': 1}}}
            with self.assertRaisesRegex(RuntimeError, 'original canary'):
                verification.expansion(api, before)
            api.save.assert_not_called()

    def test_repeat_cycle_detects_unexpected_new_resource_even_when_original_ids_survive(self):
        api = Mock()
        api.desired_topology.return_value = {'nodes': {'app': {'role': 'app'}}, 'etcd_members': []}
        api.managed_ids.side_effect = [{'app': 'same'}, {'app': 'same', 'unexpected': 'new'}, {'app': 'same', 'unexpected': 'new'}]
        with patch('verification.snapshot', return_value={}), patch.object(test_cycle, 'progress'), \
                patch.object(test_cycle, 'check_original'), patch('observability.command'), \
                self.assertRaisesRegex(RuntimeError, 'added nodes'):
            test_cycle.stages(api)
        api.verify.assert_not_called()

    def test_cloud_namespace_preflight_runs_only_for_sequential_placement(self):
        with tempfile.TemporaryDirectory() as directory:
            api = cycle_fixtures.CycleSafetyTests().fixture(Path(directory))
            api.Cloud.return_value.resource_regions.return_value = []
            with patch('cloud_settings.sequential_enabled', return_value=True), \
                    patch('staged_runtime.reject_shared_namespace') as reject:
                test_cycle.reject_existing(api)
            reject.assert_called_once_with(api)

    def test_staged_namespace_guard_delegates_to_provider_adapter(self):
        api = object()
        with patch('cloud_settings.adapter') as factory:
            staged_runtime.reject_shared_namespace(api)
        factory.return_value.reject_shared_namespace.assert_called_once_with(api)

    def test_standalone_diagnostic_queries_include_app_database(self):
        helper = controller_fixtures.ControllerBoundaries()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        with patch.object(lab, 'lab_output', return_value={'node_roles': {'app': 'app'}, 'private_ips': {'app': '10.77.0.10'}}), \
                patch('network_diagnostics.console_evidence'), patch.object(lab, 'connect', side_effect=lambda *a: contextlib.nullcontext(object())), \
                patch.object(lab, 'remote') as remote:
            path = helper.root / 'scripts/remote/network_diagnostics.py'
            path.parent.mkdir(parents=True)
            path.write_text('# synthetic diagnostic')
            lab.diagnostics()
        self.assertTrue(any(call.args[2] == 'diagnostics-app' for call in remote.call_args_list))

