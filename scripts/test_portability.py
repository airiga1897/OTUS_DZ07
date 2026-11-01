"""Provider boundaries and fail-closed discovery; synthetic data only."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error
from unittest.mock import Mock, patch

from cloud_contract import CapabilityStatus, InfrastructureResult, require_capabilities, validate_binding
from selectel_adapter import SelectelAdapter, discover_capabilities, audit_cleanup, check_network, preflight


class PortabilityTests(unittest.TestCase):
    def cloud(self, network=None, compute=None):
        cloud = Mock()
        def get(service, path, payload=None):
            if path == '/extensions':
                return {'extensions': [{'alias': a} for a in (compute or [])]}
            if path == '/v2.0/extensions':
                return {'extensions': [{'alias': a} for a in (network or [])]}
            if path == '/os-server-groups':
                return {'server_groups': []}
            if path == '/v2.0/security-groups':
                return {'security_groups': []}
            raise AssertionError(path)
        cloud.get.side_effect = get
        return cloud

    def test_declared_required_properties_and_missing_extension(self):
        cloud = self.cloud(['security-group', 'port-security', 'network_availability_zone', 'router_availability_zone'], ['os-server-groups'])
        found = discover_capabilities(cloud)
        require_capabilities(found, ['network_isolation', 'hard_anti_affinity', 'placement_hints'])
        self.assertEqual(found['network_isolation'].status, CapabilityStatus.SUPPORTED)
        found = discover_capabilities(self.cloud())
        self.assertEqual(found['hard_anti_affinity'].status, CapabilityStatus.UNSUPPORTED)
        with self.assertRaises(ValueError):
            require_capabilities(found, ['network_isolation'])

    def test_staged_ownership_requires_declared_network_router_tags(self):
        for aliases, expected in ((['standard-attr-tag'], CapabilityStatus.SUPPORTED),
                                  (['tag', 'tag-ext'], CapabilityStatus.SUPPORTED),
                                  ([], CapabilityStatus.UNSUPPORTED)):
            result = discover_capabilities(self.cloud(aliases))
            self.assertEqual(result['ownership_tags'].status, expected)

    def test_staged_backend_factory_delegates_to_the_selected_provider(self):
        import cloud_settings
        provider = Mock()
        api = Mock()
        with patch('cloud_settings.adapter', return_value=provider):
            self.assertIs(cloud_settings.staged_backend(api, cleanup=True), provider.staged_backend.return_value)
        provider.staged_backend.assert_called_once_with(api, cleanup=True)

    def test_404_discovery_is_unknown_but_declared_operation_error_propagates(self):
        error = urllib.error.HTTPError('https://example.invalid', 404, 'Not found', {}, None)
        cloud = self.cloud()
        cloud.get.side_effect = error
        found = discover_capabilities(cloud)
        self.assertEqual(found['network_isolation'].status, CapabilityStatus.UNKNOWN)
        with self.assertRaises(ValueError):
            require_capabilities(found, ['network_isolation'])
        cloud = self.cloud(['security-group', 'port-security'], ['os-server-groups'])
        original = cloud.get.side_effect
        def get(service, path, payload=None):
            if path == '/os-server-groups':
                raise error
            return original(service, path, payload)
        cloud.get.side_effect = get
        with self.assertRaises(urllib.error.HTTPError):
            discover_capabilities(cloud)

    def test_permission_and_network_failures_are_not_success(self):
        for error in [urllib.error.HTTPError('https://example.invalid', 403, 'Forbidden', {}, None), TimeoutError()]:
            cloud = self.cloud()
            cloud.get.side_effect = error
            with self.assertRaises(type(error)):
                discover_capabilities(cloud)

    def test_unknown_or_unsupported_property_blocks_preflight_before_quotas(self):
        from types import SimpleNamespace
        from pool_selection import CandidateUnavailable
        root = Path(__file__).resolve().parents[1]
        api = SimpleNamespace(ROOT=root, STACK={}, PREFIX='otus-dz07', Cloud=Mock(),
                              placement=Mock(), desired_topology=Mock(),managed_ids=Mock(),save=Mock())
        with patch('selectel_adapter.discover_capabilities', return_value={}):
            with self.assertRaises(CandidateUnavailable):
                preflight(api)
        api.Cloud.return_value.get.assert_not_called()
        api.save.assert_not_called()

    def test_power_checks_name_and_scope_before_mutation(self):
        cloud = SelectelAdapter.__new__(SelectelAdapter)
        cloud.config={'project_id':'own'}
        cloud.get=Mock(return_value={'server':{'name':'otus-dz05-db1','tenant_id':'own'}})
        with self.assertRaises(ValueError):
            cloud.set_power('db1','vm',running=False,prefix='otus-dz07')
        self.assertEqual(cloud.get.call_count,1)
        cloud.get.reset_mock()
        cloud.get.return_value={'server':{'name':'otus-dz07-db1','tenant_id':'foreign'}}
        with self.assertRaises(ValueError):
            cloud.set_power('db1','vm',running=False,prefix='otus-dz07')
        self.assertEqual(cloud.get.call_count,1)

    def result(self):
        return dict(provider='selectel', scope={'project_id': 'synthetic'}, namespace='otus-dz07',
                    placement={'region': 'ru-7', 'zone': 'ru-7a'}, subnet_cidr='10.77.0.0/24',
                    nodes={'app': dict(roles=['app'], id='app-id', private_ip='10.77.0.10', public_ip='192.0.2.1', volume_id='v1', port_id='p1'),
                           'db1': dict(roles=['db', 'etcd'], id='db-id', private_ip='10.77.0.21', volume_id='v2', port_id='p2')})

    def test_contract_validates_roles_addresses_and_unique_ids(self):
        result = InfrastructureResult.from_dict(self.result())
        self.assertEqual(result.application()['etcd_members'], ['db1'])
        for field, value in [('id', 'app-id'), ('private_ip', '10.77.0.10'), ('roles', [])]:
            bad = copy.deepcopy(self.result())
            bad['nodes']['db1'][field] = value
            with self.assertRaises(ValueError):
                InfrastructureResult.from_dict(bad)

    def test_provider_scope_namespace_and_placement_cannot_switch(self):
        own = {k: self.result()[k] for k in ('provider', 'scope', 'namespace', 'placement')}
        validate_binding(own, own)
        for field, value in [('provider', 'yc'), ('scope', {'project_id': 'foreign'}),
                             ('namespace', 'otus-dz05'), ('placement', {'region': 'ru-9', 'zone': 'ru-9a'})]:
            other = {**own, field: value}
            with self.assertRaises(ValueError):
                validate_binding(own, other)

    def test_network_postcheck_detects_weakened_filtering_and_wrong_zone(self):
        result = InfrastructureResult.from_dict(self.result())
        rule = lambda protocol, low, high, cidr, group: dict(direction='ingress', ethertype='IPv4', protocol=protocol,
            port_range_min=low, port_range_max=high, remote_ip_prefix=cidr, remote_group_id=group)
        data = {'/v2.0/security-groups': {'security_groups': [dict(name='otus-dz07', id='sg', security_group_rules=[
            rule(None,None,None,None,'sg'),rule('tcp',80,80,'0.0.0.0/0',None),rule('tcp',22,22,'192.0.2.2/32',None)])]},
            '/v2.0/routers': {'routers': [dict(name='otus-dz07',availability_zone_hints=['ru-7a'],availability_zones=['ru-7a'])]},
            '/v2.0/networks/net': {'network': dict(shared=False,port_security_enabled=True,**{'router:external':False},
                availability_zone_hints=['ru-7a'],availability_zones=['ru-7a'])},
            '/v2.0/floatingips': {'floatingips': []}}
        for name, node in result.nodes.items():
            data['/v2.0/ports/'+node['port_id']] = {'port':dict(name='otus-dz07-'+name,network_id='net',
                port_security_enabled=True,security_groups=['sg'],fixed_ips=[{'ip_address':node['private_ip']}])}
        cloud = Mock()
        cloud.get.side_effect = lambda service,path: data[path]
        self.assertTrue(check_network(cloud,result,['192.0.2.2/32'])['success'])
        data['/v2.0/ports/p2']['port']['port_security_enabled'] = False
        with self.assertRaises(RuntimeError):
            check_network(cloud,result,['192.0.2.2/32'])
        data['/v2.0/ports/p2']['port']['port_security_enabled'] = True
        data['/v2.0/routers']['routers'][0]['availability_zones'] = ['ru-7b']
        with self.assertRaises(RuntimeError):
            check_network(cloud,result,['192.0.2.2/32'])
        from selectel_adapter import binding
        from dataclasses import replace
        result = replace(result, placement=binding({'project_id': 'synthetic', 'region': 'ru-3'}, 'otus-dz07', 'ru-3b')['placement'])
        for resource in (data['/v2.0/routers']['routers'][0], data['/v2.0/networks/net']['network']):
            resource.update(availability_zone_hints=['ru-3a'], availability_zones=['ru-3a'])
        self.assertTrue(check_network(cloud, result, ['192.0.2.2/32'])['success'])
        data['/v2.0/routers']['routers'][0]['availability_zones'] = ['ru-3b']
        with self.assertRaises(RuntimeError):
            check_network(cloud, result, ['192.0.2.2/32'])

    def test_cleanup_audit_does_not_match_neighbor_namespace_and_propagates_errors(self):
        cloud = Mock()
        cloud.get.return_value = {'servers':[{'name':'otus-dz05-db1'},{'name':'otus-dz070-db1'}],
            'server_groups':[], 'volumes':[], 'networks':[], 'subnets':[], 'routers':[], 'ports':[],
            'security_groups':[], 'floatingips':[]}
        self.assertEqual(audit_cleanup(cloud,'otus-dz07',set()),([],0))
        cloud.get.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            audit_cleanup(cloud,'otus-dz07',set())

    def test_foreign_context_prevents_local_open_and_preserves_recovery_files(self):
        from dataclasses import asdict
        from local_runtime import open_portal, validate_state
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / '.local') as directory:
            root=Path(directory)
            (root/'.local').mkdir()
            (root/'terraform-selectel').mkdir()
            result=self.result()
            state={'resources':[dict(mode='managed',type='openstack_compute_instance_v2',instances=[
                dict(index_key='app',attributes=dict(id='app-id',name='otus-dz07-app',tenant_id='synthetic',region='ru-7'))])],
                'outputs':{'infrastructure':{'value':result}}}
            (root/'terraform-selectel/terraform.tfstate').write_text(json.dumps(state))
            context={k:result[k] for k in ('provider','scope','namespace','placement')}
            context['provider']='yc'
            (root/'.local/context.json').write_text(json.dumps(context))
            key=root/'.local/key';key.write_text('synthetic')
            with self.assertRaises(ValueError):
                validate_state(root,{'project_id':'synthetic','region':'ru-7'},'otus-dz07')
            opener=Mock()
            with self.assertRaises(ValueError):
                open_portal(root,6,opener)
            opener.assert_not_called()
            self.assertTrue(key.exists())

    def test_foreign_provider_state_blocks_cycle_and_local_cleanup(self):
        from types import SimpleNamespace
        from local_runtime import cleanup_runtime
        import test_cycle
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / '.local') as directory:
            root=Path(directory)
            (root/'.local').mkdir()
            (root/'terraform-selectel').mkdir()
            (root/'terraform-other').mkdir()
            state={'resources':[dict(mode='managed',instances=[{'attributes':{'id':'foreign'}}])]}
            (root/'terraform-other/terraform.tfstate').write_text(json.dumps(state))
            key=root/'.local/key';key.write_text('synthetic')
            api=SimpleNamespace(ROOT=root,LOCAL=root/'.local',managed_ids=Mock(return_value={}),Cloud=Mock())
            with self.assertRaises(RuntimeError):
                test_cycle.reject_existing(api)
            api.Cloud.assert_not_called()
            with self.assertRaises(ValueError):
                cleanup_runtime(root)
            self.assertTrue(key.exists())

    def test_pool_selection_never_overwrites_foreign_provider_context(self):
        import pool_selection
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / '.local') as directory:
            root=Path(directory)
            (root/'.local').mkdir()
            (root/'terraform-selectel').mkdir()
            saved=dict(project_id='synthetic',provider='other',region='ru-7',phase='prepared',namespace='otus-dz07')
            context={k:self.result()[k] for k in ('provider','scope','namespace','placement')}
            context['provider']='other'
            (root/'.local/placement.json').write_text(json.dumps(saved))
            path=root/'.local/context.json';path.write_text(json.dumps(context))
            original=path.read_bytes()
            with self.assertRaises(ValueError):
                pool_selection.select(root,{'region':'ru-7','availability_zone':'ru-7a'},'synthetic','context.json')
            self.assertEqual(path.read_bytes(),original)

    def test_collectstatic_runs_as_application_user_without_ansible_temp_acl(self):
        import yaml
        root = Path(__file__).resolve().parents[1]
        tasks = yaml.safe_load((root / 'ansible/roles/portal/tasks/main.yml').read_text())
        task = next(t for t in tasks if t['name'] == 'Collect static assets')
        self.assertNotIn('become_user', task)
        self.assertEqual(task['ansible.builtin.command']['argv'][:4], ['/usr/sbin/runuser', '-u', '{{ stack.portal.service_user }}', '--'])
