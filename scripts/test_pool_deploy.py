"""Resumed deployments preserve resources; an owned fresh cycle may retry capacity."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import lab

ROOT = Path(__file__).resolve().parents[1]


class DeploymentPoolTest(unittest.TestCase):
    def test_owned_temporary_cycle_retries_capacity_after_verified_cleanup(self):
        import json
        for cleanup_fails in (False, True):
            with tempfile.TemporaryDirectory(dir=ROOT / '.local') as directory:
                root = Path(directory)
                local = root / '.local'
                local.mkdir()
                (local / 'test-cycle.json').write_text(json.dumps({'run_id': 'owned'}))
                with patch.multiple(lab, ROOT=root, LOCAL=local, _TEST_CYCLE_ID='owned'), \
                     patch.object(lab, 'managed_ids', return_value={}), \
                     patch.object(lab, 'desired_topology', return_value={}), \
                     patch.object(lab.pools, 'selected', return_value=None), \
                     patch('infrastructure.apply', side_effect=lab.pools.TerraformError('NoValidHost')), \
                     patch.object(lab.pools, 'scheduler_unavailable', return_value=True), \
                     patch.object(lab, 'diagnostics'), patch.object(lab, 'save'), \
                     patch.object(lab, 'placement', return_value={'region': 'ru-9', 'availability_zone': 'ru-9a'}), \
                     patch.object(lab, 'cleanup', side_effect=RuntimeError('audit failed') if cleanup_fails else None) as cleanup:
                    with self.assertRaises(RuntimeError if cleanup_fails else lab.pools.CapacityRetry):
                        lab.deploy()
                    cleanup.assert_called_once_with(clean_local=False)

    def test_capacity_failure_preserves_preparation_and_partial_resources(self):
        for cleanup_fails in (False, True):
            with self.subTest(cleanup_fails=cleanup_fails), tempfile.TemporaryDirectory(dir=ROOT / '.local') as d:
                root = Path(d)
                local = root / '.local'
                local.mkdir()
                (local / 'key').write_text('synthetic')
                with patch.multiple(lab, ROOT=root, LOCAL=local), \
                     patch.object(lab.topology, 'load', return_value={}), \
                     patch.object(lab, 'managed_ids', return_value={}), patch.object(lab, 'preflight'), \
                     patch.object(lab, 'checked_plan'), patch.object(lab.pools, 'phase'), \
                     patch.object(lab, 'diagnostics'), \
                     patch.object(lab, 'tf', side_effect=lab.pools.TerraformError('NoValidHost')), \
                     patch.object(lab, 'cleanup', side_effect=RuntimeError('audit failed') if cleanup_fails else None) as cleanup:
                    with self.assertRaises(lab.pools.TerraformError):
                        lab.deploy()
                    cleanup.assert_not_called()
                    self.assertTrue((local / 'key').exists())
