"""Local gaps: readiness sentinel, incomplete initial data, and process entry points."""
import runpy
from pathlib import Path
import sys
from unittest.mock import patch

from django.test import TestCase, override_settings

from knowledge.models import InitialDataState
from knowledge.services.initial_data import load_initial_data


class ReadinessAndInitialDataGaps(TestCase):
    def test_wrong_sentinel_is_not_ready(self):
        storage = Path(self._call_storage())
        (storage / '.ready').write_bytes(b'not-the-marker\n')
        with override_settings(STATIC_ROOT=storage / 'static', REQUIRE_SHARED_MOUNT=False):
            response = self.client.get('/health/ready', HTTP_HOST='localhost')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['status'], 'storage_unavailable')

    def _call_storage(self):
        from tempfile import TemporaryDirectory
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        return folder.name

    def test_incomplete_marker_and_mismatched_fixture_do_not_report_success(self):
        InitialDataState.objects.create(key='portal-initial-v1', status='pending', fixture_sha256='abc')
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            load_initial_data()
        InitialDataState.objects.all().delete()
        with patch('knowledge.services.initial_data.serializers.serialize', return_value='[]'):
            with self.assertRaisesRegex(ValueError, 'do not match'):
                load_initial_data()

    def test_process_entry_points_stay_importable(self):
        import knowledge.management.commands.seed_lessons as seed
        from knowledge.management.commands.load_initial_data import Command as InitialDataCommand
        self.assertIs(seed.Command, InitialDataCommand)
        import config.wsgi
        self.assertTrue(callable(config.wsgi.application))
        root = Path(__file__).resolve().parents[2]
        with patch.object(sys, 'argv', ['manage.py', 'check']):
            try:
                runpy.run_path(str(root / 'app/manage.py'), run_name='__main__')
            except SystemExit as error:
                self.assertIn(error.code, (0, None))
