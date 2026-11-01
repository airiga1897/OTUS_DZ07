"""Resumption bypasses provisioning; stop confirmation precedes any VM delete."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import staged_runtime
from ansible_operation import OperationUnknown
import test_staged_lifecycle as fixtures

ROOT = Path(__file__).resolve().parents[1]


class OperationLifecycle(unittest.TestCase):
    def test_cycle_unknown_connection_and_stop_preserve_resources_and_cleanup_marker(self):
        import test_cycle
        with tempfile.TemporaryDirectory(dir=ROOT/'.tools') as directory:
            helper=fixtures.RetainedLifecycleTest();api,provider=helper.fixture(Path(directory))
            provider.journal['stages']={'app':{'status':'active','candidate':{'region':'ru-9'}}}
            provider.objects={'app':'owned-app'};provider.save()
            api._TOPOLOGY_OVERRIDE=None
            api.cleanup=lambda:staged_runtime.cleanup(api)
            provider.cleanup_attempt=Mock()
            with helper.patches(provider),patch.object(test_cycle,'reject_existing'), \
                    patch.object(test_cycle,'stages',side_effect=OperationUnknown('connection unavailable')), \
                    patch('ansible_operation.before_destroy',side_effect=OperationUnknown('stop unconfirmed')), \
                    self.assertRaises(OperationUnknown):
                test_cycle.run(api)
            provider.cleanup_attempt.assert_not_called();self.assertTrue(provider.path.exists())
            marker=json.loads((api.LOCAL/'test-cycle.json').read_text())
            self.assertEqual(marker['status'],'cleanup_failed')
            report=json.loads((api.ROOT/'docs/test-cycle-result.json').read_text())
            self.assertFalse(report['success']);self.assertFalse(report['state_and_api_cleanup_verified'])
            self.assertEqual(report['cleanup_error_type'],'OperationUnknown')

    def test_resume_finalizes_saved_configuration_without_any_provisioning(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'.tools') as directory:
            helper=fixtures.RetainedLifecycleTest();api,provider=helper.fixture(Path(directory));provider.save()
            with patch('configuration.resume',return_value=True),patch('staged_runtime.checked_backend') as backend, \
                    patch('ansible_operation.finalize') as finalize:
                staged_runtime.deploy(api)
            backend.assert_not_called();api.configure.assert_not_called();finalize.assert_called_once_with(api)
            self.assertTrue(json.loads(provider.path.read_text())['configured'])

    def test_resume_preservation_is_checked_before_operation_is_finalized(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'.tools') as directory:
            helper=fixtures.RetainedLifecycleTest();api,provider=helper.fixture(Path(directory));provider.save()
            (api.LOCAL/'expansion-pending.json').write_text('{"canary":"synthetic"}')
            with patch('configuration.resume',return_value=True),patch('verification.expansion',side_effect=RuntimeError('canary mismatch')), \
                    patch('ansible_operation.finalize') as finalize,self.assertRaises(RuntimeError):
                staged_runtime.deploy(api)
            finalize.assert_not_called();self.assertFalse(json.loads(provider.path.read_text())['configured'])

    def test_unknown_stop_blocks_every_vm_deletion_and_keeps_recovery(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'.tools') as directory:
            helper=fixtures.RetainedLifecycleTest();api,provider=helper.fixture(Path(directory));provider.save()
            provider.cleanup_attempt=Mock()
            with helper.patches(provider),patch('ansible_operation.before_destroy',side_effect=OperationUnknown('offline')), \
                    self.assertRaises(OperationUnknown):
                staged_runtime.cleanup(api)
            provider.cleanup_attempt.assert_not_called();self.assertTrue(provider.path.exists())
            report=json.loads((api.ROOT/'docs/cleanup-result.json').read_text())
            self.assertFalse(report['success']);self.assertTrue(report['recovery_files_preserved'])

    def test_stop_is_confirmed_before_nodes_and_local_cleanup(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'.tools') as directory:
            helper=fixtures.RetainedLifecycleTest();api,provider=helper.fixture(Path(directory))
            provider.journal['stages']={n:{'status':'active','candidate':{'region':'ru-9'}} for n in ('app','es1')}
            provider.objects={'app':'owned-app','es1':'owned-es1'}
            provider.audit_deletion=Mock(return_value={})
            with helper.patches(provider),patch('artifact_lifecycle.release'), \
                    patch('ansible_operation.before_destroy',side_effect=lambda *a:provider.events.append(('stop-confirmed',))):
                staged_runtime.cleanup(api)
            self.assertEqual(provider.events[0],('stop-confirmed',))
            self.assertFalse(provider.objects);self.assertFalse(provider.path.exists())
