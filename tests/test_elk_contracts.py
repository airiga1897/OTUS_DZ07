"""Real acceptance logic with synthetic HTTP responses and persistent local checkpoints."""
import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from remote import elk_api as elk


def stream(data):
    return io.BytesIO(json.dumps(data).encode())


class ElasticAcceptanceContracts(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(dir=ROOT / '.tools');self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        self.config = {'ips': {n: '10.77.0.' + str(i) for i,n in enumerate(('es1','es2','es3'),31)},
            'ip':'10.77.0.10','kibana_port':5601,'expected_services':['system','nginx'],
            'log_hosts':['app','es1'], 'log_expectations':{'app':['system','nginx'],'es1':['system']}}
        self.health = {'status':'green','number_of_nodes':3}
        for key,value in {'C':self.config,'S':{'elastic':'synthetic','kibana':'synthetic','shipper':'synthetic'},'BASE':self.base}.items():
            override = patch.object(elk,key,value);override.start();self.addCleanup(override.stop)

    def printed(self, function):
        out = io.StringIO()
        with contextlib.redirect_stdout(out): function()
        return json.loads(out.getvalue())

    def test_https_fallback_and_authorization_failure(self):
        with patch.object(elk.urllib.request,'urlopen',side_effect=[TimeoutError(),stream({'ok':True})]) as request:
            self.assertTrue(elk.api('/_nodes')['ok'])
            self.assertEqual(request.call_count,2)
        denied = urllib.error.HTTPError('https://synthetic.invalid',401,'Denied',{},None)
        with patch.object(elk.urllib.request,'urlopen',side_effect=denied) as request:
            with self.assertRaises(urllib.error.HTTPError): elk.api('/_nodes')
            self.assertEqual(request.call_count,1)
        with patch.object(elk.urllib.request,'urlopen',side_effect=TimeoutError()) as request:
            with self.assertRaises(TimeoutError): elk.api('/_nodes')
            self.assertEqual(request.call_count,3)

    def test_health_requires_exact_membership_and_green_state(self):
        with patch.object(elk,'api',side_effect=[TimeoutError(),{'number_of_nodes':2,'status':'green'},
             {'number_of_nodes':3,'status':'yellow'},self.health]),patch.object(elk.time,'sleep'):
            self.assertEqual(elk.wait_health(),self.health)
        with patch.object(elk,'api',return_value={'number_of_nodes':2,'status':'yellow'}),patch.object(elk.time,'sleep'):
            self.assertEqual(elk.wait_health(nodes=2,green=False)['status'],'yellow')
            with self.assertRaisesRegex(RuntimeError,'health timeout'): elk.wait_health()

    def test_setup_builds_least_privilege_shipper_and_replicated_index(self):
        def reply(path,payload=None,method=None):
            if path=='/': return {'cluster_uuid':'cluster'}
            return {}
        with patch.object(elk,'api',side_effect=reply) as calls,patch.object(elk,'wait_health',return_value=self.health):
            self.assertEqual(self.printed(elk.setup)['cluster_uuid'],'cluster')
        payloads={call.args[0]:call.args[1] for call in calls.call_args_list if len(call.args)>1}
        self.assertEqual(payloads['/_security/role/otus_shipper']['indices'][0]['privileges'],['auto_configure','create_doc'])
        self.assertEqual(payloads['/_index_template/otus-logs']['template']['settings']['number_of_replicas'],1)
        self.assertEqual(payloads['/_security/user/otus_shipper']['roles'],['otus_shipper'])
        self.assertIn('/_ingest/pipeline/otus-logs',payloads)

    def log_reply(self,path,payload=None,method=None,missing=False,parsed=True):
        if path=='/': return {'cluster_uuid':'cluster'}
        if path.endswith('_count'):
            if 'exists' in payload['query']: return {'count':int(parsed)}
            host=payload['query']['bool']['filter'][0]['term']['host.name']
            return {'count':0 if missing and host.endswith('es1') else 1}
        if payload.get('size')==0:
            return {'aggregations':{'services':{'buckets':[{'key':n} for n in ('system','nginx')]},
                'hosts':{'buckets':[{'key':'otus-dz07-'+n} for n in ('app','es1')]}}}
        return {'hits':{'hits':[{'_source':{'message':'synthetic event'}}]}}

    def test_logs_require_every_node_source_and_parsed_nginx_fields(self):
        with patch.object(elk,'api',side_effect=self.log_reply),patch.object(elk,'wait_health',return_value=self.health):
            result=self.printed(elk.verify)
        self.assertEqual(set(result['source_counts']),{'app/system','app/nginx','es1/system'})
        self.assertEqual(result['parsed_nginx_count'],1)
        for missing,parsed,message in [(True,True,'Missing log sources'),(False,False,'parsed HTTP status')]:
            with self.subTest(missing=missing,parsed=parsed),patch.object(elk,'api',side_effect=lambda *a,**k:self.log_reply(*a,**k,missing=missing,parsed=parsed)),patch.object(elk.time,'sleep'):
                with self.assertRaisesRegex(RuntimeError,message): self.printed(elk.verify)

    def test_missing_index_retries_but_forbidden_logs_do_not_pass(self):
        first=True
        def not_ready(*args,**kwargs):
            nonlocal first
            if first:
                first=False
                raise urllib.error.HTTPError('https://synthetic.invalid',404,'Not ready',{},None)
            return self.log_reply(*args,**kwargs)
        with patch.object(elk,'api',side_effect=not_ready),patch.object(elk,'wait_health',return_value=self.health),patch.object(elk.time,'sleep'):
            self.assertEqual(self.printed(elk.verify)['parsed_nginx_count'],1)
        with patch.object(elk,'api',side_effect=urllib.error.HTTPError('https://synthetic.invalid',403,'Denied',{},None)):
            with self.assertRaises(urllib.error.HTTPError): elk.verify()

    def test_checkpoint_reuse_and_reapply_preserve_cluster_ids_and_committed_document(self):
        state={'uuid':'cluster','nodes':{n+'-id':{'name':n} for n in self.config['ips']},'documents':{}}
        def reply(path,payload=None,method=None):
            clean=path.split('?')[0]
            if clean=='/': return {'cluster_uuid':state['uuid']}
            if clean=='/_nodes': return {'nodes':state['nodes']}
            if method=='PUT': state['documents'][clean]=copy.deepcopy(payload);return {'result':'created'}
            return {'found':clean in state['documents'],'_source':state['documents'].get(clean,{})}
        with patch.object(elk,'api',side_effect=reply),patch.object(elk,'wait_health',return_value=self.health):
            self.assertTrue(self.printed(elk.checkpoint)['checkpoint_written'])
            original=(self.base/'scaling-checkpoint.json').read_bytes()
            self.assertTrue(self.printed(elk.checkpoint)['checkpoint_reused'])
            self.assertEqual((self.base/'scaling-checkpoint.json').read_bytes(),original)
            self.assertTrue(self.printed(elk.preservation)['committed_document_preserved'])
            saved=copy.deepcopy(state)
            for fault,message in [('uuid','UUID changed'),('nodes','node was replaced'),('document','document was lost')]:
                state.update(copy.deepcopy(saved))
                if fault=='uuid': state['uuid']='other'
                elif fault=='nodes': state['nodes'].pop('es1-id')
                else: state['documents'].pop('/otus-scaling-check/_doc/probe')
                with self.assertRaisesRegex(RuntimeError,message): elk.preservation()
            state.update(saved);state['uuid']='other'
            with self.assertRaisesRegex(RuntimeError,'checkpoint'): elk.checkpoint()

    def test_kibana_views_dashboard_and_availability_contract(self):
        status={'level':'available'}
        def reply(request,**kwargs):
            self.assertIn('Authorization',request.headers)
            if request.full_url.endswith('/api/status'): return stream({'status':{'overall':status}})
            if '/api/data_views/' in request.full_url: return stream({'data_view':{'id':'otus-logs'}})
            return stream({'id':'otus-logs'})
        with patch.object(elk.urllib.request,'urlopen',side_effect=reply):
            self.assertEqual(self.printed(elk.kibana)['dashboard_id'],'otus-logs')
            self.assertEqual(self.printed(elk.kibana_check)['status'],'available')
            status['level']='unavailable'
            with self.assertRaisesRegex(RuntimeError,'not available'): elk.kibana_check()
            with patch.object(elk.time,'sleep'):
                with self.assertRaisesRegex(RuntimeError,'not ready'): elk.kibana()
