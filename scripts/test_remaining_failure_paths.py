"""Remaining recovery guards use synthetic resources and disposable roots only."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import cloud_contract
import configuration
import network_diagnostics
import placement_diagnostics as diagnostics
import selectel_adapter as adapter
import staged_deploy
import terraform_plugins
import test_cycle as cycle
import test_cycle_safety as cycle_fixtures
import test_terraform_plugins as provider_fixtures
import test_staged_backend as staged_fixtures
import test_provider_postcheck_matrix as network_fixtures
import pool_selection
import s3_artifacts
import selectel_staged_audit
import local_runtime
import test_runtime_contract as runtime_fixtures
from selectel_staged import Backend
from stage_store import StageStore
from remote import verify as remote_verify


class RemainingFailurePaths(unittest.TestCase):
    def setUp(self):
        secrets = patch('privacy._values', set())
        secrets.start()
        self.addCleanup(secrets.stop)

    def test_direct_lookup_permission_error_cannot_prove_resource_absence(self):
        import urllib.error
        backend = SimpleNamespace(journal={'stages': {'app': {'candidate': {'region': 'ru-9'}}}}, region=Mock())
        cloud = backend.region.return_value
        cloud.get.side_effect = urllib.error.HTTPError('https://example.invalid', 403, 'Forbidden', {}, None)
        with patch.object(selectel_staged_audit, 'objects', return_value=[]), self.assertRaises(urllib.error.HTTPError):
            selectel_staged_audit.inventory(backend, 'app',
                [{'type': 'openstack_compute_instance_v2', 'attrs': {'id': 'own-id'}}])
        cloud.get.assert_called_once_with('compute', '/servers/own-id')

    def test_open_prints_kibana_for_a_saved_dedicated_kibana_node(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local, tf, state = runtime_fixtures.RuntimeContract().fixture(root, 5)
            value = state['outputs']['lab']['value']
            value['server_ids']['kibana'] = 'kibana-id'
            state['resources'][0]['instances'].append({'index_key': 'kibana',
                'attributes': {'id': 'kibana-id', 'name': 'otus-dz05-kibana', 'tenant_id': 'project', 'region': 'ru-9'}})
            (tf / 'terraform.tfstate').write_text(json.dumps(state))
            saved = json.loads((local / 'lab.json').read_text())
            saved['server_ids'] = value['server_ids']
            (local / 'lab.json').write_text(json.dumps(saved))
            with patch('topology.services', return_value={'kibana_port': 5601}), patch('sys.stdout', new_callable=io.StringIO) as output:
                local_runtime.open_portal(root, 5, Mock(return_value=True))
            self.assertIn('Kibana: http://192.0.2.10:5601', output.getvalue())

    def test_invalid_node_role_is_rejected_before_address_processing(self):
        value = cloud_contract.InfrastructureResult('selectel', {'project_id': 'own'}, 'otus-dz07',
            {'region': 'ru-9', 'zone': 'ru-9a'}, {'app': {'roles': ['foreign']}}, '10.77.0.0/24')
        with self.assertRaisesRegex(ValueError, 'Invalid node name or roles'):
            value.validate()

    def test_network_port_with_disabled_filtering_is_rejected(self):
        result, data = network_fixtures.ProviderPostcheckMatrix().network_fixture()
        cloud = Mock()
        cloud.get.side_effect = lambda service, path: data[path]
        data['/v2.0/ports/ep']['port']['port_security_enabled'] = False
        with self.assertRaisesRegex(RuntimeError, 'filtering or identity'):
            adapter.check_network(cloud, result, ['8.8.8.8/32'])

    def test_s3_preparation_requires_verified_local_package_before_connecting(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(s3_artifacts, 'settings', return_value={}), \
                    patch.object(s3_artifacts, 'package_lock', return_value={'package.deb': 'a' * 128}), \
                    patch.object(s3_artifacts, 'client') as connect:
                with self.assertRaisesRegex(ValueError, 'verified package is missing'):
                    s3_artifacts.prepare(Path(directory))
            connect.assert_not_called()

    def test_saved_placement_overrides_preferences_and_saved_context_is_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'config').mkdir()
            (root / '.local').mkdir()
            (root / 'config/custom.json').write_text('{"pools":["ru-9","ru-3"]}')
            (root / '.local/context.json').write_text('{"placement":{"region":"ru-3"}}')
            self.assertEqual(pool_selection.policy(root, 'custom.json', 'context.json')['region'], 'ru-3')
            (root / '.local/placement.json').write_text('{"region":"ru-8","availability_zone":"ru-8a"}')
            result = pool_selection.policy(root, 'custom.json', 'context.json')
            self.assertEqual((result['region'], result['availability_zone']), ('ru-8', 'ru-8a'))

    def test_cleanup_audit_retries_until_eventual_resource_absence(self):
        cloud = Mock()
        responses = iter([[{'name': 'otus-dz07-app'}], [], [], []])
        with patch.object(adapter, 'OWNED_COLLECTIONS', [('compute', '/servers', 'servers')]), \
                patch.object(adapter, 'collection', side_effect=lambda *args: next(responses)), \
                patch.object(adapter.time, 'sleep') as sleep:
            self.assertEqual(adapter.audit_cleanup(cloud, 'otus-dz07', set()), ([], 0))
        sleep.assert_called_once_with(3)

    def test_extension_access_denied_and_malformed_response_are_not_support(self):
        import urllib.error
        cloud = Mock()
        cloud.get.side_effect = urllib.error.HTTPError('https://example.invalid', 403, 'Forbidden', {}, None)
        with self.assertRaises(urllib.error.HTTPError):
            adapter._extensions(cloud, 'network', '/v2.0/extensions')
        cloud.get.side_effect = None
        cloud.get.return_value = {'extensions': [{'alias': None}]}
        with self.assertRaisesRegex(ValueError, 'Malformed'):
            adapter._extensions(cloud, 'network', '/v2.0/extensions')

    def test_recovered_verifier_checks_all_committed_records_on_each_replica(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'probes').mkdir()
            run_id = 'a' * 32
            records = {name: {'id': index, 'slug': name, 'body': 'committed'}
                       for index, name in enumerate(('baseline', 'failover', 'recovered'), 1)}
            context = {**records, 'media': {}, 'data': {}}
            (root / 'probes' / (run_id + '.json')).write_text(json.dumps(context))
            (root / 'initial-data-result.json').write_text('{"status":"loaded"}')
            conn = Mock()
            conn.execute.return_value.fetchone.return_value = (10,)
            committed = {'id': 4, 'slug': 'new', 'body': 'committed'}
            members = {'db1': {'role': 'primary', 'state': 'running'}, 'db2': {'role': 'replica', 'state': 'running'}}
            with patch.object(remote_verify, 'initialize'), patch.object(remote_verify, 'APP', root), \
                    patch.object(remote_verify.sys, 'argv', ['verify.py', 'recovered', run_id]), \
                    patch.object(remote_verify, 'lab', {'private_ips': {'db1': '10.77.0.11', 'db2': '10.77.0.12'}}), \
                    patch.object(remote_verify, 'cluster', side_effect=[{}, members]), \
                    patch.object(remote_verify, 'sql_connect', side_effect=lambda *args: contextlib.nullcontext(conn)) as sql, \
                    patch.object(remote_verify, 'check_data'), patch.object(remote_verify, 'check_record') as check, \
                    patch.object(remote_verify, 'check_media'), patch.object(remote_verify, 'check_http_media'), \
                    patch.object(remote_verify, 'check_http'), patch.object(remote_verify, 'insert_record', return_value=committed) as write, \
                    patch.object(remote_verify.time, 'sleep'), patch('sys.stdout', new_callable=io.StringIO) as output:
                remote_verify.main()
            report = json.loads(output.getvalue())
            self.assertTrue(report['replica_records_verified'])
            self.assertEqual(report['preserved_records'], list(records.values()))
            self.assertEqual([call.args for call in sql.call_args_list], [(), ('10.77.0.11',), ('10.77.0.12',)])
            self.assertEqual([call.args[1] for call in check.call_args_list],
                list(records.values()) + (list(records.values()) + [committed]) * 2)
            write.assert_called_once_with('recovered')

    def test_stage_recovery_requires_finishing_destroy_and_unambiguous_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            backend, _ = staged_fixtures.StagedBackendTests().backend(Path(directory))
            backend.journal['destroy_requested'] = True
            with self.assertRaisesRegex(ValueError, 'finish destroy'):
                backend.validate_existing()
            backend.journal['stages']['app'] = {'status': 'deleting'}
            backend.cleanup_attempt = Mock()
            backend.recover_attempt('app')
            backend.cleanup_attempt.assert_called_once_with('app')
            backend.store.entries.return_value = [dict(type='kind', attrs={'id': value}) for value in ('one', 'two')]
            with self.assertRaisesRegex(ValueError, 'Ambiguous'):
                backend.saved_id('app', 'kind', 'output')

    def test_destroy_refuses_lost_stage_configuration_with_remaining_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            backend, _ = staged_fixtures.StagedBackendTests().backend(Path(directory))
            backend.journal['stages']['app'] = {'status': 'failed', 'candidate': {'region': 'ru-9'}}
            backend.store.folder = Mock(return_value=Path(directory) / 'missing-stage')
            backend.store.identities = Mock(return_value={'vm': 'preserve'})
            backend.save = Mock()
            with patch('selectel_staged_audit.assert_stage_owned'), self.assertRaisesRegex(ValueError, 'configuration missing'):
                backend.cleanup_attempt('app')
            self.assertEqual(backend.journal['stages']['app']['status'], 'deleting')

    def test_console_and_key_queries_use_the_saved_node_pool(self):
        backend = Backend.__new__(Backend)
        backend.api = SimpleNamespace(PREFIX='otus-dz07')
        cloud = Mock()
        cloud.ssh_host_key.return_value = 'ssh-ed25519 key'
        cloud.get.return_value = {'output': 'console evidence'}
        backend.region = Mock(return_value=cloud)
        lab = {'node_regions': {'es1': 'ru-3'}, 'server_ids': {'es1': 'saved-id'}}
        self.assertEqual(backend.ssh_host_key(lab, 'es1', None), 'ssh-ed25519 key')
        self.assertEqual(backend.console_output(lab, 'es1'), 'console evidence')
        self.assertEqual([call.args for call in backend.region.call_args_list], [('ru-3',), ('ru-3',)])
        cloud.ssh_host_key.assert_called_once_with('es1', 'saved-id', 'otus-dz07', None)

    def test_stage_provider_requirements_are_preserved_in_generated_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'module'
            source.mkdir()
            api = SimpleNamespace(ROOT=root, LOCAL=root / '.local')
            store = StageStore(api, {'stages': {}}, environment=lambda candidate: {}, cli_config=root / 'cli.rc')
            providers = {'openstack': {'source': 'terraform-provider-openstack/openstack', 'version': '= 3.4.0'}}
            store.configure('app', source, {'name': 'otus-dz07-app'}, providers=providers)
            config = json.loads((store.folder('app') / 'main.tf.json').read_text())
            self.assertEqual(config['terraform']['required_providers'], providers)
            self.assertEqual(config['module']['stage']['name'], 'otus-dz07-app')

    def test_ipv4_summary_reads_only_address_evidence_and_handles_missing_checks(self):
        record = {'checks': {'addresses': {'returncode': 0, 'stdout': json.dumps([
            {'addr_info': [{'family': 'inet6', 'local': '::1'}, {'family': 'inet', 'local': '10.77.0.10'}]}])}}}
        self.assertTrue(network_diagnostics.summary(record, '10.77.0.10')['expected_private_ipv4_present'])
        self.assertFalse(network_diagnostics.summary(record, '10.77.0.11')['expected_private_ipv4_present'])
        self.assertIsNone(network_diagnostics.summary({}, '10.77.0.10')['expected_private_ipv4_present'])

    def test_remote_verifier_reexecutes_in_the_application_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'settings.json').write_text('{"lab":{"private_ips":{}},"passwords":{"app":"synthetic"}}')
            with patch.object(remote_verify, 'BASE', root), patch.object(remote_verify, 'lab', {}), \
                    patch.object(remote_verify, 'passwords', {}), patch.object(remote_verify.sys, 'prefix', 'controller'), \
                    patch.object(remote_verify.os, 'execv') as execute:
                remote_verify.initialize()
                self.assertEqual(remote_verify.passwords['app'], 'synthetic')
                execute.assert_called_once_with(remote_verify.PYTHON,
                    [remote_verify.PYTHON, remote_verify.__file__, *remote_verify.sys.argv[1:]])

    def test_remote_verifier_detects_changed_committed_record(self):
        connection = Mock()
        connection.execute.return_value.fetchone.return_value = (7, 'slug', 'changed')
        with self.assertRaisesRegex(AssertionError, 'absent or changed'):
            remote_verify.check_record(connection, {'id': 7, 'slug': 'slug', 'body': 'original'})
        connection.execute.assert_called_once_with(
            'SELECT id,slug,body FROM knowledge_article WHERE id=%s', (7,))

    def test_remote_verifier_rejects_invalid_mode_before_writing_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(remote_verify, 'initialize'), patch.object(remote_verify, 'APP', Path(directory)), \
                    patch.object(remote_verify.sys, 'argv', ['verify.py', 'invalid', 'a' * 32]):
                with self.assertRaisesRegex(ValueError, 'Invalid verification context'):
                    remote_verify.main()
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_no_placement_candidates_never_allocates_or_cleans_an_existing_node(self):
        backend = Mock(unsafe=True)
        backend.completed.return_value = False
        backend.pending.return_value = None
        backend.identities.return_value = {'es1': 'preserved'}
        with self.assertRaisesRegex(staged_deploy.PlacementExhausted, 'app'):
            staged_deploy.place(backend, 'app', [])
        backend.begin.assert_not_called()
        backend.apply.assert_not_called()
        backend.cleanup_attempt.assert_not_called()
        backend.assert_preserved.assert_called_once_with({'es1': 'preserved'})

    def test_configuration_resume_does_not_rewrite_inputs_or_connect_again(self):
        with tempfile.TemporaryDirectory() as directory:
            api = Mock(ROOT=Path(directory), LOCAL=Path(directory) / '.local')
            with patch.object(configuration, 'resume', return_value=True):
                configuration._configure(api, existing=True)
        api.lab_output.assert_not_called()
        api.connect.assert_not_called()
        api.put.assert_not_called()

    def test_provider_download_success_still_requires_offline_lock_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            api = provider_fixtures.ProviderMirrorTests().fixture(Path(directory))
            results = [subprocess.CompletedProcess([], code, b'', message) for code, message in
                       [(1, b'cache empty'), (0, b''), (1, b'lock mismatch')]]
            with patch.object(terraform_plugins.subprocess, 'run', side_effect=results) as run:
                with self.assertRaises(cloud_contract.TerraformError) as caught:
                    terraform_plugins.prepare(api)
            self.assertEqual(caught.exception.output, 'lock mismatch')
            self.assertNotIn('TF_PLUGIN_CACHE_DIR', run.call_args.kwargs['env'])
            self.assertEqual(list((api.ROOT / '.tools').glob('provider-check-*')), [])

    def test_partial_state_without_segment_is_not_a_valid_cleanup_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('.local/context.json', '.local/placement.json',
                         'terraform-selectel/terraform.tfvars.json'):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('{}')
            with self.assertRaisesRegex(ValueError, 'no segment'):
                adapter.partial_state_binding(root, {'region': 'ru-9'}, 'otus-dz07')

    def test_managed_state_rejects_project_and_pool_independently(self):
        for attrs, expected in (({'tenant_id': 'foreign'}, 'project'),
                                ({'region': 'ru-3'}, 'pool')):
            state = {'resources': [{'mode': 'managed', 'type': 'openstack_compute_instance_v2',
                                   'instances': [{'attributes': attrs}]}]}
            with self.subTest(attrs=attrs), self.assertRaisesRegex(ValueError, expected):
                adapter.validate_managed_state(state, {'project_id': 'own', 'region': 'ru-9'}, 'otus-dz07')

    def test_power_transition_timeout_never_reports_success(self):
        cloud = adapter.SelectelAdapter.__new__(adapter.SelectelAdapter)
        cloud.config = {'project_id': 'own'}
        cloud.get = Mock(return_value={'server': {'name': 'otus-dz07-es1', 'tenant_id': 'own', 'status': 'BUILD'}})
        with patch.object(adapter.time, 'sleep'), self.assertRaisesRegex(RuntimeError, 'unconfirmed'):
            cloud.set_power('es1', 'id', running=True, prefix='otus-dz07')
        self.assertEqual(cloud.get.call_count, 62)

    def test_cached_host_key_is_returned_only_after_identity_check(self):
        cloud = adapter.SelectelAdapter.__new__(adapter.SelectelAdapter)
        cloud.get = Mock(return_value={'server': {'name': 'otus-dz07-app'}})
        self.assertEqual(cloud.ssh_host_key('app', 'id', 'otus-dz07', 'ssh-ed25519 cached'), 'ssh-ed25519 cached')
        cloud.get.assert_called_once()
        cloud.get.return_value = {'server': {'name': 'foreign-app'}}
        with self.assertRaises(ValueError):
            cloud.ssh_host_key('app', 'id', 'otus-dz07', 'ssh-ed25519 cached')

    def test_placement_fault_lookup_error_is_saved_without_exception_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / 'terraform-selectel/terraform.tfstate'
            state.parent.mkdir()
            state.write_text(json.dumps({'resources': [{'mode': 'managed',
                'type': 'openstack_compute_instance_v2', 'instances': [{'index_key': 'app'}]}]}))
            def save(path, value):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(value))
            api = SimpleNamespace(ROOT=root, LOCAL=root / '.local', save=save,
                                  placement=lambda: {'region': 'ru-9'})
            with patch.object(diagnostics, 'collect_nodes', side_effect=OSError('PRIVATE-SECRET')):
                report = json.loads(diagnostics.capture(api, RuntimeError()).read_text())
            self.assertEqual(report['provider_error']['type'], 'OSError')
            self.assertNotIn('PRIVATE', json.dumps(report))

    def test_missing_server_attributes_are_recorded_per_node(self):
        cloud = Mock(config={'project_id': 'own'})
        cloud.get.side_effect = KeyError('PRIVATE')
        report = {'region': 'ru-9', 'nodes': {}}
        api = SimpleNamespace(PREFIX='otus-dz07', Cloud=lambda: cloud)
        diagnostics.collect_nodes(api, [{'index_key': 'app',
            'attributes': {'name': 'otus-dz07-app', 'id': 'id'}}], report)
        self.assertEqual(report['nodes']['app']['diagnostic_error'], 'KeyError')
        self.assertNotIn('PRIVATE', json.dumps(report))

    def test_cycle_secondary_local_guards_remain_read_only(self):
        for mutation, message in (
            ('managed', 'empty stand'), ('state', 'nonempty state'), ('pending', 'existing context')):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                api = cycle_fixtures.CycleSafetyTests().fixture(Path(directory))
                if mutation == 'managed':
                    api.managed_ids.return_value = {'app': 'id'}
                elif mutation == 'state':
                    (api.ROOT / 'terraform-selectel/terraform.tfstate').write_text('{"outputs":{"x":1}}')
                else:
                    (api.LOCAL / 'context.json').write_text('{}')
                with patch.object(cycle, 'assert_empty'), self.assertRaisesRegex(RuntimeError, message):
                    cycle.reject_existing(api)
                api.Cloud.assert_not_called()
                api.cleanup.assert_not_called()

    def test_cycle_attaches_only_structured_error_evidence_before_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            api = cycle_fixtures.CycleSafetyTests().fixture(Path(directory))
            error = RuntimeError('PRIVATE')
            error.api_error_context = {'service': 'network', 'http_status': 409}
            error.failure_evidence = 'docs/fault.json'
            with patch.object(cycle, 'reject_existing'), patch.object(cycle, 'stages', side_effect=error):
                with self.assertRaises(RuntimeError) as caught:
                    cycle.run(api)
            self.assertIs(caught.exception, error)
            report = json.loads((api.ROOT / 'docs/test-cycle-result.json').read_text())
            self.assertEqual(report['api_error_context']['http_status'], 409)
            self.assertEqual(report['failure_evidence'], 'docs/fault.json')
            self.assertNotIn('PRIVATE', json.dumps(report))

    def test_cleanup_failure_links_evidence_and_keeps_recovery_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            api = cycle_fixtures.CycleSafetyTests().fixture(Path(directory))
            error = RuntimeError('PRIVATE')
            error.failure_evidence = 'docs/cleanup-fault.json'
            api.cleanup.side_effect = error
            with patch.object(cycle, 'reject_existing'), patch.object(cycle, 'stages', return_value={}):
                with self.assertRaises(RuntimeError):
                    cycle.run(api)
            report = json.loads((api.ROOT / 'docs/test-cycle-result.json').read_text())
            self.assertEqual(report['cleanup_failure_evidence'], error.failure_evidence)
            self.assertFalse(report['state_and_api_cleanup_verified'])
            self.assertEqual(json.loads((api.LOCAL / 'test-cycle.json').read_text())['status'], 'cleanup_failed')




