"""Every public command is dispatched locally with its cloud boundary disabled."""

import io
import json
import shutil
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr
from pathlib import Path
from unittest.mock import patch

import lab as cli
from test_runtime_support import lab

ROOT = Path(__file__).resolve().parents[1]


class CommandRoutingContracts(unittest.TestCase):
    def test_cli_builds_runtime_explicitly_before_dispatch(self) -> None:
        with (
            patch("sys.argv", ["lab.py", "preflight"]),
            patch("runtime.create_runtime", return_value=lab) as factory,
            patch("cloud_settings.sequential_enabled", return_value=False),
            patch.object(lab, "preflight") as preflight,
        ):
            cli.main()
        factory.assert_called_once_with(ROOT)
        preflight.assert_called_once_with()

    def test_optional_es_commands_are_explicit_and_ordinary_verify_keeps_its_contract(self) -> None:
        with (
            patch("sys.argv", ["lab.py", "verify", "--es-failover"]),
            patch("elastic_options.verify_failover") as check,
            patch.object(lab, "verify") as normal,
        ):
            lab.main()
        check.assert_called_once_with(lab)
        normal.assert_not_called()
        with patch("sys.argv", ["lab.py", "add-es4"]), patch("elastic_options.add_es4") as add:
            lab.main()
        add.assert_called_once_with(lab)
        for action in ("deploy", "test-cycle", "add-es4"):
            with (
                patch("sys.argv", ["lab.py", action, "--es-failover"]),
                patch("test_cycle.require_access") as access,
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                lab.main()
            access.assert_not_called()

    def test_in_memory_topology_override_is_not_replaced_by_saved_optional_topology(self) -> None:
        value = {"nodes": {"sentinel": {}}}
        with patch.object(lab, "_TOPOLOGY_OVERRIDE", value), patch("elastic_options.desired") as desired:
            self.assertIs(lab.desired_topology(), value)
        desired.assert_not_called()

    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory(dir=ROOT / ".tools")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.local = self.root / ".local"
        self.local.mkdir()
        shutil.copytree(ROOT / "config", self.root / "config")
        shutil.copyfile(ROOT / ".env.example", self.root / ".env.example")
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.multiple(lab, ROOT=self.root, LOCAL=self.local, _TEST_CYCLE_ID=None))
        self.access = self.stack.enter_context(patch("operator_network.check_saved_access"))

    def test_deploy_verify_configure_test_aliases_and_destroy(self) -> None:
        targets = {
            "deploy": "deploy_selected",
            "configure": "configure",
            "verify": "verify",
            "test": "regression",
            "test-cycle": "regression",
            "destroy": "destroy",
            "cleanup": "destroy",
        }
        for action, target in targets.items():
            with self.subTest(action=action), patch("sys.argv", ["lab.py", action]), patch.object(lab, target) as call:
                lab.main()
                if action in ("verify", "destroy", "cleanup"):
                    call.assert_called_once_with(False)
                else:
                    call.assert_called_once_with()
        with patch("sys.argv", ["lab.py", "destroy", "--purge-artifacts"]), patch.object(lab, "destroy") as call:
            lab.main()
            call.assert_called_once_with(True)

    def test_prepare_and_preflight_use_selected_infrastructure_backend(self) -> None:
        for action in ("prepare", "preflight"):
            for sequential in (False, True):
                legacy = "prepare_selected" if action == "prepare" else action
                with (
                    self.subTest(action=action, sequential=sequential),
                    patch("sys.argv", ["lab.py", action]),
                    patch("cloud_settings.sequential_enabled", return_value=sequential),
                    patch("staged_runtime." + action) as stage,
                    patch.object(lab, legacy) as previous,
                ):
                    lab.main()
                    if sequential:
                        stage.assert_called_once_with(lab)
                        previous.assert_not_called()
                    else:
                        previous.assert_called_once()
                        stage.assert_not_called()

    def test_open_needs_no_cloud_auth_and_selects_saved_context(self) -> None:
        for sequential in (False, True):
            marker = self.local / "staged-deployment.json"
            if sequential:
                marker.write_text("{}")
            with (
                patch("sys.argv", ["lab.py", "open"]),
                patch("staged_runtime.open_portal") as stage,
                patch("local_runtime.open_portal") as previous,
                patch.object(lab, "auth_config", side_effect=AssertionError("no auth")),
            ):
                lab.main()
                if sequential:
                    stage.assert_called_once_with(lab)
                    previous.assert_not_called()
                else:
                    previous.assert_called_once_with(self.root, 6)
                    stage.assert_not_called()

    def test_invalid_options_and_foreign_active_cycle_never_dispatch(self) -> None:
        for argv in (["lab.py", "verify", "--purge-artifacts"], ["lab.py", "unknown"]):
            with patch("sys.argv", argv), patch("test_cycle.require_access") as access, redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    lab.main()
                access.assert_not_called()
        (self.local / "test-cycle.json").write_text(json.dumps({"run_id": "other", "status": "running"}))
        with patch("sys.argv", ["lab.py", "deploy"]), patch.object(lab, "deploy_selected") as deploy:
            with self.assertRaises(RuntimeError):
                lab.main()
            deploy.assert_not_called()
