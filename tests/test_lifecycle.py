"""Safety checks without cloud access or credentials."""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from lab import run


class LifecycleTest(unittest.TestCase):
    def test_success_keeps_deployment(self):
        provider, packages = Mock(), Mock()
        provider.LOCAL = Path('/local')
        run('deploy', provider, packages)
        provider.prepare.assert_called_once()
        packages.main.assert_called_once()
        provider.lab.configure.assert_called_once()
        provider.cleanup.assert_not_called()

    def test_verification_checks_application_after_recovery(self):
        provider = Mock()
        calls = []
        provider.lab.verify.side_effect = lambda: calls.append('application')
        provider.failover.side_effect = lambda: calls.append('failure-and-recovery')
        run('verify', provider, Mock())
        self.assertEqual(calls, ['application', 'failure-and-recovery', 'application'])
        provider.cleanup.assert_not_called()

    def test_cloud_failure_cleans_up_even_if_diagnostics_fail(self):
        for action in ['deploy', 'verify']:
            with self.subTest(action=action):
                provider = Mock()
                provider.LOCAL = Path('/local')
                provider.lab.verify.side_effect = RuntimeError('unhealthy')
                provider.diagnostics.side_effect = RuntimeError('SSH unavailable')
                with self.assertRaises(RuntimeError):
                    run(action, provider, Mock())
                provider.cleanup.assert_called_once()

    def test_preparation_failure_preserves_existing_resources(self):
        provider = Mock()
        provider.prepare.side_effect = RuntimeError('quota unavailable')
        with self.assertRaises(RuntimeError):
            run('deploy', provider, Mock())
        provider.tf.assert_not_called()
        provider.cleanup.assert_not_called()

    def test_destroy_does_not_deploy_or_run_failure_experiment(self):
        provider = Mock()
        run('destroy', provider, Mock())
        provider.cleanup.assert_called_once()
        provider.tf.assert_not_called()
        provider.failover.assert_not_called()


if __name__ == '__main__':
    unittest.main()
