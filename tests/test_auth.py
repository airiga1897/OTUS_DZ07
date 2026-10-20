"""A clone must accept its operator's project without author-specific IDs."""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import selectel


class OperatorTest(unittest.TestCase):
    def test_saved_operator_works_in_fresh_session(self):
        config = {'project_id': 'own-project', 'region': 'ru-9'}
        with patch.dict(os.environ, {'OTUS_SELECTEL_AUTH': 'operator.json'}, clear=True), \
                patch.object(Path, 'read_text', return_value=json.dumps(config)):
            self.assertEqual(selectel.auth(), config)

    def test_conflicting_project_is_rejected(self):
        config = {'project_id': 'own-project', 'region': 'ru-9'}
        with patch.dict(os.environ, {'OTUS_SELECTEL_AUTH': 'operator.json', 'OS_PROJECT_ID': 'other-project'}, clear=True), \
                patch.object(Path, 'read_text', return_value=json.dumps(config)):
            with self.assertRaisesRegex(RuntimeError, 'Unexpected Selectel'):
                selectel.auth()


if __name__ == '__main__':
    unittest.main()
