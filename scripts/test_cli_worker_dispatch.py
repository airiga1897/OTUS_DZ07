"""Execute original CLI guards in private namespaces with isolated destinations."""

import io
import json
import tarfile
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

import cache_packages
import prepare_ansible
import release_artifacts
import s3_artifacts
import terraform_plugins
from remote import check_ipv4, download_packages, fetch_artifacts, network_diagnostics, portal_probe, validate_filebeat
from test_cursor_coverage import entrypoint_code

ROOT = Path(__file__).resolve().parents[1]


class WorkerDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(dir=ROOT / ".tools")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "config").mkdir()

    def execute(self, module, **overrides):
        namespace = {**vars(module), "__name__": "__main__", **overrides}
        exec(entrypoint_code(module), namespace)
        return namespace

    def test_worker_entrypoints_dispatch_once_and_propagate_exit_status(self) -> None:
        for module in (check_ipv4, fetch_artifacts, portal_probe):
            main = Mock()
            with self.subTest(module=module.__name__):
                self.execute(module, main=main)
            main.assert_called_once_with()
        main = Mock(return_value=7)
        with (
            patch("sys.argv", ["validate_filebeat.py", "/synthetic/settings.json"]),
            self.assertRaises(SystemExit) as error,
        ):
            self.execute(validate_filebeat, main=main)
        main.assert_called_once_with("/synthetic/settings.json")
        self.assertEqual(error.exception.code, 7)
        collect = Mock(return_value={"checks": {}, "hostname": "synthetic"})
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            self.execute(network_diagnostics, collect=collect)
        prefix, body = output.getvalue().strip().split("=", 1)
        self.assertEqual(prefix, "DZ07_NETWORK_DIAGNOSTICS")
        self.assertEqual(json.loads(body)["hostname"], "synthetic")
        collect.assert_called_once_with()

    def test_local_package_cli_passes_explicit_source_directory(self) -> None:
        main = Mock()
        with patch("sys.argv", ["cache_packages.py", "--source", str(self.root)]):
            self.execute(cache_packages, main=main)
        main.assert_called_once_with(str(self.root))

    def test_release_prepare_dispatches_without_build_or_publication(self) -> None:
        prepare, build = Mock(), Mock()
        with (
            patch("sys.argv", ["release_artifacts.py", "prepare"]),
            patch.multiple(release_artifacts, ROOT=self.root, prepare=prepare, build=build),
        ):
            self.execute(release_artifacts)
        prepare.assert_called_once_with(self.root)
        build.assert_not_called()

    def test_s3_cli_distinguishes_probe_from_package_download_verification(self) -> None:
        for action in ("verify", "verify-packages"):
            probe, verify, prepare = Mock(), Mock(), Mock()
            with (
                self.subTest(action=action),
                patch("sys.argv", ["s3_artifacts.py", action]),
                patch("artifact_lifecycle.probe", probe),
            ):
                self.execute(s3_artifacts, ROOT=self.root, verify_packages=verify, prepare=prepare)
            prepare.assert_not_called()
            if action == "verify":
                probe.assert_called_once_with(self.root)
                verify.assert_not_called()
            else:
                verify.assert_called_once_with()
                probe.assert_not_called()

    def test_s3_cli_redacts_failure_instead_of_exposing_credentials(self) -> None:
        with (
            patch("sys.argv", ["s3_artifacts.py"]),
            patch("privacy.redact", return_value="[REDACTED]"),
            self.assertRaises(SystemExit) as caught,
        ):
            self.execute(s3_artifacts, prepare=Mock(side_effect=RuntimeError("PRIVATE-SECRET")))
        self.assertNotIn("PRIVATE", str(caught.exception))
        self.assertIn("[REDACTED]", str(caught.exception))

    def test_ipv4_worker_success_requires_valid_address_and_route(self) -> None:
        import subprocess

        address = [
            {
                "ifname": "eth0",
                "flags": ["UP"],
                "addr_info": [{"family": "inet", "local": "10.77.0.10", "prefixlen": 24}],
            }
        ]
        routes = [{"dst": "default", "gateway": "10.77.0.1", "dev": "eth0"}]
        replies = [subprocess.CompletedProcess([], 0, json.dumps(value), "") for value in (address, routes)]
        with (
            patch("sys.argv", ["check_ipv4.py", "10.77.0.10/24", "10.77.0.1"]),
            patch("subprocess.run", side_effect=replies),
            patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            check_ipv4.main()
        self.assertIn("verified", output.getvalue())

    def test_public_download_cli_reports_403_and_preserves_other_http_errors(self) -> None:
        (self.root / "elk-settings.json").write_text(
            '{"version":"8.19.0","elastic_download_base":"https://artifacts.elastic.co/downloads"}'
        )
        for code in (403, 503):
            fetch = Mock(
                side_effect=urllib.error.HTTPError("https://artifacts.elastic.co", code, "synthetic", {}, None)
            )
            with (
                self.subTest(code=code),
                self.assertRaises(SystemExit if code == 403 else urllib.error.HTTPError) as error,
            ):
                self.execute(download_packages, BASE=self.root, fetch=fetch)
            fetch.assert_called_once_with(self.root / "packages", "8.19.0", "https://artifacts.elastic.co/downloads")
            if code == 403:
                self.assertIn("ARTIFACT_SOURCE_UNAVAILABLE", str(error.exception))

    def test_local_preparation_cli_selects_online_or_cached(self) -> None:
        for online in (True, False):
            prepare = Mock()
            with patch("delivery.internet", return_value=online), patch("builtins.print"):
                self.execute(prepare_ansible, __file__=str(self.root / "scripts/prepare_ansible.py"), prepare=prepare)
            if online:
                prepare.assert_not_called()
            else:
                prepare.assert_called_once_with(self.root)
        prepare = Mock()
        dependencies = Mock()
        with patch("runtime.create_runtime", return_value=dependencies) as create:
            self.execute(terraform_plugins, prepare=prepare, __file__=str(self.root / "scripts/terraform_plugins.py"))
        create.assert_called_once_with(self.root)
        prepare.assert_called_once_with(dependencies)

    def test_source_archive_excludes_runtime_secret_json_in_allowed_directories(self) -> None:
        (self.root / "app").mkdir()
        (self.root / "app/source.py").write_text("public source")
        (self.root / "config/secrets.json").write_text('{"app":"synthetic-private-secret"}')
        archive = self.root / "source.tar.gz"
        s3_artifacts.source_archive(self.root, archive)
        with tarfile.open(archive) as stream:
            self.assertIn("app/source.py", stream.getnames())
            self.assertNotIn("config/secrets.json", stream.getnames())
