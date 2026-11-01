"""Offline integration contract for portal initialization; no real database or cloud."""
from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
import tempfile
import subprocess
import io
from contextlib import redirect_stdout
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]


def initializer():
    import importlib
    return importlib.import_module('remote.initial_data')


class InitialDataRuntimeTests(unittest.TestCase):
    def test_migrations_precede_guarded_fixture_load_through_haproxy(self):
        module = initializer()
        with tempfile.TemporaryDirectory(dir=ROOT / '.local') as directory:
            root = Path(directory)
            base, app = root / 'input', root / 'app'
            base.mkdir()
            app.mkdir()
            (base / 'settings.json').write_text(json.dumps({'passwords': {
                'postgres': 'synthetic-admin', 'app': 'synthetic-app', 'django': 'synthetic-key'}}))
            report = {'source': 'bundled_DZ04_materials_fixture', 'status': 'loaded', 'loaded_now': True}
            with patch.object(module, 'BASE', base), patch.object(module, 'APP', app), \
                 patch.object(module, 'wait_for_primary'), patch.object(module, 'prepare_database'), \
                 patch.object(module.subprocess, 'run') as run:
                run.return_value.stdout = json.dumps(report)
                module.main()
                calls = run.call_args_list
                self.assertEqual([c.args[0][2:] for c in calls], [['migrate', '--noinput'], ['load_initial_data']])
                self.assertTrue(all(c.kwargs['env']['DB_PORT'] == '5432' for c in calls))
                self.assertTrue(all(c.kwargs['env']['DB_HOST'] == '127.0.0.1' for c in calls))
                self.assertEqual(json.loads((app / 'initial-data-result.json').read_text()), report)
                report['loaded_now'] = False
                run.side_effect = [subprocess.CompletedProcess([], 0, 'No migrations to apply.'),
                                   subprocess.CompletedProcess([], 0, json.dumps(report))]
                out = io.StringIO()
                with redirect_stdout(out):
                    module.main()
                self.assertIn('PORTAL_CHANGED=false', out.getvalue())

    def test_existing_role_and_database_are_never_recreated(self):
        module = initializer()
        conn = MagicMock()
        conn.execute.return_value.fetchone.side_effect = [(1,), ('otus',)]
        module.prepare_database(conn, 'synthetic-app')
        self.assertEqual(conn.execute.call_count, 2)

    def test_existing_database_with_other_owner_is_rejected(self):
        module = initializer()
        conn = MagicMock()
        conn.execute.return_value.fetchone.side_effect = [(1,), ('different-owner',)]
        with self.assertRaisesRegex(RuntimeError, 'different owner'):
            module.prepare_database(conn, 'synthetic-app')
        self.assertEqual(conn.execute.call_count, 2)


if __name__ == '__main__':
    unittest.main()
