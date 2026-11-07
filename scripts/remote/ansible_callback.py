"""Ansible notification callback; never serializes a module result or arguments."""

import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from ansible.plugins.callback import CallbackBase

if TYPE_CHECKING:
    from ansible.executor.task_result import TaskResult
    from ansible.inventory.host import Host
    from ansible.playbook.task import Task
    from remote.ansible_events import Writer as ProgressWriter

DOCUMENTATION = """
name: dz07_progress
type: notification
short_description: Private bounded DZ07 task progress
version_added: "2.18"
requirements:
  - enabled in the DZ07 systemd operation
"""


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "notification"
    CALLBACK_NAME = "dz07_progress"
    CALLBACK_NEEDS_ENABLED = True

    def __init__(self) -> None:
        super().__init__()
        self.writer: ProgressWriter | None = None
        try:
            # The operation snapshot contains a private namespace module beside runner.py.
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            from remote.ansible_events import Writer

            self.writer = Writer(os.environ["DZ07_PROGRESS_FILE"])
        except Exception as error:
            self.unavailable("initialize", error)

    def unavailable(self, phase: str = "unknown", error: Exception | None = None) -> None:
        sequence = self.writer.sequence if self.writer is not None else 0
        self.writer = None
        try:
            from remote.ansible_events import write_failure

            path = Path(os.environ["DZ07_PROGRESS_FILE"]).with_suffix(".unavailable")
            write_failure(path, phase, error, sequence)
        except Exception:
            try:
                path = Path(os.environ["DZ07_PROGRESS_FILE"]).with_suffix(".unavailable")
                descriptor = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
                os.close(descriptor)
            except Exception:
                pass  # Observation must never interrupt the playbook or print its inputs.

    def event(
        self, task: "Task", host: "Host", kind: str, status: str, result: Mapping[str, object] | None = None
    ) -> None:
        if self.writer is None:
            return
        phase = "metadata"
        try:
            hidden = task.no_log is not False
            if result is not None:
                hidden |= bool(result.get("_ansible_no_log"))
                items = result.get("results")
                if items is not None:
                    hidden |= not isinstance(items, list) or any(
                        bool(item.get("_ansible_no_log")) for item in items if isinstance(item, dict)
                    )
            if hidden:
                name, status = "[no_log]", "hidden"
            else:
                # get_name() may render variables or synthesize module arguments.
                source = task.get_ds()
                name = source.get("name", "") if isinstance(source, dict) else ""
                if (
                    not isinstance(name, str)
                    or not name
                    or len(name) > 200
                    or any(c in name for c in ("{", "}", "\n", "\r", "\x1b", "://"))
                    or any(ord(c) < 32 for c in name)
                ):
                    name = "[task name hidden]"
            hostname = host.get_name()
            phase = "write"
            self.writer.emit(name, hostname, kind, status)
        except Exception as error:
            self.unavailable(phase, error)

    def v2_runner_on_start(self, host: "Host", task: "Task") -> None:
        self.event(task, host, "start", "start")

    def completed(self, result: "TaskResult", status: str) -> None:
        if self.writer is None:
            return  # Preserve the first cause after observation was disabled.
        try:
            payload = result._result
            if not isinstance(payload, dict):
                payload = {"_ansible_no_log": True}
            if status == "ok" and payload.get("changed"):
                status = "changed"
            self.event(result._task, result._host, "result", status, payload)
        except Exception as error:
            self.unavailable("metadata", error)

    def v2_runner_on_ok(self, result: "TaskResult") -> None:
        self.completed(result, "ok")

    def v2_runner_on_failed(self, result: "TaskResult", ignore_errors: bool = False) -> None:
        self.completed(result, "failed")

    def v2_runner_on_unreachable(self, result: "TaskResult") -> None:
        self.completed(result, "failed")

    def v2_runner_on_skipped(self, result: "TaskResult") -> None:
        self.completed(result, "skipped")
