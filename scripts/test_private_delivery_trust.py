"""Private delivery cannot proceed without explicit credentials, TLS trust and integrity."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import s3_artifacts as artifacts

ROOT=Path(__file__).resolve().parents[1]


class PrivateDeliveryTrust(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory(dir=ROOT/'.tools');self.addCleanup(directory.cleanup)
        self.root=Path(directory.name);(self.root/'config').mkdir()
        (self.root/'config/artifact-auth.json').write_text('{"fields":{"access_key":{"env":"SYNTHETIC_ACCESS"},"secret_key":{"env":"SYNTHETIC_SECRET"}}}')
        self.config={'endpoint':'https://s3.ru-7.storage.selcloud.ru','region':'ru-7','bucket':'synthetic-bucket','prefix':'otus-dz07','expires_seconds':900}

    def test_missing_keys_and_empty_windows_trust_store_never_create_sdk_client(self):
        with patch.object(artifacts,'os',SimpleNamespace(name='nt',environ={})),patch('boto3.client') as client,self.assertRaisesRegex(ValueError,'credentials missing'):
            artifacts.client(self.root,self.config)
        client.assert_not_called()
        environment={'SYNTHETIC_ACCESS':'synthetic-access','SYNTHETIC_SECRET':'synthetic-secret'}
        with patch.object(artifacts,'os',SimpleNamespace(name='nt',environ=environment)), \
                patch.object(artifacts.ssl,'create_default_context',return_value=Mock(get_ca_certs=Mock(return_value=[]))), \
                patch('boto3.client') as client,self.assertRaisesRegex(ValueError,'trust anchors'):
            artifacts.client(self.root,self.config)
        client.assert_not_called()

    def test_non_windows_sdk_always_verifies_tls(self):
        environment={'SYNTHETIC_ACCESS':'synthetic-access','SYNTHETIC_SECRET':'synthetic-secret'}
        with patch.object(artifacts,'os',SimpleNamespace(name='posix',environ=environment)),patch('boto3.client') as client:
            artifacts.client(self.root,self.config)
        self.assertIs(client.call_args.kwargs['verify'],True)
        self.assertEqual(client.call_args.kwargs['config'].s3['addressing_style'],'virtual')

    def test_source_links_are_rejected_in_subdirectories_and_root_allowlist(self):
        (self.root/'app').mkdir();source=self.root/'app/source.py';source.write_text('public')
        destination=self.root/'source.tar.gz'
        with patch.object(Path,'is_symlink',lambda path:path==source),self.assertRaisesRegex(ValueError,'links'):
            artifacts.source_archive(self.root,destination)
        readme=self.root/'README.md';readme.write_text('public README')
        with patch.object(Path,'is_symlink',lambda path:path==readme),self.assertRaisesRegex(ValueError,'links'):
            artifacts.source_archive(self.root,destination)

    def test_private_policy_rejects_access_errors_and_notprincipal_allow(self):
        storage=Mock();storage.get_bucket_acl.return_value={'Owner':{'ID':'owner'},'Grants':[{'Grantee':{'Type':'CanonicalUser','ID':'owner'}}]}
        storage.get_bucket_policy.side_effect=PermissionError('synthetic denied')
        with self.assertRaises(PermissionError):artifacts.require_private(storage,'synthetic')
        storage.get_bucket_policy.side_effect=None
        for statement in ({'Effect':'Allow','NotPrincipal':{'AWS':'specific'}},{'Effect':'Deny','Principal':'*'}):
            storage.get_bucket_policy.return_value={'Policy':json.dumps({'Statement':[statement]})}
            if statement['Effect']=='Allow':
                with self.assertRaises(ValueError):artifacts.require_private(storage,'synthetic')
            else:artifacts.require_private(storage,'synthetic')

    def test_immutable_object_size_and_signed_url_scheme_are_verified(self):
        path=self.root/'package.deb';path.write_bytes(b'verified');sha=artifacts.digest(path)
        storage=Mock();storage.head_object.return_value={'ContentLength':1,'Metadata':{'sha512':sha}}
        with self.assertRaisesRegex(ValueError,'differs'):artifacts.ensure(storage,self.config,path,'packages',sha)
        storage.upload_file.assert_not_called()
        storage.head_object.return_value['ContentLength']=path.stat().st_size
        storage.get_object_acl.return_value={'Owner':{'ID':'owner'},'Grants':[{'Grantee':{'Type':'CanonicalUser','ID':'owner'}}]}
        storage.generate_presigned_url.return_value='http://private.invalid/package'
        with self.assertRaisesRegex(ValueError,'HTTPS'):artifacts.ensure(storage,self.config,path,'packages',sha)

    def test_invalid_package_metadata_aborts_roundtrip_and_writes_failure_report(self):
        lock={p+'-8.19.0-amd64.deb':'a'*128 for p in ('elasticsearch','filebeat','kibana')}
        (self.root/'config/elastic-artifacts.json').write_text(json.dumps(lock))
        (self.root/'config/services.json').write_text('{"elastic_version":"8.19.0"}')
        (self.root/'config/artifacts.json').write_text(json.dumps({'s3':self.config}))
        storage=Mock();storage.head_object.return_value={'Metadata':{'sha512':'b'*128}}
        with patch.object(artifacts,'require_private'),self.assertRaisesRegex(ValueError,'metadata'):
            artifacts.verify_packages(self.root,storage)
        self.assertFalse(json.loads((self.root/'docs/s3-package-downloads.json').read_text())['success'])
        storage.generate_presigned_url.assert_not_called()
        self.assertEqual(list((self.root/'.tools/artifact-build').iterdir()),[])
