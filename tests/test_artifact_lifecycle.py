import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import artifact_lifecycle as lifecycle
import s3_artifacts
from test_runtime_support import lab

ROOT = Path(__file__).resolve().parents[1]


class Storage:
    def __init__(self) -> None:
        self.objects = ["otus-dz07/sources/source", "otus-dz07/packages/package", "other-project/keep"]
        self.versions = []
        self.uploads = [{"Key": "otus-dz07/packages/incomplete", "UploadId": "own-upload"}]
        self.status = None
        self.deletes = []
        self.fail = False

    def get_bucket_versioning(self, **kwargs):
        return {"Status": self.status} if self.status else {}

    def get_paginator(self, action):
        def pages(**kwargs):
            prefix = kwargs["Prefix"]
            if action == "list_objects_v2":
                return [{"Contents": [{"Key": key} for key in self.objects if key.startswith(prefix)]}]
            if action == "list_object_versions":
                return [{"Versions": [dict(v) for v in self.versions if v["Key"].startswith(prefix)]}]
            if action == "list_multipart_uploads":
                return [{"Uploads": [dict(v) for v in self.uploads if v["Key"].startswith(prefix)]}]
            raise AssertionError(action)

        return Mock(paginate=pages)

    def delete_objects(self, **kwargs):
        if self.fail:
            return {"Errors": [{"Code": "AccessDenied"}]}
        values = kwargs["Delete"]["Objects"]
        self.deletes.extend(copy.deepcopy(values))
        for value in values:
            if value["Key"] in self.objects:
                self.objects.remove(value["Key"])
            self.versions = [v for v in self.versions if v != value]
        return {"Deleted": values}

    def abort_multipart_upload(self, **kwargs) -> None:
        self.uploads = [u for u in self.uploads if u["UploadId"] != kwargs["UploadId"]]


class ArtifactLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / ".tools")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "config").mkdir()
        (self.root / ".local").mkdir()
        self.config = {
            "endpoint": "https://s3.ru-7.storage.selcloud.ru",
            "region": "ru-7",
            "bucket": "otus-dz07-synthetic",
            "prefix": "otus-dz07",
            "expires_seconds": 900,
        }
        (self.root / "config/artifacts.json").write_text(json.dumps({"s3": self.config}))
        (self.root / "config/selectel-auth.json").write_text('{"fields":{"project_id":{"env":"TEST_PROJECT_ID"}}}')
        environment = patch.dict(os.environ, TEST_PROJECT_ID="own-project")
        environment.start()
        self.addCleanup(environment.stop)
        lifecycle.bind(self.root, self.config)
        guard = patch.object(s3_artifacts, "require_private")
        guard.start()
        self.addCleanup(guard.stop)
        self.storage = Storage()

    def test_purge_after_prior_destroy_keeps_foreign_objects_and_bucket(self) -> None:
        lifecycle.purge(self.root, self.storage)
        self.assertEqual(self.storage.objects, ["other-project/keep"])
        self.assertEqual(self.storage.uploads, [])
        report = json.loads((self.root / "docs/s3-cleanup.json").read_text())
        self.assertTrue(report["success"])
        self.assertFalse(report["bucket_deleted"])
        self.assertTrue((self.root / ".local/artifact-storage.json").exists())

    def test_active_lease_prevents_any_object_deletion(self) -> None:
        self.storage.objects.append("otus-dz07/runs/other/lease.json")
        with self.assertRaisesRegex(RuntimeError, "lease"):
            lifecycle.purge(self.root, self.storage)
        self.assertEqual(self.storage.deletes, [])
        self.assertTrue((self.root / ".local/artifact-purge-pending.json").exists())

    def test_versions_and_multipart_uploads_are_audited(self) -> None:
        self.storage.status = "Enabled"
        self.storage.versions = [{"Key": key, "VersionId": "old"} for key in self.storage.objects[:-1]]
        lifecycle.purge(self.root, self.storage)
        self.assertEqual(self.storage.versions, [])
        self.assertEqual(self.storage.uploads, [])

    def test_delete_error_retains_binding_and_reports_failure(self) -> None:
        self.storage.fail = True
        with self.assertRaisesRegex(RuntimeError, "deletion errors"):
            lifecycle.purge(self.root, self.storage)
        self.assertTrue((self.root / ".local/artifact-storage.json").exists())
        self.assertTrue((self.root / ".local/artifact-purge-pending.json").exists())
        self.assertFalse(json.loads((self.root / "docs/s3-cleanup.json").read_text())["success"])

    def test_changed_project_or_missing_binding_cannot_purge(self) -> None:
        with (
            patch.dict(os.environ, TEST_PROJECT_ID="another-project"),
            self.assertRaisesRegex(ValueError, "binding changed"),
        ):
            lifecycle.purge(self.root, self.storage)
        (self.root / ".local/artifact-storage.json").unlink()
        with self.assertRaisesRegex(ValueError, "No confirmed"):
            lifecycle.purge(self.root, self.storage)
        self.assertEqual(self.storage.deletes, [])

    def test_nonempty_compute_state_blocks_purge(self) -> None:
        (self.root / "terraform-selectel").mkdir()
        (self.root / "terraform-selectel/terraform.tfstate").write_text(
            '{"resources":[{"mode":"managed","instances":[{}]}]}'
        )
        with self.assertRaisesRegex(ValueError, "Non-empty state"):
            lifecycle.purge(self.root, self.storage)
        self.assertEqual(self.storage.deletes, [])

    def test_compute_cleanup_failure_prevents_s3_purge(self) -> None:
        with (
            patch.object(lifecycle, "purge") as purge,
            patch.object(lab, "ROOT", self.root),
            patch.object(lab, "cleanup", side_effect=RuntimeError("Compute cleanup failed")),
        ):
            with self.assertRaisesRegex(RuntimeError, "Compute cleanup"):
                lab.destroy(purge_artifacts=True)
            purge.assert_not_called()

    def test_failed_probe_cleanup_keeps_recovery_for_retry(self) -> None:
        recovery = self.root / ".local/artifact-probe.json"
        key = "otus-dz07/probes/" + "a" * 32
        recovery.write_text(json.dumps({"key": key}))
        storage = Mock()
        storage.delete_object.side_effect = RuntimeError("Deletion failed")
        with (
            patch.object(s3_artifacts, "client", return_value=storage),
            self.assertRaisesRegex(RuntimeError, "Deletion failed"),
        ):
            lifecycle.release(self.root)
        self.assertTrue(recovery.exists())
        storage.delete_object.side_effect = None
        absent = Exception("Missing")
        absent.response = {"Error": {"Code": "NoSuchKey"}}
        storage.head_object.side_effect = absent
        with patch.object(s3_artifacts, "client", return_value=storage):
            lifecycle.release(self.root)
        self.assertFalse(recovery.exists())
        storage.delete_object.assert_called_with(Bucket=self.config["bucket"], Key=key)

    def test_probe_recovery_cannot_delete_a_package(self) -> None:
        recovery = self.root / ".local/artifact-probe.json"
        recovery.write_text(json.dumps({"key": "otus-dz07/packages/" + "a" * 32}))
        storage = Mock()
        with (
            patch.object(s3_artifacts, "client", return_value=storage),
            self.assertRaisesRegex(ValueError, "probe recovery"),
        ):
            lifecycle.release(self.root)
        storage.delete_object.assert_not_called()
        self.assertTrue(recovery.exists())
