"""Private callback journals and resumable observation; no Ansible/SSH/systemd calls."""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from unittest.mock import MagicMock, Mock, patch

import ansible_operation as controller
import ansible_progress as progress
import test_ansible_operation as fixtures
from paramiko import SSHClient
from remote import ansible_events as events
from remote import ansible_job as worker

if TYPE_CHECKING:
    from ansible.executor.task_result import TaskResult as AnsibleTaskResult
    from ansible.inventory.host import Host as AnsibleHost
    from ansible.playbook.task import Task as AnsibleTask
else:

    class AnsibleTask:
        pass

    class AnsibleHost:
        pass

    class AnsibleTaskResult:
        pass


from runtime_ports import FilePort, StandDescription

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = object()


class CallbackTask(AnsibleTask):
    def __init__(
        self, name: object = "Install service", no_log: object = False, source: object = DEFAULT_SOURCE
    ) -> None:
        self.name = name
        self.no_log = no_log if isinstance(no_log, (bool, type(None))) else None
        self.source = {"name": name} if source is DEFAULT_SOURCE else source

    def get_ds(self) -> object:
        return self.source


class CallbackHost(AnsibleHost):
    def __init__(self, name: str = "app") -> None:
        self.name = name

    def get_name(self) -> str:
        return self.name


class CallbackResult(AnsibleTaskResult):
    def __init__(self, task: CallbackTask, host: CallbackHost, result: object) -> None:
        self._task = task
        self._host = host
        self._result = result


class BrokenCallbackResult(AnsibleTaskResult):
    pass


@runtime_checkable
class CallbackPort(Protocol):
    writer: object | None

    def unavailable(self, phase: str = "unknown", error: Exception | None = None) -> None: ...
    def v2_runner_on_start(self, host: CallbackHost, task: CallbackTask) -> None: ...
    def v2_runner_on_ok(self, result: AnsibleTaskResult) -> None: ...
    def v2_runner_on_failed(self, result: AnsibleTaskResult, ignore_errors: bool = False) -> None: ...
    def v2_runner_on_unreachable(self, result: AnsibleTaskResult) -> None: ...
    def v2_runner_on_skipped(self, result: AnsibleTaskResult) -> None: ...


def task(name: object = "Install service", no_log: object = False, source: object = DEFAULT_SOURCE) -> CallbackTask:
    return CallbackTask(name, no_log, source)


def host(name: str = "app") -> CallbackHost:
    return CallbackHost(name)


def result(task_value: CallbackTask, host_value: CallbackHost, payload: object) -> CallbackResult:
    return CallbackResult(task_value, host_value, payload)


def broken_result() -> BrokenCallbackResult:
    return BrokenCallbackResult()


class AnsibleProgressTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory(dir=ROOT / ".tools")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.path = self.root / "events.jsonl"
        self.value: controller.OperationContext = {
            "bootstrap": True,
            "request": {
                "id": "a" * 32,
                "deployment_id": "b" * 32,
                "app_id": "app-id",
                "namespace": "otus-dz07",
                "parameters_sha256": "c" * 64,
                "timeout": 1800,
                "stop_wait": 1,
                "payload": dict.fromkeys(("inventory.json", "controls.json", "key", "known_hosts"), "d" * 64),
            },
        }
        self.lab: StandDescription = {"server_ids": {"app": "app-id", "es1": "es-id"}}
        self.api = SimpleNamespace(
            ROOT=self.root, STACK={"paths": {"remote_root": "/test/otus-dz07"}}, connect=Mock(return_value=MagicMock())
        )
        self.saved: list[controller.OperationContext] = []

        def save(api: FilePort, value: controller.OperationContext) -> None:
            self.saved.append(json.loads(json.dumps(value)))

        self.save = save

    def callback(self) -> CallbackPort:
        modules = {name: ModuleType(name) for name in ("ansible", "ansible.plugins", "ansible.plugins.callback")}
        modules["ansible.plugins.callback"].__dict__["CallbackBase"] = object
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.dict(sys.modules, modules))
        stack.enter_context(patch.object(sys, "path", sys.path.copy()))
        stack.enter_context(patch.dict(os.environ, {"DZ07_PROGRESS_FILE": str(self.path)}))
        spec = importlib.util.spec_from_file_location("dz07_test_callback", ROOT / "scripts/remote/ansible_callback.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        callback_factory = module.CallbackModule
        assert callable(callback_factory)
        callback = callback_factory()
        assert isinstance(callback, CallbackPort)
        return callback

    def response(self, position: object) -> str:
        return json.dumps(
            {"binding": self.value["request"], "available": True, **events.read_page(self.path, position)}
        )

    def command(self, client: SSHClient, text: str, data: object) -> str:
        assert isinstance(data, dict)
        self.assertEqual(data["action"], "events")
        self.assertEqual(data["request"], self.value["request"])
        return self.response(data["cursor"])

    def test_callback_start_and_results_on_multiple_hosts(self) -> None:
        callback = self.callback()
        callback.v2_runner_on_start(host(), task())
        for name, changed in (("app", False), ("es1", True)):
            callback.v2_runner_on_ok(result(task(), host(name), {"changed": changed, "stdout": "SECRET-STDOUT"}))
        failed = result(task(), host(), {"stderr": "SECRET-STDERR"})
        callback.v2_runner_on_failed(failed)
        callback.v2_runner_on_unreachable(failed)
        callback.v2_runner_on_skipped(failed)
        page = events.read_page(self.path, {"offset": 0, "seq": 0})
        self.assertEqual(
            [v["status"] for v in page["events"]], ["start", "ok", "changed", "failed", "failed", "skipped"]
        )
        self.assertEqual([v["seq"] for v in page["events"]], list(range(1, 7)))
        self.assertNotIn("SECRET", self.path.read_text())
        with self.assertLogs(progress.LOGGER, "INFO") as logs:
            progress.observe(self.api, self.lab, self.value, self.command, self.save)
        self.assertEqual(len(logs.output), 6)
        self.assertTrue(any("es1" in line and "changed" in line for line in logs.output))
        self.assertEqual(events.cursor(self.value["progress_cursor"])["seq"], 6)

    def test_no_log_hides_names_and_results_including_unresolved_flags(self) -> None:
        callback = self.callback()
        for flag in (True, None, "{{ private_flag }}"):
            hidden = task("SECRET-NAME", flag)
            callback.v2_runner_on_start(host(), hidden)
            callback.v2_runner_on_ok(
                result(hidden, host(), {"changed": True, "stdout": "SECRET", "password": "SECRET"})
            )
        for payload in ({"_ansible_no_log": True}, {"results": [{"_ansible_no_log": True}]}):
            callback.v2_runner_on_failed(result(task("SECRET-NAME"), host(), payload))
        text = self.path.read_text()
        self.assertNotIn("SECRET", text)
        page = events.read_page(self.path, {"offset": 0, "seq": 0})
        self.assertTrue(all(v["status"] == "hidden" and v["task"] == "[no_log]" for v in page["events"]))
        with self.assertLogs(progress.LOGGER, "INFO") as logs:
            progress.observe(self.api, self.lab, self.value, self.command, self.save)
        self.assertNotIn("SECRET", repr(logs.output))

    def test_dynamic_unnamed_and_unsafe_names_never_use_module_arguments(self) -> None:
        callback = self.callback()
        for name in (
            "{{ password }}",
            "https://example.invalid/?signature=SECRET",
            "line\nSECRET",
            "",
            None,
            "x" * 201,
        ):
            callback.v2_runner_on_start(host(), task(name))
        self.assertNotIn("SECRET", self.path.read_text())
        self.assertTrue(
            all(
                v["task"] == "[task name hidden]"
                for v in events.read_page(self.path, {"offset": 0, "seq": 0})["events"]
            )
        )

    def test_partial_tail_does_not_advance_cursor_until_newline(self) -> None:
        writer = events.Writer(self.path)
        writer.emit("First", "app", "start", "start")
        tail = json.dumps({"seq": 2, "task": "Second", "host": "es1", "kind": "result", "status": "ok"}).encode()
        with self.path.open("ab") as stream:
            stream.write(tail[:20])
        first = events.read_page(self.path, {"offset": 0, "seq": 0})
        second = events.read_page(self.path, first["cursor"])
        self.assertEqual(second["events"], [])
        self.assertEqual(second["cursor"], first["cursor"])
        with self.path.open("ab") as stream:
            stream.write(tail[20:] + b"\n")
        final = events.read_page(self.path, second["cursor"])
        self.assertEqual(final["events"][0]["seq"], 2)

    def test_batch_limits_and_restart_use_saved_offset_without_reprinting_history(self) -> None:
        writer = events.Writer(self.path)
        for _ in range(events.MAX_EVENTS + 3):
            writer.emit("Task", "app", "result", "ok")
        first = events.read_page(self.path, {"offset": 0, "seq": 0})
        self.assertEqual(len(first["events"]), events.MAX_EVENTS)
        self.assertTrue(first["more"])
        self.value["progress_cursor"] = first["cursor"]
        restarted = json.loads(json.dumps(self.value))
        with self.assertLogs(progress.LOGGER, "INFO") as logs:
            progress.observe(self.api, self.lab, restarted, self.command, self.save)
        self.assertEqual(len(logs.output), 3)
        self.assertEqual(events.cursor(restarted["progress_cursor"])["seq"], events.MAX_EVENTS + 3)
        with patch.object(progress.LOGGER, "info") as info:
            progress.observe(self.api, self.lab, restarted, self.command, self.save)
        info.assert_not_called()

    def test_read_error_and_lost_reply_keep_cursor_and_do_not_expose_error_text(self) -> None:
        writer = events.Writer(self.path)
        writer.emit("Configure", "app", "start", "start")
        with self.assertLogs(progress.LOGGER, "INFO") as logs:
            progress.observe(self.api, self.lab, self.value, Mock(side_effect=OSError("SECRET")), self.save)
            progress.observe(self.api, self.lab, self.value, Mock(side_effect=OSError("SECRET")), self.save)
        self.assertEqual(len(logs.output), 1)
        self.assertNotIn("SECRET", repr(logs.output))
        self.assertNotIn("progress_cursor", self.value)
        progress.observe(self.api, self.lab, self.value, self.command, self.save)
        self.assertEqual(events.cursor(self.value["progress_cursor"])["seq"], 1)

    def test_print_before_failed_cursor_save_may_repeat_but_never_loses_position(self) -> None:
        writer = events.Writer(self.path)
        writer.emit("Task", "app", "result", "ok")
        with self.assertLogs(progress.LOGGER, "INFO") as logs:
            progress.observe(self.api, self.lab, self.value, self.command, Mock(side_effect=OSError("SECRET")))
            progress.observe(self.api, self.lab, self.value, self.command, self.save)
        self.assertEqual(sum(" | Task | " in s for s in logs.output), 2)
        self.assertEqual(events.cursor(self.value["progress_cursor"])["seq"], 1)

    def test_changed_operation_binding_and_bad_batches_are_rejected_before_output(self) -> None:
        writer = events.Writer(self.path)
        writer.emit("Task", "app", "result", "ok")
        normal = json.loads(self.response({"offset": 0, "seq": 0}))
        variants = []
        for key in ("id", "deployment_id", "app_id", "parameters_sha256"):
            value = json.loads(json.dumps(normal))
            value["binding"][key] = "foreign"
            variants.append(value)
        updates: list[dict[str, object]] = [
            {"available": "yes"},
            {"events": {}},
            {"more": "yes"},
            {"extra": "SECRET"},
            {"cursor": {**normal["cursor"], "seq": 2}},
            {"cursor": {**normal["cursor"], "offset": 0}},
            {"events": [{**normal["events"][0], "host": "foreign"}]},
            {"events": [{**normal["events"][0], "seq": 5}]},
        ]
        variants.extend({**normal, **update} for update in updates)
        for value in variants:
            with patch.object(progress.LOGGER, "info") as info:
                progress.observe(self.api, self.lab, self.value, Mock(return_value=json.dumps(value)), self.save)
            info.assert_not_called()
            self.assertNotIn("progress_cursor", self.value)

    def test_corrupt_truncated_oversized_or_linked_journal_is_not_read_as_success(self) -> None:
        self.path.write_bytes(b"{broken}\n")
        with self.assertRaises(ValueError):
            events.read_page(self.path, {"offset": 0, "seq": 0})
        self.path.write_bytes(b"x" * (events.MAX_LINE + 1))
        with self.assertRaises(ValueError):
            events.read_page(self.path, {"offset": 0, "seq": 0})
        self.path.write_bytes(b"")
        with self.assertRaises(ValueError):
            events.read_page(self.path, {"offset": 10, "seq": 1})
        with patch.object(Path, "is_symlink", return_value=True):
            with self.assertRaises(ValueError):
                events.read_page(self.path, {"offset": 0, "seq": 0})
            with self.assertRaises(ValueError):
                events.Writer(self.path)

    def test_callback_io_failure_disables_only_observer(self) -> None:
        callback = self.callback()
        with patch.object(callback.writer, "emit", side_effect=OSError("SECRET")):
            callback.v2_runner_on_start(host(), task())
        self.assertIsNone(callback.writer)
        self.assertTrue(self.path.with_suffix(".unavailable").exists())
        callback.v2_runner_on_start(host(), task("SECRET"))
        with patch.dict(os.environ, {}, clear=True):
            callback.unavailable()  # Even the fallback marker may be impossible to write.
        self.assertEqual(self.path.read_text(), "")

    def test_invalid_cursor_event_and_duplicate_writer_fail_closed(self) -> None:
        for value in (None, {}, {"offset": True, "seq": 0}, {"offset": 0, "seq": 1}, {"offset": -1, "seq": 0}):
            with self.assertRaises(ValueError):
                events.cursor(value)
        writer = events.Writer(self.path)
        with self.assertRaises(FileExistsError):
            events.Writer(self.path)
        for args in (
            ("Task", "bad host", "start", "start"),
            ("Task", "app", "start", "ok"),
            ("[no_log]", "app", "result", "start"),
        ):
            with self.assertRaises(ValueError):
                writer.emit(*args)
        writer.emit("Task", "app", "result", "ok")
        self.assertEqual(writer.sequence, 1)

    def test_callback_initialization_failure_is_not_an_ansible_failure(self) -> None:
        self.path.touch()
        callback = self.callback()  # An existing producer journal is never overwritten.
        self.assertIsNone(callback.writer)
        self.assertTrue(self.path.with_suffix(".unavailable").exists())

    def test_writer_rejects_oversized_encoded_line_and_short_write(self) -> None:
        writer = events.Writer(self.path)
        with patch.object(events, "MAX_LINE", 20), self.assertRaises(ValueError):
            writer.emit("Task", "app", "result", "ok")
        stream = MagicMock()
        stream.__enter__.return_value.write.return_value = 0
        with patch.object(Path, "open", return_value=stream), self.assertRaises(OSError):
            writer.emit("Task", "app", "result", "ok")
        self.assertEqual(writer.sequence, 0)

    def test_byte_limit_leaves_the_next_complete_event_for_the_next_page(self) -> None:
        writer = events.Writer(self.path)
        for _ in range(3):
            writer.emit("Task", "app", "result", "ok")
        with patch.object(events, "MAX_BYTES", 120):
            first = events.read_page(self.path, {"offset": 0, "seq": 0})
            second = events.read_page(self.path, first["cursor"])
        self.assertEqual(len(first["events"]), 1)
        self.assertEqual(second["events"][0]["seq"], 2)
        self.assertLessEqual(first["cursor"]["offset"], 120)

    def test_bad_response_size_unavailable_and_non_json_never_advance_position(self) -> None:
        for raw in (
            "x" * (events.MAX_BYTES * 2 + 1),
            "SECRET-NOT-JSON",
            json.dumps({"binding": self.value["request"], "available": False}),
        ):
            progress.observe(self.api, self.lab, self.value, Mock(return_value=raw), self.save)
            self.assertNotIn("progress_cursor", self.value)
        self.value["progress_cursor"] = {"bad": "SECRET"}
        progress.observe(self.api, self.lab, self.value, Mock(), self.save)
        self.assertEqual(self.value["progress_cursor"], {"bad": "SECRET"})

    def test_bounded_poll_retains_backlog_and_existing_cursor_on_save_failure(self) -> None:
        writer = events.Writer(self.path)
        for _ in range(events.MAX_EVENTS + 1):
            writer.emit("Task", "app", "result", "ok")
        with patch.object(progress, "MAX_PAGES", 1), self.assertLogs(progress.LOGGER, "INFO"):
            progress.observe(self.api, self.lab, self.value, self.command, self.save)
        previous = events.cursor(self.value["progress_cursor"])
        progress.observe(self.api, self.lab, self.value, self.command, Mock(side_effect=OSError()))
        self.assertEqual(self.value["progress_cursor"], previous)
        progress.observe(self.api, self.lab, self.value, self.command, self.save)
        self.assertEqual(events.cursor(self.value["progress_cursor"])["seq"], events.MAX_EVENTS + 1)

    def test_broken_logging_and_optional_delivery_cannot_abort_operation(self) -> None:
        with patch.object(progress.LOGGER, "warning", side_effect=OSError("SECRET")):
            progress.observe(self.api, self.lab, self.value, Mock(side_effect=OSError()), self.save)
        source = self.root / "scripts/remote"
        source.mkdir(parents=True)
        (source / "ansible_job.py").write_text("# synthetic runner")
        logger = Mock()
        logger.warning.side_effect = OSError("SECRET")
        with (
            patch.object(controller, "command"),
            patch.object(controller, "put_private") as put,
            patch.object(controller, "get_logger", return_value=logger),
        ):
            controller.install(self.api, Mock())
        self.assertEqual(put.call_count, 1)  # Main runner installed; optional observer absent.

    def test_optional_observer_command_failure_does_not_replace_main_protocol(self) -> None:
        source = self.root / "scripts/remote"
        source.mkdir(parents=True)
        (source / "ansible_job.py").write_text("# synthetic runner")
        with (
            patch.object(controller, "command", side_effect=[None, controller.OperationProtocolError("SECRET")]),
            patch.object(controller, "put_private") as put,
            self.assertLogs("otus_dz07.ansible_operation", "WARNING") as logs,
        ):
            controller.install(self.api, Mock())
        self.assertEqual(put.call_count, 1)
        self.assertNotIn("SECRET", repr(logs.output))


class ProgressOperationIntegration(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.DurableAnsibleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.reserve()
        self.api, self.lab, self.inventory = self.fixture.api, self.fixture.lab, self.fixture.inventory
        self.request = controller.required(self.api)["request"]
        self.folder = self.fixture.manager.folder(self.request["id"])

    def test_connection_loss_in_progress_does_not_start_a_second_ansible(self) -> None:
        writer = events.Writer(self.folder / "events.jsonl")
        writer.emit("Install service", "app", "start", "start")
        original = self.fixture.transport
        attempts = []

        def transport(client: SSHClient, text: str, data: object = None, timeout: float = 15) -> str:
            if isinstance(data, dict) and data["action"] == "events":
                attempts.append(1)
                if len(attempts) == 1:
                    raise OSError("SECRET-LOST-REPLY")
            return original(client, text, data, timeout)

        with patch.object(controller, "command", side_effect=transport):
            result = controller.wait(self.api, self.lab, self.inventory, upload=True)
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(self.fixture.launches, 1)
        saved = controller.required(self.api)
        self.assertEqual(saved["request"]["id"], self.request["id"])
        self.assertEqual(events.cursor(saved["progress_cursor"])["seq"], 1)

    def test_terminal_result_is_independent_of_missing_or_corrupt_progress(self) -> None:
        for state, code in (("succeeded", 0), ("failed", 2)):
            result = {
                "id": self.request["id"],
                "parameters_sha256": self.request["parameters_sha256"],
                "state": state,
                "exit_code": code,
                "started_utc": "2026-11-01T00:00:00+00:00",
                "finished_utc": "2026-11-01T00:01:00+00:00",
            }
            worker.atomic(self.folder / "result.json", result)
            (self.folder / "events.jsonl").write_text("CORRUPT-SECRET\n")
            if code:
                with self.assertRaises(controller.OperationFailed):
                    controller.wait(self.api, self.lab, self.inventory)
            else:
                self.assertEqual(controller.wait(self.api, self.lab, self.inventory)["state"], state)
        self.assertEqual(self.fixture.launches, 0)

    def test_remote_binding_mismatch_is_rejected_before_reading_a_journal(self) -> None:
        foreign: worker.OperationRequest = {**self.request, "app_id": "foreign"}
        with self.assertRaises(ValueError):
            self.fixture.manager.events(foreign, {"offset": 0, "seq": 0})

    def test_marker_missing_file_and_bad_cursor_are_observational_failures(self) -> None:
        self.assertFalse(self.fixture.manager.events(self.request, {"offset": 0, "seq": 0})["available"])
        (self.folder / "events.unavailable").touch()
        self.assertFalse(self.fixture.manager.events(self.request, {"offset": 0, "seq": 0})["available"])
        (self.folder / "events.unavailable").unlink()
        self.assertFalse(self.fixture.manager.events(self.request, None)["available"])

    def test_partial_tail_is_tolerated_during_execution_but_reported_after_result(self) -> None:
        (self.folder / "events.jsonl").write_text('{"seq":')
        page = self.fixture.manager.events(self.request, {"offset": 0, "seq": 0})
        self.assertTrue(page["available"])
        result = json.loads((self.folder / "result.json").read_text())
        result["state"] = "failed"
        worker.atomic(self.folder / "result.json", result)
        self.assertFalse(self.fixture.manager.events(self.request, {"offset": 0, "seq": 0})["available"])

    def test_controller_restart_resumes_persisted_cursor_and_same_operation(self) -> None:
        writer = events.Writer(self.folder / "events.jsonl")
        writer.emit("Before restart", "app", "result", "ok")
        writer.emit("After restart", "app", "result", "changed")
        value = controller.required(self.api)
        with patch.object(events, "MAX_EVENTS", 1), patch.object(progress, "MAX_PAGES", 1):
            progress.observe(self.api, self.lab, value, controller.command, controller.save)
        self.assertEqual(events.cursor(controller.required(self.api)["progress_cursor"])["seq"], 1)
        with self.assertLogs(progress.LOGGER, "INFO") as logs:
            controller.wait(self.api, self.lab, self.inventory, upload=True)
        self.assertNotIn("Before restart", repr(logs.output))
        self.assertIn("After restart", repr(logs.output))
        self.assertEqual(events.cursor(controller.required(self.api)["progress_cursor"])["seq"], 2)
        self.assertEqual(controller.required(self.api)["request"]["id"], self.request["id"])
        self.assertEqual(self.fixture.launches, 1)
        controller.wait(self.api, self.lab, self.inventory)
        self.assertEqual(self.fixture.launches, 1)

    def test_callback_snapshot_is_private_and_not_replaced_by_reconnect(self) -> None:
        for name, content in controller.payload(self.api, self.inventory).items():
            self.api.put(None, str(self.folder / name), content)
        self.fixture.manager.start(self.request)
        snapshot = self.folder / "callback_plugins/dz07_progress.py"
        original = snapshot.read_bytes()
        (self.fixture.manager.root / ".ansible-callback.py").write_text("different observer")
        self.fixture.manager.start(self.request)
        self.assertEqual(snapshot.read_bytes(), original)
        self.assertEqual(self.fixture.launches, 1)
        child = Mock()
        child.wait.return_value = child.poll.return_value = 0
        with patch("subprocess.Popen", return_value=child) as process:
            worker.execute(self.fixture.manager, self.request["id"])
        environments = [call.kwargs["env"] for call in process.call_args_list]
        self.assertNotIn("ANSIBLE_CALLBACKS_ENABLED", environments[0])
        self.assertNotIn("ANSIBLE_CALLBACKS_ENABLED", environments[1])
        self.assertEqual(environments[2]["ANSIBLE_CALLBACKS_ENABLED"], "dz07_progress")
        self.assertNotIn("PRIVATE-", repr(environments[2]))

    def test_events_rpc_passes_cursor_without_changing_state_protocol(self) -> None:
        data = {"action": "events", "request": self.request, "cursor": {"offset": 0, "seq": 0}}
        manager = Mock()
        manager.events.return_value = {"available": False}
        import io

        with (
            patch.object(sys, "argv", ["ansible_job.py", "--root", str(self.fixture.manager.root), "rpc"]),
            patch.object(sys, "stdin", io.StringIO(json.dumps(data))),
            patch.object(worker, "Manager", return_value=manager),
            patch("builtins.print") as printed,
        ):
            worker.main()
        manager.events.assert_called_once_with(self.request, data["cursor"])
        printed.assert_called_once_with('{"available": false}')

    def test_missing_callback_copy_does_not_stop_configuration(self) -> None:
        (self.fixture.manager.root / ".ansible-callback.py").unlink()
        for name, content in controller.payload(self.api, self.inventory).items():
            self.api.put(None, str(self.folder / name), content)
        self.fixture.manager.start(self.request)
        self.assertFalse((self.folder / "callback_plugins/dz07_progress.py").exists())
        child = Mock()
        child.wait.return_value = child.poll.return_value = 0
        with patch("subprocess.Popen", return_value=child) as process:
            worker.execute(self.fixture.manager, self.request["id"])
        self.assertEqual(process.call_count, 3)
        self.assertTrue(all("ANSIBLE_CALLBACKS_ENABLED" not in call.kwargs["env"] for call in process.call_args_list))
        self.assertEqual(json.loads((self.folder / "result.json").read_text())["state"], "succeeded")

    def test_partial_observer_snapshot_never_replaces_an_existing_directory(self) -> None:
        (self.folder / "callback_plugins").mkdir()
        for name, content in controller.payload(self.api, self.inventory).items():
            self.api.put(None, str(self.folder / name), content)
        self.fixture.manager.start(self.request)
        self.assertEqual(self.fixture.launches, 1)
        self.assertFalse((self.folder / "callback_plugins/dz07_progress.py").exists())

    def test_unit_becoming_visible_during_start_is_not_launched_twice(self) -> None:
        for name, content in controller.payload(self.api, self.inventory).items():
            self.api.put(None, str(self.folder / name), content)
        states = [
            {"active": False, "loaded": False},
            {"active": True, "loaded": True},
            {"active": True, "loaded": True},
        ]
        with patch.object(self.fixture.manager, "unit", side_effect=states):
            result = self.fixture.manager.start(self.request)
        self.assertEqual(result["state"], "running")
        self.assertEqual(self.fixture.launches, 0)

    def test_deadline_expires_during_preparation_before_any_child_launch(self) -> None:
        for name, content in controller.payload(self.api, self.inventory).items():
            self.api.put(None, str(self.folder / name), content)
        self.fixture.manager.start(self.request)
        with (
            patch("time.monotonic", side_effect=[0, self.request["timeout"] + 1]),
            patch("subprocess.Popen") as process,
        ):
            worker.execute(self.fixture.manager, self.request["id"])
        process.assert_not_called()
        result = json.loads((self.folder / "result.json").read_text())
        self.assertEqual((result["state"], result["exit_code"], result["reason"]), ("failed", 124, "operation_timeout"))
