"""Callback metadata failures and safe diagnostics, without SSH or Ansible."""

import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import ansible_operation as controller
import ansible_progress as progress
import test_ansible_operation as operation_fixtures
import test_ansible_progress as fixtures
from remote import ansible_events as events


class CallbackDiagnostics(unittest.TestCase):
    def setUp(self) -> None:
        self.h = fixtures.AnsibleProgressTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    def test_missing_task_source_and_nullable_results_do_not_disable_callback(self) -> None:
        callback = self.h.callback()
        task = fixtures.task(source=None)
        callback.v2_runner_on_start(fixtures.host(), task)
        callback.v2_runner_on_ok(fixtures.result(task, fixtures.host(), {"results": None}))
        self.assertIsNotNone(callback.writer)
        page = events.read_page(self.h.path, {"offset": 0, "seq": 0})
        self.assertEqual([e["status"] for e in page["events"]], ["start", "ok"])
        self.assertEqual([e["task"] for e in page["events"]], ["[task name hidden]"] * 2)

    def test_no_log_never_reads_task_source(self) -> None:
        callback = self.h.callback()
        task = fixtures.task(no_log=True)
        with patch.object(task, "get_ds", Mock(side_effect=AssertionError("SECRET"))) as get_ds:
            callback.v2_runner_on_start(fixtures.host(), task)
        get_ds.assert_not_called()
        self.assertIsNotNone(callback.writer)
        self.assertNotIn("SECRET", self.h.path.read_text())

    def test_valid_unicode_task_does_not_overflow_the_journal_line(self) -> None:
        callback = self.h.callback()
        callback.v2_runner_on_start(fixtures.host(), fixtures.task("\U0001f642" * 200))
        self.assertIsNotNone(callback.writer)
        self.assertEqual(
            events.read_page(self.h.path, {"offset": 0, "seq": 0})["events"][0]["task"], "\U0001f642" * 200
        )

    def test_metadata_matrix_keeps_callback_alive_and_never_serializes_inputs(self) -> None:
        callback = self.h.callback()
        sources: tuple[object, ...] = (None, [], "SECRET", {}, {"name": None}, {"name": "Literal task"})
        for source in sources:
            task = fixtures.task(source=source)
            payloads: tuple[object, ...] = (
                None,
                [],
                "SECRET",
                {},
                {"results": None},
                {"results": []},
                {"results": "SECRET"},
                {"results": [{"_ansible_no_log": True}]},
                {"results": [None, {}, "SECRET"]},
            )
            for payload in payloads:
                callback.v2_runner_on_ok(fixtures.result(task, fixtures.host(), payload))
        self.assertIsNotNone(callback.writer)
        self.assertFalse(self.h.path.with_suffix(".unavailable").exists())
        self.assertNotIn("SECRET", self.h.path.read_text())
        page = events.read_page(self.h.path, {"offset": 0, "seq": 0})
        self.assertEqual(len(page["events"]), 54)

    def test_failure_metadata_is_atomic_private_and_allowlisted(self) -> None:
        marker = self.h.root / "failure.json"
        for error, category in (
            (PermissionError("SECRET"), "permission"),
            (FileExistsError("SECRET"), "existing"),
            (OSError("SECRET"), "io"),
            (ImportError("SECRET"), "import"),
            (AttributeError("SECRET"), "attribute"),
            (TypeError("SECRET"), "type"),
            (ValueError("SECRET"), "value"),
            (RuntimeError("SECRET"), "unexpected"),
        ):
            events.write_failure(marker, "write", error, 7)
            value = json.loads(marker.read_text())
            self.assertEqual(value, {"version": 1, "phase": "write", "error": category, "seq": 7})
            self.assertEqual(events.read_failure(marker), "callback_write_" + category)
            self.assertNotIn("SECRET", marker.read_text())
            self.assertFalse(marker.with_suffix(".next").exists())
        events.write_failure(marker, "SECRET", RuntimeError("SECRET"), -1)
        self.assertEqual(json.loads(marker.read_text())["phase"], "unknown")
        self.assertEqual(json.loads(marker.read_text())["seq"], 0)

    def test_callback_reports_stage_and_error_without_task_name_or_exception_text(self) -> None:
        callback = self.h.callback()
        with patch.object(callback.writer, "emit", side_effect=PermissionError("SECRET-URL")):
            callback.v2_runner_on_start(fixtures.host(), fixtures.task("SECRET-TASK"))
        marker = self.h.path.with_suffix(".unavailable")
        self.assertEqual(events.read_failure(marker), "callback_write_permission")
        self.assertNotIn("SECRET", marker.read_text())
        first_failure = marker.read_bytes()
        callback.v2_runner_on_ok(fixtures.broken_result())
        self.assertEqual(marker.read_bytes(), first_failure)
        callback.v2_runner_on_start(fixtures.host(), fixtures.task())
        self.assertIsNone(callback.writer)

    def test_metadata_failure_and_malformed_result_never_escape_into_ansible(self) -> None:
        callback = self.h.callback()
        callback.v2_runner_on_ok(fixtures.broken_result())
        self.assertEqual(events.read_failure(self.h.path.with_suffix(".unavailable")), "callback_metadata_attribute")
        self.h.path.unlink()
        callback = self.h.callback()
        task = fixtures.task()
        with patch.object(task, "get_ds", Mock(side_effect=TypeError("SECRET"))):
            callback.v2_runner_on_start(fixtures.host(), task)
        self.assertEqual(events.read_failure(self.h.path.with_suffix(".unavailable")), "callback_metadata_type")

    def test_initialization_failure_and_diagnostic_write_fallback(self) -> None:
        self.h.path.touch()
        callback = self.h.callback()
        marker = self.h.path.with_suffix(".unavailable")
        self.assertEqual(events.read_failure(marker), "callback_initialize_existing")
        with patch.object(events, "write_failure", side_effect=OSError("SECRET")):
            callback.unavailable("write", OSError("SECRET"))
        self.assertEqual(marker.read_bytes(), b"")
        with (
            patch.object(events, "write_failure", side_effect=OSError("SECRET")),
            patch("os.open", side_effect=PermissionError("SECRET")),
        ):
            callback.unavailable("write", OSError("SECRET"))

    def test_corrupted_or_legacy_diagnostics_never_echo_untrusted_content(self) -> None:
        marker = self.h.root / "failure.json"
        marker.write_bytes(b"")
        self.assertEqual(events.read_failure(marker), "callback_disabled")
        valid = {"version": 1, "phase": "write", "error": "io", "seq": 0}
        variants = [
            "SECRET",
            "x" * 1025,
            "{}" + " " * 1023,
            "[]",
            json.dumps({**valid, "version": True}),
            json.dumps({**valid, "version": 2}),
            json.dumps({**valid, "phase": []}),
            json.dumps({**valid, "phase": "SECRET"}),
            json.dumps({**valid, "error": "SECRET"}),
            json.dumps({**valid, "seq": True}),
            json.dumps({**valid, "seq": -1}),
            json.dumps({**valid, "seq": 2**53 + 1}),
            json.dumps({**valid, "extra": "SECRET"}),
        ]
        for raw in variants:
            marker.write_text(raw)
            self.assertEqual(events.read_failure(marker), "callback_diagnostic_invalid")
        with patch.object(Path, "is_symlink", return_value=True):
            self.assertEqual(events.read_failure(marker), "callback_diagnostic_invalid")

    def test_bound_rpc_and_console_have_safe_reason_and_keep_native_result(self) -> None:
        helper = operation_fixtures.DurableAnsibleTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        helper.reserve()
        context = controller.read(helper.api)
        assert context is not None
        request = context["request"]
        folder = helper.manager.folder(request["id"])
        events.write_failure(folder / "events.unavailable", "metadata", TypeError("SECRET"), 3)
        result_before = (folder / "result.json").read_bytes()
        response = helper.manager.events(request, {"offset": 0, "seq": 0})
        self.assertEqual(response["reason"], "callback_metadata_type")
        self.assertEqual((folder / "result.json").read_bytes(), result_before)
        with self.assertLogs(progress.LOGGER, "WARNING") as logs:
            progress.observe(
                self.h.api,
                self.h.lab,
                self.h.value,
                Mock(return_value=json.dumps({**response, "binding": self.h.value["request"]})),
                self.h.save,
                operation_state="running",
            )
        text = "\n".join(logs.output)
        self.assertIn("чтение метаданных", text)
        self.assertIn("неподходящий тип", text)
        self.assertIn("операция ещё выполняется", text)
        self.assertNotIn("SECRET", text)
