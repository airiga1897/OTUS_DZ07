import hashlib
import io
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from remote import fetch_artifacts as fetch

ROOT = Path(__file__).resolve().parents[1]


class DownloadTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(dir=ROOT / ".tools")
        self.addCleanup(directory.cleanup)
        self.target = Path(directory.name) / "package.deb"
        self.target.write_bytes(b"previous")
        self.body = b"verified package"
        self.item = {
            "url": "https://private.invalid/package?signature=synthetic",
            "size": len(self.body),
            "sha512": hashlib.sha512(self.body).hexdigest(),
        }

    def response(self, content):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.geturl.return_value = self.item["url"]
        response.read1.side_effect = io.BytesIO(content).read1
        response.read.side_effect = AssertionError("Must not wait to fill a large buffer")
        return response

    def test_streaming_reads_verify_before_replacement(self) -> None:
        opener = Mock()
        response = self.response(self.body)
        opener.open.return_value = response
        progress = Mock()
        fetch.download(self.item, self.target, opener, progress=progress)
        self.assertEqual(self.target.read_bytes(), self.body)
        response.read.assert_not_called()
        progress.assert_called_with(len(self.body), len(self.body))

    def test_network_failure_retries_from_zero(self) -> None:
        first = self.response(self.body)
        first.read1.side_effect = [b"ver", TimeoutError("Interrupted")]
        opener = Mock()
        opener.open.side_effect = [first, self.response(self.body)]
        fetch.download(self.item, self.target, opener)
        self.assertEqual(self.target.read_bytes(), self.body)
        self.assertEqual(opener.open.call_count, 2)

    def test_trickled_bytes_cannot_reset_the_total_deadline(self) -> None:
        response = self.response(self.body)
        opener = Mock()
        opener.open.return_value = response
        with (
            patch("time.monotonic", side_effect=[0, 0, 1, 6, 6]),
            self.assertRaisesRegex(TimeoutError, "deadline"),
        ):
            fetch.download(self.item, self.target, opener, deadline=5)
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(self.target.read_bytes(), b"previous")
        self.assertFalse(self.target.with_suffix(".partial").exists())

    def test_retries_are_bounded_and_cleanup_preserves_existing_file(self) -> None:
        opener = Mock()
        opener.open.side_effect = urllib.error.URLError("Connection failed")
        with self.assertRaises(urllib.error.URLError):
            fetch.download(self.item, self.target, opener)
        self.assertEqual(opener.open.call_count, 3)
        self.assertEqual(self.target.read_bytes(), b"previous")
        self.assertFalse(self.target.with_suffix(".partial").exists())

    def test_expired_access_is_not_retried(self) -> None:
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError(self.item["url"], 403, "Forbidden", {}, None)
        with self.assertRaises(urllib.error.HTTPError):
            fetch.download(self.item, self.target, opener)
        self.assertEqual(opener.open.call_count, 1)

    def test_checksum_failure_is_not_retried_or_published(self) -> None:
        opener = Mock()
        opener.open.return_value = self.response(b"X" * len(self.body))
        with self.assertRaisesRegex(ValueError, "checksum"):
            fetch.download(self.item, self.target, opener)
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(self.target.read_bytes(), b"previous")
        self.assertFalse(self.target.with_suffix(".partial").exists())
