"""Run bootstrap worker contracts without cloud-init, apt or pip on the host."""

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from remote import ansible_control, check_cloud_init
from test_cursor_coverage import entrypoint_code

ROOT = Path(__file__).resolve().parents[1]


class BootstrapWorkers(unittest.TestCase):
    def test_completed_cloud_init_accepts_only_done_with_no_fatal_stage_errors(self) -> None:
        for code, status, errors, nested, expected in (
            (0, "done", [], {}, True),
            (2, "done", [], {}, True),
            (1, "done", [], {}, False),
            (0, "running", [], {}, False),
            (0, "done", ["fatal"], {}, False),
            (2, "done", [], {"errors": ["fatal"]}, False),
            (2, "done", [], {"errors": []}, True),
        ):
            result: dict[str, object] = {
                "status": status,
                "errors": errors,
                "modules-final": nested,
                "recoverable_errors": {"warning": "synthetic"},
            }
            with self.subTest(code=code, status=status, errors=errors, nested=nested):
                self.assertIs(check_cloud_init.completed(result, code), expected)

    def test_cloud_init_cli_checks_real_json_and_exit_code_before_reporting_ready(self) -> None:
        for status, code, success in (("done", 2, True), ("error", 1, False)):
            run = Mock(
                stdout=json.dumps({"status": status, "recoverable_errors": {"warning": "synthetic"}}), returncode=code
            )
            output = io.StringIO()
            with (
                patch("subprocess.run", return_value=run) as process,
                contextlib.redirect_stdout(output),
            ):
                if success:
                    exec(entrypoint_code(check_cloud_init), {**vars(check_cloud_init), "__name__": "__main__"})
                else:
                    with self.assertRaisesRegex(SystemExit, "fatal errors"):
                        exec(entrypoint_code(check_cloud_init), {**vars(check_cloud_init), "__name__": "__main__"})
            process.assert_called_once_with(
                ["cloud-init", "status", "--wait", "--format", "json"], capture_output=True, text=True
            )
            self.assertEqual("cloud-init completed" in output.getvalue(), success)

    def test_missing_ansible_runtime_installs_system_dependencies_and_pinned_version(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tools") as directory:
            venv = Path(directory) / "venv"
            with (
                patch.dict(os.environ, {}, clear=False),
                patch("subprocess.run", return_value=Mock(returncode=1, stdout="")) as process,
            ):
                ansible_control.main(
                    ansible_control.ControllerSettings(ansible_control.load_settings(ROOT).version, venv)
                )
                self.assertEqual(os.environ["DEBIAN_FRONTEND"], "noninteractive")
            calls = [call.args[0] for call in process.call_args_list]
            self.assertEqual(
                calls[:3],
                [
                    ["apt-get", "update", "-qq"],
                    ["apt-get", "install", "-y", "-qq", "python3-venv", "openssh-client"],
                    ["python3", "-m", "venv", str(venv)],
                ],
            )
            self.assertEqual(
                calls[-1],
                [
                    str(venv / "bin/python"),
                    "-m",
                    "pip",
                    "install",
                    "ansible-core==" + ansible_control.load_settings(ROOT).version,
                ],
            )

    def test_existing_correct_version_is_reused_and_wrong_version_is_replaced(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tools") as directory:
            venv = Path(directory) / "venv"
            (venv / "bin").mkdir(parents=True)
            (venv / "bin/python").write_text("synthetic")
            for version, expected in ((ansible_control.load_settings(ROOT).version + "\n", 1), ("old-version", 2)):
                with (
                    self.subTest(version=version),
                    patch.dict(os.environ, {}, clear=False),
                    patch("subprocess.run", return_value=Mock(returncode=0, stdout=version)) as process,
                ):
                    ansible_control.main(
                        ansible_control.ControllerSettings(ansible_control.load_settings(ROOT).version, venv)
                    )
                    self.assertEqual(process.call_count, expected)
                    self.assertFalse(any(call.args[0][0] == "apt-get" for call in process.call_args_list))

    def test_failed_system_preparation_does_not_continue_to_pip(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tools") as directory:
            with (
                patch.dict(os.environ, {}, clear=False),
                patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, ["apt-get"])) as process,
                self.assertRaises(subprocess.CalledProcessError),
            ):
                ansible_control.main(ansible_control.ControllerSettings("synthetic", Path(directory) / "missing"))
            self.assertEqual(process.call_count, 1)

    def test_ansible_cli_invokes_preparation_once(self) -> None:
        main = Mock()
        exec(entrypoint_code(ansible_control), {**vars(ansible_control), "__name__": "__main__", "main": main})
        main.assert_called_once_with()
