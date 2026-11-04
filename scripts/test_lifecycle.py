"""Regression checks for resource ownership on deployment errors; no cloud calls."""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from selectel_adapter import SelectelAdapter
from test_runtime_support import lab


class LifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        original_root = lab.ROOT
        original_local = lab.LOCAL
        original_local.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=original_local)
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        shutil.copytree(original_root / "config", root / "config")
        shutil.copyfile(original_root / ".env.example", root / ".env.example")
        (root / ".local").mkdir()
        (root / "terraform-selectel").mkdir()
        isolated = patch.multiple(lab, ROOT=root, LOCAL=root / ".local", RUN=root / ".local/runs", _AUTH_CONFIG=None)
        isolated.start()
        self.addCleanup(isolated.stop)

    def test_environment_auth_needs_no_credential_file(self) -> None:
        config = {
            "auth_url": "https://cloud.api.selcloud.ru/identity/v3",
            "region": lab.placement()["region"],
            "project_id": "test-project",
            "domain_name": "test-account",
            "username": "test-operator",
            "password": "test-password",
        }
        names = {
            "auth_url": "OS_AUTH_URL",
            "region": "OS_REGION_NAME",
            "project_id": "OS_PROJECT_ID",
            "domain_name": "OS_USER_DOMAIN_NAME",
            "username": "OS_USERNAME",
            "password": "OS_PASSWORD",
        }
        with (
            patch.dict(os.environ, {names[k]: v for k, v in config.items()}, clear=True),
            patch.object(lab, "_AUTH_CONFIG", None),
            patch.object(lab, "bind_context") as bind,
        ):
            self.assertEqual(lab.auth_config(), config)
            bind.assert_called_once_with(config)

    def test_context_adopts_existing_stand_and_rejects_other_project(self) -> None:
        with tempfile.TemporaryDirectory(dir=lab.LOCAL) as directory:
            root = Path(directory)
            (root / "terraform-selectel").mkdir()
            (root / "terraform-selectel/terraform.tfstate").write_text(
                json.dumps(
                    {
                        "resources": [
                            {
                                "mode": "managed",
                                "type": "openstack_compute_instance_v2",
                                "instances": [{"attributes": {"tenant_id": "own-project"}}],
                            }
                        ]
                    }
                )
            )
            with (
                patch.object(lab, "ROOT", root),
                patch.object(lab, "LOCAL", root / ".local"),
                patch.object(lab, "placement", return_value={"availability_zone": "ru-7a"}),
            ):
                with self.assertRaisesRegex(ValueError, "binding"):
                    lab.bind_context({"project_id": "own-project", "region": "ru-7"})
                state_path = root / "terraform-selectel/terraform.tfstate"
                state = json.loads(state_path.read_text())
                state["outputs"] = {
                    "infrastructure": {
                        "value": {
                            "provider": "selectel",
                            "scope": {"project_id": "own-project"},
                            "namespace": "otus-dz07",
                            "placement": {"region": "ru-7", "zone": "ru-7a"},
                        }
                    }
                }
                state_path.write_text(json.dumps(state))
                lab.bind_context({"project_id": "own-project", "region": "ru-7", "password": "not-saved"})
                context = json.loads((root / ".local/context.json").read_text())
                self.assertEqual(context["scope"], {"project_id": "own-project"})
                self.assertEqual(context["provider"], "selectel")
                with self.assertRaisesRegex(ValueError, "scope"):
                    lab.bind_context({"project_id": "foreign-project", "region": "ru-7"})

    def test_redaction_removes_credentials_and_application_secrets(self) -> None:
        with tempfile.TemporaryDirectory(dir=lab.LOCAL) as directory:
            local = Path(directory)
            (local / "secrets.json").write_text(json.dumps({"app": "application-secret"}))
            config = dict(
                zip(
                    ["domain_name", "project_id", "username", "password"],
                    ["sample-account", "sample-project", "sample-user", "sample-password"],
                    strict=False,
                )
            )
            with patch.object(lab, "LOCAL", local), patch.object(lab, "_AUTH_CONFIG", config):
                original = " ".join([*config.values(), "application-secret"])
                self.assertEqual(lab.redact(original), " ".join(["<redacted>"] * 5))

    def test_operator_file_removed_only_after_verified_cleanup(self) -> None:
        for success in [True, False]:
            with self.subTest(success=success), tempfile.TemporaryDirectory(dir=lab.LOCAL) as directory:
                root = Path(directory)
                local = root / ".local"
                local.mkdir()
                operator = local / "operator.json"
                operator.write_text("{}")
                config = {"project_id": "test-project", "region": "ru-7"}
                with (
                    patch.object(lab, "ROOT", root),
                    patch.object(lab, "LOCAL", local),
                    patch.object(lab, "auth_config", return_value=config),
                    patch.object(lab, "checked_plan"),
                    patch.object(lab, "tf", return_value="{}"),
                    patch.object(lab, "cloud") as cloud,
                ):
                    cloud.return_value.config = config
                    if success:
                        cloud.return_value.get.return_value = {
                            "servers": [],
                            "server_groups": [],
                            "volumes": [],
                            "networks": [],
                            "subnets": [],
                            "routers": [],
                            "ports": [],
                            "security_groups": [],
                            "floatingips": [],
                        }
                        lab.cleanup()
                    else:
                        cloud.return_value.get.side_effect = TimeoutError()
                        with self.assertRaises(TimeoutError):
                            lab.cleanup()
                    self.assertTrue(operator.exists())

    def test_cloud_retries_timed_out_reads(self) -> None:
        import io

        cloud = SelectelAdapter.__new__(SelectelAdapter)
        cloud.endpoints, cloud.token = {"compute": "https://example.invalid"}, "test-token"
        response = io.BytesIO(b'{"servers": []}')
        response.headers = {}
        opener = Mock()
        opener.open.side_effect = [TimeoutError(), response]
        with patch("selectel_api.urllib.request.build_opener", return_value=opener), patch("selectel_api.time.sleep"):
            self.assertEqual(cloud.get("compute", "/servers"), {"servers": []})
            self.assertEqual(opener.open.call_count, 2)

    def test_cloud_does_not_retry_power_actions(self) -> None:
        cloud = SelectelAdapter.__new__(SelectelAdapter)
        cloud.endpoints, cloud.token = {"compute": "https://example.invalid"}, "test-token"
        with patch("selectel_adapter.request_json", side_effect=TimeoutError()) as request:
            with self.assertRaises(TimeoutError):
                cloud.get("compute", "/servers/own-id/action", {"os-stop": None})
            self.assertEqual(request.call_count, 1)

    def test_placement_requires_hard_policy_and_distinct_hosts(self) -> None:
        ids = {"es1": "id1", "es2": "id2", "es3": "id3"}
        for policy, hosts, valid in [
            (["anti-affinity"], ["a", "b", "c"], True),
            (["soft-anti-affinity"], ["a", "b", "c"], False),
            (["anti-affinity"], ["a", "a", "b"], False),
        ]:
            group = {"name": "otus-dz07-es", "policies": policy, "members": list(ids.values())}
            servers = {
                identifier: {
                    "name": "otus-dz07-" + name,
                    "status": "ACTIVE",
                    "hostId": host,
                    "OS-EXT-AZ:availability_zone": "ru-7a",
                }
                for (name, identifier), host in zip(ids.items(), hosts, strict=False)
            }
            with (
                self.subTest(policy=policy, hosts=hosts),
                patch.object(lab, "cloud") as cloud,
                patch.object(lab, "save"),
                patch.object(lab, "placement", return_value={"availability_zone": "ru-7a"}),
                patch.object(lab, "auth_config", return_value={"region": "ru-7"}),
            ):
                cloud.return_value.get.side_effect = lambda service, path, *, group=group, servers=servers: (
                    {"server_groups": [group]}
                    if path == "/os-server-groups"
                    else {"server": servers[path.rsplit("/", 1)[1]]}
                )
                if valid:
                    lab.check_placement({"server_ids": ids, "private_ips": dict.fromkeys(ids, "10.77.0.21")})
                else:
                    with self.assertRaises(RuntimeError):
                        lab.check_placement({"server_ids": ids, "private_ips": dict.fromkeys(ids, "10.77.0.21")})

    def test_verify_uses_normal_checks_without_power_operations(self) -> None:
        with (
            patch("sys.argv", ["lab.py", "verify"]),
            patch.object(lab, "verify") as verify,
            patch("operator_network.check_saved_access"),
        ):
            lab.main()
        verify.assert_called_once_with(False)

    def test_destroy_and_cleanup_use_same_owned_resource_cleanup(self) -> None:
        for action in ["destroy", "cleanup"]:
            with (
                self.subTest(action=action),
                patch("sys.argv", ["lab.py", action]),
                patch.object(lab, "cleanup") as cleanup,
            ):
                lab.main()
                cleanup.assert_called_once_with()

    def check_deploy_failure(self, existing):
        with (
            patch.object(lab, "managed_ids", return_value=existing),
            patch.object(lab, "preflight"),
            patch.object(lab, "checked_plan"),
            patch.object(lab, "tf"),
            patch.object(
                lab,
                "lab_output",
                return_value={
                    "private_ips": {"app": "10.77.0.10", "es1": "10.77.0.31", "es2": "10.77.0.32", "es3": "10.77.0.33"},
                },
            ),
            patch("verification.snapshot", return_value={}),
            patch.object(lab, "configure", side_effect=RuntimeError("configuration unavailable")),
            patch.object(lab, "diagnostics"),
            patch.object(lab.pools, "phase"),
            patch.object(lab, "cleanup") as cleanup,
        ):
            with self.assertRaisesRegex(RuntimeError, "configuration unavailable"):
                lab.deploy()
            return cleanup.call_count

    def test_failed_repeat_deploy_preserves_existing_resources(self) -> None:
        self.assertEqual(self.check_deploy_failure({"existing-node": "owned-id"}), 0)

    def test_failed_initial_deploy_preserves_partial_resources(self) -> None:
        self.assertEqual(self.check_deploy_failure({}), 0)

    def test_test_alias_uses_safe_cycle(self) -> None:
        with patch("test_cycle.run") as run:
            lab.regression()
            run.assert_called_once_with(lab)


class TrustTests(unittest.TestCase):
    def test_verified_key_survives_console_reset_for_same_uuid(self) -> None:
        key = (
            Ed25519PrivateKey.generate()
            .public_key()
            .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
            .split()[1]
            .decode()
        )
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory)
            (local / "trusted-hosts.json").write_text(json.dumps({"same-uuid": key}))
            with (
                patch.object(lab, "ROOT", local),
                patch.object(lab, "LOCAL", local),
                patch.object(lab, "cloud") as cloud,
            ):
                cloud.return_value.get.return_value = {"server": {"name": "otus-dz07-app"}}
                cloud.return_value.ssh_host_key.side_effect = lambda *args: lab.SelectelAdapter.ssh_host_key(
                    cloud.return_value, *args
                )
                lab.trust(
                    {
                        "server_ids": {"app": "same-uuid"},
                        "private_ips": {"app": "10.77.0.10"},
                        "app_public_ip": "192.0.2.1",
                    }
                )
                cloud.return_value.get.assert_called_once_with("compute", "/servers/same-uuid")
                self.assertIn(key, (local / "known_hosts").read_text())
                self.assertIn("10.77.0.10,192.0.2.1 ssh-ed25519 ", (local / "known_hosts").read_text())

    def test_new_uuid_requires_new_api_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory)
            (local / "trusted-hosts.json").write_text(json.dumps({"old-uuid": "old-key"}))
            with (
                patch.object(lab, "ROOT", local),
                patch.object(lab, "LOCAL", local),
                patch.object(lab, "cloud") as cloud,
                patch("selectel_adapter.time.sleep"),
            ):
                cloud.return_value.ssh_host_key.side_effect = lambda *args: lab.SelectelAdapter.ssh_host_key(
                    cloud.return_value, *args
                )
                cloud.return_value.get.side_effect = lambda service, path, *args: (
                    {"server": {"name": "otus-dz07-app"}} if path.endswith("/new-uuid") else {"output": ""}
                )
                with self.assertRaisesRegex(RuntimeError, "No trusted SSH key"):
                    lab.trust(
                        {
                            "server_ids": {"app": "new-uuid"},
                            "private_ips": {"app": "10.77.0.10"},
                            "app_public_ip": "192.0.2.1",
                        }
                    )


if __name__ == "__main__":
    unittest.main()
