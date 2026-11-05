import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import Mock, patch

from mypy_boto3_s3.client import S3Client

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import artifact_lifecycle as lifecycle
import s3_artifacts
from artifact_types import S3Settings
from test_runtime_support import lab

ROOT = Path(__file__).resolve().parents[1]


class Storage:
    def __init__(self) -> None:
        self.objects = ["otus-dz07/sources/source", "otus-dz07/packages/package", "other-project/keep"]
        self.versions: list[dict[str, str]] = []
        self.uploads: list[dict[str, str]] = [{"Key": "otus-dz07/packages/incomplete", "UploadId": "own-upload"}]
        self.status: str | None = None
        self.deletes: list[dict[str, str]] = []
        self.fail = False

    def get_bucket_versioning(self, **kwargs: object) -> dict[str, str]:
        return {"Status": self.status} if self.status else {}

    def get_paginator(self, action: str) -> Mock:
        def pages(**kwargs: object) -> list[dict[str, object]]:
            prefix = kwargs["Prefix"]
            if not isinstance(prefix, str):
                raise AssertionError(prefix)
            if action == "list_objects_v2":
                return [{"Contents": [{"Key": key} for key in self.objects if key.startswith(prefix)]}]
            if action == "list_object_versions":
                return [{"Versions": [dict(version) for version in self.versions if version["Key"].startswith(prefix)]}]
            if action == "list_multipart_uploads":
                return [{"Uploads": [dict(upload) for upload in self.uploads if upload["Key"].startswith(prefix)]}]
            raise AssertionError(action)

        return Mock(paginate=pages)

    def delete_objects(self, **kwargs: object) -> dict[str, object]:
        if self.fail:
            return {"Errors": [{"Code": "AccessDenied"}]}
        request = kwargs["Delete"]
        if not isinstance(request, dict):
            raise AssertionError(request)
        values = request["Objects"]
        if not isinstance(values, list):
            raise AssertionError(values)
        copied: list[dict[str, str]] = []
        for value in values:
            if not isinstance(value, dict):
                raise AssertionError(value)
            item: dict[str, str] = {}
            for key, item_value in value.items():
                if type(key) is not str or not isinstance(item_value, str):
                    raise AssertionError(value)
                item[key] = item_value
            copied.append(item)
        self.deletes.extend(copy.deepcopy(copied))
        for value in copied:
            if value["Key"] in self.objects:
                self.objects.remove(value["Key"])
            self.versions = [version for version in self.versions if version != value]
        return {"Deleted": copied}

    def abort_multipart_upload(self, **kwargs: object) -> None:
        upload_id = kwargs["UploadId"]
        if not isinstance(upload_id, str):
            raise AssertionError(upload_id)
        self.uploads = [upload for upload in self.uploads if upload["UploadId"] != upload_id]


class MissingObjectError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__("Missing")
        self.response: dict[str, dict[str, str]] = {"Error": {"Code": code}}


def s3_client(storage: Storage) -> S3Client:
    if type(storage) is not Storage:
        raise AssertionError(type(storage))
    return cast(S3Client, storage)


class ArtifactLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / ".tools")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "config").mkdir()
        (self.root / ".local").mkdir()
        self.config: S3Settings = {
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
        lifecycle.purge(self.root, s3_client(self.storage))
        self.assertEqual(self.storage.objects, ["other-project/keep"])
        self.assertEqual(self.storage.uploads, [])
        report = json.loads((self.root / "docs/s3-cleanup.json").read_text())
        self.assertTrue(report["success"])
        self.assertFalse(report["bucket_deleted"])
        self.assertTrue((self.root / ".local/artifact-storage.json").exists())

    def test_active_lease_prevents_any_object_deletion(self) -> None:
        self.storage.objects.append("otus-dz07/runs/other/lease.json")
        with self.assertRaisesRegex(RuntimeError, "lease"):
            lifecycle.purge(self.root, s3_client(self.storage))
        self.assertEqual(self.storage.deletes, [])
        self.assertTrue((self.root / ".local/artifact-purge-pending.json").exists())

    def test_versions_and_multipart_uploads_are_audited(self) -> None:
        self.storage.status = "Enabled"
        self.storage.versions = [{"Key": key, "VersionId": "old"} for key in self.storage.objects[:-1]]
        lifecycle.purge(self.root, s3_client(self.storage))
        self.assertEqual(self.storage.versions, [])
        self.assertEqual(self.storage.uploads, [])

    def test_delete_error_retains_binding_and_reports_failure(self) -> None:
        self.storage.fail = True
        with self.assertRaisesRegex(RuntimeError, "deletion errors"):
            lifecycle.purge(self.root, s3_client(self.storage))
        self.assertTrue((self.root / ".local/artifact-storage.json").exists())
        self.assertTrue((self.root / ".local/artifact-purge-pending.json").exists())
        self.assertFalse(json.loads((self.root / "docs/s3-cleanup.json").read_text())["success"])

    def test_changed_project_or_missing_binding_cannot_purge(self) -> None:
        with (
            patch.dict(os.environ, TEST_PROJECT_ID="another-project"),
            self.assertRaisesRegex(ValueError, "binding changed"),
        ):
            lifecycle.purge(self.root, s3_client(self.storage))
        (self.root / ".local/artifact-storage.json").unlink()
        with self.assertRaisesRegex(ValueError, "No confirmed"):
            lifecycle.purge(self.root, s3_client(self.storage))
        self.assertEqual(self.storage.deletes, [])

    def test_nonempty_compute_state_blocks_purge(self) -> None:
        (self.root / "terraform-selectel").mkdir()
        (self.root / "terraform-selectel/terraform.tfstate").write_text(
            '{"resources":[{"mode":"managed","instances":[{}]}]}'
        )
        with self.assertRaisesRegex(ValueError, "Non-empty state"):
            lifecycle.purge(self.root, s3_client(self.storage))
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
        storage.head_object.side_effect = MissingObjectError("NoSuchKey")
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
