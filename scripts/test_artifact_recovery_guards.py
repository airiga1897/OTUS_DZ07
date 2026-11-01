"""Synthetic storage failures preserve ownership and cleanup recovery information."""
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import Mock, patch

import artifact_lifecycle as lifecycle
import s3_artifacts as artifacts

ROOT = Path(__file__).resolve().parents[1]


class ArtifactRecoveryGuards(unittest.TestCase):
    def test_probe_still_rejects_mismatch_if_downloader_returns_without_validation(self):
        storage = Mock()
        missing = RuntimeError('missing')
        missing.response = {'Error': {'Code': 'NoSuchKey'}}
        storage.head_object.side_effect = missing
        storage.generate_presigned_url.return_value = 'https://example.invalid/private-probe'
        def corrupted_download(item, destination):
            destination.write_bytes(b'corrupt')
        with patch.object(artifacts, 'require_private'), patch('remote.fetch_artifacts.download', side_effect=corrupted_download), \
                self.assertRaisesRegex(RuntimeError, 'integrity probe failed'):
            lifecycle.probe(self.root, storage)
        storage.delete_object.assert_called_once()
        report = json.loads((self.root / 'docs/s3-probe.json').read_text())
        self.assertTrue(report['deletion_verified'])
        self.assertFalse(report['download_hash_verified'])
        self.assertFalse(report['success'])
        self.assertFalse((self.local / 'artifact-probe.json').exists())

    def setUp(self):
        directory = tempfile.TemporaryDirectory(dir=ROOT / '.tools')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root/'config').mkdir()
        (self.root/'config/selectel-auth.json').write_text('{"fields":{"project_id":{"env":"TEST_ARTIFACT_PROJECT"}}}')
        self.config = {'endpoint':'https://s3.ru-7.storage.selcloud.ru', 'region':'ru-7',
                       'bucket':'synthetic-bucket', 'prefix':'otus-dz07', 'expires_seconds':900}
        (self.root/'config/artifacts.json').write_text(json.dumps({'s3':self.config}))
        self.local = self.root/'.local'
        env = patch.dict(os.environ, TEST_ARTIFACT_PROJECT='synthetic-project')
        env.start()
        self.addCleanup(env.stop)

    def test_missing_environment_and_noncanonical_uuid_fail_before_storage(self):
        with patch.dict(os.environ, TEST_ARTIFACT_PROJECT=''), self.assertRaisesRegex(ValueError, 'environment'):
            lifecycle.bind(self.root, self.config)
        self.assertFalse(self.local.exists())
        canonical = uuid.uuid4().hex
        self.assertTrue(lifecycle.run_key(self.config, canonical).endswith('/'+canonical+'/lease.json'))
        with self.assertRaisesRegex(ValueError, 'identity'):
            lifecycle.run_key(self.config, str(uuid.UUID(canonical)))
        lifecycle.release(self.root)  # No binding and no run need no credentials/client.

    def test_release_requires_confirmed_absence_and_preserves_failed_recovery(self):
        lifecycle.bind(self.root, self.config)
        path = self.local/'artifact-run.json'
        path.write_text(json.dumps({'run_id':uuid.uuid4().hex}))
        storage = Mock()
        with patch.object(artifacts, 'client', return_value=storage), self.assertRaisesRegex(RuntimeError, 'not confirmed'):
            lifecycle.release(self.root)
        self.assertTrue(path.exists())
        storage.head_object.side_effect = PermissionError('denied')
        with patch.object(artifacts, 'client', return_value=storage), self.assertRaises(PermissionError):
            lifecycle.release(self.root)
        self.assertTrue(path.exists())
        missing = RuntimeError('missing')
        missing.response = {'Error':{'Code':'NoSuchKey'}}
        storage.head_object.side_effect = missing
        with patch.object(artifacts, 'client', return_value=storage):
            lifecycle.release(self.root)
        self.assertFalse(path.exists())
        probe = self.local/'artifact-probe.json'
        for key in ('foreign/probes/'+uuid.uuid4().hex, 'otus-dz07/probes/'+str(uuid.uuid4())):
            probe.write_text(json.dumps({'key':key}))
            with patch.object(artifacts, 'client', return_value=storage), self.assertRaises(ValueError):
                lifecycle.release(self.root)
            self.assertTrue(probe.exists())

    def test_unknown_versioning_or_foreign_listing_aborts_inventory(self):
        storage = Mock()
        storage.get_paginator.return_value.paginate.return_value = [{}]
        storage.get_bucket_versioning.return_value = {'Status':'Unknown'}
        with self.assertRaisesRegex(ValueError, 'versioning'):
            lifecycle.inventory(storage, self.config)
        storage.get_bucket_versioning.return_value = {'Status':'Suspended'}
        storage.get_paginator.return_value.paginate.return_value = [{'Contents':[{'Key':'foreign/object'}]}]
        with self.assertRaisesRegex(ValueError, 'namespace'):
            lifecycle.inventory(storage, self.config)
        storage.delete_objects.assert_not_called()

    def test_incomplete_purge_retains_binding_and_pending_marker(self):
        lifecycle.bind(self.root, self.config)
        storage = Mock()
        storage.delete_objects.return_value = {}
        with patch.object(artifacts, 'require_private'), patch.object(lifecycle, 'inventory', return_value=([{'Key':'otus-dz07/file'}], [], [])), \
                self.assertRaisesRegex(RuntimeError, 'incomplete'):
            lifecycle.purge(self.root, storage)
        self.assertTrue((self.local/'artifact-purge-pending.json').exists())
        self.assertTrue((self.local/'artifact-storage.json').exists())
        self.assertFalse(json.loads((self.root/'docs/s3-cleanup.json').read_text())['success'])
        storage.delete_bucket.assert_not_called()

    def test_existing_probe_refused_before_upload_and_invalid_s3_settings(self):
        lifecycle.bind(self.root, self.config)
        (self.local/'artifact-probe.json').write_text('{}')
        storage = Mock()
        with patch.object(artifacts, 'require_private'), self.assertRaisesRegex(ValueError, 'earlier probe'):
            lifecycle.probe(self.root, storage)
        storage.put_object.assert_not_called()
        for changes in ({'prefix':'other'}, {'expires_seconds':299}, {'bucket':'INVALID'},
                        {'endpoint':'https://s3.ru-7.storage.selcloud.ru/path'}, {'endpoint':'https://s3.ru-7.storage.selcloud.ru:444'}):
            (self.root/'config/artifacts.json').write_text(json.dumps({'s3':{**self.config, **changes}}))
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                artifacts.settings(self.root)
