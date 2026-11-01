"""Synthetic local state and cleanup contracts; no cloud services."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
(ROOT / '.local').mkdir(exist_ok=True)
sys.path.insert(0, str(ROOT / 'scripts'))
from local_runtime import cleanup_runtime, open_portal, validate_state


class RuntimeContract(unittest.TestCase):
    def fixture(self, root, homework):
        local = root / '.local'
        local.mkdir()
        tf = root / 'terraform-selectel'
        tf.mkdir()
        prefix = f'otus-dz0{homework}'
        name = 'es3' if homework == 7 else 'app'
        attrs = dict(id='vm-1', name=prefix + '-' + name, tenant_id='project', region='ru-9')
        value = {name: dict(id='vm-1', public_ip='192.0.2.10')} if homework == 7 else dict(server_ids={name: 'vm-1'}, app_public_ip='192.0.2.10')
        state = dict(lineage='own', resources=[dict(mode='managed', type='openstack_compute_instance_v2', instances=[dict(index_key=name, attributes=attrs)])], outputs=dict(lab=dict(value=value)))
        context = dict(project_id='project', region='ru-9', availability_zone='ru-9a', phase='active', deployment_id='deployment', state_lineage='own')
        if homework == 6:
            from selectel_adapter import infrastructure_result
            from dataclasses import asdict
            full = {**value, 'private_ips': {'app':'10.77.0.10'}, 'etcd_members': [],
                    'subnet_cidr':'10.77.0.0/24', 'volume_ids':{'app':'v1'},'port_ids':{'app':'p1'}}
            contract = infrastructure_result(full, {'project_id':'project','region':'ru-9'},prefix,'ru-9a')
            state['outputs']['infrastructure'] = {'value': asdict(contract)}
            context.update(contract.binding())
        (tf / 'terraform.tfstate').write_text(json.dumps(state))
        (tf / 'terraform.tfvars.json').write_text(json.dumps(dict(availability_zone='ru-9a')))
        context_name = {5: 'deployment.json', 6: 'context.json', 7: 'selectel-context.json'}[homework]
        (local / context_name).write_text(json.dumps(context))
        if homework == 5:
            value['deployment_id'] = 'deployment'
        (local / 'lab.json').write_text(json.dumps(value))
        return local, tf, state

    def test_open_needs_no_auth_and_refuses_stale_or_missing_state(self):
        for homework in (5, 6, 7):
            with self.subTest(homework=homework), tempfile.TemporaryDirectory(dir=ROOT / '.local') as d:
                root = Path(d)
                local, tf, state = self.fixture(root, homework)
                opener = Mock(return_value=True)
                self.assertEqual(open_portal(root, homework, opener), 'http://192.0.2.10')
                opener.assert_called_once()
                state['resources'][0]['instances'][0]['attributes']['id'] = 'replacement'
                (tf / 'terraform.tfstate').write_text(json.dumps(state))
                with self.assertRaisesRegex(ValueError, 'контекста'):
                    open_portal(root, homework, Mock())
                (tf / 'terraform.tfstate').unlink()
                with self.assertRaisesRegex(ValueError, 'контекста'):
                    open_portal(root, homework, Mock())

    def test_cleanup_keeps_inputs_and_credentials_only_after_empty_state(self):
        with tempfile.TemporaryDirectory(dir=ROOT / '.local') as d:
            root = Path(d)
            local, tf, state = self.fixture(root, 6)
            for name in ('operator.json', 'lab-config.json', 'secrets.json', 'key'):
                (local / name).write_text('{}')
            with self.assertRaises(ValueError):
                cleanup_runtime(root)
            self.assertTrue((local / 'key').exists())
            state['resources'] = []
            (tf / 'terraform.tfstate').write_text(json.dumps(state))
            (tf / 'terraform.tfstate.backup').write_text('{}')
            cleanup_runtime(root)
            self.assertEqual({p.name for p in local.iterdir()}, {'operator.json', 'lab-config.json'})
            self.assertFalse((tf / 'terraform.tfstate').exists())
            self.assertFalse((tf / 'terraform.tfvars.json').exists())

    def test_foreign_state_refused(self):
        with tempfile.TemporaryDirectory(dir=ROOT / '.local') as d:
            root = Path(d)
            local, tf, state = self.fixture(root, 6)
            for config, prefix in [(dict(project_id='other', region='ru-9'), 'otus-dz07'),
                                   (dict(project_id='project', region='ru-7'), 'otus-dz07'),
                                   (dict(project_id='project', region='ru-9'), 'otus-dz07')]:
                with self.assertRaises(ValueError):
                    validate_state(root, config, prefix)

    def test_cleanup_rejects_links_before_removing_recovery_data(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory(dir=ROOT / '.local') as d:
            root = Path(d)
            (root / '.local').mkdir()
            (root / 'terraform-selectel').mkdir()
            key = root / '.local/key'
            key.write_text('synthetic')
            with patch('local_runtime.linked', side_effect=lambda p: p == key):
                with self.assertRaises(ValueError):
                    cleanup_runtime(root)
            self.assertTrue(key.exists())


if __name__ == '__main__':
    (ROOT / '.local').mkdir(exist_ok=True)
    unittest.main()
