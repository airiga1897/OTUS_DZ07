"""An interrupted checked /32 rule addition must be retried, never mistaken for applied."""
import copy
import unittest
from unittest.mock import Mock

from selectel_staged import Backend


class AdministrativeAccessRecoveryTests(unittest.TestCase):
    def backend(self):
        backend = Backend.__new__(Backend)
        backend.journal = {'admin_cidrs': ['8.8.8.8/32'], 'stages': {'network-ru-3': {
            'inputs': {}, 'output': {'security_group_id': 'own-security-group'}}}}
        backend.store = Mock();backend.configure_stage = Mock();backend.finish = Mock()
        backend.snapshots = []
        backend.save = lambda: backend.snapshots.append(copy.deepcopy(backend.journal))
        return backend

    def test_failed_apply_remains_pending_then_retry_applies_once_without_duplicate_rules(self):
        backend = self.backend();backend.store.apply.side_effect = [RuntimeError('temporary failure'), {}]
        lab = {'node_regions': {'app': 'ru-3'}}
        with self.assertRaises(RuntimeError): backend.extend_admin_access(lab, '1.1.1.1/32')
        self.assertEqual(backend.snapshots[-1]['admin_access_pending'], '1.1.1.1/32')
        backend.extend_admin_access(lab, '1.1.1.1/32')
        self.assertEqual(backend.store.apply.call_count, 2)
        self.assertEqual(backend.journal['admin_cidrs'], ['8.8.8.8/32', '1.1.1.1/32'])
        self.assertNotIn('admin_access_pending', backend.journal)
        backend.extend_admin_access(lab, '1.1.1.1/32')
        self.assertEqual(backend.store.apply.call_count, 2)

    def test_different_request_cannot_overwrite_unfinished_access_change(self):
        backend = self.backend();backend.journal['admin_access_pending'] = '1.1.1.1/32'
        with self.assertRaises(ValueError): backend.extend_admin_access({}, '9.9.9.9/32')
        backend.configure_stage.assert_not_called();backend.store.apply.assert_not_called()

    def test_checked_plan_rejects_existing_resource_changes(self):
        backend = self.backend()
        def unsafe_plan(name, validate_extra):
            validate_extra({'resource_changes': [{'mode': 'managed', 'type': 'openstack_compute_instance_v2',
                'change': {'actions': ['delete'], 'before': {'id': 'existing-app'}}}]})
        backend.store.apply.side_effect = unsafe_plan
        with self.assertRaises(ValueError):
            backend.extend_admin_access({'node_regions': {'app': 'ru-3'}}, '1.1.1.1/32')
        backend.finish.assert_not_called()
        self.assertEqual(backend.journal['admin_access_pending'], '1.1.1.1/32')
