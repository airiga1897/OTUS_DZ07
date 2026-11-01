import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import s3_artifacts as artifacts

spec = importlib.util.spec_from_file_location('private_fetch', ROOT / 'scripts/remote/fetch_artifacts.py')
fetcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetcher)


class Missing(Exception):
    response = {'Error': {'Code': 'NoSuchKey'}}


class ArtifactTest(unittest.TestCase):
    def test_private_dotenv_and_hidden_children_are_rejected_before_any_write(self):
        for name in ('.env','.env.example/child','config/.env','scripts/.private'):
            with self.subTest(name=name):
                path=self.root/'forbidden.tar.gz'
                with tarfile.open(path,'w:gz') as archive:
                    for member in ('app/safe.py',name):
                        info=tarfile.TarInfo(member);info.size=4
                        archive.addfile(info,io.BytesIO(b'code'))
                target=self.root/'received'
                with self.assertRaises(ValueError):fetcher.extract(path,target)
                self.assertFalse(target.exists())

    def test_download_diagnostics_include_phase_and_status_without_the_signed_url(self):
        import urllib.error
        url='https://private.invalid/source?secret=never-disclose'
        manifest=self.root/'artifact-delivery.json'
        manifest.write_text(json.dumps({'source':{'url':url}}))
        with patch.object(fetcher,'BASE',self.root),patch.object(fetcher,'download',side_effect=urllib.error.HTTPError(url,403,'Forbidden',{},None)):
            with self.assertRaises(SystemExit) as failure:fetcher.main()
        message=str(failure.exception)
        self.assertIn('phase=download-source',message)
        self.assertIn('http=403',message)
        self.assertNotIn('private.invalid',message)
        self.assertNotIn('never-disclose',message)
        self.assertFalse(manifest.exists())

    def test_complete_preparation_uploads_packages_and_source_only_once(self):
        (self.root / 'config').mkdir()
        cache = self.root / '.tools/elastic-packages'
        cache.mkdir(parents=True)
        lock = {}
        for product in ('elasticsearch', 'filebeat', 'kibana'):
            path = cache / (product + '-8.19.0-amd64.deb')
            path.write_bytes(product.encode())
            lock[path.name] = artifacts.digest(path)
        (self.root / 'config/elastic-artifacts.json').write_text(json.dumps(lock))
        (self.root / 'config/services.json').write_text('{"elastic_version":"8.19.0"}')
        (self.root / 'config/selectel-auth.json').write_text('{"fields":{"project_id":{"env":"TEST_PROJECT_ID"}}}')
        config = {'endpoint': 'https://s3.ru-3.storage.selcloud.ru', 'region': 'ru-3',
                  'bucket': 'otus-dz07-synthetic', 'prefix': 'otus-dz07', 'expires_seconds': 900}
        (self.root / 'config/artifacts.json').write_text(json.dumps({'s3': config}))
        objects = {}
        acl = {'Owner': {'ID': 'owner'}, 'Grants': [{'Grantee': {'Type': 'CanonicalUser', 'ID': 'owner'}}]}
        storage = Mock()
        storage.get_bucket_acl.return_value = acl
        storage.get_object_acl.return_value = acl
        no_policy = Missing()
        no_policy.response = {'Error': {'Code': 'NoSuchBucketPolicy'}}
        storage.get_bucket_policy.side_effect = no_policy
        def head(**kwargs):
            if kwargs['Key'] not in objects:
                raise Missing()
            return objects[kwargs['Key']]
        def upload(path, bucket, key, ExtraArgs, Config):
            self.assertEqual(ExtraArgs['ACL'], 'private')
            self.assertEqual(Config.max_concurrency, 1)
            self.assertFalse(Config.use_threads)
            objects[key] = {'ContentLength': Path(path).stat().st_size, 'Metadata': ExtraArgs['Metadata']}
        storage.head_object.side_effect = head
        storage.upload_file.side_effect = upload
        storage.generate_presigned_url.return_value = 'https://private.invalid/object?signature=synthetic'
        with patch.dict(os.environ, TEST_PROJECT_ID='synthetic-project'):
            first = artifacts.prepare(self.root, storage)
            second = artifacts.prepare(self.root, storage)
        self.assertEqual(first['source']['sha512'], second['source']['sha512'])
        self.assertEqual(storage.upload_file.call_count, 4)
        self.assertTrue((self.root / '.local/artifact-delivery.json').is_file())

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=ROOT / '.local')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_source_version_is_stable_and_excludes_runtime_and_secrets(self):
        for name in ('app', 'scripts/remote', '.local', 'terraform-selectel'):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        for name in ('app/manage.py', 'scripts/remote/task.py', '.local/operator.json',
                     'terraform-selectel/terraform.tfstate', 'terraform-selectel/terraform.tfvars.json', 'app/test.key'):
            (self.root / name).write_text(name)
        first, second = self.root / 'first.tar.gz', self.root / 'second.tar.gz'
        self.assertEqual(artifacts.source_archive(self.root, first), artifacts.source_archive(self.root, second))
        with tarfile.open(first) as archive:
            self.assertEqual(set(archive.getnames()), {'app/manage.py', 'scripts/remote/task.py'})

    def test_existing_package_is_reused_without_upload_and_url_is_bounded(self):
        path = self.root / 'package.deb'
        path.write_bytes(b'public-package')
        sha = artifacts.digest(path)
        storage = Mock()
        storage.head_object.return_value = {'ContentLength': path.stat().st_size, 'Metadata': {'sha512': sha}}
        storage.get_object_acl.return_value = {'Owner': {'ID': 'owner'}, 'Grants': [
            {'Grantee': {'Type': 'CanonicalUser', 'ID': 'owner'}, 'Permission': 'FULL_CONTROL'}]}
        storage.generate_presigned_url.return_value = 'https://private.invalid/object?signature=synthetic'
        config = {'prefix': 'otus-dz07', 'bucket': 'own-bucket', 'expires_seconds': 900}
        item = artifacts.ensure(storage, config, path, 'packages', sha)
        storage.upload_file.assert_not_called()
        self.assertEqual(item['sha512'], sha)
        self.assertEqual(storage.generate_presigned_url.call_args.kwargs['ExpiresIn'], 900)

    def test_unknown_head_failure_does_not_upload_or_overwrite(self):
        storage = Mock()
        storage.head_object.side_effect = OSError('unknown access error')
        path = self.root / 'file'
        path.write_bytes(b'code')
        with self.assertRaises(OSError):
            artifacts.ensure(storage, {'prefix': 'otus-dz07', 'bucket': 'own'}, path, 'sources', artifacts.digest(path))
        storage.upload_file.assert_not_called()

    def test_broad_acl_is_rejected(self):
        with self.assertRaises(ValueError):
            artifacts.private_acl({'Owner': {'ID': 'owner'}, 'Grants': [
                {'Grantee': {'Type': 'Group', 'URI': 'AllUsers'}, 'Permission': 'READ'}]})

    def test_redirect_cannot_send_the_fetch_to_http(self):
        with self.assertRaises(ValueError):
            fetcher.HTTPSRedirect().redirect_request(None, None, 302, '', {}, 'http://unsafe.invalid/object')

    def test_expired_or_truncated_download_never_replaces_a_file(self):
        target = self.root / 'source.tar.gz'
        target.write_bytes(b'previous')
        item = {'url': 'https://private.invalid/source?signature=synthetic',
                'sha512': hashlib.sha512(b'expected').hexdigest(), 'size': 8}
        response = Mock()
        response.status = 200
        response.geturl.return_value = item['url']
        response.read1.side_effect = [b'bad', b'']
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = response
        with self.assertRaises(ValueError):
            fetcher.download(item, target, opener)
        self.assertEqual(target.read_bytes(), b'previous')
        self.assertFalse(target.with_suffix('.partial').exists())

    def test_archive_traversal_is_refused_before_writing_any_source(self):
        path = self.root / 'source.tar.gz'
        with tarfile.open(path, 'w:gz') as archive:
            for name in ('app/safe.py', '../outside'):
                info = tarfile.TarInfo(name)
                info.size = 4
                archive.addfile(info, io.BytesIO(b'code'))
        with self.assertRaises(ValueError):
            fetcher.extract(path, self.root / 'target')
        self.assertFalse((self.root / 'target').exists())

    def test_bucket_configuration_requires_https_and_own_prefix(self):
        (self.root / 'config').mkdir()
        path = self.root / 'config/artifacts.json'
        values = {'endpoint': 'https://s3.ru-3.storage.selcloud.ru', 'region': 'ru-3',
                  'bucket': 'otus-dz07-synthetic', 'prefix': 'otus-dz07', 'expires_seconds': 900}
        path.write_text(json.dumps({'s3': values}))
        self.assertEqual(artifacts.settings(self.root), values)
        path.write_text(json.dumps({'s3': {**values, 'endpoint': 'http://s3.ru-3.storage.selcloud.ru'}}))
        with self.assertRaises(ValueError):
            artifacts.settings(self.root)
