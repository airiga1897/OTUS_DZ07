"""Release binding is a local, verified two-commit step, never publication."""
import json
from unittest.mock import Mock, patch
import unittest

import release_artifacts as release
import test_release_artifacts as fixtures


class ReleaseConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ReleaseArtifactsTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        (self.root / 'config/artifacts.json').write_text('{"github_release":{},"s3":{"bucket":"unused"}}')
        services = json.loads((self.root / 'config/services.json').read_text())
        services['artifact_delivery'] = 's3'
        (self.root / 'config/services.json').write_text(json.dumps(services))
        self.fixture.commit()
        self.assets = self.fixture.build()

    def test_configure_pins_manifest_and_changes_only_local_delivery_configuration(self):
        before = release.git(self.root, 'rev-parse', 'HEAD')
        with patch('s3_artifacts.client') as s3:
            settings = release.configure(self.root, self.assets)
        s3.assert_not_called()
        self.assertEqual(release.git(self.root, 'rev-parse', 'HEAD'), before)
        self.assertEqual(json.loads((self.root / 'config/services.json').read_text())['artifact_delivery'], 'release')
        config = json.loads((self.root / 'config/artifacts.json').read_text())
        self.assertEqual(config['github_release'], settings)
        self.assertEqual(config['s3'], {'bucket': 'unused'})
        self.assertEqual(len(release.git(self.root, 'status', '--porcelain').splitlines()), 2)

    def test_corrupt_manifest_or_package_cannot_switch_mode(self):
        for name in ('manifest.json', next(iter(self.fixture.lock))):
            path = self.assets / name
            original = path.read_bytes()
            path.write_bytes(b'corrupt')
            try:
                with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'SHA512 mismatch'):
                    release.configure(self.root, self.assets)
                self.assertEqual(json.loads((self.root / 'config/services.json').read_text())['artifact_delivery'], 's3')
            finally:
                path.write_bytes(original)

    def test_existing_stand_dirty_tree_and_wrong_source_commit_block_configuration(self):
        (self.root / '.local').mkdir()
        (self.root / '.local/context.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'Existing deployment'):
            release.configure(self.root, self.assets)
        (self.root / '.local/context.json').unlink()
        (self.root / 'app/public.py').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'clean'):
            release.configure(self.root, self.assets)
        self.fixture.commit()
        with self.assertRaisesRegex(ValueError, 'exact source commit'):
            release.configure(self.root, self.assets)

    def test_invalid_settings_are_rejected_before_manifest_download(self):
        settings = json.loads((self.assets / 'release-settings.json').read_text())
        for key, value in [('commit', None), ('commit', 'invalid'), ('manifest_sha512', None),
                           ('manifest_sha512', 'invalid'), ('manifest_size', True), ('manifest_size', 0)]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                release.checked_settings({**settings, key: value})
        with self.assertRaises(ValueError):
            release.checked_settings(None)

    def test_parallel_edit_during_asset_verification_is_not_overwritten(self):
        original = release.digest
        path = self.root / 'config/services.json'
        def edited(asset):
            value = original(asset)
            data = json.loads(path.read_text())
            data['parallel_edit'] = 'preserve-this'
            path.write_text(json.dumps(data))
            return value
        with patch.object(release, 'digest', side_effect=edited), self.assertRaisesRegex(ValueError, 'Repository changed'):
            release.configure(self.root, self.assets)
        self.assertEqual(json.loads(path.read_text())['parallel_edit'], 'preserve-this')
        self.assertEqual(json.loads(path.read_text())['artifact_delivery'], 's3')
        self.assertEqual(json.loads((self.root / 'config/artifacts.json').read_text())['github_release'], {})

    def test_configure_cli_requires_explicit_bundle_directory(self):
        with patch('sys.argv', ['release_artifacts.py', 'configure']), self.assertRaises(SystemExit):
            release.main()
        with patch('sys.argv', ['release_artifacts.py', 'configure', '--assets', str(self.assets)]), \
                patch.object(release, 'ROOT', self.root), patch.object(release, 'configure') as configure:
            release.main()
        configure.assert_called_once_with(self.root, self.assets)
