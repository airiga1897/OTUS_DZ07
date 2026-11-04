"""Credential sources are explicit and share one schema."""

import copy
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import credentials
from runtime import create_runtime

ROOT = Path(__file__).resolve().parents[1]


class CredentialTests(unittest.TestCase):
    def _internal_clone(self) -> Path:
        folder = tempfile.TemporaryDirectory(dir=ROOT / ".tools")
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        shutil.copytree(ROOT / "config", root / "config")
        shutil.copy(ROOT / ".env.example", root / ".env.example")
        return root

    def test_authentication_schema_checks_external_shapes_and_types(self) -> None:
        original = json.loads((ROOT / "config/selectel-auth.json").read_text())
        cases = [
            None,
            [],
            {},
            {"auth_url": None, "fields": {}},
            {**original, "auth_url": "http://invalid"},
            {**original, "fields": []},
            {**original, "fields": {}},
        ]
        for key, value in (("env", None), ("env", "OTHER_PASSWORD"), ("prompt", None), ("prompt", ""), ("secret", 1)):
            invalid = copy.deepcopy(original)
            invalid["fields"]["password"][key] = value
            cases.append(invalid)
        for field in (None, {}, []):
            invalid = copy.deepcopy(original)
            invalid["fields"]["password"] = field
            cases.append(invalid)
        invalid = copy.deepcopy(original)
        invalid["fields"][""] = invalid["fields"].pop("password")
        cases.append(invalid)
        with tempfile.TemporaryDirectory(dir=ROOT / ".tools") as directory:
            root = Path(directory)
            (root / "config").mkdir()
            path = root / "config/selectel-auth.json"
            for index, document in enumerate(cases):
                path.write_text(json.dumps(document), encoding="utf-8")
                with self.subTest(index=index), self.assertRaises(ValueError):
                    credentials.schema(root)
            path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(credentials.schema(root), original)
            with patch("pool_selection.policy", return_value={"region": None}), self.assertRaises(ValueError):
                credentials.auth_settings(root)

    def test_redaction_field_selection_does_not_read_placement_or_saved_state(self) -> None:
        with patch("pool_selection.policy", side_effect=AssertionError("redaction must not read stand state")):
            self.assertEqual(credentials.credential_values({"password": "synthetic"}), ["synthetic"])

    def test_runtime_uses_the_schema_of_its_own_clone(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tools") as directory:
            root = Path(directory)
            shutil.copytree(ROOT / "config", root / "config")
            shutil.copy(ROOT / ".env.example", root / ".env.example")
            path = root / "config/selectel-auth.json"
            schema = json.loads(path.read_text())
            schema["fields"]["project_id"]["env"] = "OS_ALT_PROJECT_ID"
            path.write_text(json.dumps(schema))
            values = {field["env"]: "synthetic" for field in schema["fields"].values()}
            values["OS_ALT_PROJECT_ID"] = "clone-specific-project"
            values["OS_PROJECT_ID"] = "another-clone-project"
            with patch.dict(os.environ, values, clear=True):
                runtime = create_runtime(root)
                self.assertEqual(runtime.load_credentials()["project_id"], "clone-specific-project")
            self.assertFalse((root / ".local").exists())

    def test_runtime_redaction_uses_its_validated_schema_without_file_reads(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tools") as directory:
            root = Path(directory)
            shutil.copytree(ROOT / "config", root / "config")
            shutil.copy(ROOT / ".env.example", root / ".env.example")
            runtime = create_runtime(root)
            runtime._AUTH_CONFIG = {"password": "redaction-test-secret"}
            with patch.object(Path, "read_text", side_effect=AssertionError("redaction must not load configuration")):
                self.assertEqual(runtime.redact("redaction-test-secret"), "<redacted>")

    def test_file_selector_is_ignored(self) -> None:
        root = self._internal_clone()
        with (
            patch.dict(os.environ, {"OTUS_SELECTEL_AUTH": "missing.json"}, clear=True),
            self.assertRaisesRegex(ValueError, "Incomplete"),
        ):
            credentials.load_credentials(root)

    def test_environment_mapping_and_redaction_use_schema(self) -> None:
        root = self._internal_clone()
        values = {
            "domain_name": "sample-account",
            "project_id": "sample-project",
            "username": "sample-user",
            "password": "sample-password",
        }
        env = credentials.to_environment(values, root)
        with patch.dict(os.environ, env, clear=True):
            config = credentials.load_credentials(root)
        self.assertEqual(set(credentials.credential_values(config, root)), set(values.values()))


if __name__ == "__main__":
    unittest.main()
