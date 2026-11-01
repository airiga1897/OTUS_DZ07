"""Fail-closed configuration, TLS identities and guarded database initialization."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import runpy
import shutil
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, Mock, patch

import cloud_settings
from cloud_contract import InfrastructureResult
import elk_tls
import run_settings
import stack_config
import topology
from remote import initial_data, portal_probe

ROOT=Path(__file__).resolve().parents[1]


class ConfigurationInvariants(unittest.TestCase):
    def root(self):
        folder=tempfile.TemporaryDirectory(dir=ROOT/'.tools');self.addCleanup(folder.cleanup)
        root=Path(folder.name);shutil.copytree(ROOT/'config',root/'config');shutil.copy(ROOT/'.env.example',root/'.env.example')
        return root

    def test_dotenv_rejects_duplicate_unknown_malformed_and_executable_values(self):
        root=self.root();path=root/'.env'
        for text in ('UNKNOWN=1','APP_VCPUS=4\nAPP_VCPUS=8','APP_VCPUS','APP_VCPUS="4',
                     'APP_VCPUS=$secret','APP_VCPUS=`command`','SELECTEL_FLAVOR_NAME_PATTERNS=SL1.{unknown}',
                     'SELECTEL_FLAVOR_NAME_PATTERNS=SL1.{','SELECTEL_DISK_TYPE_PATTERNS=BAD/NAME',
                     'SELECTEL_EXCLUDED_POOLS=ru-7,ru-7','APP_VCPUS=0','ES_RAM_MB=8193','BOOT_DISK_GB=19'):
            path.write_text(text)
            with self.subTest(text=text),self.assertRaises(ValueError):run_settings.load(root,environ={})
        path.write_text('# comment\nAPP_VCPUS="4"\nES_VCPUS=\'2\'')
        self.assertEqual(run_settings.load(root,environ={})['profiles']['app']['vcpus'],4)
        with patch.object(Path,'is_symlink',lambda p:p==path):
            with self.assertRaises(ValueError):run_settings.dotenv(path)
        path.unlink();(root/'.env.example').write_text('APP_VCPUS=4')
        with self.assertRaises(ValueError):run_settings.load(root,environ={})

    def test_single_configuration_source_and_subnet_exhaustion(self):
        root=self.root();p=root/'config/placement.json';value=json.loads(p.read_text());value['pools']=['ru-9'];p.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError,'duplicated'):run_settings.placement(root)
        (root/'.env.example').unlink();p.write_text('{}')
        with self.assertRaisesRegex(ValueError,'Missing'):run_settings.placement(root)
        policy={'sequential_placement':{'pool_subnets':{},'subnet_pool':'10.77.0.0/24'}}
        self.assertEqual(run_settings.pool_subnet(policy,'ru-9'),'10.77.0.0/24')
        with self.assertRaisesRegex(ValueError,'exhausted'):run_settings.pool_subnet(policy,'ru-3',{'ru-9':'10.77.0.0/24'})
        for bad in ('2001:db8::/32','8.8.8.0/24','10.77.0.0/25'):
            policy['sequential_placement']['subnet_pool']=bad
            with self.assertRaises(ValueError):run_settings.pool_subnet(policy,'ru-9')

    def test_stack_cloud_topology_and_node_contracts_refuse_unsafe_changes(self):
        root=self.root();original=json.loads((root/'config/stack.json').read_text())
        for field,value in [('subnet_cidr','10.77.0.0/25'),('postgresql',{'max_nodes':2}),('paths','relative;command')]:
            modified=copy.deepcopy(original)
            if field=='paths':modified['paths']['remote_root']=value
            elif field=='postgresql':modified['postgresql'].update(value)
            else:modified[field]=value
            (root/'config/stack.json').write_text(json.dumps(modified))
            with self.subTest(field=field),self.assertRaises(ValueError):stack_config.load(root)
        (root/'config/cloud.json').write_text('{"provider":"other","terraform_directory":"terraform-selectel"}')
        with self.assertRaises(ValueError):cloud_settings.load(root)
        desired=json.loads((ROOT/'config/topology.json').read_text())
        for change in ('app','voters','missing-es','duplicate','host-type'):
            value=copy.deepcopy(desired)
            if change=='app':value['nodes']['app']['host']=11
            if change=='voters':value['etcd_members']=['es1']
            if change=='missing-es':value['nodes'].pop('es1')
            if change=='duplicate':value['nodes']['es1']['host']=10
            if change=='host-type':value['nodes']['es1']['host']=True
            with self.subTest(change=change),self.assertRaises(ValueError):topology.validate(value)
        legacy={'nodes':{'app':{'host':10,'role':'app'},**{'db'+str(i):{'host':20+i,'role':'db'} for i in range(1,5)}},'etcd_members':['db1','db2','db3']}
        topology.validate(legacy)
        for change in ('too-few','role','masters','voters','host','name'):
            value=copy.deepcopy(legacy)
            if change=='too-few':value['nodes'].pop('db3');value['nodes'].pop('db4')
            if change=='role':value['nodes']['other']={'host':44,'role':'unsupported'}
            if change=='masters':value['nodes']['es1']={'host':31,'role':'es'}
            if change=='voters':value['etcd_members']=['db1','db1','db2']
            if change=='host':value['nodes']['db1']['host']=10
            if change=='name':value['nodes']['other']=value['nodes'].pop('db4')
            with self.subTest(legacy=change),self.assertRaises(ValueError):topology.validate(value)
        changed=copy.deepcopy(legacy);changed['etcd_members']=['db1','db2','db4']
        with self.assertRaisesRegex(ValueError,'membership'):topology.validate_extension(legacy,changed)
        (root/'config/topology.json').write_text(json.dumps(legacy))
        with self.assertRaisesRegex(ValueError,'masters'):topology.load(root)
        contract={'provider':'selectel','scope':{'project_id':'synthetic'},'namespace':'otus-dz07',
                  'placement':{'region':'ru-9','zone':'ru-9a'},'subnet_cidr':'10.77.0.0/24',
                  'nodes':{'app':{'id':'vm','volume_id':'disk','port_id':'port','private_ip':'10.77.0.10','public_ip':'8.8.8.8','roles':['app']}}}
        for change in ('identity','etcd','no-app'):
            value=copy.deepcopy(contract)
            if change=='identity':value['nodes']['app']['private_ip']='10.78.0.10'
            if change=='etcd':value['nodes']['app']['roles']=['app','etcd']
            if change=='no-app':value['nodes']['app']['roles']=['es']
            with self.subTest(change=change),self.assertRaises(ValueError):InfrastructureResult.from_dict(value)

    def test_certificate_partial_files_and_changed_node_identity_are_not_overwritten(self):
        root=self.root();local=root/'.local';local.mkdir()
        elk_tls.certificates(local,{'es1':'10.77.0.31'})
        original=(local/'certs/es1.crt').read_bytes()
        with self.assertRaises(RuntimeError):elk_tls.certificates(local,{'es1':'10.77.0.32'})
        self.assertEqual((local/'certs/es1.crt').read_bytes(),original)
        (local/'certs/es1.key').unlink()
        with self.assertRaises(RuntimeError):elk_tls.certificates(local,{'es1':'10.77.0.31'})
        (local/'certs/ca.key').unlink()
        with self.assertRaises(RuntimeError):elk_tls.certificates(local,{'es1':'10.77.0.31'})

    def test_database_initialization_preserves_existing_owner_and_reports_real_changes(self):
        root=self.root();app=root/'portal';app.mkdir()
        config=types.ModuleType('runtime_config');config.BASE=root;config.APP=app;config.SHARED=root/'shared';config.STACK=initial_data.STACK
        (root/'settings.json').write_text('{"passwords":{"postgres":"synthetic","app":"synthetic","django":"synthetic"}}')
        connection=MagicMock()
        def execute(query,*args):
            row=(True,) if str(query).startswith('SELECT NOT') else (1,) if 'pg_roles' in str(query) else ('otus',)
            return Mock(fetchone=Mock(return_value=row))
        connection.execute.side_effect=execute;connection.__enter__.return_value=connection
        for migrated,loaded in [('No migrations to apply.',False),('Applying migration... OK',False),('No migrations to apply.',True)]:
            report={'status':'loaded','loaded_now':loaded};output=io.StringIO()
            with patch.dict(sys.modules,{'runtime_config':config}),patch('psycopg.connect',return_value=connection), \
                 patch('subprocess.run',side_effect=[Mock(stdout=migrated),Mock(stdout=json.dumps(report))]) as process, \
                 patch('logging.basicConfig'),contextlib.redirect_stdout(output):
                runpy.run_path(initial_data.__file__,run_name='__main__')
            self.assertIn('PORTAL_CHANGED='+str(loaded or migrated!='No migrations to apply.').lower(),output.getvalue())
            self.assertEqual(json.loads((app/'initial-data-result.json').read_text()),report)
            self.assertEqual([call.args[0][-1] for call in process.call_args_list],['--noinput','load_initial_data'])
        connection.execute.side_effect=None;connection.execute.return_value.fetchone.side_effect=[(1,),('foreign',)]
        with self.assertRaisesRegex(RuntimeError,'different owner'):initial_data.prepare_database(connection,'synthetic')

    def test_portal_process_loads_only_its_local_settings_and_calls_the_application(self):
        root=self.root();app=root/'portal';config=types.ModuleType('runtime_config');config.BASE=root;config.APP=app
        settings={'passwords':{'app':'synthetic','django':'synthetic'}};(root/'settings.json').write_text(json.dumps(settings))
        with patch.dict(sys.modules,{'initial_data':initial_data,'runtime_config':config}), \
             patch.object(sys,'path',list(sys.path)),patch.object(sys,'argv',['portal_probe.py','baseline']), \
             patch.dict(os.environ,{},clear=True),patch('django.setup') as setup,patch.object(portal_probe,'create_article',return_value={'id':2}) as write,contextlib.redirect_stdout(io.StringIO()) as output:
            portal_probe.main()
            self.assertEqual(os.environ['DJANGO_SETTINGS_MODULE'],'config.settings')
            self.assertEqual(os.environ['DB_PASSWORD'],'synthetic')
        setup.assert_called_once();write.assert_called_once_with('baseline');self.assertEqual(json.loads(output.getvalue()),{'id':2})
