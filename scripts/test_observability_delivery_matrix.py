"""Check delivery modes and log probes with SSH transports replaced locally."""
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

import observability as elk

ROOT=Path(__file__).resolve().parents[1]


class ObservabilityDeliveryMatrix(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory(dir=ROOT/'.tools')
        self.addCleanup(directory.cleanup)
        self.root=Path(directory.name)
        shutil.copytree(ROOT/'config',self.root/'config')
        (self.root/'scripts/remote').mkdir(parents=True)
        (self.root/'scripts/remote/check_cloud_init.py').write_text('synthetic cloud init probe')
        self.local=self.root/'.local'
        (self.local/'certs').mkdir(parents=True)
        for name in ('ca.crt','es1.crt','es1.key'):(self.local/'certs'/name).write_text('synthetic certificate')
        self.lab={'private_ips':{'app':'10.77.0.10','es1':'10.77.0.31'},'server_ids':{'app':'app-id','es1':'es-id'},
                  'node_roles':{'app':'app','es1':'es'},'etcd_members':[]}
        self.secrets=dict.fromkeys(('elastic','shipper','kibana','kibana_encryption'),'synthetic')
        self.api=SimpleNamespace(ROOT=self.root,LOCAL=self.local,PREFIX='otus-dz07',STACK={'paths':{'remote_root':'/synthetic'}},
            remote=Mock(),put=Mock(),upload_tree=Mock(),connect=MagicMock(),lab_output=lambda:self.lab,save=Mock())

    def test_cached_and_internet_packages_have_exact_manifest_before_distribution(self):
        services=json.loads((self.root/'config/services.json').read_text())
        # A one-node fixture tests transport selection, not cluster formation.
        services['initial_masters']=['es1']
        lock=json.loads((self.root/'config/elastic-artifacts.json').read_text())
        for mode in ('cached','internet'):
            services['artifact_delivery']=mode
            (self.root/'config/services.json').write_text(json.dumps(services))
            self.api.remote.reset_mock();self.api.put.reset_mock()
            self.api.remote.side_effect=lambda client,command,label,**kw: json.dumps(lock) if label=='verified-package-manifest' else ''
            with patch('elk_tls.certificates'),patch('artifact_transport.cached_packages') as cache:
                elk.stage(self.api,Mock(),self.lab,self.secrets)
            if mode=='cached':cache.assert_called_once()
            else:
                cache.assert_not_called()
                self.assertIn('official-package-download',[call.args[2] for call in self.api.remote.call_args_list])
            distributed=[call for call in self.api.put.call_args_list if call.args[1].endswith('/packages/manifest.json')]
            self.assertEqual(len(distributed),2)
            self.assertTrue(all(json.loads(call.args[2])==lock for call in distributed))
        self.api.remote.side_effect=lambda client,command,label,**kw: '{}' if label=='verified-package-manifest' else ''
        with patch('elk_tls.certificates'),self.assertRaisesRegex(ValueError,'pinned hashes'):
            elk.stage(self.api,Mock(),self.lab,self.secrets)

    def test_workload_only_inventory_is_preserved_and_remote_command_parses_json(self):
        result=Mock()
        result.application.return_value={'private_ips':{'app':'10.77.0.10'}}
        value={'all':{'children':{}}}
        self.assertIs(elk.inventory(result,{},value),value)
        self.api.remote.return_value='{"healthy":true}'
        self.assertEqual(elk.command(self.api,'status',name='es1'),{'healthy':True})
        self.assertEqual(self.api.remote.call_args.args[2],'elk-status')
        self.assertEqual(self.api.connect.call_count,2)

    def test_log_probe_writes_sql_marker_on_separate_database_and_saves_result(self):
        self.lab['private_ips']['db1']='10.77.0.21'
        self.lab['server_ids']['db1']='db-id'
        self.lab['node_roles']['db1']='db'
        with patch.object(elk,'command',side_effect=[{'sources_verified':True},{'available':True}]) as command:
            result=elk.verify(self.api)
        self.assertTrue(result['kibana']['available'])
        self.assertIn('sql-log-db1',[call.args[2] for call in self.api.remote.call_args_list])
        self.api.save.assert_called_once_with(self.root/'docs/logs-verification.json',result)
        self.assertEqual(command.call_args.args[1],'kibana-check')

    def test_optional_failover_delegates_without_using_global_pool_client(self):
        self.api.Cloud=Mock()
        with patch('elastic_options.failover',side_effect=RuntimeError('restore unconfirmed')) as check, \
                self.assertRaisesRegex(RuntimeError,'restore unconfirmed'):
            elk.failover(self.api)
        check.assert_called_once_with(self.api)
        self.api.Cloud.assert_not_called()
