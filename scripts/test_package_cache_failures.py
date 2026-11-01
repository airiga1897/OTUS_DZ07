"""Offline range transport and cache failures cannot publish corrupt packages."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

import cache_packages as cache
from remote import download_packages as public

ROOT = Path(__file__).resolve().parents[1]


class PackageCacheFailures(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(dir=ROOT / '.tools')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root/'config').mkdir()
        (self.root/'config/services.json').write_text('{"elastic_version":"8.19.0","artifact_delivery":"cached"}')

    def response(self, body, status=206, content_range='bytes 0-3/4'):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = status
        response.headers = {'Content-Length':'4','Content-Range':content_range}
        response.read.return_value = body
        return response

    def test_range_status_length_and_content_range_fail_with_bounded_retry(self):
        for response in (self.response(b'data', status=200), self.response(b'data', content_range='bytes 1-4/5'),
                         self.response(b'dat')):
            with self.subTest(response=response), patch.object(cache.urllib.request, 'urlopen', return_value=response) as transport, \
                    self.assertRaises(RuntimeError):
                cache.download('https://artifacts.elastic.co/test', self.root/'package.partial')
            self.assertEqual(transport.call_count, 4)  # HEAD then at most three GETs.

    def test_corrupt_explicit_digest_rejected_and_same_directory_import_is_not_copied(self):
        name = 'filebeat-8.19.0-amd64.deb'
        (self.root/name).write_bytes(b'filebeat')
        checksum = self.root/(name+'.sha512')
        checksum.write_text('bad')
        with patch.multiple(cache, ROOT=self.root, CACHE=self.root), patch('prepare_ansible.prepare'), \
                self.assertRaisesRegex(ValueError, 'Invalid SHA512'):
            cache.main(self.root)
        for product in ('filebeat','elasticsearch','kibana'):
            name = product+'-8.19.0-amd64.deb'
            body = product.encode()
            (self.root/name).write_bytes(body)
            (self.root/(name+'.sha512')).write_text(hashlib.sha512(body).hexdigest())
        with patch.multiple(cache, ROOT=self.root, CACHE=self.root), patch('prepare_ansible.prepare'), \
                patch.object(cache.shutil, 'copyfile') as copy_file:
            cache.main(self.root)
        copy_file.assert_not_called()
        self.assertEqual(len(json.loads((self.root/'manifest.json').read_text())), 3)

    def test_official_checksum_space_and_content_guards(self):
        destination = self.root/'cache'
        sha = hashlib.sha512(b'valid').hexdigest()
        for mode in ('invalid-sha', 'no-space', 'corrupt', 'valid'):
            with self.subTest(mode=mode), patch.multiple(cache, ROOT=self.root, CACHE=destination), \
                    patch('prepare_ansible.prepare'), patch.object(cache.urllib.request, 'urlopen', side_effect=lambda *a, **kw: io.BytesIO(('bad' if mode=='invalid-sha' else sha).encode())), \
                    patch.object(cache.shutil, 'disk_usage', return_value=SimpleNamespace(free=0 if mode=='no-space' else 4*2**30)), \
                    patch.object(cache, 'download', side_effect=lambda url, path: path.write_bytes(b'wrong' if mode=='corrupt' else b'valid')):
                if mode == 'valid':
                    cache.main()
                    self.assertEqual(len(json.loads((destination/'manifest.json').read_text())), 3)
                else:
                    with self.assertRaises((ValueError, RuntimeError)):
                        cache.main()
                    self.assertFalse((destination/'filebeat-8.19.0-amd64.deb').exists())
                    self.assertFalse(list(destination.glob('*.partial')))

    def test_vm_rejects_invalid_official_digest_and_replaces_stale_cache(self):
        opener = Mock(open=Mock(return_value=io.BytesIO(b'not-a-digest')))
        with self.assertRaisesRegex(ValueError, 'Invalid official'):
            public.fetch(self.root, '8.19.0', 'https://artifacts.elastic.co/downloads', opener)
        self.assertEqual(opener.open.call_count, 1)
        (self.root/'filebeat-8.19.0-amd64.deb').write_bytes(b'stale')
        def response(url, **kwargs):
            body=b'verified'
            return io.BytesIO(hashlib.sha512(body).hexdigest().encode() if url.endswith('.sha512') else body)
        opener.open.side_effect=response
        public.fetch(self.root, '8.19.0', 'https://artifacts.elastic.co/downloads', opener)
        self.assertEqual((self.root/'filebeat-8.19.0-amd64.deb').read_bytes(), b'verified')
        self.assertFalse(list(self.root.glob('*.partial')))
