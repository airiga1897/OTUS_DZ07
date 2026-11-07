"""Checker subprocess boundary and reports use temporary directories only."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import quality
from test_cursor_coverage import entrypoint_code

ROOT = Path(__file__).resolve().parents[1]


class QualityPipelineTests(unittest.TestCase):
    def test_both_mypy_targets_run_and_either_failure_is_preserved(self) -> None:
        for windows, linux in ((0, 2), (2, 0), (0, 0)):
            results = [subprocess.CompletedProcess([], code, f"target {code}") for code in (windows, linux)]
            with self.subTest(windows=windows, linux=linux), patch("subprocess.run", side_effect=results) as run:
                report = quality.run_checks(self.root, ["mypy"])
            self.assertEqual(report[0].exit_code, windows or linux)
            self.assertEqual(
                [call.args[0][-2:] for call in run.call_args_list], [["--platform", "win32"], ["--platform", "linux"]]
            )
            self.assertEqual(
                [call.args[0][-4:-2] for call in run.call_args_list],
                [
                    ["--cache-dir", str(self.root / ".tools/quality" / ("mypy-cache-" + target))]
                    for target in ("win32", "linux")
                ],
            )
            log = (self.root / report[0].log).read_text(encoding="utf-8")
            self.assertIn("Target: win32", log)
            self.assertIn("Target: linux", log)

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir=ROOT / ".tools")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_all_tools_run_and_failures_remain_visible_without_credentials(self) -> None:
        results = [subprocess.CompletedProcess([], code, f"checker {code}") for code in (1, 0, 2, 0)]
        with (
            patch.dict(
                os.environ,
                {
                    "OS_PASSWORD": "SECRET",
                    "AWS_SECRET_ACCESS_KEY": "SECRET",
                    "DJANGO_SECRET_KEY": "SECRET",
                    "PATH": "path",
                },
            ),
            patch("subprocess.run", side_effect=results) as run,
        ):
            report = quality.run_checks(self.root)
        self.assertEqual([r.exit_code for r in report], [1, 0, 2])
        self.assertEqual(run.call_count, 4)
        for call in run.call_args_list:
            self.assertEqual(call.args[0][0], sys.executable)
            self.assertEqual(call.kwargs["cwd"], self.root)
            self.assertNotIn("OS_PASSWORD", call.kwargs["env"])
            self.assertNotIn("AWS_SECRET_ACCESS_KEY", call.kwargs["env"])
            self.assertNotEqual(call.kwargs["env"]["DJANGO_SECRET_KEY"], "SECRET")
        value = json.loads((self.root / ".tools/quality/latest-checks.json").read_text())
        self.assertEqual([r["exit_code"] for r in value], [1, 0, 2])
        self.assertNotIn("SECRET", json.dumps(value))

    def test_cli_success_failure_selection_and_unknown_tool(self) -> None:
        for code in (0, 1):
            with patch("subprocess.run", return_value=subprocess.CompletedProcess([], code, "safe")):
                self.assertEqual(quality.main(self.root, ["mypy"]), int(bool(code)))
        with patch("subprocess.run") as run:
            with self.assertRaises(ValueError):
                quality.run_checks(self.root, ["unknown"])
            run.assert_not_called()

    def test_default_cli_runs_all_tools_and_rejects_unknown_before_execution(self) -> None:
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "safe")) as run:
            self.assertEqual(quality.main(self.root, []), 0)
            self.assertEqual(run.call_count, 4)
        with patch("subprocess.run") as run:
            with self.assertRaises(SystemExit) as raised:
                quality.main(self.root, ["unknown"])
            self.assertEqual(raised.exception.code, 2)
            run.assert_not_called()

    def test_cli_default_root_and_entrypoint_propagate_the_result(self) -> None:
        with (
            patch.object(quality, "__file__", str(self.root / "scripts/quality.py")),
            patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "safe")) as run,
        ):
            self.assertEqual(quality.main(argv=[]), 0)
        self.assertTrue(all(call.kwargs["cwd"] == self.root for call in run.call_args_list))
        with self.assertRaises(SystemExit) as raised:
            exec(entrypoint_code(quality), {**vars(quality), "__name__": "__main__", "main": lambda: 7})
        self.assertEqual(raised.exception.code, 7)
