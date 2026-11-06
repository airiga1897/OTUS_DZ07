"""Run real PowerShell preparation with fully isolated mock native/network boundaries."""

import os
import shutil
import subprocess
import unittest
from pathlib import Path


class SetupContracts(unittest.TestCase):
    def test_bootstrap_reuse_and_failure_contracts_without_network(self) -> None:
        root = Path(__file__).resolve().parents[1]
        executable = shutil.which("powershell.exe")
        self.assertIsNotNone(executable, "Windows PowerShell is required for the supported Windows setup")
        assert executable is not None
        environment = os.environ.copy()
        environment = {key: value for key, value in environment.items() if key.upper() != "PSMODULEPATH"}
        result = subprocess.run(
            [executable, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(root / "tests/test_setup.ps1")],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("verified offline", result.stdout)
