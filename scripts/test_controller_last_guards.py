"""Controller safety checks with an isolated clone and no live credentials."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock, patch

import lab
import selectel_adapter
import test_controller_boundaries as fixtures
import staged_runtime


class ControllerLastGuards(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ControllerBoundaries()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_reauthentication_validates_existing_context_before_overwrite(self):
        api = self.fixture
        with patch.object(lab, 'placement', return_value=api.choice):
            lab.bind_context(api.config)
            original = (api.local / 'context.json').read_bytes()
            lab.bind_context(api.config)
            self.assertEqual((api.local / 'context.json').read_bytes(), original)
            with self.assertRaisesRegex(ValueError, 'scope'):
                lab.bind_context({**api.config, 'project_id': 'foreign'})
            self.assertEqual((api.local / 'context.json').read_bytes(), original)

    def test_successful_terraform_without_capture_does_not_return_private_stdout(self):
        with patch.object(lab, 'auth_config', return_value=self.fixture.config), \
                patch.object(lab.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, b'private stdout', b'')), \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertIsNone(lab.tf('plan'))
        self.assertIn('Terraform plan: OK', output.getvalue())
        self.assertNotIn('private stdout', output.getvalue())

    def test_upload_routes_release_sources_through_the_private_manifest(self):
        client = Mock()
        with patch.object(lab.topology, 'services', return_value={'artifact_delivery': 'release'}), \
                patch('artifact_transport.source', return_value='delivered') as source:
            self.assertEqual(lab.upload_tree(client), 'delivered')
        source.assert_called_once_with(lab, client, 'app')
        client.open_sftp.assert_not_called()

    def test_placement_check_uses_staged_backend_and_persists_only_result(self):
        result = {'success': True}
        provider = Mock()
        provider.verify_placement.return_value = result
        with patch('stage_store.deployment_exists', return_value=True), \
                patch('staged_runtime.backend', return_value=provider), patch.object(lab, 'save') as save:
            self.assertEqual(lab.check_placement({'server_ids': {}}), result)
        save.assert_called_once_with(self.fixture.root / 'docs/network-result.json', result)

    def test_requested_purge_is_separate_from_verified_stand_cleanup(self):
        events = []
        with patch.object(lab, 'cleanup', side_effect=lambda: events.append('cleanup')), \
                patch('artifact_lifecycle.purge', side_effect=lambda root: events.append('purge')):
            lab.destroy(purge_artifacts=True)
        self.assertEqual(events, ['cleanup', 'purge'])
        with patch.object(lab, 'cleanup', side_effect=RuntimeError('unconfirmed')), \
                patch('artifact_lifecycle.purge') as purge, self.assertRaises(RuntimeError):
            lab.destroy(purge_artifacts=True)
        purge.assert_not_called()

    def test_backend_cache_does_not_construct_a_second_provider(self):
        with patch.object(staged_runtime, 'Backend') as factory:
            api = type('Api', (), {})()
            api._STAGED_BACKEND = object()
            self.assertIs(staged_runtime.backend(api), api._STAGED_BACKEND)
            factory.assert_not_called()
            del api._STAGED_BACKEND
            self.assertIs(staged_runtime.backend(api), factory.return_value)
            factory.assert_called_once_with(api)

    def test_selectel_entrypoints_preserve_provider_specific_dispatch(self):
        api = object()
        with patch('selectel_staged.Backend', return_value='backend') as backend:
            self.assertEqual(selectel_adapter.staged_backend(api, cleanup=True), 'backend')
        backend.assert_called_once_with(api, cleanup=True)
        with patch('selectel_staged.reject_shared_namespace') as reject:
            selectel_adapter.reject_shared_namespace(api)
        reject.assert_called_once_with(api)

