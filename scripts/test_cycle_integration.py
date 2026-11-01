"""Full controller cycle: real JSON/SSH, canary and verify orchestration; provider IO is synthetic."""
import contextlib
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import lab
import test_cycle
import topology
import verification
from local_runtime import cleanup_runtime
from test_remote_streams import Channel

ROOT = Path(__file__).resolve().parents[1]


class CycleIntegrationTests(unittest.TestCase):
    def exercise(self, fault=None):
        with tempfile.TemporaryDirectory(dir=ROOT / '.tools') as folder:
            root=Path(folder);local=root/'.local';local.mkdir();(root/'config').mkdir()
            shutil.copyfile(ROOT/'config/services.json',root/'config/services.json')
            (root/'terraform-selectel').mkdir()
            desired=topology.load(ROOT)
            ids={};events=[]
            value={'private_ips':{n:'10.77.0.'+str(v['host']) for n,v in desired['nodes'].items()},
                'node_roles':{n:v['role'] for n,v in desired['nodes'].items()},'etcd_members':[],
                'server_ids':{n:n+'-original' for n in desired['nodes']}}
            canary={'id':17,'slug':'private-probe','body':'committed'}
            def answer(command):
                if 'verify.py expanded' in command: return {'preserved_records':[canary]}
                if 'verify.py baseline' in command: return {'new_record':canary,'leader':'app','sql_and_http':True}
                if 'elk_api.py verify' in command: return {'all_log_sources':True}
                if 'elk_api.py kibana-check' in command: return {'status':'available'}
                return {'ok':True}
            class Worker(Channel):
                def exec_command(self,command):
                    events.append(command)
                    self.out.append(json.dumps(answer(command)).encode())
                    self.err.append(b'HTTP method=POST status=302\n')
                    if fault=='logs' and 'elk_api.py verify' in command: self.status=1
            client=SimpleNamespace(get_transport=lambda:SimpleNamespace(open_session=lambda **kw:Worker([],[])))
            def save(path,data):path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(data))
            def deploy():
                events.append('initial-deploy');ids.update(value['server_ids']);(local/'key').write_text('synthetic-key')
            def repeat():
                events.append('repeat-deploy')
                if fault=='identity':ids['app']='replacement'
            def cleanup():
                events.append('cleanup')
                if fault=='cleanup':raise RuntimeError('API deletion unconfirmed')
                ids.clear();cleanup_runtime(root)
            api=SimpleNamespace(ROOT=root,LOCAL=local,PREFIX='otus-dz07',STACK={'paths':{'remote_root':'/synthetic'}},
                _TEST_CYCLE_ID=None,_TOPOLOGY_OVERRIDE=None,desired_topology=lambda:desired,
                managed_ids=lambda:dict(ids),lab_output=lambda:value,deploy_selected=deploy,deploy=repeat,
                connect=lambda *args:contextlib.nullcontext(client),upload_tree=lambda *args:None,
                remote=lab.remote,save=save,cleanup=cleanup,Cloud=Mock())
            api.Cloud.return_value.resource_regions.return_value=[]
            api.verify=lambda failover:verification.verify(api,failover)
            with patch.multiple(lab,ROOT=root,LOCAL=local,RUN=local/'runs',_AUTH_CONFIG=None), \
                 patch('observability.topology.services',return_value={'kibana_host':'app'}):
                if fault:
                    with self.assertRaises(RuntimeError):test_cycle.run(api)
                else:test_cycle.run(api)
            report=json.loads((root/'docs/test-cycle-result.json').read_text())
            self.assertEqual(events[-1],'cleanup')
            api.Cloud.return_value.set_power.assert_not_called()
            if fault=='cleanup':
                self.assertFalse(report['state_and_api_cleanup_verified'])
                self.assertTrue((local/'key').exists())
            else:
                self.assertFalse(ids);self.assertFalse((local/'key').exists())
                self.assertTrue(report['state_and_api_cleanup_verified'])
            return report,events

    def test_full_cycle_checks_site_json_documents_logs_kibana_and_repeat(self):
        report,events=self.exercise()
        self.assertTrue(report['success']);self.assertTrue(report['repeat_deploy_verified'])
        self.assertTrue(report['elasticsearch_documents_preserved'])
        self.assertIn('repeat-deploy',events)
        self.assertTrue(any('kibana-check' in event for event in events))

    def test_replacement_and_log_failure_are_not_reported_successful(self):
        for fault in ('identity','logs','cleanup'):
            with self.subTest(fault=fault):
                report,events=self.exercise(fault)
                self.assertFalse(report['success'])
