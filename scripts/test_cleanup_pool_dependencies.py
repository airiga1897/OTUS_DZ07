"""Independent pools can clean up while true parents retain recovery state."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import staged_runtime
import test_staged_lifecycle as fixtures
from cleanup_dependencies import children

ROOT = Path(__file__).resolve().parents[1]


class PoolCleanupTests(unittest.TestCase):
    def test_malformed_or_unknown_pool_preserves_every_possible_parent(self) -> None:
        records: tuple[object, ...] = (
            None,
            {"candidate": None},
            {"candidate": []},
            {"candidate": {"region": 7}},
            {"candidate": {"region": "unknown"}},
        )
        for record in records:
            stages: dict[str, object] = {
                "app": record,
                "network-ru-9": {},
                "connection-ru-9": {},
                "network-ru-3": {},
                "connection-ru-3": {},
                "global": {},
            }
            with self.subTest(record=record):
                graph = children(stages, {"app"})
                for parent in ("network-ru-9", "connection-ru-9", "network-ru-3", "connection-ru-3"):
                    self.assertIn("app", graph[parent])

    def test_failed_pool_does_not_block_independent_network_cleanup(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tools") as directory:
            helper = fixtures.RetainedLifecycleTest()
            api, provider = helper.fixture(Path(directory))
            names = ["app", "es1", "network-ru-9", "network-ru-3", "connection-ru-9", "connection-ru-3", "global"]
            provider.journal["stages"] = {
                n: {"status": "active", "candidate": {"region": "ru-3" if n.endswith("ru-3") or n == "app" else "ru-9"}}
                for n in names
            }
            provider.objects = {n: n + "-owned" for n in names}
            real = provider.cleanup_attempt
            attempted: list[str] = []

            def cleanup(name: str) -> None:
                attempted.append(name)
                if name == "es1":
                    raise OSError("synthetic fault")
                real(name)

            with (
                patch.object(provider, "cleanup_attempt", side_effect=cleanup),
                helper.patches(provider),
                self.assertRaises(RuntimeError),
            ):
                staged_runtime.cleanup(api)
            self.assertIn("network-ru-3", attempted)
            self.assertLess(attempted.index("connection-ru-3"), attempted.index("network-ru-3"))
            self.assertEqual(set(provider.objects), {"es1", "network-ru-9", "connection-ru-9", "global"})
            with (
                patch.object(provider, "audit_deletion", Mock(return_value={})),
                patch.object(provider, "cleanup_attempt", side_effect=real),
                helper.patches(provider),
                patch("artifact_lifecycle.release"),
            ):
                staged_runtime.cleanup(api)
            self.assertFalse(provider.objects)

    def test_unknown_placement_blocks_all_potential_parents(self) -> None:
        stages: dict[str, dict[str, object]] = {"app": {}, "network-ru-9": {}, "connection-ru-9": {}, "global": {}}
        result = children(stages, {"app": {}})
        self.assertEqual(result["network-ru-9"], {"app", "connection-ru-9"})
        self.assertEqual(result["connection-ru-9"], {"app"})
        self.assertEqual(result["global"], {"connection-ru-9"})
        with self.assertRaises(ValueError):
            children({"unrecognized": {}}, {})
