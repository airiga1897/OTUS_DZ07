"""Source/package delivery and S3 probe failures clean only their own temporary data."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import artifact_transport
import artifact_lifecycle
import prepare_ansible


class Missing(Exception):
    response={'Error':{'Code':'NoSuchKey'}}


class DeliveryBoundaryContracts(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory(dir=ROOT/'.tools');self.addCleanup(folder.cleanup)
        self.root=Path(folder.name);(self.root/'config').mkdir();(self.root/'.local').mkdir()

    def test_source_delivery_cleans_bootstrap_links_on_success_and_failure(self):
        (self.root/'scripts/remote').mkdir(parents=True)
        (self.root/'scripts/remote/fetch_artifacts.py').write_text('# synthetic fetcher')
        (self.root/'config/services.json').write_text('{"elastic_version":"8.19.0","artifact_delivery":"s3"}')
        (self.root/'.local/artifact-delivery.json').write_text('{"url":"https://example.invalid/temporary"}')
        (self.root/'.local/artifact-credentials.json').write_text('PERMANENT-KEY-MUST-STAY-LOCAL')
        for fails in (False,True):
            client=MagicMock();api=SimpleNamespace(ROOT=self.root,LOCAL=self.root/'.local',
                STACK={'paths':{'remote_root':'/synthetic'}},put=Mock(),remote=Mock())
            if fails:api.remote.side_effect=[None,RuntimeError('download failed')]
            with patch('s3_artifacts.prepare') as prepare:
                if fails:
                    with self.assertRaises(RuntimeError):artifact_transport.source(api,client,'app')
                else:artifact_transport.source(api,client,'app')
            prepare.assert_called_once_with(self.root)
            self.assertEqual(client.open_sftp.return_value.__enter__.return_value.remove.call_count,2)
            self.assertNotIn('PERMANENT-KEY',repr(api.put.call_args_list))

    def test_offline_packages_require_pinned_hashes_before_transfer(self):
        directory=self.root/'.tools/elastic-packages';directory.mkdir(parents=True)
        (directory/'package.deb').write_bytes(b'package')
        lock={'package.deb':hashlib.sha512(b'package').hexdigest()}
        api=SimpleNamespace(ROOT=self.root,STACK={'paths':{'remote_root':'/synthetic'}},remote=Mock(),put=Mock())
        client=MagicMock()
        with patch('s3_artifacts.package_lock',return_value=lock):
            artifact_transport.cached_packages(api,client)
            (directory/'package.deb').write_bytes(b'corrupt')
            with self.assertRaises(ValueError):artifact_transport.cached_packages(api,client)
        self.assertEqual(client.open_sftp.return_value.__enter__.return_value.put.call_count,1)

    def test_ansible_wheelhouse_hash_manifest_reuses_only_unchanged_files(self):
        (self.root/'ansible').mkdir()
        (self.root/'ansible/requirements-control.txt').write_text('ansible-core==2.18.18 --hash=sha256:synthetic\n')
        def download(*args,**kwargs):
            destination=self.root/'.local/ansible-wheels'
            (destination/'synthetic.whl').write_bytes(b'verified-wheel')
        with patch.object(prepare_ansible.subprocess,'run',side_effect=download) as pip:
            first=prepare_ansible.prepare(self.root)
            prepare_ansible.prepare(self.root)
            self.assertEqual(pip.call_count,1)
            (first/'synthetic.whl').write_bytes(b'changed')
            prepare_ansible.prepare(self.root)
            self.assertEqual(pip.call_count,2)
        self.assertFalse(list((self.root/'.local').glob('ansible-download-*')))

    def test_s3_probe_roundtrip_and_failure_preserve_recovery_until_deletion_verified(self):
        (self.root/'config/selectel-auth.json').write_text('{"fields":{"project_id":{"env":"OS_PROJECT_ID"}}}')
        config={'endpoint':'https://example.invalid','region':'ru-7','bucket':'synthetic','prefix':'otus-dz07'}
        for fault in (None,'download','delete'):
            objects={};storage=Mock()
            storage.put_object.side_effect=lambda **kw:objects.update({kw['Key']:kw['Body']})
            def remove(**kw):
                if fault=='delete':raise RuntimeError('delete unavailable')
                objects.pop(kw['Key'],None)
            storage.delete_object.side_effect=remove
            storage.head_object.side_effect=Missing()
            storage.generate_presigned_url.return_value='https://example.invalid/probe?signature=synthetic'
            def fetch(item,target):
                if fault=='download':raise TimeoutError('download unavailable')
                target.write_bytes(next(iter(objects.values())))
            with self.subTest(fault=fault),patch.dict(os.environ,{'OS_PROJECT_ID':'synthetic-project'}), \
                 patch('s3_artifacts.settings',return_value=config),patch('s3_artifacts.require_private'), \
                 patch('remote.fetch_artifacts.download',side_effect=fetch):
                if fault:
                    with self.assertRaises((TimeoutError,RuntimeError)):artifact_lifecycle.probe(self.root,storage)
                else:artifact_lifecycle.probe(self.root,storage)
            recovery=self.root/'.local/artifact-probe.json'
            self.assertEqual(recovery.exists(),fault=='delete')
            result=json.loads((self.root/'docs/s3-probe.json').read_text())
            self.assertEqual(result['success'],fault is None)
            if recovery.exists():recovery.unlink()
