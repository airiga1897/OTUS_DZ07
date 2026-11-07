"""Progress diagnostics distinguish missing observations from confirmed failures."""

import json
import unittest
from unittest.mock import Mock, patch

import ansible_operation as controller
import ansible_progress as progress
import test_ansible_operation as operation_fixtures
import test_ansible_progress as fixtures
from paramiko import AuthenticationException, SSHClient
from remote import ansible_events as events


class ProgressDiagnosticTests(unittest.TestCase):
    def setUp(self) -> None:
        helper = fixtures.AnsibleProgressTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        self.helper = helper
        self.api, self.lab, self.value = helper.api, helper.lab, helper.value

    def response(self, **kwargs: object) -> Mock:
        return Mock(return_value=json.dumps({"binding": self.value["request"], "available": False, **kwargs}))

    def observe(
        self, command: progress.ProgressCommand, state: str = "running", save: progress.SaveProgress | None = None
    ) -> None:
        progress.observe(self.api, self.lab, self.value, command, save or self.helper.save, operation_state=state)

    def test_temporary_transport_failure_is_info_and_never_claims_ansible_failed(self) -> None:
        with self.assertLogs(progress.LOGGER, "INFO") as logs:
            for error in (
                OSError("SECRET"),
                TimeoutError("SECRET"),
                controller.OperationUnknown("SECRET"),
                controller.OperationProtocolError("SECRET"),
            ):
                self.observe(Mock(side_effect=error))
        self.assertEqual(len(logs.output), 1)
        self.assertIn("ответ SSH/RPC", logs.output[0])
        self.assertIn("операция ещё выполняется", logs.output[0])
        self.assertNotIn("WARNING", repr(logs.output))
        self.assertNotIn("SECRET", repr(logs.output))

    def test_unknown_legacy_reason_and_pending_are_info(self) -> None:
        with self.assertLogs(progress.LOGGER, "INFO") as logs:
            self.observe(self.response())
            self.observe(self.response(pending=True), state="preparing")
        self.assertEqual(len(logs.output), 2)
        self.assertIn("подготовка", logs.output[1])
        self.assertNotIn("120", repr(logs.output))
        self.assertNotIn("WARNING", repr(logs.output))

    def test_remote_confirmed_reasons_have_warning_with_safe_concrete_reason(self) -> None:
        reasons = (
            "callback_disabled",
            "journal_missing",
            "journal_incomplete",
            "journal_invalid",
            "journal_permission_denied",
            "observer_module_missing",
            "observer_internal_error",
        )
        with self.assertLogs(progress.LOGGER, "WARNING") as logs:
            for reason in reasons:
                self.observe(self.response(reason=reason))
        self.assertEqual(len(logs.output), len(reasons))
        for reason, line in zip(reasons, logs.output, strict=False):
            self.assertIn(progress.PROBLEMS[reason], line)
        self.assertNotIn("progress_cursor", self.value)

    def test_remote_read_failure_is_info_and_terminal_state_is_not_called_running(self) -> None:
        with self.assertLogs(progress.LOGGER, "INFO") as logs:
            self.observe(self.response(reason="journal_read_unavailable"), state="succeeded")
        self.assertIn("временно недоступен", logs.output[0])
        self.assertIn("успешно завершена", logs.output[0])
        self.assertNotIn("ещё выполняется", logs.output[0])

    def test_unknown_reason_or_bad_unavailable_envelope_is_a_protocol_warning(self) -> None:
        cases: tuple[dict[str, object], ...] = (
            {"reason": "SECRET"},
            {"reason": None},
            {"reason": []},
            {"pending": False},
            {"reason": "journal_invalid", "extra": "SECRET"},
        )
        with self.assertLogs(progress.LOGGER, "WARNING") as logs:
            for case in cases:
                self.observe(self.response(**case))
        self.assertNotIn("SECRET", repr(logs.output))
        self.assertIn("неподдерживаемого формата", logs.output[0])

    def test_authentication_failure_is_a_confirmed_warning(self) -> None:
        with self.assertLogs(progress.LOGGER, "WARNING") as logs:
            self.observe(Mock(side_effect=AuthenticationException("SECRET")))
        self.assertIn("реквизиты SSH", logs.output[0])
        self.assertNotIn("SECRET", logs.output[0])

    def test_raw_response_and_bad_remote_cursor_never_advance_saved_position(self) -> None:
        value = {
            "binding": self.value["request"],
            "available": True,
            "events": [],
            "cursor": {"offset": 0, "seq": True},
            "more": False,
        }
        with self.assertLogs(progress.LOGGER, "WARNING"):
            self.observe(Mock(return_value=None))
            self.observe(Mock(return_value="[]"))
            self.observe(Mock(return_value=json.dumps(value)))
        self.assertNotIn("progress_cursor", self.value)

    def test_event_recovery_clears_diagnostic_and_new_failure_is_logged_once(self) -> None:
        self.observe(self.response(reason="callback_disabled"))
        writer = events.Writer(self.helper.path)
        writer.emit("Configure service", "app", "result", "ok")
        self.observe(self.helper.command)
        self.assertNotIn("_progress_notice", self.value)
        self.assertNotIn("_progress_warning", self.value)
        with self.assertLogs(progress.LOGGER, "INFO") as logs:
            self.observe(self.response())
            self.observe(self.response())
        self.assertEqual(len(logs.output), 1)
        self.assertEqual(events.cursor(self.value["progress_cursor"])["seq"], 1)

    def test_cursor_save_failure_is_warning_not_a_transport_info(self) -> None:
        writer = events.Writer(self.helper.path)
        writer.emit("Configure service", "app", "result", "ok")
        with self.assertLogs(progress.LOGGER, "WARNING") as logs:
            self.observe(self.helper.command, save=Mock(side_effect=OSError("SECRET")))
        self.assertIn("сохранить позицию", logs.output[0])
        self.assertNotIn("SECRET", repr(logs.output))
        self.assertNotIn("progress_cursor", self.value)

    def test_local_observer_failure_and_broken_logging_preserve_operation(self) -> None:
        with (
            patch.object(progress, "page", side_effect=RuntimeError("SECRET")),
            self.assertLogs(progress.LOGGER, "WARNING") as logs,
        ):
            self.observe(Mock())
        self.assertIn("локального обработчика", logs.output[0])
        self.assertNotIn("SECRET", repr(logs.output))
        with patch.object(progress.LOGGER, "warning", side_effect=OSError()):
            progress.warning(self.value)
        with patch.object(progress.LOGGER, "info", side_effect=OSError()):
            self.observe(self.response())

    def test_invalid_event_and_unknown_operation_state_are_safe(self) -> None:
        writer = events.Writer(self.helper.path)
        writer.emit("Task", "app", "result", "ok")
        page = json.loads(self.helper.response({"offset": 0, "seq": 0}))
        page["events"][0]["status"] = "SECRET"
        with self.assertLogs(progress.LOGGER, "WARNING") as logs:
            self.observe(Mock(return_value=json.dumps(page)), state="SECRET")
        self.assertIn("не подтверждено", logs.output[0])
        self.assertNotIn("SECRET", repr(logs.output))


class RemoteDiagnosticReasons(unittest.TestCase):
    def setUp(self) -> None:
        helper = operation_fixtures.DurableAnsibleTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        helper.reserve()
        self.helper = helper
        self.request = controller.required(helper.api)["request"]
        self.folder = helper.manager.folder(self.request["id"])

    def read(self) -> dict[str, object]:
        return self.helper.manager.events(self.request, {"offset": 0, "seq": 0})

    def test_missing_completed_journal_and_callback_marker_have_explicit_reasons(self) -> None:
        result = json.loads((self.folder / "result.json").read_text())
        result["state"] = "failed"
        result["phase"] = "configure"
        (self.folder / "result.json").write_text(json.dumps(result))
        self.assertEqual(self.read()["reason"], "journal_missing")
        (self.folder / "events.unavailable").touch()
        self.assertEqual(self.read()["reason"], "callback_disabled")

    def test_no_warning_is_inferred_if_operation_ended_before_callback_could_start(self) -> None:
        result = json.loads((self.folder / "result.json").read_text())
        result.update(state="failed", phase="prepare")
        (self.folder / "result.json").write_text(json.dumps(result))
        self.assertEqual(self.read()["reason"], "not_started")

    def test_partial_tail_and_bad_json_are_confirmed_journal_problems(self) -> None:
        (self.folder / "events.jsonl").write_text("partial")
        result = json.loads((self.folder / "result.json").read_text())
        result["state"] = "succeeded"
        (self.folder / "result.json").write_text(json.dumps(result))
        self.assertEqual(self.read()["reason"], "journal_incomplete")
        (self.folder / "events.jsonl").write_text("{bad}\n")
        self.assertEqual(self.read()["reason"], "journal_invalid")

    def test_read_errors_are_classified_without_disclosing_exception_text(self) -> None:
        (self.folder / "events.jsonl").touch()
        for error, reason in (
            (OSError("SECRET"), "journal_read_unavailable"),
            (PermissionError("SECRET"), "journal_permission_denied"),
            (ImportError("SECRET"), "observer_module_missing"),
            (RuntimeError("SECRET"), "observer_internal_error"),
        ):
            with patch.object(events, "read_page", side_effect=error):
                result = self.read()
            self.assertEqual(result["reason"], reason)
            self.assertNotIn("SECRET", json.dumps(result))

    def test_transient_progress_never_changes_id_launch_count_or_execution_limit(self) -> None:
        helper = self.helper
        before = controller.policy(helper.api)
        original = helper.transport

        def transport(client: SSHClient, text: str, data: object = None, timeout: float = 15) -> str:
            if isinstance(data, dict) and data["action"] == "events":
                raise OSError("temporary read failure")
            return original(client, text, data, timeout)

        with (
            patch.object(controller, "command", side_effect=transport),
            self.assertLogs(progress.LOGGER, "INFO") as logs,
        ):
            result = controller.wait(helper.api, helper.lab, helper.inventory, upload=True)
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(helper.launches, 1)
        self.assertEqual(controller.required(helper.api)["request"]["id"], self.request["id"])
        self.assertEqual(controller.policy(helper.api), before)
        self.assertNotIn("WARNING", repr(logs.output))
