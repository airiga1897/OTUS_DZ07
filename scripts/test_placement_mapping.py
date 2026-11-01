"""Provider scheduling domains and disk bindings, entirely synthetic."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import pool_selection as pools

ROOT = Path(__file__).resolve().parents[1]


class PlacementMappingTests(unittest.TestCase):
    def policy(self):
        return {'network_placement': {'ru-3b': {'network': 'ru-3a', 'router': 'ru-3a',
                 'evidence_url': 'https://docs.selectel.ru/infrastructure/locations/'}}}

    def test_compute_segment_can_use_documented_pool_network_domain(self):
        result = pools.network_placement(self.policy(), 'ru-3', 'ru-3b')
        self.assertEqual(result, {'network_availability_zone': 'ru-3a', 'router_availability_zone': 'ru-3a'})
        self.assertEqual(pools.network_placement({}, 'ru-9', 'ru-9a')['network_availability_zone'], 'ru-9a')

    def test_cross_pool_or_unexplained_mapping_is_rejected(self):
        for values in ({'network': 'ru-9a', 'router': 'ru-3a', 'evidence_url': 'https://docs.selectel.ru/x'},
                       {'network': 'ru-3a', 'router': 'ru-3a'}):
            with self.assertRaises(ValueError):
                pools.network_placement({'network_placement': {'ru-3b': values}}, 'ru-3', 'ru-3b')

    def test_disk_priority_uses_api_binding_instead_of_suffix_alone(self):
        types = [{'id': 'u', 'name': 'universal.ru-3b'}, {'id': 'b', 'name': 'basicssd.ru-3b'}]
        get = Mock(return_value={'extra_specs': {'RESKEY:availability_zones': 'ru-3b'}})
        self.assertEqual(pools.disk_for_zone(types, ['universal', 'basicssd'], 'ru-3b', get), 'universal.ru-3b')
        get.return_value = {'extra_specs': {'RESKEY:availability_zones': 'ru-3a'}}
        with self.assertRaises(pools.CandidateUnavailable):
            pools.disk_for_zone(types, ['universal', 'basicssd'], 'ru-3b', get)
        get.return_value = {'extra_specs': {}}
        with self.assertRaises(pools.CandidateUnavailable):
            pools.disk_for_zone(types, ['universal', 'basicssd'], 'ru-3b', get)
        self.assertEqual(pools.disk_for_zone(types[1:], ['universal', 'basicssd'], 'ru-3b',
                         Mock(return_value={'extra_specs': {'RESKEY:availability_zones': 'ru-3b'}})), 'basicssd.ru-3b')

    def test_masks_never_cross_segment_and_do_not_enable_universal2(self):
        types = [{'name': 'universal.ru-3a', 'extra_specs': {'RESKEY:availability_zones': 'ru-3a'}},
                 {'name': 'universal2.ru-3b', 'extra_specs': {'RESKEY:availability_zones': 'ru-3b'}},
                 {'name': 'basicssd.ru-3b', 'extra_specs': {'RESKEY:availability_zones': 'ru-3b'}}]
        get = Mock()
        self.assertEqual(pools.disk_for_zone(types, ['universal.*', 'basicssd.*'], 'ru-3b', get), 'basicssd.ru-3b')
        get.assert_not_called()

    def test_network_mapping_is_part_of_saved_ownership_and_partial_state(self):
        from selectel_adapter import binding, partial_state_binding
        with tempfile.TemporaryDirectory(dir=ROOT / '.local') as directory:
            root = Path(directory)
            (root / 'terraform-selectel').mkdir()
            choice = {'region': 'ru-3', 'availability_zone': 'ru-3b',
                      'network_availability_zone': 'ru-3a', 'router_availability_zone': 'ru-3a'}
            pools.select(root, choice, 'synthetic', 'context.json')
            variables = {**choice, 'name_prefix': 'otus-dz07'}
            path = root / 'terraform-selectel/terraform.tfvars.json'
            path.write_text(json.dumps(variables))
            config = {'project_id': 'synthetic', 'region': 'ru-3'}
            expected = binding(config, 'otus-dz07', 'ru-3b')
            self.assertEqual(expected['placement']['network_zone'], 'ru-3a')
            self.assertEqual(partial_state_binding(root, config, 'otus-dz07'), expected)
            variables['network_availability_zone'] = 'ru-3b'
            path.write_text(json.dumps(variables))
            with self.assertRaises(ValueError):
                partial_state_binding(root, config, 'otus-dz07')

    def test_fault_is_saved_before_cleanup_without_credentials(self):
        import placement_diagnostics
        with tempfile.TemporaryDirectory(dir=ROOT / '.local') as directory:
            root = Path(directory)
            (root / 'terraform-selectel').mkdir()
            state = {'resources': [{'mode': 'managed', 'type': 'openstack_compute_instance_v2',
                'instances': [{'index_key': 'es1', 'attributes': {'id': 'synthetic-id', 'name': 'otus-dz07-es1',
                                'tenant_id': '', 'region': None}}]}]}
            (root / 'terraform-selectel/terraform.tfstate').write_text(json.dumps(state))
            api = Mock(ROOT=root, LOCAL=root / '.local', PREFIX='otus-dz07')
            api.placement.return_value = {'region': 'ru-3', 'availability_zone': 'ru-3b'}
            cloud = api.Cloud.return_value
            cloud.config = {'project_id': 'synthetic-project', 'password': 'synthetic-password'}
            cloud.get.return_value = {'server': {'name': 'otus-dz07-es1', 'tenant_id': 'synthetic-project', 'status': 'ERROR',
                'fault': {'code': 500, 'message': 'NoValidHost', 'details': 'Traceback synthetic-password',
                          'password': 'synthetic-password'}}}
            api.save.side_effect = lambda path, value: (path.parent.mkdir(parents=True, exist_ok=True), path.write_text(json.dumps(value)))
            path = placement_diagnostics.capture(api, RuntimeError('create failed'))
            text = path.read_text()
            self.assertIn('NoValidHost', text)
            self.assertIn('Traceback', text)
            self.assertNotIn('synthetic-password', text)
            self.assertEqual(json.loads(text)['region'], 'ru-3')
            self.assertEqual(json.loads(text)['nodes']['es1']['fault']['code'], 500)
            cloud.get.assert_called_once_with('compute', '/servers/synthetic-id')
            import urllib.error
            cloud.get.side_effect = urllib.error.HTTPError('https://synthetic.invalid', 403, 'Forbidden', {}, None)
            unavailable = json.loads(placement_diagnostics.capture(api, RuntimeError('failed')).read_text())
            self.assertEqual(unavailable['nodes']['es1']['http_status'], 403)
            self.assertNotIn('fault', unavailable['nodes']['es1'])
            cloud.get.reset_mock(side_effect=True)
            state['resources'][0]['instances'][0]['attributes']['tenant_id'] = 'foreign-project'
            (root / 'terraform-selectel/terraform.tfstate').write_text(json.dumps(state))
            refused = json.loads(placement_diagnostics.capture(api, RuntimeError('failed')).read_text())
            self.assertIn('ownership mismatch', refused['nodes']['es1']['diagnostic_error'])
            cloud.get.assert_not_called()
