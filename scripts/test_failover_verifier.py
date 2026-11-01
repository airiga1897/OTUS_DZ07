"""Exercise the real verifier across a transient outage and incomplete failover."""
import contextlib
import importlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]


class FailoverVerifierTests(unittest.TestCase):
    def run_verifier(self, mode, transient=False, with_failover=False):
        module = importlib.import_module('remote.verify')
        run_id = 'a' * 32
        old = {'id':1,'slug':'baseline','body':'baseline'}
        failover = {'id':2,'slug':'failover','body':'committed'}
        new = {'id':3,'slug':mode,'body':'new'}
        members = {n:{'role':'primary' if n=='db2' else 'replica','state':'running'}
                   for n in ['db1','db2','db3']}
        if mode == 'failover': members['db1'] = {'state':'unreachable'}
        with tempfile.TemporaryDirectory(dir=ROOT/'.local') as directory:
            app = Path(directory);(app/'probes').mkdir()
            context = {'baseline':old,'media':{},'data':{}}
            if with_failover: context['failover'] = failover
            (app/'probes'/(run_id+'.json')).write_text(json.dumps(context))
            (app/'initial-data-result.json').write_text('{}')
            connection = MagicMock()
            connection.__enter__.return_value.execute.return_value.fetchone.return_value = (10,)
            calls = []
            def connect(*args):
                calls.append(args)
                if transient and len(calls)==1:
                    raise module.psycopg.errors.ConnectionTimeout('synthetic transient outage')
                return connection
            output = io.StringIO()
            with patch.object(module,'APP',app),patch.object(module,'initialize'), \
                 patch.object(module,'lab',{'private_ips':dict.fromkeys(['app','db1','db2','db3'],'127.0.0.1')}), \
                 patch.object(module.sys,'argv',['verify.py',mode,run_id,'db1']), \
                 patch.object(module,'sql_connect',side_effect=connect), \
                 patch.object(module,'cluster',return_value=members), \
                 patch.object(module,'check_data'),patch.object(module,'check_record') as read_back, \
                 patch.object(module,'check_media'),patch.object(module,'check_http_media'), \
                 patch.object(module,'check_http'),patch.object(module,'insert_record',return_value=new) as insert, \
                 patch.object(module.time,'sleep') as sleep, contextlib.redirect_stdout(output):
                module.main()
                insert.assert_called_once_with(mode)
                self.assertIn(old,[call.args[1] for call in read_back.call_args_list])
                if with_failover: self.assertIn(failover,[call.args[1] for call in read_back.call_args_list])
                self.assertEqual(sleep.call_count, int(transient))
            return json.loads(output.getvalue())

    def test_failover_retries_sql_connection_before_reading_capacity(self):
        result=self.run_verifier('failover',transient=True)
        self.assertEqual(result['leader'],'db2')
        self.assertTrue(result['new_write_succeeded'])

    def test_recovery_checks_existing_commits_even_if_failover_record_is_missing(self):
        result=self.run_verifier('recovered')
        self.assertFalse(result['failover_record_available'])
        self.assertTrue(result['replica_records_verified'])

    def test_recovery_still_requires_a_successfully_committed_failover_record(self):
        result=self.run_verifier('recovered',with_failover=True)
        self.assertTrue(result['failover_record_available'])
        self.assertEqual(len(result['preserved_records']),2)
