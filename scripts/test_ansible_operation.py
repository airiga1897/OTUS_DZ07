"""Real private operation files, mock SSH/systemd, no live stand access."""
from contextlib import nullcontext
import copy
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

import ansible_operation as controller
from remote import ansible_job as worker

ROOT = Path(__file__).resolve().parents[1]


class Clock:
    def __init__(self): self.value = 0
    def monotonic(self): return self.value
    def sleep(self, amount): self.value += amount


class DurableAnsibleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=ROOT / '.tools'); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name); self.local = self.root / '.local'; self.local.mkdir()
        for folder in ('config', 'ansible', 'scripts/remote', 'app'): (self.root / folder).mkdir(parents=True)
        self.options = dict(operation_timeout_seconds=1800, connection_wait_seconds=2, poll_interval_seconds=1, stop_wait_seconds=1)
        (self.root / 'config/ansible-operation.json').write_text(json.dumps(self.options))
        (self.root / 'config/services.json').write_text('{"artifact_delivery":"s3"}')
        (self.root / 'config/stack.json').write_text('{"ansible":{"venv":"/test/ansible"}}')
        (self.local / 'staged-deployment.json').write_text(json.dumps({'deployment_id':'a'*32,'namespace':'otus-dz07'}))
        (self.local / 'otus_dz07').write_text('PRIVATE-KEY-SENTINEL')
        (self.local / 'known_hosts').write_text('trusted-host')
        self.lab = {'server_ids':{'app':'own-app'}}
        self.api = SimpleNamespace(ROOT=self.root, LOCAL=self.local, PREFIX='otus-dz07',
            STACK={'paths':{'remote_root':str(self.root / 'vm')}}, lab_output=lambda:self.lab)
        self.inventory = {'all':{'vars':{'dz07_bootstrap':True,'password':'PRIVATE-PASSWORD-SENTINEL'}}}
        self.api.connect = Mock(side_effect=lambda lab:MagicMock())
        def put(client, name, content):
            path=Path(name); path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(content.encode() if isinstance(content,str) else content)
        self.api.put = Mock(side_effect=put)
        # Public bootstrap driver is shipped by the controller, never read from
        # another homework or a real VM.
        (self.root / 'scripts/remote/ansible_job.py').write_text((ROOT / 'scripts/remote/ansible_job.py').read_text())
        (self.root / 'vm').mkdir()
        self.manager = worker.Manager(self.root / 'vm')
        for folder in ('config','ansible','scripts/remote'): (self.manager.root / folder).mkdir(parents=True)
        (self.manager.root / 'config/stack.json').write_text((self.root / 'config/stack.json').read_text())
        self.unit = {'loaded':False,'active':False}; self.launches = 0; self.status_calls = 0
        self.complete = True; self.loss = None; self.lost = False
        self.clock = Clock()
        for patcher in (patch.object(worker,'locked',side_effect=lambda *a,**k:nullcontext()),
                        patch.object(worker.Manager,'boot',return_value='same-boot'),
                        patch.object(self.manager,'unit',side_effect=lambda request:dict(self.unit)),
                        patch.object(controller,'command',side_effect=self.transport),
                        patch.object(controller,'put_private',side_effect=lambda client,name,content:self.api.put(client,name,content)),
                        patch.object(controller.time,'monotonic',side_effect=self.clock.monotonic),
                        patch.object(controller.time,'sleep',side_effect=self.clock.sleep),
                        patch.object(worker.subprocess,'run',side_effect=self.systemd)):
            patcher.start(); self.addCleanup(patcher.stop)

    def systemd(self, args, **kwargs):
        if 'systemd-run' in args:
            self.launches += 1; self.unit.update(loaded=True,active=True)
            self.assertIn('--property=KillMode=control-group',args)
            self.assertNotIn('PRIVATE-',repr(args))
        elif 'stop' in args: self.unit['active']=False
        return SimpleNamespace(returncode=0,stdout='',stderr='')

    def transport(self, client, text, data=None, timeout=15):
        if data is None: return ''
        action, request = data['action'],data['request']
        if action=='start' and self.loss=='before-start' and not self.lost:
            self.lost=True; raise OSError('WIFI-DISCONNECTED')
        result=getattr(self.manager,action)(request)
        if action=='start' and self.loss=='after-start' and not self.lost:
            self.lost=True; raise OSError('WIFI-DISCONNECTED')
        if action=='status' and self.unit['active']:
            self.status_calls += 1
            if self.complete and self.status_calls >= 2:
                path=self.manager.folder(request['id'])/'result.json'
                result.update(id=request['id'],parameters_sha256=request['parameters_sha256'],state='succeeded',
                    exit_code=0,started_utc='2026-10-31T00:00:00+00:00',finished_utc='2026-10-31T00:00:01+00:00')
                worker.atomic(path,result); self.unit['active']=False
        return json.dumps(result)

    def reserve(self): return controller.reserve(self.api,self.lab,self.inventory)

    def test_disconnect_before_start_confirmation_does_not_duplicate_ansible(self):
        self.reserve(); self.loss='before-start'
        result=controller.wait(self.api,self.lab,self.inventory,upload=True)
        self.assertEqual(result['state'],'succeeded'); self.assertEqual(self.launches,1)

    def test_lost_reply_after_launch_observes_the_same_systemd_unit(self):
        self.reserve(); identity=controller.read(self.api)['request']['id']; self.loss='after-start'
        result=controller.wait(self.api,self.lab,self.inventory,upload=True)
        self.assertEqual(self.launches,1); self.assertEqual(controller.read(self.api)['request']['id'],identity)
        self.assertEqual(result['state'],'succeeded')

    def test_restart_controller_after_disconnection_resumes_without_new_launch(self):
        self.reserve(); self.complete=False
        request=controller.read(self.api)['request']
        # Upload and start as if the first controller lost its connection.
        for name,content in controller.payload(self.api,self.inventory).items():
            self.api.put(None,str(self.manager.folder(request['id'])/name),content)
        self.manager.start(request)
        self.api.connect.side_effect=OSError('PRIVATE-ERROR-SENTINEL')
        with self.assertRaises(controller.OperationUnknown): controller.wait(self.api,self.lab,self.inventory)
        self.assertEqual(self.launches,1); self.assertEqual(controller.read(self.api)['state'],'unknown')
        self.api.connect.side_effect=lambda lab:MagicMock(); self.complete=True
        restarted=SimpleNamespace(**vars(self.api))
        with patch('observability.package_cache'):
            self.assertTrue(controller.resume(restarted,self.inventory))
        self.assertEqual(self.launches,1)
        self.assertTrue(controller.read(restarted)['acknowledged'])
        evidence=(self.root/'docs/ansible-operation-result.json').read_text()
        self.assertNotIn('PRIVATE-',evidence)

    def test_disconnect_before_reservation_keeps_identity_for_retry(self):
        self.api.connect.side_effect=OSError('offline')
        with self.assertRaises(controller.OperationUnknown): self.reserve()
        identity=controller.read(self.api)['request']['id']
        self.api.connect.side_effect=lambda lab:MagicMock()
        self.reserve()
        self.assertEqual(controller.read(self.api)['request']['id'],identity)
        self.assertEqual(self.launches,0)

    def test_parameter_vm_and_stand_changes_block_resume_before_launch(self):
        self.reserve()
        for mutation in ('vm','inventory','stand','source'):
            with self.subTest(mutation=mutation):
                lab=copy.deepcopy(self.lab); inventory=copy.deepcopy(self.inventory)
                if mutation=='vm': lab['server_ids']['app']='foreign'
                elif mutation=='inventory': inventory['all']['vars']['password']='other'
                elif mutation=='stand':
                    file=self.local/'staged-deployment.json'; original=file.read_text()
                    file.write_text(json.dumps({'deployment_id':'b'*32,'namespace':'otus-dz07'}))
                else: (self.root/'app/new.py').write_text('changed')
                with self.assertRaises(controller.OperationProtocolError):
                    controller.validate(self.api,lab,inventory,controller.read(self.api))
                if mutation=='stand': file.write_text(original)
                if mutation=='source': (self.root/'app/new.py').unlink()
        self.assertEqual(self.launches,0)

    def test_parallel_operation_is_blocked_until_acknowledgement(self):
        self.reserve(); request=controller.read(self.api)['request']
        other={**request,'id':'b'*32}
        with self.assertRaisesRegex(ValueError,'Another configuration'): self.manager.reserve(other)
        self.manager.stop(request); self.manager.acknowledge(request)
        self.assertEqual(self.manager.reserve(other)['state'],'preparing')

    def test_missing_corrupt_or_wrong_result_never_becomes_success(self):
        self.reserve(); request=controller.read(self.api)['request']; file=self.manager.folder(request['id'])/'result.json'
        for content in (None,'PRIVATE-PASSWORD-SENTINEL',json.dumps({'id':'foreign'}),
                        json.dumps({'id':request['id'],'parameters_sha256':request['parameters_sha256'],'state':'succeeded','exit_code':0})):
            if content is None: file.unlink(missing_ok=True)
            else: file.write_text(content)
            result=self.manager.status(request)
            self.assertEqual(result['state'],'unknown'); self.assertNotIn('PRIVATE-',json.dumps(result))

    def test_boot_change_and_failed_unit_without_result_block_reexecution(self):
        self.reserve(); request=controller.read(self.api)['request']
        with patch.object(self.manager,'boot',return_value='another-boot'):
            self.assertEqual(self.manager.start(request)['state'],'unknown')
        self.unit.update(loaded=True,active=False)
        self.assertEqual(self.manager.start(request)['state'],'unknown')
        self.assertEqual(self.launches,0)

    def test_explicit_ansible_failure_and_finalizer_error_preserve_primary(self):
        self.reserve(); value=controller.read(self.api); request=value['request']
        result={'id':request['id'],'parameters_sha256':request['parameters_sha256'],'state':'failed','exit_code':2,
                'phase':'configure','started_utc':'start','finished_utc':'end'}
        worker.atomic(self.manager.folder(request['id'])/'result.json',result)
        with patch('observability.package_cache',side_effect=OSError('PRIVATE-ERROR-SENTINEL')):
            with self.assertRaises(controller.OperationFailed): controller.configure(self.api,None,self.inventory)
        self.assertEqual(controller.read(self.api)['result']['exit_code'],2)
        self.assertNotIn('PRIVATE-',(self.root/'docs/ansible-operation-result.json').read_text())

    def test_stop_waits_for_child_control_group_and_blocks_unconfirmed_stop(self):
        self.reserve(); request=controller.read(self.api)['request']
        self.unit.update(loaded=True,active=True)
        with patch.object(worker.subprocess,'run',return_value=SimpleNamespace(returncode=0)):
            with self.assertRaisesRegex(RuntimeError,'child processes'): self.manager.stop(request)
        controller.before_destroy(self.api)
        self.assertTrue(controller.read(self.api)['stop_confirmed'])
        self.assertFalse(self.unit['active'])

    def test_worker_success_and_failure_are_atomic_and_bound(self):
        for exit_code in (0,2):
            with self.subTest(exit_code=exit_code):
                if controller.read(self.api) is not None:
                    old=controller.read(self.api)['request']; self.manager.stop(old); self.manager.acknowledge(old)
                    controller.finalize(self.api) if controller.read(self.api).get('acknowledged') else None
                    self.local.joinpath('ansible-operation.json').unlink()
                    self.unit.update(loaded=False,active=False)
                self.reserve(); request=controller.read(self.api)['request']
                for name,content in controller.payload(self.api,self.inventory).items():
                    self.api.put(None,str(self.manager.folder(request['id'])/name),content)
                self.manager.start(request)
                child=Mock(pid=123); child.wait.return_value=exit_code; child.poll.return_value=exit_code
                with patch.object(worker.subprocess,'Popen',return_value=child) as process:
                    worker.execute(self.manager,request['id'])
                self.unit['active']=False
                result=self.manager.status(request)
                self.assertEqual(result['state'],'succeeded' if exit_code==0 else 'failed')
                self.assertEqual(result['exit_code'],exit_code)
                self.assertIn('started_utc',result); self.assertIn('finished_utc',result)
                self.assertTrue(all(c.kwargs['start_new_session'] for c in process.call_args_list))
                self.assertNotIn('PRIVATE-',repr([c.args for c in process.call_args_list]))
                self.assertFalse(list(self.manager.folder(request['id']).glob('*.next')))

    def test_worker_timeout_terminates_process_group_and_reports_operation_timeout(self):
        self.reserve(); request=controller.read(self.api)['request']
        for name,content in controller.payload(self.api,self.inventory).items():
            self.api.put(None,str(self.manager.folder(request['id'])/name),content)
        self.manager.start(request)
        child=Mock(pid=123); child.poll.return_value=None
        child.wait.side_effect=[subprocess.TimeoutExpired('ansible',1),subprocess.TimeoutExpired('ansible',1),0]
        with patch.object(worker.subprocess,'Popen',return_value=child), patch.object(worker.os,'killpg',create=True) as kill, \
                patch.object(worker.signal,'SIGKILL',9,create=True):
            worker.execute(self.manager,request['id'])
        self.unit['active']=False; result=self.manager.status(request)
        self.assertEqual(result['exit_code'],124); self.assertEqual(result['reason'],'operation_timeout')
        self.assertEqual(kill.call_count,2)
