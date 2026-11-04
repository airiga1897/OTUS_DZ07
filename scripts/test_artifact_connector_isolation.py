"""Release works without loading the optional S3 connector or its SDK."""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import delivery
import release_artifacts
import test_release_artifacts as fixtures

CONSUMER = r"""
import builtins, io, json, socket, sys
from pathlib import Path
from unittest.mock import Mock, patch
sys.path.insert(0, sys.argv[1])
original_import = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.split('.')[0] in ('s3_artifacts', 'boto3', 'botocore'):
        raise AssertionError('Release unexpectedly imported the S3 connector or SDK')
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded
def forbidden(*args, **kwargs):
    raise AssertionError('External networking is forbidden')
import release_artifacts, artifact_transport, artifact_lifecycle, ansible_operation, delivery
root, assets = map(Path, sys.argv[2:])
class Response(io.BytesIO):
    status = 200
    def __init__(self, body, url): super().__init__(body); self.url=url
    def geturl(self): return self.url
    def read1(self, n): return self.read(n)
opener = Mock(open=lambda url, **kw: Response((assets/'manifest.json').read_bytes(),url))
download = release_artifacts.download
with patch.object(socket.socket,'connect',forbidden), patch.object(socket,'create_connection',forbidden), \
        patch.object(release_artifacts,'download',side_effect=lambda item,path: download(item,path,opener=opener)):
    manifest = delivery.prepare(root)
    api=Mock(ROOT=root, LOCAL=root/'.local')
    assert ansible_operation.source_version(api)==manifest['source']['sha512']
    artifact_lifecycle.release(root)  # No S3 lease exists for this connector.
assert not any(name in sys.modules for name in ('s3_artifacts','boto3','botocore'))
print('RELEASE_WITHOUT_S3_IMPORTS_OK')
"""


class ArtifactConnectorIsolation(unittest.TestCase):
    def setUp(self) -> None:
        fixture = fixtures.ReleaseArtifactsTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture, self.root = fixture, fixture.root
        self.assets = fixture.build()
        fixture.configure(self.assets)

    def test_release_prepare_fingerprint_and_empty_cleanup_do_not_import_s3(self) -> None:
        environment = {k: v for k, v in os.environ.items() if not k.startswith(("OS_", "AWS_", "S3_", "OTUS_"))}
        scripts = Path(release_artifacts.__file__).parent
        result = subprocess.run(
            [sys.executable, "-c", CONSUMER, str(scripts), str(self.root), str(self.assets)],
            env=environment,
            capture_output=True,
            text=True,
            timeout=40,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("RELEASE_WITHOUT_S3_IMPORTS_OK", result.stdout)

    def test_release_failure_never_tries_the_s3_connector(self) -> None:
        failure = OSError("synthetic release unavailable")
        with (
            patch("release_artifacts.prepare", side_effect=failure),
            patch("s3_artifacts.prepare") as s3,
            self.assertRaises(OSError) as caught,
        ):
            delivery.prepare(self.root)
        self.assertIs(caught.exception, failure)
        s3.assert_not_called()

    def test_s3_is_only_used_when_explicitly_selected(self) -> None:
        path = self.root / "config/services.json"
        value = json.loads(path.read_text())
        value["artifact_delivery"] = "s3"
        path.write_text(json.dumps(value))
        with (
            patch("release_artifacts.prepare") as release,
            patch("s3_artifacts.prepare", return_value={"source": "selected"}) as s3,
        ):
            self.assertEqual(delivery.prepare(self.root), {"source": "selected"})
        s3.assert_called_once_with(self.root)
        release.assert_not_called()
