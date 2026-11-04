"""Public dependency cache survives runtime cleanup and verifies content offline."""

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import cache_packages

ROOT = Path(__file__).resolve().parents[1]


class PackageCacheTest(unittest.TestCase):
    def test_explicit_import_and_reuse_require_verified_sha512_without_network(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tools") as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "config/services.json").write_text('{"elastic_version": "8.19.0"}')
            source = root / "source"
            source.mkdir()
            for product in ("elasticsearch", "filebeat", "kibana"):
                name = product + "-8.19.0-amd64.deb"
                data = ("synthetic-" + product).encode()
                (source / name).write_bytes(data)
                (source / (name + ".sha512")).write_text(hashlib.sha512(data).hexdigest())
            cache = root / ".tools/elastic-packages"
            with (
                patch.multiple(cache_packages, ROOT=root, CACHE=cache),
                patch("prepare_ansible.prepare"),
                patch("urllib.request.urlopen") as network,
            ):
                cache_packages.main(source)
                cache_packages.main()
                network.assert_not_called()
                self.assertEqual(len(json.loads((cache / "manifest.json").read_text())), 3)

    def test_bad_import_checksum_is_rejected_before_copy(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tools") as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "config/services.json").write_text('{"elastic_version": "8.19.0"}')
            source = root / "source"
            source.mkdir()
            (source / "filebeat-8.19.0-amd64.deb").write_bytes(b"corrupt")
            (source / "filebeat-8.19.0-amd64.deb.sha512").write_text("0" * 128)
            cache = root / ".tools/elastic-packages"
            with patch.multiple(cache_packages, ROOT=root, CACHE=cache), patch("prepare_ansible.prepare"):
                with self.assertRaisesRegex(ValueError, "Source SHA512 mismatch"):
                    cache_packages.main(source)
                self.assertFalse((cache / "filebeat-8.19.0-amd64.deb").exists())
