"""Safe retained scheduler evidence and semantic-empty normalization."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from plan_diagnostics import hints, summarize
from selectel_refresh import _scheduler
from stage_store import StageStore
from test_node_refresh import fixture

ROOT = Path(__file__).resolve().parents[1]


class SchedulerEvidence(unittest.TestCase):
    def test_equivalent_empty_defaults_keep_the_configured_group(self):
        inputs = {'require_distinct_hosts': True, 'server_group_id': 'own-group'}
        old = [{'group': 'own-group', 'additional_properties': None}]
        new = [{'group': 'own-group', 'additional_properties': {}, 'same_host': [], 'target_cell': ''}]
        self.assertTrue(_scheduler(old, new, inputs, 'es'))
        self.assertTrue(_scheduler(new, old, inputs, 'es'))

    def test_scheduler_cannot_change_group_or_add_placement_constraints(self):
        inputs = {'require_distinct_hosts': True, 'server_group_id': 'own-group'}
        good = [{'group': 'own-group'}]
        for bad in (None, {}, [], [None], good * 2, [{}], [{'group': 'foreign'}],
                    [{'group': 'own-group', 'unknown': ''}], [{'group': 'own-group', 'same_host': ['foreign']}],
                    [{'group': 'own-group', 'target_cell': False}], [{'group': 'own-group', 'query': ''}]):
            with self.subTest(bad=bad):
                self.assertFalse(_scheduler(good, bad, inputs, 'es'))
                self.assertFalse(_scheduler(bad, good, inputs, 'es'))
        for policy in ({}, {'require_distinct_hosts': False}, {'require_distinct_hosts': True},
                       {'require_distinct_hosts': True, 'server_group_id': 5}):
            self.assertFalse(_scheduler(good, good, policy, 'es'))
        self.assertFalse(_scheduler(good, good, inputs, 'app'))

    def test_backend_accepts_only_equivalent_scheduler_defaults_with_noop_plan(self):
        b, drifts, entries = fixture()
        b.desired['nodes']['app']['role'] = 'es'
        b.journal['stages']['app']['inputs'].update(require_distinct_hosts=True, server_group_id='own-group')
        d = drifts[1]
        d['change']['before']['scheduler_hints'] = [{'group': 'own-group', 'additional_properties': None}]
        d['change']['after']['scheduler_hints'] = [{'group': 'own-group', 'additional_properties': {}}]
        self.assertTrue(b.allow_refresh('app', d))
        d['change']['after']['scheduler_hints'][0]['group'] = 'foreign'
        self.assertFalse(b.allow_refresh('app', d))

    def test_structural_evidence_never_contains_arbitrary_values(self):
        value = [{'group': 'secret-group', 'query': ['PASSWORD-DO-NOT-SAVE'],
                  'additional_properties': {'secret': 'TOKEN-DO-NOT-SAVE'}, 'secret-field-name': 'hidden'}]
        result = hints(value, 'secret-group')
        serialized = json.dumps(result)
        for secret in ('secret-group', 'PASSWORD', 'TOKEN', 'secret-field-name'):
            self.assertNotIn(secret, serialized)
        self.assertTrue(result['items'][0]['group_matches_saved'])
        self.assertEqual(result['items'][0]['unknown_fields'], 1)
        self.assertFalse(result['items'][0]['fields']['query']['empty'])
        self.assertEqual(hints(None, None), {'type': 'NoneType'})
        self.assertEqual(hints([None], '')['items'], [{'type': 'NoneType'}])

    def test_guard_saves_scheduler_shapes_before_apply_and_cleanup(self):
        with tempfile.TemporaryDirectory(dir=ROOT / '.tools') as folder:
            root = Path(folder)
            api = SimpleNamespace(ROOT=root, LOCAL=root / '.local', redact=lambda v:v, _TEST_CYCLE_ID='synthetic')
            inputs = {'server_group_id': 'PRIVATE-GROUP', 'require_distinct_hosts': True}
            store = StageStore(api, {'stages': {'es1': {'inputs': inputs}}})
            before = {'id': 'PRIVATE-VM', 'scheduler_hints': [{'group': 'PRIVATE-GROUP'}], 'user_data': 'PRIVATE-PASSWORD'}
            after = copy.deepcopy(before); after['scheduler_hints'][0]['group'] = 'PRIVATE-FOREIGN'
            plan = {'resource_drift': [{'address': 'vm', 'type': 'openstack_compute_instance_v2',
                     'change': {'actions': ['update'], 'before': before, 'after': after}}]}
            store.identities = Mock(return_value={'vm': 'PRIVATE-VM'})
            store.tf = Mock(side_effect=lambda name, op, *args: json.dumps(plan) if op == 'show' else '')
            with self.assertRaises(ValueError) as caught:
                store.apply('es1', unchanged=True)
            evidence = (root / caught.exception.failure_evidence).read_text()
            self.assertNotIn('PRIVATE-', evidence)
            summary = json.loads(json.loads(evidence)['terraform_error'])
            details = summary['resource_drift'][0]['scheduler_hints']
            self.assertTrue(details['before']['items'][0]['group_matches_saved'])
            self.assertFalse(details['after']['items'][0]['group_matches_saved'])
            self.assertNotIn('apply', [c.args[1] for c in store.tf.call_args_list])
            self.assertTrue((root / caught.exception.failure_evidence).exists())
            # The real local cleanup keeps public evidence after state/API have
            # been confirmed absent by the isolated test fixture.
            from local_runtime import cleanup_runtime
            cleanup_runtime(root)
            self.assertTrue((root / caught.exception.failure_evidence).exists())

    def test_non_scheduler_evidence_remains_minimal(self):
        plan = {'resource_changes': [{'address': 'disk', 'type': 'volume', 'change': {
            'actions': ['create'], 'before': None, 'after': {'password': 'SECRET'}}}]}
        result = summarize(plan, {})
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertEqual(result['resource_changes'][0]['changed_fields'], ['password'])
