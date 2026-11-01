"""Remote verifier is importable offline; its IO is replaced by local mocks."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from unittest.mock import MagicMock
import contextlib
import io
import json

ROOT = Path(__file__).resolve().parents[1]


def verifier():
    import importlib
    return importlib.import_module('remote.verify')


class RemoteExpansionTests(unittest.TestCase):
    def test_http_verification_requires_probe_to_be_private(self):
        module = verifier()
        import urllib.error
        record = {'slug': 'baseline-synthetic', 'body': 'committed-content'}
        response = MagicMock()
        response.__enter__.return_value.status = 200
        response.__enter__.return_value.read.return_value = b'course materials'
        missing = urllib.error.HTTPError('http://127.0.0.1/articles/baseline-synthetic/', 404, 'Not Found', {}, None)
        with patch.object(module.urllib.request, 'urlopen', side_effect=[response, response, missing]):
            module.check_http(record)
        with patch.object(module.urllib.request, 'urlopen', return_value=response):
            with self.assertRaisesRegex(AssertionError, 'publicly accessible'):
                module.check_http(record)

    def test_legacy_visibility_migration_preserves_manifest_but_content_changes_fail(self):
        module = verifier()
        conn = Mock()
        slug = 'baseline-' + 'a' * 32
        row = (7, slug, slug, 6, 'proof', 'committed-' + 'b' * 32, '', True, 'timestamp')
        conn.execute.return_value.fetchall.side_effect = [[row], [], [], []]
        expected = module.data_manifest(conn)
        hidden = (*row[:7], False, row[8])
        conn.execute.return_value.fetchall.side_effect = [[hidden], [], [], []]
        module.check_data(expected, conn)
        edited = (*hidden[:5], 'committed-' + 'c' * 32, *hidden[6:])
        conn.execute.return_value.fetchall.side_effect = [[edited], [], [], []]
        with self.assertRaises(AssertionError): module.check_data(expected, conn)

    def test_expansion_writes_a_distinct_record_after_reading_the_baseline(self):
        module = verifier()
        run_id = 'a' * 32
        old = {'id': 1, 'slug': 'baseline', 'body': 'old'}
        new = {'id': 2, 'slug': 'expanded', 'body': 'new'}
        members = {'db1': {'role': 'primary', 'state': 'running'},
                   'db2': {'role': 'replica', 'state': 'running'},
                   'db3': {'role': 'replica', 'state': 'running'}}
        with tempfile.TemporaryDirectory(dir=ROOT / '.local') as directory:
            app = Path(directory)
            (app / 'probes').mkdir()
            (app / 'probes' / (run_id + '.json')).write_text(json.dumps({'baseline': old, 'media': {}, 'data': {}}))
            (app / 'initial-data-result.json').write_text('{}')
            connection = MagicMock()
            connection.__enter__.return_value.execute.return_value.fetchone.return_value = (10,)
            output = io.StringIO()
            with patch.object(module, 'APP', app), patch.object(module, 'initialize'), \
                 patch.object(module, 'lab', {'private_ips': dict.fromkeys(['app', 'db1', 'db2', 'db3', 'es1', 'es2', 'es3', 'kibana'], '127.0.0.1')}), \
                 patch.object(module.sys, 'argv', ['verify.py', 'expanded', run_id]), \
                 patch.object(module, 'sql_connect', return_value=connection), \
                 patch.object(module, 'cluster', return_value=members), \
                 patch.object(module, 'check_data'), patch.object(module, 'check_record'), \
                 patch.object(module, 'check_media'), patch.object(module, 'check_http_media'), \
                 patch.object(module, 'check_http'), patch.object(module, 'insert_record', return_value=new) as insert, \
                 contextlib.redirect_stdout(output):
                module.main()
            insert.assert_called_once_with('expanded')
            self.assertEqual(json.loads(output.getvalue())['new_record'], new)

    def test_media_manifest_detects_changes_and_allows_new_files(self):
        module = verifier()
        with tempfile.TemporaryDirectory(dir=ROOT / '.local') as directory:
            root = Path(directory)
            (root / 'existing.png').write_bytes(b'original')
            expected = module.media_manifest(root)
            (root / 'new.png').write_bytes(b'new')
            module.check_media(expected, root)
            (root / 'existing.png').write_bytes(b'changed')
            with self.assertRaises(AssertionError):
                module.check_media(expected, root)

    def test_data_manifest_detects_edits_and_allows_new_rows(self):
        module = verifier()
        conn = Mock()
        conn.execute.return_value.fetchall.return_value = [(1, 'original')]
        expected = module.data_manifest(conn)
        conn.execute.return_value.fetchall.return_value = [(1, 'original'), (2, 'new')]
        module.check_data(expected, conn)
        conn.execute.return_value.fetchall.return_value = [(1, 'edited')]
        with self.assertRaises(AssertionError):
            module.check_data(expected, conn)

    def test_membership_probe_includes_added_replica(self):
        module = verifier()
        module.lab = {'private_ips': dict.fromkeys(['app', 'db1', 'db2', 'db3', 'db4'], '127.0.0.1')}
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.object(module.urllib.request, 'urlopen', return_value=response) as request, \
             patch.object(module.json, 'load', return_value={'role': 'replica', 'state': 'running'}):
            self.assertEqual(set(module.cluster()), {'db1', 'db2', 'db3', 'db4'})
            self.assertEqual(request.call_count, 4)
