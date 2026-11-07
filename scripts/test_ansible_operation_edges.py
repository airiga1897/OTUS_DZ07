"""Operation transport deadlines, process groups and lifecycle recovery boundaries."""

import copy
import io
import json
import signal
import subprocess
import threading
import unittest
from collections.abc import Callable
from pathlib import Path
from types import FrameType
from unittest.mock import MagicMock, Mock, patch

import ansible_operation as controller
import test_ansible_operation as fixtures
from cloud_contract import ConfigurationTarget, StandDescription
from paramiko import SSHClient
from remote import ansible_job as worker
from runtime_ports import OperationPort

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_UPLOAD = controller.put_private


class OperationEdges(unittest.TestCase):
    def test_native_worker_operations_reject_windows_before_touching_os_resources(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("linux_worker_guard", ROOT / "scripts/remote/ansible_job.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        path = self.fixture.root / "must-not-create-lock"
        with patch("sys.platform", "win32"):
            with self.assertRaisesRegex(OSError, "Linux application VM"), module.locked(path):
                self.fail("Unsupported native lock was acquired")
            with patch("os.killpg", create=True) as kill, self.assertRaisesRegex(OSError, "Linux application VM"):
                module.signal_group(123)
            kill.assert_not_called()
        self.assertFalse(path.exists())

    def test_corrupt_saved_request_blocks_status_and_preserves_files(self) -> None:
        self.fixture.reserve()
        request = controller.required(self.api)["request"]
        path = self.fixture.manager.folder(request["id"]) / "request.json"
        invalid: tuple[object, ...] = (None, [], {}, {"request": request, "boot_id": None})
        for value in invalid:
            document = json.dumps(value)
            path.write_text(document)
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "parameters or ownership"):
                self.fixture.manager.status(request)
            self.assertEqual(path.read_text(), document)
            self.assertEqual(self.fixture.launches, 0)

    def test_corrupt_remote_result_is_unknown_without_restarting_the_unit(self) -> None:
        self.fixture.reserve()
        request = controller.required(self.api)["request"]
        path = self.fixture.manager.folder(request["id"]) / "result.json"
        value: object
        for value in (None, [], "PRIVATE"):
            path.write_text(json.dumps(value))
            with self.subTest(value=value):
                result = self.fixture.manager.status(request)
                self.assertEqual(result, {"state": "unknown", "reason": "result_missing_or_invalid"})
                self.assertEqual(self.fixture.launches, 0)
                self.assertEqual(controller.required(self.api)["request"]["id"], request["id"])

    def test_invalid_saved_operation_state_blocks_resumption(self) -> None:
        self.fixture.reserve()
        saved = controller.required(self.api)
        state: object
        for state in (None, [], "PRIVATE"):
            controller.path(self.api).write_text(json.dumps({**saved, "state": state}))
            with self.subTest(state=state), self.assertRaises(controller.OperationProtocolError):
                controller.read(self.api)

    def test_verified_deleted_target_still_requires_the_same_app_identity(self) -> None:
        self.fixture.reserve()
        provider = Mock(
            configuration_target=Mock(
                return_value=ConfigurationTarget({"server_ids": {"app": "foreign-app"}}, deletion_verified=True)
            )
        )
        with patch.object(controller, "rpc") as rpc, self.assertRaises(controller.OperationProtocolError):
            controller.before_destroy(self.api, provider)
        rpc.assert_not_called()
        self.assertFalse(controller.required(self.api).get("stop_confirmed"))

    def test_non_boolean_bootstrap_is_rejected_before_saving_or_rpc(self) -> None:
        flag: object
        for flag in (None, "false", []):
            controller.path(self.api).unlink(missing_ok=True)
            inventory = copy.deepcopy(self.inventory)
            inventory["all"]["vars"]["dz07_bootstrap"] = flag
            with self.subTest(flag=flag), patch.object(controller, "rpc", return_value={"state": "preparing"}) as rpc:
                with self.assertRaises(controller.OperationProtocolError):
                    controller.reserve(self.api, self.lab, inventory)
                self.assertFalse(controller.path(self.api).exists())
                rpc.assert_not_called()

    def test_collection_reason_in_evidence_does_not_mask_the_operation_failure(self) -> None:
        self.fixture.reserve()
        value = controller.required(self.api)
        controller.record(self.api, value, {"state": "failed", "reason": ["PRIVATE"], "phase": {"PRIVATE": True}})
        public = (self.fixture.root / "docs/ansible-operation-result.json").read_text()
        self.assertNotIn("PRIVATE", public)
        self.assertEqual(json.loads(public)["state"], "failed")

    def test_unhashable_rpc_state_is_a_protocol_error(self) -> None:
        self.fixture.reserve()
        request = controller.required(self.api)["request"]
        with (
            patch.object(controller, "command", return_value=json.dumps({"state": []})),
            self.assertRaises(controller.OperationProtocolError),
        ):
            controller.rpc(self.api, self.lab, "status", request)

    def test_corrupt_saved_context_never_becomes_a_new_operation(self) -> None:
        raw: object
        for raw in (None, [], "PRIVATE", {}, {"request": None}):
            controller.path(self.api).write_text(json.dumps(raw))
            original = controller.path(self.api).read_bytes()
            with self.subTest(raw=raw), patch.object(controller, "rpc", return_value={"state": "preparing"}) as rpc:
                with self.assertRaises(controller.OperationProtocolError):
                    controller.reserve(self.api, self.lab, self.inventory)
                rpc.assert_not_called()
                self.assertEqual(controller.path(self.api).read_bytes(), original)

    def test_saved_context_boolean_flags_are_not_coerced_from_strings(self) -> None:
        self.fixture.reserve()
        saved = controller.required(self.api)
        for field in ("bootstrap", "acknowledged", "finalized", "stop_confirmed", "finishing_pending"):
            value = {**saved, field: "false"}
            controller.path(self.api).write_text(json.dumps(value))
            with self.subTest(field=field), self.assertRaises(controller.OperationProtocolError):
                controller.read(self.api)

    def test_private_upload_without_an_sftp_channel_preserves_network_failure(self) -> None:
        client = MagicMock()
        sftp = client.open_sftp.return_value.__enter__.return_value
        sftp.get_channel.return_value = None
        with self.assertRaises(OSError):
            PRIVATE_UPLOAD(client, "/synthetic/key", "PRIVATE-synthetic")
        sftp.open.assert_not_called()
        client.open_sftp.return_value.__exit__.assert_called_once()

    def test_corrupt_release_source_digest_blocks_operation_creation(self) -> None:
        (self.fixture.root / "config/services.json").write_text('{"artifact_delivery":"release"}')
        value: object
        for value in (None, [], {}, {"source": None}, {"source": {"sha512": []}}, {"source": {"sha512": "PRIVATE"}}):
            (self.fixture.local / "artifact-delivery.json").write_text(json.dumps(value))
            with self.subTest(value=value):
                with self.assertRaises(controller.OperationProtocolError) as caught:
                    controller.source_version(self.api)
                self.assertNotIn("PRIVATE", str(caught.exception))
        self.assertFalse(controller.path(self.api).exists())

    def test_policy_rejects_non_object_documents_without_creating_an_operation(self) -> None:
        value: object
        for value in (None, [], 42):
            (self.fixture.root / "config/ansible-operation.json").write_text(json.dumps(value))
            with self.subTest(value=value), self.assertRaises(ValueError):
                controller.policy(self.api)
        self.assertFalse(controller.path(self.api).exists())

    def test_rpc_without_transport_reports_a_network_error(self) -> None:
        client = Mock()
        client.get_transport.return_value = None
        with self.assertRaises(OSError):
            controller.command_body(client, "synthetic-command", None, 1)

    def test_rpc_transport_loss_after_opening_channel_closes_that_channel(self) -> None:
        channel = Mock()
        channel.exit_status_ready.return_value = False
        transport = Mock()
        transport.open_session.return_value = channel
        transport.is_active.return_value = False
        client = Mock()
        client.get_transport.side_effect = [transport, None]
        with self.assertRaises(TimeoutError):
            controller.command_body(client, "synthetic-command", None, 1)
        channel.close.assert_called_once()
        client.get_transport.assert_called_once()

    def test_worker_completes_all_three_steps_and_persists_exit_zero(self) -> None:
        self.fixture.reserve()
        request = controller.required(self.api)["request"]
        manager = self.fixture.manager
        for name, content in controller.payload(self.api, self.inventory).items():
            self.api.put(None, str(manager.folder(request["id"]) / name), content)
        manager.start(request)
        child = Mock()
        child.wait.return_value = 0
        child.poll.return_value = 0
        with patch("subprocess.Popen", return_value=child) as process:
            worker.execute(manager, request["id"])
        self.assertEqual(process.call_count, 3)
        commands = [call.args[0] for call in process.call_args_list]
        self.assertIn("--syntax-check", commands[1])
        self.assertNotIn("--syntax-check", commands[2])
        self.assertNotIn("PRIVATE-", repr(commands))
        self.fixture.unit["active"] = False
        result = manager.status(request)
        self.assertEqual((result["state"], result["exit_code"]), ("succeeded", 0))
        self.assertIsNotNone(result["started_utc"])
        self.assertIsNotNone(result["finished_utc"])

    def setUp(self) -> None:
        self.fixture = fixtures.DurableAnsibleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.api = self.fixture.api
        self.lab = self.fixture.lab
        self.inventory = self.fixture.inventory

    def test_protocol_rejection_and_unknown_result_preserve_context(self) -> None:
        self.fixture.reserve()
        value = controller.required(self.api)
        with (
            patch.object(controller, "command", return_value='{"state":"foreign"}'),
            self.assertRaises(controller.OperationProtocolError),
        ):
            controller.rpc(self.api, self.lab, "status", value["request"])
        with (
            patch.object(controller, "command", return_value='{"state":"running","id":"foreign"}'),
            self.assertRaises(controller.OperationProtocolError),
        ):
            controller.rpc(self.api, self.lab, "status", value["request"])
        with (
            patch.object(controller, "rpc", return_value={"state": "unknown", "reason": "result_missing_or_invalid"}),
            self.assertRaises(controller.OperationUnknown),
        ):
            controller.wait(self.api, self.lab, self.inventory)
        self.assertTrue(controller.path(self.api).exists())

    def test_partial_response_after_start_is_observed_not_replaced(self) -> None:
        self.fixture.reserve()
        request = controller.required(self.api)["request"]
        with patch.object(
            controller, "command", side_effect=["", json.dumps({"state": "running", "id": request["id"]})]
        ) as command:
            result = controller.rpc(self.api, self.lab, "start", request)
        self.assertEqual(result["state"], "running")
        self.assertEqual([c.args[2]["action"] for c in command.call_args_list], ["start", "status"])

    def test_ssh_authentication_and_host_key_errors_do_not_retry_or_launch(self) -> None:
        self.fixture.reserve()
        request = controller.required(self.api)["request"]
        import paramiko

        for error in (
            paramiko.AuthenticationException("PRIVATE"),
            paramiko.BadHostKeyException("host", Mock(), Mock()),
        ):
            self.api.connect.side_effect = error
            with self.assertRaises(controller.OperationProtocolError) as caught:
                controller.rpc(self.api, self.lab, "start", request)
            self.assertNotIn("PRIVATE", str(caught.exception))
        self.assertEqual(self.fixture.launches, 0)

    def test_finishing_success_requires_acknowledgement_and_finalization(self) -> None:
        self.fixture.reserve()
        controller.wait(self.api, self.lab, self.inventory, upload=True)
        with self.assertRaises(controller.OperationUnknown):
            controller.finalize(self.api)
        value = controller.required(self.api)
        value["finishing_pending"] = True
        controller.save(self.api, value)
        with patch("observability.package_cache"):
            controller.finish(self.api, self.lab)
        self.assertFalse(controller.required(self.api).get("finishing_pending"))
        controller.finalize(self.api)
        old = controller.required(self.api)["request"]["id"]
        self.fixture.unit.update(loaded=False, active=False)
        controller.reserve(self.api, self.lab, self.inventory)
        self.assertNotEqual(controller.required(self.api)["request"]["id"], old)

    def test_missing_result_blocks_finish_and_successful_finalizer_does_not_hide_failure(self) -> None:
        self.fixture.reserve()
        with (
            patch("observability.package_cache", side_effect=OSError("PRIVATE-TOKEN")),
            self.assertRaises(controller.OperationUnknown) as caught,
        ):
            controller.finish(self.api, self.lab)
        self.assertNotIn("PRIVATE", str(caught.exception))
        self.assertTrue(controller.required(self.api)["finishing_pending"])
        self.assertFalse(controller.required(self.api)["acknowledged"])

    def test_finalizer_disk_failure_cannot_mask_primary_ansible_error(self) -> None:
        self.fixture.reserve()
        primary = controller.OperationFailed("Ansible failed")
        with (
            patch("observability.package_cache", side_effect=OSError("PRIVATE")),
            patch.object(controller, "save", side_effect=OSError("disk full")),
        ):
            controller.finish(self.api, self.lab, primary=primary)

    def test_diagnostic_filters_arbitrary_phase_reason_timestamp_and_exit_strings(self) -> None:
        self.fixture.reserve()
        value = controller.required(self.api)
        controller.record(
            self.api,
            value,
            {
                "state": "failed",
                "phase": "PRIVATE-PASSWORD",
                "reason": "PRIVATE-TOKEN",
                "exit_code": "PRIVATE-SECRET",
                "created_utc": "2026-10-31T11:00:00",
                "started_utc": "PRIVATE-DATA",
                "finished_utc": "2026-10-31T12:00:00+00:00",
            },
        )
        text = (self.api.ROOT / "docs/ansible-operation-result.json").read_text()
        self.assertNotIn("PRIVATE", text)
        self.assertIn("2026-10-31", text)
        self.assertNotIn("created_utc", json.loads(text))
        from stage_store import atomic_json

        original = atomic_json

        def failing_public(path: Path, value: object, root: Path) -> None:
            if path.name == "ansible-operation-result.json":
                raise OSError("PRIVATE")
            return original(path, value, root)

        with patch.object(controller, "atomic_json", side_effect=failing_public):
            controller.record(self.api, controller.required(self.api), {"state": "failed", "exit_code": 2})

    def test_legacy_binding_release_source_and_invalid_policy_fail_safely(self) -> None:
        (self.api.LOCAL / "staged-deployment.json").unlink()
        (self.api.LOCAL / "context.json").write_text('{"namespace":"otus-dz07"}')
        self.assertEqual(len(controller.binding(self.api, self.lab)["deployment_id"]), 32)
        (self.api.ROOT / "config/services.json").write_text('{"artifact_delivery":"release"}')
        digest = "c" * 128
        (self.api.LOCAL / "artifact-delivery.json").write_text(json.dumps({"source": {"sha512": digest}}))
        self.assertEqual(controller.source_version(self.api), digest)
        path = self.api.ROOT / "config/ansible-operation.json"
        data = json.loads(path.read_text())
        data["connection_wait_seconds"] = False
        path.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            controller.policy(self.api)

    def test_resume_preparation_and_vm_mismatch_do_not_launch(self) -> None:
        self.assertFalse(controller.resume(self.api, self.inventory))
        self.fixture.reserve()
        self.assertFalse(controller.resume(self.api, self.inventory))
        self.lab["server_ids"]["app"] = "foreign-vm"
        with self.assertRaises(controller.OperationProtocolError):
            controller.before_destroy(self.api)
        self.assertEqual(self.fixture.launches, 0)

    def test_resume_failed_result_reports_same_failure_and_does_not_repeat(self) -> None:
        self.fixture.reserve()
        request = controller.required(self.api)["request"]
        worker.atomic(
            self.fixture.manager.folder(request["id"]) / "result.json",
            {
                "id": request["id"],
                "parameters_sha256": request["parameters_sha256"],
                "state": "failed",
                "exit_code": 2,
                "started_utc": "2026-10-31T00:00:00+00:00",
                "finished_utc": "2026-10-31T00:00:01+00:00",
            },
        )
        with patch("observability.package_cache"), self.assertRaises(controller.OperationFailed):
            controller.resume(self.api, self.inventory)
        self.assertEqual(self.fixture.launches, 0)
        self.assertTrue(controller.required(self.api)["finalized"])
        old = controller.required(self.api)["request"]["id"]
        self.fixture.unit.update(loaded=False, active=False)
        controller.reserve(self.api, self.lab, self.inventory)
        self.assertNotEqual(controller.required(self.api)["request"]["id"], old)

    def test_stop_already_deleted_app_and_unconfirmed_remote_stop(self) -> None:
        self.fixture.reserve()
        provider = Mock(configuration_target=Mock(return_value=ConfigurationTarget(self.lab, deletion_verified=True)))
        controller.before_destroy(self.api, provider)
        self.assertTrue(controller.required(self.api)["stop_confirmed"])
        controller.before_destroy(self.api, provider)
        value = controller.required(self.api)
        value["stop_confirmed"] = False
        controller.save(self.api, value)
        with (
            patch.object(controller, "rpc", return_value={"state": "running"}),
            self.assertRaises(controller.OperationUnknown),
        ):
            controller.before_destroy(self.api)

    def test_short_rpc_transport_closes_channel_and_never_exposes_stderr(self) -> None:
        # Use the actual transport helper; the fixture normally mocks it.
        import importlib.util

        specification = importlib.util.spec_from_file_location(
            "operation_transport_copy", ROOT / "scripts/ansible_operation.py"
        )
        assert specification is not None and specification.loader is not None
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        client = MagicMock()
        channel = client.get_transport.return_value.open_session.return_value
        channel.exit_status_ready.return_value = True
        channel.recv_ready.side_effect = [True, True, False]
        channel.recv_stderr_ready.return_value = False
        channel.recv.return_value = b'{"state":"running"}'
        channel.recv_exit_status.return_value = 0
        self.assertEqual(json.loads(module.command(client, "rpc", {"id": "safe"}))["state"], "running")
        channel.close.assert_called_once()
        channel.reset_mock()
        channel.recv_ready.side_effect = None
        channel.recv_ready.return_value = False
        channel.recv_exit_status.return_value = 1
        with self.assertRaises(module.OperationProtocolError):
            module.command(client, "rpc")
        channel.exit_status_ready.return_value = False
        client.get_transport.return_value.is_active.return_value = False
        with self.assertRaises(TimeoutError):
            module.command(client, "rpc")

    def test_stderr_only_rpc_closes_channel_without_exposing_private_output(self) -> None:
        client = MagicMock()
        channel = client.get_transport.return_value.open_session.return_value
        channel.exit_status_ready.return_value = True
        channel.recv_ready.return_value = False
        channel.recv_stderr_ready.side_effect = [True, True, False]
        channel.recv_stderr.return_value = b"private remote stderr"
        channel.recv_exit_status.return_value = 0
        self.assertEqual(controller.command_body(client, "rpc", None, 15), "")
        channel.close.assert_called_once()

    def test_worker_unit_inspection_checks_children_and_rejects_unknown_systemd_state(self) -> None:
        manager = worker.Manager(self.api.ROOT / "vm")
        request: worker.UnitIdentity = {"namespace": "otus-dz07", "id": "a" * 32}

        def show(text: str, code: int = 0) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess([], code, stdout=text)

        base = MagicMock()
        base.resolve.return_value = base
        child = (base.__truediv__.return_value).resolve.return_value
        child.is_relative_to.return_value = True
        process = Mock()
        process.read_text.return_value = "123\n"
        child.rglob.return_value = [process]
        with (
            patch.object(worker, "Path", return_value=base),
            patch(
                "subprocess.run",
                return_value=show("LoadState=loaded\nActiveState=inactive\nControlGroup=/own\n"),
            ),
        ):
            self.assertTrue(manager.unit(request)["active"])
            process.read_text.return_value = ""
            self.assertFalse(manager.unit(request)["active"])
            child.is_relative_to.return_value = False
            with self.assertRaises(ValueError):
                manager.unit(request)
        for response in (show("", 1), show("LoadState=error\n", 0)):
            with patch("subprocess.run", return_value=response), self.assertRaises(RuntimeError):
                manager.unit(request)
        with patch("subprocess.run", return_value=show("LoadState=not-found\nActiveState=inactive\n", 4)):
            self.assertFalse(manager.unit(request)["active"])

    def test_atomic_result_failure_preserves_previous_result_and_removes_temporary(self) -> None:
        folder = self.api.ROOT / "vm"
        path = folder / "atomic.json"
        worker.atomic(path, {"value": "old"})
        with patch("os.replace", side_effect=OSError("disk full")), self.assertRaises(OSError):
            worker.atomic(path, {"value": "new"})
        self.assertEqual(json.loads(path.read_text()), {"value": "old"})
        self.assertFalse(path.with_suffix(".next").exists())

    def test_worker_stop_signal_and_unexpected_error_are_terminal_without_secret_details(self) -> None:
        for error, code in ((InterruptedError("PRIVATE"), 143), (OSError("PRIVATE"), 1)):
            if controller.read(self.api) is not None:
                old = controller.required(self.api)["request"]
                self.fixture.manager.stop(old)
                self.fixture.manager.acknowledge(old)
                controller.path(self.api).unlink()
                self.fixture.unit.update(loaded=False, active=False)
            self.fixture.reserve()
            request = controller.required(self.api)["request"]
            for name, content in controller.payload(self.api, self.inventory).items():
                self.api.put(None, str(self.fixture.manager.folder(request["id"]) / name), content)
            self.fixture.manager.start(request)
            with patch("subprocess.Popen", side_effect=error):
                worker.execute(self.fixture.manager, request["id"])
            self.fixture.unit["active"] = False
            result = self.fixture.manager.status(request)
            self.assertEqual(result["exit_code"], code)
            self.assertNotIn("PRIVATE", json.dumps(result))

    def test_dispatcher_rpc_and_runner_validate_actions_without_printing_inputs(self) -> None:
        self.fixture.reserve()
        request = controller.required(self.api)["request"]
        for action in ("reserve", "status"):
            fake = Mock()
            fake.status.return_value = {"state": "preparing"}
            fake.reserve.return_value = {"state": "preparing"}
            body = io.StringIO(json.dumps({"action": action, "request": request}))
            output = io.StringIO()
            with (
                patch.object(worker, "Manager", return_value=fake),
                patch("sys.stdin", body),
                patch("sys.stdout", output),
                patch("sys.argv", ["ansible_job.py", "--root", str(self.api.ROOT / "vm"), "rpc"]),
            ):
                worker.main()
            self.assertEqual(json.loads(output.getvalue())["state"], "preparing")
            fake.validate.assert_called_once_with(request)
        with (
            patch.object(worker, "Manager", return_value=Mock()),
            patch.object(worker, "execute") as execute,
            patch("sys.argv", ["ansible_job.py", "--root", str(self.api.ROOT / "vm"), "run", request["id"]]),
        ):
            worker.main()
        execute.assert_called_once()
        with (
            patch.object(worker, "Manager", return_value=Mock()),
            patch("sys.stdin", io.StringIO(json.dumps({"action": "delete-everything", "request": request}))),
            patch("sys.argv", ["ansible_job.py", "--root", str(self.api.ROOT / "vm"), "rpc"]),
            self.assertRaises(ValueError),
        ):
            worker.main()

    def test_command_line_failure_suppresses_arbitrary_exception_details(self) -> None:
        from test_cursor_coverage import entrypoint_code

        namespace = {**vars(worker), "__name__": "__main__", "main": Mock(side_effect=RuntimeError("PRIVATE-TOKEN"))}
        with self.assertRaises(SystemExit) as caught:
            exec(entrypoint_code(worker), namespace)
        self.assertNotIn("PRIVATE", str(caught.exception))
        self.assertIn("RuntimeError", str(caught.exception))

    def test_worker_validates_identity_payload_ownership_and_paths(self) -> None:
        self.fixture.reserve()
        request = controller.required(self.api)["request"]
        manager = self.fixture.manager
        for key, value in (
            ("deployment_id", "bad"),
            ("namespace", "bad;command"),
            ("app_id", ""),
            ("parameters_sha256", "bad"),
            ("timeout", False),
            ("stop_wait", 0),
            ("payload", {}),
        ):
            changed = {**request, key: value}
            with self.subTest(key=key), self.assertRaises(ValueError):
                manager.validate(changed)
        with self.assertRaises(ValueError):
            manager.folder("../outside")
        with patch.object(Path, "is_symlink", return_value=True):
            with self.assertRaises(ValueError):
                manager.folder(request["id"])
            with self.assertRaises(ValueError):
                worker.Manager(self.api.ROOT / "vm")
        with self.assertRaises(ValueError):
            manager.saved({**request, "timeout": 1})
        with self.assertRaises(ValueError):
            manager.reserve({**request, "app_id": "foreign-app", "id": "b" * 32})

    def test_acknowledgement_requires_no_active_children_and_the_same_owner(self) -> None:
        self.fixture.reserve()
        request = controller.required(self.api)["request"]
        manager = self.fixture.manager
        with self.assertRaises(ValueError):
            manager.acknowledge(request)
        manager.stop(request)
        worker.atomic(manager.home / "active.json", {"id": "b" * 32})
        with self.assertRaises(ValueError):
            manager.acknowledge(request)
        worker.atomic(manager.home / "active.json", {"id": request["id"]})
        self.fixture.unit["active"] = True
        with self.assertRaises(ValueError):
            manager.acknowledge(request)
        self.fixture.unit["active"] = False
        manager.acknowledge(request)
        manager.acknowledge(request)

    def test_payload_corruption_is_rejected_before_systemd_start(self) -> None:
        self.fixture.reserve()
        request = controller.required(self.api)["request"]
        manager = self.fixture.manager
        for name, content in controller.payload(self.api, self.inventory).items():
            self.api.put(None, str(manager.folder(request["id"]) / name), content)
        (manager.folder(request["id"]) / "key").write_text("corrupt")
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            manager.start(request)
        self.assertEqual(self.fixture.launches, 0)
        (manager.folder(request["id"]) / "key").write_text("PRIVATE-KEY-SENTINEL")
        altered = copy.deepcopy(request)
        altered["payload"]["foreign"] = "a" * 64
        worker.atomic(manager.folder(request["id"]) / "request.json", {"request": altered, "boot_id": "same-boot"})
        with self.assertRaisesRegex(ValueError, "Unexpected"):
            manager.start(altered)

    def test_runner_cannot_repeat_an_already_started_or_stopped_operation(self) -> None:
        self.fixture.reserve()
        request = controller.required(self.api)["request"]
        manager = self.fixture.manager
        manager.stop(request)
        with patch("subprocess.Popen") as child:
            worker.execute(manager, request["id"])
        child.assert_not_called()
        result = json.loads((manager.folder(request["id"]) / "result.json").read_text())
        result.update(state="running", started_utc="2026-10-31T00:00:00+00:00")
        worker.atomic(manager.folder(request["id"]) / "result.json", result)
        with patch("subprocess.Popen") as child:
            worker.execute(manager, request["id"])
        child.assert_not_called()

    def test_real_lock_protocol_acquires_exclusive_nonblocking_lock_and_releases_it(self) -> None:
        import importlib.util

        specification = importlib.util.spec_from_file_location("job_lock_copy", ROOT / "scripts/remote/ansible_job.py")
        assert specification is not None and specification.loader is not None
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        flock = Mock(LOCK_EX=1, LOCK_NB=4, LOCK_UN=8)
        with (
            patch("sys.platform", "linux"),
            patch.dict("sys.modules", {"fcntl": flock}),
            module.locked(self.api.ROOT / "test-lock", nonblocking=True),
        ):
            pass
        self.assertEqual([c.args[1] for c in flock.flock.call_args_list], [5, 8])

    def test_direct_backend_configuration_without_prior_reservation_is_durable(self) -> None:
        with patch("observability.package_cache"):
            controller.configure(self.api, None, self.inventory)
        self.assertTrue(controller.required(self.api)["acknowledged"])
        self.assertEqual(self.fixture.launches, 1)

    def test_lost_payload_upload_preserves_context_without_starting_ansible(self) -> None:
        self.fixture.reserve()
        self.api.put.side_effect = OSError("offline")
        with self.assertRaises(controller.OperationUnknown):
            controller.wait(self.api, self.lab, self.inventory, upload=True)
        self.assertEqual(self.fixture.launches, 0)
        self.assertEqual(controller.required(self.api)["state"], "unknown")

    def test_finished_job_and_local_disk_failure_keep_primary_error(self) -> None:
        self.fixture.reserve()
        controller.wait(self.api, self.lab, self.inventory, upload=True)
        with patch("observability.package_cache"), patch.object(controller, "save", side_effect=OSError("disk full")):
            controller.finish(self.api, self.lab, primary=controller.OperationFailed("original"))
            with self.assertRaises(OSError):
                controller.finish(self.api, self.lab)

    def test_corrupt_state_and_host_boot_identity_cannot_fake_completion(self) -> None:
        self.fixture.reserve()
        request = controller.required(self.api)["request"]
        worker.atomic(
            self.fixture.manager.folder(request["id"]) / "result.json",
            {"id": request["id"], "parameters_sha256": request["parameters_sha256"], "state": "invented"},
        )
        self.assertEqual(self.fixture.manager.status(request)["state"], "unknown")
        import importlib.util

        specification = importlib.util.spec_from_file_location("job_boot_copy", ROOT / "scripts/remote/ansible_job.py")
        assert specification is not None and specification.loader is not None
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        manager = module.Manager(self.api.ROOT / "vm")
        with patch.object(module, "Path") as path:
            path.return_value.read_text.return_value = "synthetic-boot\n"
            self.assertEqual(manager.boot(), "synthetic-boot")

    def test_deadline_before_subprocess_start_is_an_operation_timeout(self) -> None:
        self.fixture.reserve()
        request = controller.required(self.api)["request"]
        manager = self.fixture.manager
        for name, content in controller.payload(self.api, self.inventory).items():
            self.api.put(None, str(manager.folder(request["id"]) / name), content)
        manager.start(request)
        with (
            patch("time.monotonic", side_effect=[0, 1801]),
            patch("subprocess.Popen") as child,
        ):
            worker.execute(manager, request["id"])
        child.assert_not_called()
        self.fixture.unit["active"] = False
        self.assertEqual(manager.status(request)["reason"], "operation_timeout")

    def test_child_exit_race_during_timeout_does_not_erase_result(self) -> None:
        self.fixture.reserve()
        request = controller.required(self.api)["request"]
        manager = self.fixture.manager
        for name, content in controller.payload(self.api, self.inventory).items():
            self.api.put(None, str(manager.folder(request["id"]) / name), content)
        manager.start(request)
        child = Mock(pid=123)
        child.poll.return_value = None
        child.wait.side_effect = subprocess.TimeoutExpired("ansible", 1)
        with (
            patch("subprocess.Popen", return_value=child),
            patch("sys.platform", "linux"),
            patch("os.killpg", side_effect=ProcessLookupError(), create=True),
        ):
            worker.execute(manager, request["id"])
        self.fixture.unit["active"] = False
        self.assertEqual(manager.status(request)["exit_code"], 124)

    def test_sigterm_handler_records_stop_with_no_secret_details(self) -> None:
        self.fixture.reserve()
        request = controller.required(self.api)["request"]
        manager = self.fixture.manager
        for name, content in controller.payload(self.api, self.inventory).items():
            self.api.put(None, str(manager.folder(request["id"]) / name), content)
        manager.start(request)
        handlers: list[Callable[[int, FrameType | None], None]] = []

        def signal_handler(signum: int, callback: Callable[[int, FrameType | None], None]) -> Mock:
            handlers.append(callback)
            return Mock()

        child = Mock(pid=123)
        child.poll.return_value = 0
        child.wait.side_effect = lambda **kw: handlers[0](signal.SIGTERM, None)
        with (
            patch("signal.signal", side_effect=signal_handler),
            patch("subprocess.Popen", return_value=child),
        ):
            worker.execute(manager, request["id"])
        self.fixture.unit["active"] = False
        self.assertEqual(manager.status(request)["state"], "stopped")

    def test_private_sftp_has_a_short_timeout_and_owner_only_mode(self) -> None:
        import importlib.util

        specification = importlib.util.spec_from_file_location(
            "operation_sftp_copy", ROOT / "scripts/ansible_operation.py"
        )
        assert specification is not None and specification.loader is not None
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        client = MagicMock()
        sftp = client.open_sftp.return_value.__enter__.return_value
        module.put_private(client, "/private/inventory.json", "PRIVATE-PAYLOAD")
        sftp.get_channel.return_value.settimeout.assert_called_once_with(15)
        sftp.chmod.assert_called_once_with("/private/inventory.json", 0o600)
        sftp.open.return_value.__enter__.return_value.write.assert_called_once_with(b"PRIVATE-PAYLOAD")

    def test_final_actions_use_short_transport_and_a_separate_connection_budget(self) -> None:
        self.fixture.reserve()
        controller.wait(self.api, self.lab, self.inventory, upload=True)

        def cache(
            api: OperationPort,
            app: SSHClient,
            lab: StandDescription,
            *,
            start: bool,
            remote: Callable[[SSHClient, str, str], object],
        ) -> None:
            self.assertFalse(start)
            self.assertEqual(remote(app, "stop safe unit", "stop-cache"), "")

        with patch("observability.package_cache", side_effect=cache):
            controller.finish(self.api, self.lab)
        self.assertTrue(controller.required(self.api)["acknowledged"])
        self.api.connect.side_effect = OSError("PRIVATE-WIFI")
        with self.assertRaises(controller.OperationUnknown) as caught:
            controller.finish(self.api, self.lab)
        self.assertNotIn("PRIVATE", str(caught.exception))

    def test_untrusted_host_in_final_actions_does_not_become_ansible_failure(self) -> None:
        import paramiko

        self.fixture.reserve()
        self.api.connect.side_effect = paramiko.AuthenticationException("PRIVATE")
        with self.assertRaises(controller.OperationUnknown):
            controller.finish(self.api, self.lab)
        self.assertTrue(controller.required(self.api)["finishing_pending"])

    def test_missing_local_context_blocks_observation_and_final_actions(self) -> None:
        with self.assertRaises(controller.OperationProtocolError):
            controller.wait(self.api, self.lab, self.inventory)
        with self.assertRaises(controller.OperationProtocolError):
            controller.finish(self.api, self.lab)
        self.assertEqual(self.fixture.launches, 0)

    def test_destroy_keeps_original_completed_operation_result(self) -> None:
        self.fixture.reserve()
        controller.wait(self.api, self.lab, self.inventory, upload=True)
        request = controller.required(self.api)["request"]
        file = self.fixture.manager.folder(request["id"]) / "result.json"
        original = json.loads(file.read_text())
        controller.before_destroy(self.api)
        self.assertEqual(json.loads(file.read_text()), original)
        self.assertTrue(controller.required(self.api)["stop_confirmed"])
        result = controller.required(self.api)["result"]
        assert isinstance(result, dict)
        self.assertEqual(result["exit_code"], 0)

    def test_exec_acknowledgement_and_sftp_open_cannot_wait_forever(self) -> None:
        import importlib.util

        specification = importlib.util.spec_from_file_location(
            "operation_deadline_copy", ROOT / "scripts/ansible_operation.py"
        )
        assert specification is not None and specification.loader is not None
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        for phase in ("exec", "sftp"):
            gate = threading.Event()
            client = MagicMock()
            client.close.side_effect = gate.set
            if phase == "exec":
                client.get_transport.return_value.open_session.return_value.exec_command.side_effect = (
                    lambda text, *, gate=gate: gate.wait()
                )
            else:
                client.open_sftp.side_effect = gate.wait
            original = module.bounded_ssh
            try:
                with (
                    self.subTest(phase=phase),
                    patch.object(
                        module,
                        "bounded_ssh",
                        side_effect=lambda client, action, timeout, *, original=original: original(
                            client, action, 0.02
                        ),
                    ),
                    self.assertRaises(TimeoutError),
                ):
                    if phase == "exec":
                        module.command(client, "rpc")
                    else:
                        module.put_private(client, "/private/file", "PRIVATE")
                client.close.assert_called_once()
            finally:
                gate.set()

    def test_transport_deadline_preserves_error_even_when_close_fails(self) -> None:
        gate = threading.Event()
        client = Mock()
        client.close.side_effect = OSError("PRIVATE")
        try:
            with self.assertRaises(TimeoutError) as caught:
                controller.bounded_ssh(client, gate.wait, 0.02)
            self.assertNotIn("PRIVATE", str(caught.exception))
        finally:
            gate.set()

    def test_privileged_child_signal_error_does_not_mask_operation_timeout(self) -> None:
        self.fixture.reserve()
        request = controller.required(self.api)["request"]
        manager = self.fixture.manager
        for name, content in controller.payload(self.api, self.inventory).items():
            self.api.put(None, str(manager.folder(request["id"]) / name), content)
        manager.start(request)
        child = Mock(pid=123)
        child.poll.return_value = None
        child.wait.side_effect = subprocess.TimeoutExpired("ansible", 1)
        with (
            patch("subprocess.Popen", return_value=child),
            patch("sys.platform", "linux"),
            patch("os.killpg", side_effect=PermissionError("privileged child"), create=True),
        ):
            worker.execute(manager, request["id"])
        # The systemd control group must still report running until privileged
        # descendants are killed by the service manager.
        self.assertEqual(manager.status(request)["state"], "running")
        self.fixture.unit["active"] = False
        self.assertEqual(manager.status(request)["exit_code"], 124)
