"""Retry refactoring preserves budgets, backoff, original errors and cleanup."""

import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, call, patch

import cache_packages
import elastic_options
import selectel_api
from remote import fetch_artifacts

ROOT = Path(__file__).resolve().parents[1]


class Response(io.BytesIO):
    status = 200

    def geturl(self) -> str:
        return "https://example.invalid/package"


class RetryLimitEquivalence(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory(dir=ROOT / ".tools")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.body = b"new"
        self.item = {
            "url": "https://example.invalid/package",
            "size": len(self.body),
            "sha512": hashlib.sha512(self.body).hexdigest(),
        }

    def test_zero_or_negative_download_budget_keeps_destination_and_cleans_partial(self) -> None:
        for attempts in (0, -2):
            target = self.root / "package.deb"
            target.write_bytes(b"old")
            target.with_suffix(".partial").write_bytes(b"stale")
            opener = Mock()
            fetch_artifacts.download(self.item, target, opener, attempts=attempts)
            opener.open.assert_not_called()
            self.assertEqual(target.read_bytes(), b"old")
            self.assertFalse(target.with_suffix(".partial").exists())

    def test_invalid_download_budget_type_still_cleans_partial(self) -> None:
        target = self.root / "package.deb"
        target.with_suffix(".partial").write_bytes(b"stale")
        opener = Mock()
        with self.assertRaises(TypeError):
            fetch_artifacts.download(self.item, target, opener, attempts=1.5)
        opener.open.assert_not_called()
        self.assertFalse(target.with_suffix(".partial").exists())

    def test_download_succeeds_on_last_allowed_attempt_with_clean_restarts(self) -> None:
        opener = Mock()
        opener.open.side_effect = [OSError("first"), OSError("second"), Response(self.body)]
        progress = Mock()
        target = self.root / "package.deb"
        fetch_artifacts.download(self.item, target, opener, attempts=3, progress=progress)
        self.assertEqual(opener.open.call_count, 3)
        self.assertEqual(target.read_bytes(), self.body)
        self.assertEqual(progress.call_args_list, [call(0, 3), call(0, 3), call(3, 3), call(3, 3)])

    def test_download_raises_original_last_error_at_exact_budget(self) -> None:
        for attempts in (1, 3):
            errors = [OSError("synthetic failure") for _ in range(attempts)]
            opener = Mock()
            opener.open.side_effect = errors
            target = self.root / "package.deb"
            with self.assertRaises(OSError) as caught:
                fetch_artifacts.download(self.item, target, opener, attempts=attempts)
            self.assertIs(caught.exception, errors[-1])
            self.assertEqual(opener.open.call_count, attempts)
            self.assertFalse(target.exists())
            self.assertFalse(target.with_suffix(".partial").exists())

    def test_zero_restore_budget_does_not_touch_vm_or_health_api(self) -> None:
        backend = Mock()
        for attempts in (0, -1):
            with patch("observability.command") as command:
                self.assertIsNone(elastic_options.restore(None, backend, {}, {"restore_attempts": attempts}))
            command.assert_not_called()
        backend.region.assert_not_called()

    def test_restore_rejects_non_integer_budget_and_missing_client(self) -> None:
        backend = Mock()
        with patch("observability.command") as command:
            with self.assertRaises(TypeError):
                elastic_options.restore(None, backend, {}, {"restore_attempts": "1"})
            with self.assertRaises(TypeError):
                elastic_options.restore(None, backend, {}, {"restore_attempts": 1})
        command.assert_not_called()
        backend.region.assert_not_called()

    def test_restore_retries_same_vm_and_keeps_backoff_and_membership_check(self) -> None:
        cloud = Mock()
        cloud.set_power.side_effect = [OSError("first"), OSError("second"), None]
        backend = Mock()
        backend.region.return_value = cloud
        api = SimpleNamespace(
            PREFIX="otus-dz07", lab_output=lambda: {"private_ips": {"es1": "10.77.0.31"}, "node_roles": {"es1": "es"}}
        )
        request = {"region": "ru-2", "node": "es1", "server_id": "saved-vm"}
        with (
            patch("observability.command", return_value={"status": "green", "number_of_nodes": 1}) as health_command,
            patch("time.sleep") as delay,
        ):
            result = elastic_options.restore(api, backend, request, {"restore_attempts": 3, "restore_delay_seconds": 7})
        self.assertIsInstance(result, dict)
        assert isinstance(result, dict)
        membership = result["health"]
        self.assertIsInstance(membership, dict)
        assert isinstance(membership, dict)
        self.assertEqual(membership["status"], "green")
        self.assertEqual(
            cloud.set_power.call_args_list, [call("es1", "saved-vm", running=True, prefix="otus-dz07")] * 3
        )
        self.assertEqual(delay.call_args_list, [call(7), call(7)])
        health_command.assert_called_once_with(api, "health")

    def test_api_retry_metadata_and_delay_preserve_exact_budget(self) -> None:
        for attempts in (1, 3):
            errors = [OSError("synthetic temporary error") for _ in range(attempts)]
            opener = Mock()
            opener.open.side_effect = errors
            with (
                patch.object(
                    selectel_api,
                    "transport_policy",
                    return_value={"read_attempts": attempts, "timeout_seconds": 15, "retry_delay_seconds": 2},
                ),
                patch("urllib.request.build_opener", return_value=opener),
                patch("time.sleep") as delay,
                self.assertRaises(OSError) as caught,
            ):
                selectel_api.request_json("https://example.invalid/resource")
            self.assertIs(caught.exception, errors[-1])
            context = caught.exception.__dict__["api_error_context"]
            self.assertIsInstance(context, dict)
            assert isinstance(context, dict)
            self.assertEqual(context["attempts"], attempts)
            self.assertEqual(opener.open.call_count, attempts)
            self.assertEqual(delay.call_args_list, [call(2)] * (attempts - 1))

    def test_official_range_download_has_exactly_three_attempts(self) -> None:
        head = MagicMock()
        head.__enter__.return_value.headers = {"Content-Length": "4"}
        errors = [OSError("first"), OSError("second"), OSError("third")]
        with (
            patch("urllib.request.urlopen", side_effect=[head, *errors]) as get,
            self.assertRaises(OSError) as caught,
        ):
            cache_packages.download("https://example.invalid/package", self.root / "official.deb")
        self.assertIs(caught.exception, errors[-1])
        self.assertEqual(get.call_count, 4)  # HEAD and three range attempts.
