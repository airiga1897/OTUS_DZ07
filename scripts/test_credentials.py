"""Credential sources are explicit and share one schema."""
import os
import unittest
from unittest.mock import patch

import credentials


class CredentialTests(unittest.TestCase):
    def test_file_selector_is_ignored(self):
        with patch.dict(os.environ, {'OTUS_SELECTEL_AUTH': 'missing.json'}, clear=True):
            with self.assertRaisesRegex(ValueError, 'Incomplete'):
                credentials.load_credentials()

    def test_environment_mapping_and_redaction_use_schema(self):
        values = {'domain_name': 'sample-account', 'project_id': 'sample-project',
                  'username': 'sample-user', 'password': 'sample-password'}
        env = credentials.to_environment(values)
        with patch.dict(os.environ, env, clear=True):
            config = credentials.load_credentials()
        self.assertEqual(set(credentials.credential_values(config)), set(values.values()))


if __name__ == '__main__':
    unittest.main()
