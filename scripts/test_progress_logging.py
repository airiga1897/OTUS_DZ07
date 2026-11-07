"""Progress precedes slow work, stays configurable and never enables SDK tracing."""

import contextlib
import io
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import pool_selection
import privacy
import progress_logging as progress
import selectel_staged
import staged_runtime
from stage_journal import StageRecord
from test_runtime_support import lab

ROOT = Path(__file__).resolve().parents[1]


def call_unchecked(function: object, *args: object) -> None:
    if not callable(function):
        raise AssertionError("expected callable")
    function(*args)


class ProgressLoggingTests(unittest.TestCase):
    def test_startup_precedes_dependency_factory_and_import_has_no_config_reads(self) -> None:
        import ast

        import lab as cli

        tree = ast.parse(Path(cli.__file__).read_text())
        main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
        factory = next(n for n in ast.walk(main) if isinstance(n, ast.ImportFrom) and n.module == "runtime")
        announcements = [
            n.lineno
            for n in ast.walk(main)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "info"
        ]
        self.assertLess(min(announcements), factory.lineno)
        self.assertFalse(
            any(
                isinstance(n, ast.Assign)
                and isinstance(n.value, ast.Call)
                and isinstance(n.value.func, ast.Attribute)
                and n.value.func.attr in ("load", "read_text")
                for n in tree.body
            )
        )

    def test_preparation_duration_is_reported_before_access_or_dispatch(self) -> None:
        with (
            patch.object(lab.time, "perf_counter", side_effect=[10, 12]),
            patch("lab.configure_logging"),
            patch("sys.argv", ["lab.py", "destroy"]),
            patch.object(lab, "destroy") as destroy,
            patch("test_cycle.require_access") as access,
            self.assertLogs(lab.LOGGER, "INFO") as logs,
        ):

            def check(*args: object, **kwargs: object) -> None:
                self.assertTrue(any("длительность 2.0 с" in line for line in logs.output))

            access.side_effect = check
            lab.main()
            destroy.assert_called_once_with(False)

    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory(dir=ROOT / ".tools")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        (self.root / "config").mkdir()
        (self.root / "config/logging.json").write_text('{"level":"INFO"}')
        self.logger = logging.getLogger("otus_dz07")
        before = (self.logger.handlers[:], self.logger.level, self.logger.propagate)
        self.logger.handlers = []

        def restore() -> None:
            for handler in self.logger.handlers:
                handler.close()
            self.logger.handlers, self.logger.level, self.logger.propagate = before

        self.addCleanup(restore)

    def test_configuration_levels_redaction_and_sdk_isolation(self) -> None:
        stream = io.StringIO()
        sdk = logging.getLogger("botocore")
        root_level, sdk_level = logging.getLogger().level, sdk.getEffectiveLevel()
        with patch.object(privacy, "_values", {"PRIVATE-SENTINEL"}), contextlib.redirect_stderr(stream):
            progress.configure(root=self.root)
            logger = progress.get_logger("acceptance")
            logger.debug("hidden detail")
            logger.info("Check started: %s", "PRIVATE-SENTINEL")
            self.assertNotIn("hidden detail", stream.getvalue())
            self.assertNotIn("PRIVATE-SENTINEL", stream.getvalue())
            self.assertIn("INFO Check started: [REDACTED]", stream.getvalue())
            progress.configure("DEBUG", self.root)
            logger.debug("visible detail")
        self.assertIn("DEBUG visible detail", stream.getvalue())
        self.assertEqual(logging.getLogger().level, root_level)
        self.assertEqual(sdk.getEffectiveLevel(), sdk_level)
        self.assertFalse(self.logger.propagate)

    def test_reconfiguration_has_no_duplicate_owned_handler_and_preserves_external_handlers(self) -> None:
        external = logging.NullHandler()
        self.logger.addHandler(external)
        stream = io.StringIO()
        with contextlib.redirect_stderr(stream):
            progress.configure("WARNING", self.root)
            progress.configure("ERROR", self.root)
            progress.get_logger("case").warning("not shown")
            progress.get_logger("case").error("single error")
        self.assertIn(external, self.logger.handlers)
        self.assertEqual(len(self.logger.handlers), 2)
        self.assertNotIn("not shown", stream.getvalue())
        self.assertEqual(stream.getvalue().count("single error"), 1)

    def test_invalid_level_is_rejected_without_changing_handlers(self) -> None:
        invalid_levels: tuple[object, ...] = ("TRACE", 123, {})
        for level in invalid_levels:
            with self.subTest(level=level), self.assertRaisesRegex(ValueError, "logging level"):
                call_unchecked(progress.configure, level, self.root)
        self.assertEqual(self.logger.handlers, [])

    def test_preflight_announces_checks_before_their_calls(self) -> None:
        provider = Mock(policy={"operator_ip_services": ["synthetic"], "admin_cidrs": []}, journal={})
        with self.assertLogs(staged_runtime.LOGGER, "INFO") as captured:

            def address(*args: object) -> list[str]:
                self.assertIn("IPv4 оператора: начало", captured.output[-1])
                return ["8.8.8.8/32"]

            def existing() -> None:
                self.assertIn("сохранённых ресурсов: начало", captured.output[-1])

            def preflight() -> None:
                self.assertIn("проверка квот: начало", captured.output[-1])

            provider.validate_existing.side_effect = existing
            provider.preflight.side_effect = preflight
            with (
                patch.object(staged_runtime, "backend_factory", return_value=provider),
                patch("operator_network.discover_cidrs", side_effect=address),
            ):
                self.assertIs(staged_runtime.checked_backend(lab), provider)
        self.assertIn("размещения: успешно", captured.output[-1])

    def test_pool_catalog_start_is_visible_even_when_the_first_api_request_fails(self) -> None:
        catalog = [
            {
                "type": kind,
                "endpoints": [{"interface": "public", "region": "ru-2", "url": "https://catalog.invalid/" + kind}],
            }
            for kind in pool_selection.SERVICES
        ]
        original = OSError("private transport detail")
        with self.assertLogs(pool_selection.LOGGER, "INFO") as captured:

            def images(*args: object) -> None:
                self.assertIn("Пул ru-2: начало чтения каталогов", captured.output[-1])
                raise original

            with (
                patch.object(pool_selection, "authenticate", return_value=("synthetic-token", catalog)),
                patch.object(pool_selection, "image_catalog", side_effect=images),
                self.assertRaises(OSError) as error,
            ):
                list(
                    pool_selection.candidates(
                        {
                            "disk_type_patterns": ["universal.*"],
                            "pools": ["ru-2"],
                            "profiles": {"app": {"vcpus": 2, "ram_mb": 4096}},
                        },
                        {},
                    )
                )
        self.assertIs(error.exception, original)
        self.assertNotIn("private transport detail", str(captured.output))
        self.assertNotIn("synthetic-token", str(captured.output))

    def test_cleanup_progress_precedes_ownership_apply_and_api_confirmation(self) -> None:
        for failed in (False, True):
            with self.subTest(failed=failed):
                backend = selectel_staged.Backend.__new__(selectel_staged.Backend)
                record: StageRecord = {"status": "active", "candidate": {"region": "ru-2"}}
                backend.journal = {"stages": {"es1": record}, "destroy_requested": True}
                backend.desired = {"nodes": {"es1": {"host": 31, "role": "es"}}}
                save = Mock()
                backend.store = Mock()
                backend.store.folder.return_value = self.root
                (self.root / "main.tf.json").write_text("{}")
                with patch.object(backend, "save", save), self.assertLogs("otus_dz07", "INFO") as captured:

                    def owned(*args: object, **kwargs: object) -> None:
                        self.assertIn("начало проверки принадлежности", captured.output[-1])

                    def apply(*args: object, **kwargs: object) -> None:
                        self.assertIn("начало Terraform apply", captured.output[-1])

                    def absent(*args: object, failed: bool = failed) -> None:
                        self.assertIn("начало подтверждения через state и API", captured.output[-1])
                        if failed:
                            raise RuntimeError("API unavailable")

                    backend.store.apply.side_effect = apply
                    with (
                        patch("selectel_staged_audit.assert_stage_owned", side_effect=owned),
                        patch("selectel_staged_audit.assert_stage_absent", side_effect=absent),
                    ):
                        if failed:
                            with self.assertRaises(RuntimeError):
                                backend.cleanup_attempt("es1")
                        else:
                            backend.cleanup_attempt("es1")
                self.assertEqual(record["status"], "deleting" if failed else "deleted")
                self.assertEqual(any("Удаление этапа es1: подтверждено" in m for m in captured.output), not failed)

    def test_cli_log_level_keeps_existing_open_command_interface(self) -> None:
        with (
            patch("sys.argv", ["lab.py", "open", "--log-level", "DEBUG"]),
            patch.object(lab, "ROOT", self.root),
            patch.object(lab, "LOCAL", self.root / ".local"),
            patch("lab.configure_logging") as configure,
            patch("test_cycle.require_access"),
            patch("stage_store.deployment_exists", return_value=False),
            patch("local_runtime.open_portal") as opened,
        ):
            lab.main()
        configure.assert_called_once_with("DEBUG")
        opened.assert_called_once_with(self.root, 6)
