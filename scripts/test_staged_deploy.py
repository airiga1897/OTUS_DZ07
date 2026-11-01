"""Placement retry and recovery contracts, with no provider or real clone state."""
import unittest
from unittest.mock import Mock

import staged_deploy


class StagedDeploymentTests(unittest.TestCase):
    def backend(self):
        backend = Mock(unsafe=True)
        backend.completed.return_value = False
        backend.identities.return_value = {'app': 'existing-app'}
        backend.apply.return_value = {'node': 'new-id'}
        backend.is_capacity_error.return_value = True
        backend.pending.return_value = None
        return backend

    def test_retry_cleans_only_failed_attempt_before_next_candidate(self):
        backend = self.backend()
        backend.apply.side_effect = [RuntimeError('capacity'), {'node': 'new-id'}]
        staged_deploy.place(backend, 'es2', [{'zone': 'a'}, {'zone': 'b'}])
        backend.cleanup_attempt.assert_called_once_with('es2')
        backend.assert_preserved.assert_called_with({'app': 'existing-app'})
        self.assertEqual(backend.apply.call_count, 2)
        self.assertLess(backend.mock_calls.index(unittest.mock.call.capture_fault('es2')),
                        backend.mock_calls.index(unittest.mock.call.cleanup_attempt('es2')))

    def test_failed_cleanup_blocks_retry_and_retains_context(self):
        backend = self.backend()
        backend.apply.side_effect = RuntimeError('capacity')
        backend.cleanup_attempt.side_effect = RuntimeError('audit failed')
        with self.assertRaisesRegex(RuntimeError, 'audit failed'):
            staged_deploy.place(backend, 'es2', [{'zone': 'a'}, {'zone': 'b'}])
        self.assertEqual(backend.apply.call_count, 1)
        backend.finish.assert_not_called()

    def test_active_placement_never_uses_new_candidates(self):
        backend = self.backend()
        backend.completed.return_value = True
        staged_deploy.place(backend, 'app', [{'zone': 'different'}])
        backend.reapply.assert_called_once_with('app')
        backend.begin.assert_not_called()
        backend.cleanup_attempt.assert_not_called()

    def test_noncapacity_error_preserves_partial_attempt(self):
        backend = self.backend()
        backend.apply.side_effect = RuntimeError('permission')
        backend.is_capacity_error.return_value = False
        with self.assertRaisesRegex(RuntimeError, 'permission'):
            staged_deploy.place(backend, 'es2', [{'zone': 'a'}, {'zone': 'b'}])
        backend.cleanup_attempt.assert_not_called()

    def test_resume_pending_attempt_before_considering_alternatives(self):
        backend = self.backend()
        backend.pending.return_value = {'zone': 'saved'}
        staged_deploy.place(backend, 'es1', [{'zone': 'new'}])
        backend.begin.assert_not_called()
        backend.apply.assert_called_once_with('es1')
        backend.finish.assert_called_once_with('es1')

    def test_candidate_shape_is_owned_by_the_provider(self):
        backend = self.backend()
        staged_deploy.place(backend, 'node', [{'provider_specific_option': 'opaque'}])
        backend.begin.assert_called_once_with('node', {'provider_specific_option': 'opaque'})
        backend.finish.assert_called_once_with('node')
