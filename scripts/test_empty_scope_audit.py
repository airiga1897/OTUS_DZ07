"""A pre-allocation failure still requires an explicit, scoped API absence audit."""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error

import selectel_staged
import staged_runtime

ROOT=Path(__file__).resolve().parents[1]


class EmptyScopeAuditContracts(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory(dir=ROOT/'.tools');self.addCleanup(folder.cleanup)
        self.root=Path(folder.name);self.local=self.root/'.local';self.local.mkdir()
        (self.root/'terraform-selectel').mkdir()
        self.api=SimpleNamespace(ROOT=self.root,LOCAL=self.local,PREFIX='otus-dz07',_TEST_CYCLE_ID=None)
        self.api.save=lambda path,value:(path.parent.mkdir(parents=True,exist_ok=True),path.write_text(json.dumps(value)))
        self.regional=Mock(endpoints={'network':'https://example.invalid/network'})
        self.regional.get.side_effect=lambda service,path:{path.split('?')[0].split('/')[-1].replace('-','_'):[]}
        self.cloud=Mock();self.cloud.resource_regions.return_value=['ru-9','ru-3']
        self.cloud.read_only_pool.return_value=self.regional
        self.global_api=Mock();self.global_api.listing.return_value=[]
        self.patches=ExitStack();self.addCleanup(self.patches.close)
        self.patches.enter_context(patch('selectel_staged.load_credentials',return_value={'project_id':'synthetic'}))
        self.cloud_factory=self.patches.enter_context(patch('selectel_adapter.SelectelAdapter',return_value=self.cloud))
        self.patches.enter_context(patch('selectel_global_router.GlobalRouter',return_value=self.global_api))

    def test_manual_destroy_after_failed_cycle_audits_all_pools_then_removes_only_own_marker(self):
        (self.local/'test-cycle.json').write_text('{"status":"cleanup_failed","run_id":"own"}')
        (self.local/'operator.json').write_text('{"synthetic":true}')
        staged_runtime.cleanup_empty(self.api)
        report=json.loads((self.root/'docs/cleanup-result.json').read_text())
        self.assertTrue(report['success']);self.assertEqual(report['pools_audited'],['ru-9','ru-3'])
        self.assertFalse((self.local/'test-cycle.json').exists())
        self.assertTrue((self.local/'operator.json').exists())

    def test_foreign_local_project_is_refused_before_cloud_access(self):
        for name in ('context.json','selectel-context.json','infrastructure.json'):
            path=self.local/name;path.write_text('{"project_id":"foreign"}')
            with self.assertRaises(ValueError):selectel_staged.audit_empty_scope(self.api)
            self.cloud_factory.assert_not_called();path.unlink()

    def test_missing_floating_api_only_counts_absent_if_router_extension_is_absent(self):
        for declared in (False,True):
            def read(service,path):
                if 'floatingips' in path:raise urllib.error.HTTPError('https://example.invalid',404,'Not found',{},None)
                if 'extensions' in path:return {'extensions':[{'alias':'router'}] if declared else []}
                return {path.split('?')[0].split('/')[-1].replace('-','_'):[]}
            self.regional.get.side_effect=read
            if declared:
                with self.assertRaises(urllib.error.HTTPError):selectel_staged.audit_empty_scope(self.api)
            else:self.assertTrue(selectel_staged.audit_empty_scope(self.api)['empty_without_context'])

    def test_remaining_cloud_objects_and_api_failures_never_confirm_cleanup(self):
        self.global_api.listing.return_value=[{'name':'otus-dz07-global','tags':[]}]
        with self.assertRaises(RuntimeError):selectel_staged.audit_empty_scope(self.api)
        self.global_api.listing.return_value=[]
        def remaining(service,path):
            return {'floatingips':[{'description':'otus-dz07-owned'}]} if 'floatingips' in path else {path.split('?')[0].split('/')[-1].replace('-','_'):[]}
        self.regional.get.side_effect=remaining
        with self.assertRaises(RuntimeError):selectel_staged.audit_empty_scope(self.api)
        self.cloud_factory.side_effect=TimeoutError('API unavailable')
        with self.assertRaises(TimeoutError):selectel_staged.audit_empty_scope(self.api)
