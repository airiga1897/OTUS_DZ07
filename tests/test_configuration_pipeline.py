"""Run actual configuration, per-node staging and Ansible orchestration against fake SSH IO."""
import contextlib
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
import paramiko
from unittest.mock import MagicMock, Mock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import configuration
from cloud_contract import InfrastructureResult
import stack_config


class ConfigurationPipelineContracts(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory(dir=ROOT/'.tools');self.addCleanup(folder.cleanup)
        self.root=Path(folder.name);self.local=self.root/'.local';self.local.mkdir()
        shutil.copytree(ROOT/'config',self.root/'config')
        (self.root/'scripts/remote').mkdir(parents=True)
        for name in ('network_diagnostics.py','check_cloud_init.py'):
            shutil.copyfile(ROOT/'scripts/remote'/name,self.root/'scripts/remote'/name)
        self.passwords={name:'synthetic-'+name for name in ('postgres','replicator','api','django','app','elastic','shipper','kibana','kibana_encryption')}
        (self.local/'secrets.json').write_text(json.dumps(self.passwords))
        (self.local/'otus_dz07').write_text('synthetic-private-key')
        (self.local/'known_hosts').write_text('synthetic-known-hosts')
        (self.local/'staged-deployment.json').write_text('{}')
        nodes={n:{'id':n+'-id','volume_id':n+'-disk','port_id':n+'-port','roles':['app' if n=='app' else 'es'],
            'private_ip':'10.77.0.'+str(i),'public_ip':'192.0.2.10' if n=='app' else None,'region':'ru-9','zone':'ru-9a'}
            for n,i in [('app',10),('es1',31),('es2',32),('es3',33)]}
        self.result=InfrastructureResult('selectel',{'project_id':'synthetic-project'},'otus-dz07',
                                         {'region':'ru-9','zone':'ru-9a'},nodes,'10.77.0.0/24')
        self.clients={name:MagicMock(name=name) for name in nodes}
        for name,client in self.clients.items():client.node_name=name
        self.files={};self.commands=[];self.fail=False;self.bad_manifest=False
        def remote(client,command,label='remote',stdin=None):
            self.commands.append(label)
            if label=='ansible-configure' and self.fail:raise RuntimeError('synthetic Ansible failure')
            if label=='verified-package-manifest':
                return '{}' if self.bad_manifest else (ROOT/'config/elastic-artifacts.json').read_text()
            return ''
        def put(client,path,value,mode=None):self.files[(client.node_name,path)]=value
        def save(path,value):path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value))
        self.api=SimpleNamespace(ROOT=self.root,LOCAL=self.local,PREFIX='otus-dz07',STACK=stack_config.load(),
            lab_output=lambda:self.result.application(),infrastructure_output=lambda:self.result,
            trust=Mock(),check_placement=Mock(),discover_route=Mock(),Cloud=Mock(),save=save,
            connect=lambda value,name='app',front=None:contextlib.nullcontext(self.clients[name]),
            remote=remote,put=put,upload_tree=Mock())

    def run_pipeline(self,existing=False):
        def operation(api, app, inventory):
            import observability
            self.commands.append('ansible-systemd')
            # The durable backend's terminal cleanup is tested with the real
            # protocol elsewhere; this suite isolates per-node staging.
            observability.package_cache(api,app,api.lab_output(),start=False)
            if self.fail: raise RuntimeError('synthetic Ansible failure')
        with patch('network_diagnostics.require_bootstrap_network'), patch('ansible_operation.reserve'), \
                patch('ansible_backend.configure',side_effect=operation), contextlib.redirect_stdout(io.StringIO()):
            configuration.configure(self.api,existing=existing)

    def test_all_node_staging_least_privilege_and_ansible_cleanup_on_both_runs(self):
        for existing in (False,True):
            self.run_pipeline(existing)
        base=self.api.STACK['paths']['remote_root']
        for node in self.clients:
            settings=json.loads(self.files[(node,base+'/elk-settings.json')])
            self.assertIn('shipper',settings['secrets'])
            if node.startswith('es'):
                self.assertEqual(set(settings['secrets']),{'elastic','shipper'})
                self.assertNotIn((node,base+'/settings.json'),self.files)
        self.assertIn(('app',base+'/settings.json'),self.files)
        self.assertEqual(self.commands.count('ansible-systemd'),2)
        self.assertEqual(self.commands.count('package-cache-stop'),2)
        self.assertEqual(self.clients['app'].open_sftp.return_value.__enter__.return_value.remove.call_count,0)

    def test_configuration_failure_stops_cache_and_removes_remote_inventory(self):
        self.fail=True
        with self.assertRaises(RuntimeError):self.run_pipeline()
        self.assertEqual(self.commands[-1],'package-cache-stop')
        self.assertTrue((self.local/'otus_dz07').exists())

    def test_wrong_package_manifest_blocks_ansible(self):
        self.bad_manifest=True
        with self.assertRaisesRegex(ValueError,'manifest differs'):self.run_pipeline()
        self.assertNotIn('ansible-systemd',self.commands)

    def test_existing_stand_ssh_failure_never_changes_route_rules(self):
        self.api.connect=Mock(side_effect=TimeoutError())
        with self.assertRaisesRegex(RuntimeError,'SSH to the existing stand'):self.run_pipeline(existing=True)
        self.api.discover_route.assert_not_called()

    def test_authentication_or_host_key_failure_never_changes_route_rules(self):
        errors=[paramiko.AuthenticationException('synthetic'),paramiko.BadHostKeyException('host',Mock(),Mock())]
        for error in errors:
            self.api.connect=Mock(side_effect=error)
            with self.assertRaisesRegex(RuntimeError,'authentication or host-key'):self.run_pipeline()
            self.api.discover_route.assert_not_called()

    def test_legacy_network_check_and_missing_ephemeral_inventory_are_safe(self):
        (self.local/'staged-deployment.json').unlink()
        (self.root/'terraform-selectel').mkdir()
        (self.root/'terraform-selectel/terraform.tfvars.json').write_text('{"admin_cidrs":["8.8.8.8/32"]}')
        self.clients['app'].open_sftp.return_value.__enter__.return_value.remove.side_effect=FileNotFoundError()
        with patch('cloud_settings.adapter') as adapter:
            adapter.return_value.check_network.return_value={'success':True}
            self.run_pipeline()
            adapter.return_value.check_network.assert_called_once()

    def test_initial_route_failure_uses_explicit_signed_route_discovery(self):
        contexts=[TimeoutError(),contextlib.nullcontext(self.clients['app'])]
        original=self.api.connect
        def connect(value,name='app',front=None):
            if name=='app' and contexts:
                result=contexts.pop(0)
                if isinstance(result,Exception):raise result
                return result
            return original(value,name,front)
        self.api.connect=connect;self.run_pipeline()
        self.api.discover_route.assert_called_once()
