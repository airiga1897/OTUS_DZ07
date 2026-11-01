import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import cache_packages

spec = importlib.util.spec_from_file_location('vm_downloads', ROOT / 'scripts/remote/download_packages.py')
downloads = importlib.util.module_from_spec(spec)
spec.loader.exec_module(downloads)


class VMDownloadsTest(unittest.TestCase):
    def test_vm_verifies_every_download_and_reuses_matching_files(self):
        with tempfile.TemporaryDirectory(dir=ROOT / '.local') as directory:
            root = Path(directory)
            calls = []
            def open_url(url, **kwargs):
                calls.append(url)
                data = b'synthetic-public-package'
                return io.BytesIO(hashlib.sha512(data).hexdigest().encode() if url.endswith('.sha512') else data)
            opener = Mock()
            opener.open.side_effect = open_url
            for _ in range(2):
                downloads.fetch(root, '8.19.0', 'https://artifacts.elastic.co/downloads', opener)
            self.assertEqual(len(calls), 9)  # Six checksum requests, three downloads.
            self.assertEqual(len(json.loads((root / 'manifest.json').read_text())), 3)
            self.assertFalse(list(root.glob('*.partial')))

    def test_bad_content_is_never_installed_or_left_as_partial(self):
        with tempfile.TemporaryDirectory(dir=ROOT / '.local') as directory:
            root = Path(directory)
            opener = Mock()
            opener.open.side_effect = [io.BytesIO(b'0' * 128), io.BytesIO(b'corrupt')]
            with self.assertRaisesRegex(ValueError, 'SHA512 mismatch'):
                downloads.fetch(root, '8.19.0', 'https://artifacts.elastic.co/downloads', opener)
            self.assertEqual(list(root.iterdir()), [])

    def test_online_deploy_never_downloads_packages_on_controller(self):
        with tempfile.TemporaryDirectory(dir=ROOT / '.local') as directory:
            root = Path(directory)
            (root / 'config').mkdir()
            (root / 'config/services.json').write_text('{"artifact_delivery":"internet"}')
            with patch.object(cache_packages, 'ROOT', root), patch('prepare_ansible.prepare') as wheels, \
                 patch.object(cache_packages.urllib.request, 'urlopen') as request:
                cache_packages.main()
                wheels.assert_not_called()
                request.assert_not_called()
