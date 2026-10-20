"""A clone must accept its operator's project without author-specific IDs."""
import json
import os
import sys
import unittest
import tempfile
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import selectel


class OperatorTest(unittest.TestCase):
    def test_api_and_terraform_use_same_session_without_credential_files(self):
        env = {'OS_PROJECT_ID': 'test-project', 'OS_USER_DOMAIN_NAME': 'test-account',
               'OS_USERNAME': 'test-operator', 'OS_PASSWORD': 'test-password-never-log'}
        raw = json.dumps(env).encode()
        selectel.LOCAL.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=selectel.LOCAL) as folder, \
                patch.dict(os.environ, env, clear=True), patch.object(selectel, 'LOCAL', Path(folder)), \
                patch.object(selectel, 'authenticate', return_value=('test-token', [])) as authenticate, \
                patch.object(selectel.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=raw)) as process:
            selectel.Cloud()
            config = authenticate.call_args.args[0]
            captured = selectel.tf('show', '-json', capture=True)
            self.assertEqual(captured, raw.decode())
            child_env = process.call_args.kwargs['env']
            self.assertEqual(config['project_id'], child_env['OS_PROJECT_ID'])
            self.assertEqual(config['password'], child_env['OS_PASSWORD'])
            log = (Path(folder) / 'selectel-tf-show.log').read_text()
            for value in env.values():
                self.assertNotIn(value, log)
            self.assertFalse((Path(folder) / 'operator.json').exists())


if __name__ == '__main__':
    unittest.main()
