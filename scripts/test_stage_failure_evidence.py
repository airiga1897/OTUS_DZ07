"""Original Terraform failures survive cleanup; provider diagnostics remain read-only."""
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error

from cloud_contract import TerraformError
from local_runtime import cleanup_runtime
import privacy
from stage_store import StageStore
from selectel_staged_audit import faults

ROOT = Path(__file__).resolve().parents[1]


class StageFailureEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=ROOT / '.tools')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / '.local').mkdir()
        (self.root / 'terraform-selectel').mkdir()
        self.api = SimpleNamespace(ROOT=self.root, LOCAL=self.root / '.local',
            RUN=self.root / '.local/runs/test', TF=self.root / 'synthetic-terraform',
            redact=privacy.redact, _TEST_CYCLE_ID='synthetic-run')
        self.store = StageStore(self.api, {'stages': {'connection-ru-3': {'candidate': {'region': 'ru-3'}}}},
            environment=lambda candidate: {}, cli_config=self.root / 'terraform-selectel.rc')
        secrets = patch.object(privacy, '_values', set())
        secrets.start()
        self.addCleanup(secrets.stop)

    def fail(self, output):
        with patch('stage_store.subprocess.run', return_value=subprocess.CompletedProcess([], 1, b'', output.encode())):
            with self.assertRaises(TerraformError) as caught:
                self.store.tf('connection-ru-3', 'apply', 'stage.tfplan')
        return caught.exception

    def test_original_error_is_saved_before_destroy_and_survives_local_cleanup(self):
        error = self.fail("Network status ERROR; wanted ACTIVE; request failed")
        evidence = self.root / error.failure_evidence
        before = evidence.read_bytes()
        self.assertEqual(json.loads(before)['run_id'], 'synthetic-run')
        self.assertEqual(json.loads(before)['placement'], {'region': 'ru-3'})
        with patch('stage_store.subprocess.run', return_value=subprocess.CompletedProcess([], 0, b'Destroy complete', b'')):
            self.store.tf('connection-ru-3', 'apply', 'destroy.tfplan')
        log = (self.api.RUN / 'stage-connection-ru-3-apply.log').read_text()
        self.assertIn('Network status ERROR', log)
        self.assertIn('Destroy complete', log)
        cleanup_runtime(self.root)
        self.assertEqual(evidence.read_bytes(), before)
        self.assertFalse(self.api.RUN.exists())

    def test_multiple_failures_are_unique_and_sensitive_details_are_removed(self):
        privacy.remember(['synthetic-password'])
        output = ('ERROR synthetic-password X-Auth-Token: opaque-token '
                  'https://example.invalid/net?signature=opaque-signature '
                  'resource 11111111-1111-4111-8111-111111111111 public 8.8.8.8')
        first, second = self.fail(output), self.fail('cleanup failed')
        self.assertNotEqual(first.failure_evidence, second.failure_evidence)
        saved = (self.root / first.failure_evidence).read_text()
        for secret in ('synthetic-password', 'opaque-token', 'opaque-signature', '8.8.8.8',
                       '11111111-1111-4111-8111-111111111111'):
            self.assertNotIn(secret, saved)
        self.assertIn('ERROR', saved)

    def test_diagnostic_storage_failure_does_not_replace_original_error(self):
        with patch('placement_diagnostics.capture_terraform', side_effect=PermissionError('synthetic denied')):
            error = self.fail('original network ERROR')
        self.assertEqual(error.output, 'original network ERROR')
        self.assertEqual(error.diagnostics_error_type, 'PermissionError')
        self.assertEqual(error.terraform_stage, 'connection-ru-3')

    def backend(self):
        entry = {'type': 'selectel_global_router_vpc_network_v1', 'attrs': {'id': 'synthetic-network'}}
        return SimpleNamespace(store=SimpleNamespace(entries=lambda name: [entry]),
                               owner='synthetic-owner', global_router=Mock())

    def test_global_router_error_details_are_read_only_and_allowlisted(self):
        backend = self.backend()
        backend.global_router.get.return_value = {'id': 'synthetic-network', 'tags': ['synthetic-owner'],
            'status': 'ERROR', 'error_message': 'backend provisioning failed',
            'fault': {'message': 'synthetic detail'}, 'account_id': 'not-for-evidence'}
        with patch('selectel_staged_audit.assert_stage_owned') as owned:
            report = faults(backend, 'connection-ru-3')
        owned.assert_called_once_with(backend, 'connection-ru-3')
        backend.global_router.get.assert_called_once_with('/networks/synthetic-network')
        item = report['global_router']['selectel_global_router_vpc_network_v1']
        self.assertEqual(item['status'], 'ERROR')
        self.assertEqual(item['error_message'], 'backend provisioning failed')
        self.assertNotIn('account_id', item)

    def test_foreign_resources_are_not_queried_for_details(self):
        backend = self.backend()
        with patch('selectel_staged_audit.assert_stage_owned', side_effect=ValueError('foreign resource')):
            with self.assertRaises(ValueError): faults(backend, 'connection-ru-3')
        backend.global_router.get.assert_not_called()

    def test_api_forbidden_is_reported_without_inventing_a_provider_fault(self):
        backend = self.backend()
        backend.global_router.get.side_effect = urllib.error.HTTPError('https://example.invalid', 403, 'Forbidden', {}, None)
        with patch('selectel_staged_audit.assert_stage_owned'):
            report = faults(backend, 'connection-ru-3')
        item = report['global_router']['selectel_global_router_vpc_network_v1']
        self.assertEqual(item['http_status'], 403)
        self.assertNotIn('fault', item)
