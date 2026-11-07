"""Provider download checks never authenticate or allocate resources."""

import os
import subprocess
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from cloud_contract import TerraformError
from terraform_plugins import prepare


@dataclass
class MirrorDependencies:
    ROOT: Path
    TF: Path

    def redact(self, text: str) -> str:
        return text


class ProviderMirrorTests(unittest.TestCase):
    def fixture(self, root: Path) -> MirrorDependencies:
        (root / ".tools").mkdir()
        for kind in ("network", "global"):
            p = root / "terraform-selectel/staged" / kind
            p.mkdir(parents=True)
            (p / "versions.tf").write_text("terraform {}")
            (p / ".terraform.lock.hcl").write_text("# synthetic lock")
        return MirrorDependencies(ROOT=root, TF=root / "terraform")

    def test_offline_mirror_never_receives_cloud_auth_or_external_tf_flags(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            api = self.fixture(root)
            with (
                patch.dict(os.environ, {"OS_PASSWORD": "synthetic", "TF_CLI_ARGS": "-upgrade"}),
                patch(
                    "terraform_plugins.subprocess.run", return_value=subprocess.CompletedProcess([], 0, b"", b"")
                ) as run,
            ):
                config = prepare(api)
            self.assertNotIn("network_mirror", config.read_text())
            for call in run.call_args_list:
                env = call.kwargs["env"]
                self.assertNotIn("OS_PASSWORD", env)
                self.assertNotIn("TF_CLI_ARGS", env)
                self.assertIn("-lockfile=readonly", call.args[0])
            self.assertEqual(list((root / ".tools").glob("provider-check-*")), [])

    def test_connection_reset_retries_download_then_verifies_offline(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            api = self.fixture(root)
            results = [
                subprocess.CompletedProcess([], n, b"", error)
                for n, error in [(1, b"not in local mirror"), (1, b"connection reset"), (0, b""), (0, b""), (0, b"")]
            ]
            with patch("terraform_plugins.subprocess.run", side_effect=results) as run:
                prepare(api)
            self.assertEqual(run.call_count, 5)
            self.assertEqual(list((root / ".tools").glob("provider-check-*")), [])

    def test_checksum_failure_stops_without_retry_and_removes_temporary_files(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            api = self.fixture(root)
            with (
                patch(
                    "terraform_plugins.subprocess.run",
                    return_value=subprocess.CompletedProcess([], 1, b"", b"checksum mismatch"),
                ) as run,
                self.assertRaises(TerraformError),
            ):
                prepare(api)
            self.assertEqual(run.call_count, 2)
            self.assertEqual(list((root / ".tools").glob("provider-check-*")), [])
