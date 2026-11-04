"""A failed child deletion must preserve its network until an explicit retry."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import staged_runtime
import test_staged_lifecycle as fixtures

ROOT = Path(__file__).resolve().parents[1]


class CleanupDependencyRecovery(unittest.TestCase):
    def test_failed_node_preserves_parents_but_cleans_independent_nodes_then_retry_finishes(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tools") as directory:
            helper = fixtures.RetainedLifecycleTest()
            api, provider = helper.fixture(Path(directory))
            names = ["app", "es1", "es2", "es3", "network-ru-9", "connection-ru-9", "global"]
            provider.journal["stages"] = {n: {"status": "active", "candidate": {"region": "ru-9"}} for n in names}
            provider.objects = {n: n + "-owned" for n in names}
            provider.audit_deletion = Mock(return_value={"pools_audited": ["ru-9"], "global_router_remaining": 0})
            real_cleanup = provider.cleanup_attempt
            attempted = []

            def cleanup(name) -> None:
                attempted.append(name)
                if name == "es3":
                    raise RuntimeError("synthetic plugin startup timeout")
                real_cleanup(name)

            provider.cleanup_attempt = cleanup
            with helper.patches(provider), self.assertRaises(RuntimeError):
                staged_runtime.cleanup(api)
            self.assertEqual(set(attempted), {"app", "es1", "es2", "es3"})
            self.assertEqual(set(provider.objects), {"es3", "network-ru-9", "connection-ru-9", "global"})
            provider.audit_deletion.assert_not_called()
            report = json.loads((api.ROOT / "docs/cleanup-result.json").read_text())
            self.assertFalse(report["success"])
            self.assertTrue(report["recovery_files_preserved"])
            self.assertEqual(report["stage_errors"]["network-ru-9"], "BlockedByCleanupDependency")
            provider.cleanup_attempt = real_cleanup
            with helper.patches(provider), patch("artifact_lifecycle.release"):
                staged_runtime.cleanup(api)
            self.assertEqual(provider.objects, {})
            provider.audit_deletion.assert_called_once()
            self.assertFalse(provider.path.exists())

    def test_failed_connection_preserves_later_parent_stages(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tools") as directory:
            helper = fixtures.RetainedLifecycleTest()
            api, provider = helper.fixture(Path(directory))
            names = ["network-ru-9", "connection-ru-9", "global"]
            provider.journal["stages"] = {n: {"status": "active", "candidate": {"region": "ru-9"}} for n in names}
            provider.objects = {n: n + "-owned" for n in names}
            provider.cleanup_attempt = Mock(side_effect=OSError("synthetic connection failure"))
            with helper.patches(provider), self.assertRaises(RuntimeError):
                staged_runtime.cleanup(api)
            provider.cleanup_attempt.assert_called_once_with("connection-ru-9")
            self.assertEqual(set(provider.objects), set(names))
