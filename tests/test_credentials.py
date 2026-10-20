"""Credential source, isolation and disclosure contracts; no cloud access."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import credentials
import privacy


class CredentialsTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / '.local')
        self.addCleanup(self.directory.cleanup)
        self.local = Path(self.directory.name)
        self.config = {'auth_url': 'https://cloud.api.selcloud.ru/identity/v3', 'region': 'ru-9',
                       'domain_name': 'test-account', 'project_id': 'test-project',
                       'username': 'test-operator', 'password': 'test-password-never-log'}
        self.env = {'OS_USER_DOMAIN_NAME': self.config['domain_name'], 'OS_PROJECT_ID': self.config['project_id'],
                    'OS_USERNAME': self.config['username'], 'OS_PASSWORD': self.config['password']}

    def test_environment_needs_no_operator_file(self):
        with patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(credentials.load(self.local), self.config)
        self.assertEqual(list(self.local.iterdir()), [])

    def test_existing_file_is_not_an_implicit_fallback(self):
        (self.local / 'operator.json').write_text(json.dumps(self.config))
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, 'env.ps1'):
                credentials.load(self.local)

    def test_explicit_development_file(self):
        path = self.local / 'operator.json'
        path.write_text(json.dumps(self.config))
        with patch.dict(os.environ, {'OTUS_SELECTEL_AUTH': str(path)}, clear=True):
            self.assertEqual(credentials.load(self.local), self.config)

    def test_partial_environment_does_not_fall_back_to_file(self):
        path = self.local / 'operator.json'
        path.write_text(json.dumps(self.config))
        with patch.dict(os.environ, {'OS_PASSWORD': 'incomplete', 'OTUS_SELECTEL_AUTH': str(path)}, clear=True):
            with self.assertRaises(RuntimeError):
                credentials.load(self.local)

    def test_context_rejects_wrong_project_without_disclosure(self):
        (self.local / 'selectel-context.json').write_text(json.dumps({'project_id': 'another-project', 'region': 'ru-9'}))
        with patch.dict(os.environ, self.env, clear=True):
            with self.assertRaises(RuntimeError) as failure:
                credentials.load(self.local)
        self.assertNotIn('another-project', str(failure.exception))
        self.assertNotIn(self.config['project_id'], str(failure.exception))

    def test_context_contains_binding_without_operator_identity(self):
        credentials.bind(self.local, self.config)
        context = json.loads((self.local / 'selectel-context.json').read_text())
        self.assertEqual(context, {'project_id': self.config['project_id'], 'region': 'ru-9'})
        self.assertNotIn(self.config['password'], json.dumps(context))
        self.assertNotIn(self.config['username'], json.dumps(context))

    def test_logs_and_evidence_redact_all_operator_fields(self):
        with patch.dict(os.environ, self.env, clear=True):
            credentials.load(self.local)
        raw = json.dumps(self.config)
        clean = privacy.redact(raw)
        for field in ('domain_name', 'project_id', 'username', 'password'):
            self.assertNotIn(self.config[field], clean)
        evidence = privacy.sanitize({'project_id': 'unknown-project', 'access_token': 'unknown-token',
                                     'message': raw, 'resources': 3})
        self.assertNotIn('unknown-token', json.dumps(evidence))
        self.assertNotIn('unknown-project', json.dumps(evidence))
        self.assertEqual(evidence['resources'], 3)


if __name__ == '__main__':
    unittest.main()
