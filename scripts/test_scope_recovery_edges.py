"""Recovery failures cannot erase evidence or turn unknown absence into success."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import progress_logging as progress
import selectel_staged as staged
import staged_runtime
from catalog_cache import CatalogCache
from selectel_adapter import namespace_match
from selectel_global_router import GlobalRouter
from stage_store import RecoveryContextMissing
from test_runtime_support import lab

ROOT = Path(__file__).resolve().parents[1]


class ScopeRecoveryEdges(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(dir=ROOT / ".tools")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.local = self.root / ".local"
        self.local.mkdir()
        self.api = SimpleNamespace(ROOT=self.root, LOCAL=self.local, PREFIX="otus-dz07", save=Mock())

    def test_catalog_cache_copies_values_and_does_not_cache_failures(self) -> None:
        cache, loader = CatalogCache(), Mock(return_value={"items": [1]})
        first = cache.read("same", loader)
        first["items"].append(2)
        self.assertEqual(cache.read("same", loader), {"items": [1]})
        loader.assert_called_once()
        retry = Mock(side_effect=[TimeoutError(), {"items": []}])
        with self.assertRaises(TimeoutError):
            cache.read("retry", retry)
        self.assertEqual(cache.read("retry", retry), {"items": []})

    def test_global_zone_catalog_is_cached_but_live_routers_are_not(self) -> None:
        router = GlobalRouter.__new__(GlobalRouter)
        get_mock = Mock(side_effect=lambda path: [{"id": "zone"}] if "/zones?" in path else [])
        with patch.object(router, "get", get_mock):
            first = router.listing("zones")
            first[0]["id"] = "changed"
            self.assertEqual(router.listing("zones"), [{"id": "zone"}])
            router.listing("routers")
            router.listing("routers")
        self.assertEqual(get_mock.call_count, 3)

    def test_unknown_marker_schema_blocks_audit_and_renamed_resources_are_found(self) -> None:
        self.assertTrue(namespace_match({"metadata": {"dz07_owner": "otus-dz07-own"}}, "otus-dz07"))
        self.assertFalse(namespace_match({"metadata": None, "tags": None}, "otus-dz07"))
        for item in ({"metadata": "bad"}, {"tags": "bad"}):
            with self.assertRaises(ValueError):
                namespace_match(item, "otus-dz07")

    def test_lost_stage_context_is_read_only_and_original_error_reaches_caller(self) -> None:
        error = RecoveryContextMissing("lost")
        with (
            patch("test_runtime_support.lab.ROOT", self.root),
            patch("stage_store.require_context", side_effect=error),
            patch("selectel_staged.recovery_search") as search,
            self.assertRaises(RecoveryContextMissing) as caught,
        ):
            lab.cleanup()
        self.assertIs(caught.exception, error)
        self.assertIs(search.call_args.args[1], error)

    def test_corrupt_stage_context_runs_read_only_discovery_before_refusal(self) -> None:
        error = json.JSONDecodeError("bad", "{", 0)
        with (
            patch("test_cycle.require_access"),
            patch("staged_runtime.backend_factory", side_effect=error),
            patch("selectel_staged.recovery_search") as search,
            self.assertRaises(json.JSONDecodeError) as caught,
        ):
            staged_runtime.cleanup(self.api)
        self.assertIs(caught.exception, error)
        search.assert_called_once_with(self.api, error)

    def test_empty_scope_audit_error_preserves_files_and_original_error_even_if_report_fails(self) -> None:
        secret = self.local / "operator.json"
        secret.write_text('{"synthetic":true}')
        original = PermissionError("403")
        adapter = Mock()
        adapter.audit_empty_stages.side_effect = original
        for save_failure in (False, True):
            self.api.save.side_effect = OSError() if save_failure else None
            with (
                patch("cloud_settings.adapter", return_value=adapter),
                patch("test_cycle.require_access"),
                patch("local_runtime.cleanup_runtime") as clean,
                self.assertRaises(PermissionError) as caught,
            ):
                staged_runtime.cleanup_empty(self.api)
            self.assertIs(caught.exception, original)
            clean.assert_not_called()
            self.assertTrue(secret.exists())

    def test_recovery_search_success_is_never_destructive_cleanup_success(self) -> None:
        with patch("selectel_staged.audit_empty_scope", return_value={"audit_complete": True}):
            staged.recovery_search(self.api, RecoveryContextMissing("lost"))
        report = self.api.save.call_args.args[1]
        self.assertFalse(report["success"])
        self.assertTrue(report["audit_complete"])
        self.assertTrue(report["recovery_files_preserved"])

    def test_progress_precedes_work_and_broken_logging_cannot_mask_errors(self) -> None:
        logger = Mock()
        original = RuntimeError("operation failed")
        with (
            self.assertRaises(RuntimeError) as caught,
            progress.phase(logger, "scope", pool="ru-2", role="es") as watch,
        ):
            self.assertIn("начало", logger.log.call_args.args[1])
            with patch("progress_logging.time.perf_counter", side_effect=[watch.started, watch.started + 61]):
                watch.status("waiting")
                watch.status("waiting")
            raise original
        self.assertIs(caught.exception, original)
        logger.log.side_effect = OSError("console broken")
        with progress.phase(logger, "still succeeds"):
            pass
        with self.assertRaises(RuntimeError) as caught, progress.phase(logger, "still preserves failure"):
            raise original
        self.assertIs(caught.exception, original)

        @progress.timed("function")
        def function() -> int:
            return 7

        self.assertEqual(function(), 7)

    def test_invalid_wait_interval_is_rejected(self) -> None:
        (self.root / "config").mkdir()
        for value in (0, True, 3601):
            (self.root / "config/logging.json").write_text(
                json.dumps({"level": "INFO", "status_interval_seconds": value})
            )
            with self.assertRaises(ValueError):
                progress.configure(root=self.root)

    def test_failed_final_audit_preserves_recovery_even_if_report_cannot_be_saved(self) -> None:
        provider = Mock()
        provider.journal = {"stages": {}}
        provider.desired = {"nodes": {}}
        provider.cleanup_order.return_value = []
        provider.identities.return_value = {}
        original = TimeoutError("audit unavailable")
        provider.audit_deletion.side_effect = original
        for broken_report in (False, True):
            self.api.save.side_effect = OSError() if broken_report else None
            with (
                patch("test_cycle.require_access"),
                patch("staged_runtime.backend_factory", return_value=provider),
                patch("ansible_operation.before_destroy"),
                patch("local_runtime.cleanup_runtime") as clean,
                self.assertRaises(TimeoutError) as caught,
            ):
                staged_runtime.cleanup(self.api)
            self.assertIs(caught.exception, original)
            clean.assert_not_called()
            self.assertNotIn("cleanup_verified", provider.journal)
