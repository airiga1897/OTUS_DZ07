"""JSON over SSH remains strict while independent diagnostics are drained safely."""

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from collections import deque
from collections.abc import Iterable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from paramiko import SSHClient
from test_runtime_support import lab

ROOT = Path(__file__).resolve().parents[1]


class Channel:
    def __init__(self, stdout: Iterable[bytes], stderr: Iterable[bytes], status: int = 0) -> None:
        self.out, self.err = deque(stdout), deque(stderr)
        self.status, self.closed, self.combined = status, False, False

    def set_combine_stderr(self, value: bool) -> None:
        self.combined = value

    def exec_command(self, command: str) -> None:
        pass

    def exit_status_ready(self) -> bool:
        return True  # Buffers can still contain final bytes.

    def recv_ready(self) -> bool:
        return bool(self.out or (self.combined and self.err))

    def recv_stderr_ready(self) -> bool:
        return bool(self.err and not self.combined)

    def recv(self, size: int) -> bytes:
        return (self.err if self.combined and self.err else self.out).popleft()

    def recv_stderr(self, size: int) -> bytes:
        return self.err.popleft()

    def recv_exit_status(self) -> int:
        return self.status

    def sendall(self, value: str | bytes) -> None:
        self.stdin = value

    def shutdown_write(self) -> None:
        self.stdin_closed = True

    def close(self) -> None:
        self.closed = True


class RemoteStreamsTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(dir=ROOT / ".tools")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for name, value in {
            "ROOT": self.root,
            "LOCAL": self.root / ".local",
            "RUN": self.root / ".local/runs/test",
        }.items():
            setting = patch.object(lab, name, value)
            setting.start()
            self.addCleanup(setting.stop)

    def run_remote(self, channel: Channel, stdin: str | bytes | None = None) -> str:
        transport = SimpleNamespace(open_session=lambda **kw: channel)
        client = Mock(spec_set=SSHClient)
        client.get_transport.return_value = transport
        with contextlib.redirect_stdout(io.StringIO()):
            return lab.remote(client, "synthetic command", "streams", stdin=stdin)

    def test_real_child_json_stdout_and_request_journal_stderr(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                'import sys,json; sys.stderr.write("HTTP method=POST status=302\\n"); print(json.dumps({"success":True}))',
            ],
            capture_output=True,
            check=True,
        )
        with self.assertRaises(json.JSONDecodeError):
            json.loads(result.stderr + result.stdout)
        channel = Channel([result.stdout[:3], result.stdout[3:]], [result.stderr])
        self.assertTrue(json.loads(self.run_remote(channel))["success"])
        log = (lab.RUN / "streams.log").read_text()
        self.assertIn("[stderr]\nHTTP method=POST status=302", log)
        self.assertFalse(channel.combined)
        self.assertTrue(channel.closed)

    def test_large_stderr_is_drained_even_after_exit_and_secrets_are_redacted(self) -> None:
        channel = Channel([b'{"ok":true}'], [b"synthetic-secret\n" * 4000 for _ in range(3)])
        with patch.object(lab, "redact", side_effect=lambda text: text.replace("synthetic-secret", "[REDACTED]")):
            self.assertTrue(json.loads(self.run_remote(channel))["ok"])
        self.assertFalse(channel.err)
        self.assertNotIn("synthetic-secret", (lab.RUN / "streams.log").read_text())

    def test_nonzero_status_is_not_treated_as_successful_json(self) -> None:
        channel = Channel([b'{"ok":true}'], [b"configuration failed"], status=2)
        with self.assertRaisesRegex(RuntimeError, "Remote command failed: streams"):
            self.run_remote(channel)
        self.assertIn("configuration failed", (lab.RUN / "streams.log").read_text())
        self.assertTrue(channel.closed)

    def test_stderr_only_success_returns_empty_stdout_and_preserves_stdin(self) -> None:
        channel = Channel([], [b"notice"])
        self.assertEqual(self.run_remote(channel, stdin="payload"), "")
        self.assertEqual(channel.stdin, b"payload")
        self.assertTrue(channel.stdin_closed)

    def test_timeout_closes_channel(self) -> None:
        channel = Channel([], [])
        with (
            patch.object(channel, "exit_status_ready", return_value=False),
            patch.object(lab.time, "monotonic", side_effect=[0, 1801]),
            self.assertRaises(TimeoutError),
        ):
            self.run_remote(channel)
        self.assertTrue(channel.closed)

    def test_invalid_stdout_remains_invalid_json(self) -> None:
        channel = Channel([b'noise\n{"ok":true}'], [b"notice"])
        with self.assertRaises(json.JSONDecodeError):
            json.loads(self.run_remote(channel))

    def test_baseline_snapshot_parses_json_with_request_logs_on_stderr(self) -> None:
        import verification

        lab.LOCAL.mkdir(parents=True)
        channel = Channel([b'{"new_record":{"id":12}}'], [b"HTTP method=POST status=302\n"])
        client = SimpleNamespace(get_transport=lambda: SimpleNamespace(open_session=lambda **kw: channel))
        api = SimpleNamespace(
            LOCAL=lab.LOCAL,
            STACK={"paths": {"remote_root": "/synthetic"}},
            connect=lambda value: contextlib.nullcontext(client),
            upload_tree=lambda value: None,
            remote=lab.remote,
            save=lambda path, value: path.write_text(json.dumps(value)),
        )
        previous = {"server_ids": {"app": "synthetic-vm"}, "private_ips": {"app": "192.0.2.10"}}
        with contextlib.redirect_stdout(io.StringIO()):
            result = verification.snapshot(api, previous)
        self.assertEqual(result["before"]["new_record"]["id"], 12)
        self.assertEqual(json.loads((lab.LOCAL / "expansion-pending.json").read_text()), result)
