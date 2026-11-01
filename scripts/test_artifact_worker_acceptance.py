"""Exercise verified VM delivery end to end with an in-memory HTTPS transport."""
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest.mock import MagicMock, Mock, patch

from remote import fetch_artifacts as worker

ROOT = Path(__file__).resolve().parents[1]


class ArtifactWorkerAcceptance(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(dir=ROOT / '.tools')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def bundle(self, members):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w:gz') as archive:
            for name, data in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        return output.getvalue()

    def item(self, body, name='source'):
        return {'url': 'https://private.invalid/' + name + '?signature=NEVER-LOG-ME',
                'size': len(body), 'sha512': hashlib.sha512(body).hexdigest()}

    def response(self, body, status=200, url='https://private.invalid/data'):
        result = MagicMock()
        result.__enter__.return_value = result
        result.status = status
        result.geturl.return_value = url
        result.read1.side_effect = io.BytesIO(body).read1
        return result

    def test_es_coordinator_downloads_and_verifies_source_and_all_packages(self):
        source = self.bundle({'app/source.py': b'version = 1', '.env.example': b'MODE=development'})
        manifest = {'source': self.item(source), 'packages': {}}
        payloads = {manifest['source']['url']: source}
        for product in ('elasticsearch', 'filebeat', 'kibana'):
            name = product + '-8.19.0-amd64.deb'
            payload = (product + '-synthetic-content').encode()
            manifest['packages'][name] = self.item(payload, name)
            payloads[manifest['packages'][name]['url']] = payload
        path = self.root / 'artifact-delivery.json'
        path.write_text(json.dumps(manifest))
        (self.root / 'artifact-node.json').write_text('{"name":"es1","version":"8.19.0"}')
        opener = Mock()
        opener.open.side_effect = lambda url, **kw: self.response(payloads[url])
        with patch.object(worker, 'BASE', self.root), patch.object(worker.urllib.request, 'build_opener', return_value=opener), \
                patch('builtins.print') as output:
            worker.main()
        self.assertEqual(opener.open.call_count, 4)
        self.assertEqual((self.root / 'app/source.py').read_bytes(), b'version = 1')
        expected = {name: item['sha512'] for name, item in manifest['packages'].items()}
        self.assertEqual(json.loads((self.root / 'packages/manifest.json').read_text()), expected)
        for name, item in manifest['packages'].items():
            self.assertEqual(hashlib.sha512((self.root / 'packages' / name).read_bytes()).hexdigest(), item['sha512'])
        self.assertFalse(path.exists())
        self.assertNotIn('NEVER-LOG-ME', str(output.call_args_list))
        self.assertFalse(list(self.root.rglob('*.partial')))

    def test_wrong_package_set_and_http_error_remove_signed_manifest_without_disclosing_url(self):
        source = self.bundle({'app/source.py': b'valid'})
        manifest = {'source': self.item(source), 'packages': {}}
        path = self.root / 'artifact-delivery.json'
        (self.root / 'artifact-node.json').write_text('{"name":"es1","version":"8.19.0"}')
        for failure in (None, urllib.error.HTTPError(manifest['source']['url'], 403, 'NEVER-LOG-ME', {}, None)):
            path.write_text(json.dumps(manifest))
            opener = Mock()
            opener.open.side_effect = failure
            opener.open.return_value = self.response(source)
            with patch.object(worker, 'BASE', self.root), patch.object(worker.urllib.request, 'build_opener', return_value=opener), \
                    self.assertRaises(SystemExit) as error:
                worker.main()
            self.assertIn('URL suppressed', str(error.exception))
            self.assertIn('phase=read-node-settings' if failure is None else 'http=403', str(error.exception))
            self.assertNotIn('NEVER-LOG-ME', str(error.exception))
            self.assertFalse(path.exists())
            self.assertFalse(list(self.root.glob('tmp*')))

    def test_invalid_metadata_and_http_response_preserve_previous_package(self):
        target = self.root / 'package.deb'
        target.write_bytes(b'previous')
        item = self.item(b'next')
        for changes in ({'sha512':'bad'}, {'size':0}, {'size':2 * 2**30 + 1}):
            opener = Mock()
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, 'digest or length'):
                worker.download({**item, **changes}, target, opener)
            opener.open.assert_not_called()
        for response in (self.response(b'next', status=206), self.response(b'next', url='http://private.invalid/data'),
                         self.response(b'too-long'), self.response(b'x')):
            opener = Mock(open=Mock(return_value=response))
            with self.assertRaises(ValueError):
                worker.download(item, target, opener)
            self.assertEqual(target.read_bytes(), b'previous')
            self.assertFalse(target.with_suffix('.partial').exists())
        opener = Mock()
        with patch.object(worker.time, 'monotonic', side_effect=[0, 2, 2]), self.assertRaises(TimeoutError):
            worker.download(item, target, opener, deadline=1)
        opener.open.assert_not_called()

    def test_verified_cache_and_retry_progress_restart_from_zero(self):
        body = b'complete'
        target = self.root / 'package.deb'
        item = self.item(body)
        target.write_bytes(body)
        opener = Mock()
        worker.download(item, target, opener)
        opener.open.assert_not_called()
        target.write_bytes(b'old')
        opener.open.side_effect = [OSError('interrupted'), self.response(body)]
        progress = Mock()
        worker.download(item, target, opener, progress=progress)
        self.assertEqual(progress.call_args_list[0].args, (0, len(body)))
        self.assertEqual(progress.call_args_list[-1].args, (len(body), len(body)))
        self.assertEqual(target.read_bytes(), body)

    def test_tar_limits_links_and_redirect_downgrade_are_rejected(self):
        source = self.root / 'source.tar.gz'
        for name in ('/app/absolute', 'app/../escape', 'other/source', 'app/.secret', 'app/a:b', 'app/a\\b'):
            source.write_bytes(self.bundle({name: b'bad'}))
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'Unsafe'):
                worker.extract(source, self.root / 'output')
            self.assertFalse((self.root / 'output').exists())
        archive = MagicMock()
        archive.__enter__.return_value = archive
        archive.getmembers.return_value = [tarfile.TarInfo('app/large')]
        archive.getmembers.return_value[0].size = 128 * 2**20 + 1
        with patch.object(worker.tarfile, 'open', return_value=archive), self.assertRaisesRegex(ValueError, 'too large'):
            worker.extract(source, self.root)
        source.write_bytes(self.bundle({'app/source': b'ok'}))
        original = Path.is_symlink
        with patch.object(Path, 'is_symlink', lambda path: path.name == 'source' or original(path)), \
                self.assertRaisesRegex(ValueError, 'contains a link'):
            worker.extract(source, self.root)
        redirect = worker.HTTPSRedirect()
        request = urllib.request.Request('https://private.invalid/a')
        with self.assertRaisesRegex(ValueError, 'remain HTTPS'):
            redirect.redirect_request(request, None, 302, 'Found', {}, 'http://private.invalid/b')
        redirected = redirect.redirect_request(request, None, 302, 'Found', {}, 'https://private.invalid/b')
        self.assertEqual(redirected.full_url, 'https://private.invalid/b')
