"""Execute uncovered controller, delivery and acceptance branches with synthetic I/O."""

import ast
import base64
import hashlib
import hmac
import io
import json
import os
import shutil
import ssl
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from email.message import Message
from pathlib import Path
from types import CodeType, ModuleType
from typing import Literal, Self
from unittest.mock import Mock, patch

import cache_packages
import pool_selection as pools
import privacy
import s3_artifacts as artifacts
from artifact_content import digest, package_lock
from cloud_contract import Capability, CapabilityStatus, InfrastructureNode, TerraformError
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from selectel_adapter import SelectelAdapter, preflight
from selectel_api import CatalogService
from test_runtime_support import lab

ROOT = Path(__file__).resolve().parents[1]


def entrypoint_code(module: ModuleType) -> CodeType:
    """Keep real source positions and execute dispatch in a private namespace."""
    filename = module.__file__
    assert filename is not None, "Entrypoint tests require a source-backed module"
    tree = ast.parse(Path(filename).read_text(encoding="utf-8"), filename=filename)
    guard = next(
        node for node in tree.body if isinstance(node, ast.If) and ast.unparse(node.test) == "__name__ == '__main__'"
    )
    return compile(ast.Module(body=[guard], type_ignores=[]), filename, "exec")


def public_key() -> str:
    raw = (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )
    return base64.b64encode(raw).decode()


def isolated(test: unittest.TestCase) -> Path:
    folder = tempfile.TemporaryDirectory(dir=ROOT / ".tools")
    test.addCleanup(folder.cleanup)
    root = Path(folder.name)
    (root / ".local").mkdir()
    (root / "config").mkdir()
    (root / "terraform-selectel").mkdir()
    (root / "docs").mkdir()
    return root


class CatalogGapTests(unittest.TestCase):
    def test_absent_pool_invalid_catalog_and_exhausted_retry(self) -> None:
        catalog: list[CatalogService] = [
            {
                "type": kind,
                "endpoints": [{"interface": "public", "region": "ru-9", "url": "https://example.invalid/" + kind}],
            }
            for kind in pools.SERVICES
        ]
        self.assertEqual(pools.ordered_pools({"pools": ["ru-4", "ru-9"], "excluded_pools": []}, catalog), ["ru-9"])
        broken = [item for item in catalog if item["type"] != "image"]
        with self.assertRaises(pools.CandidateUnavailable):
            pools.endpoints_for(broken, "ru-9")
        with (
            patch.object(pools, "request_json", return_value=({"images": [], "next": "/v2/images?status=active"}, {})),
            self.assertRaises(ValueError),
        ):
            pools.image_catalog("https://images.invalid/v2", "token")
        noted = []
        with self.assertRaisesRegex(RuntimeError, "No configured pool"):
            pools.run_candidates([], Mock(), Mock(), Mock(), lambda: noted.append("exhausted"))
        self.assertEqual(noted, ["exhausted"])
        with self.assertRaises(pools.CandidateUnavailable):
            pools.capacity(None, 1)

    def test_candidate_inputs_and_saved_policy_fail_closed(self) -> None:
        for config in (
            {"pools": ["ru-9"], "disk_type_patterns": []},
            {"pools": ["not a pool"], "disk_type_patterns": ["universal.*"]},
            {"pools": ["ru-9"], "disk_type_patterns": ["universal.*"], "segment": "segment"},
        ):
            with self.subTest(config=config), self.assertRaises(ValueError):
                next(pools.candidates(config, {}))
        root = isolated(self)
        shutil.copy(ROOT / ".env.example", root / ".env.example")
        shutil.copy(ROOT / "config/placement.json", root / "config/placement.json")
        journal = {
            "run_settings": {"boot_disk_gb": 20},
            "pool_subnets": {"ru-3": "10.78.0.0/24"},
            "bootstrap_region": "ru-3",
        }
        (root / ".local/staged-deployment.json").write_text(json.dumps(journal))
        self.assertEqual(pools.policy(root, "placement.json", "context.json")["region"], "ru-3")
        (root / ".local/staged-deployment.json").unlink()
        (root / ".local/context.json").write_text(json.dumps({"placement": {"region": "ru-9"}}))
        (root / "terraform-selectel/terraform.tfvars.json").write_text(
            json.dumps({"region": "ru-9", "availability_zone": "ru-9a"})
        )
        saved = pools.policy(root, "placement.json", "context.json")
        self.assertEqual(saved["availability_zone"], "ru-9a")

    def test_saved_state_blocks_reselection_and_foreign_diagnostics(self) -> None:
        root = isolated(self)
        state = root / "terraform-selectel/terraform.tfstate"
        state.write_text(
            json.dumps({"resources": [{"mode": "managed", "instances": [{"attributes": {"id": "keep"}}]}]})
        )
        with self.assertRaisesRegex(ValueError, "managed state"):
            pools.select(root, {"region": "ru-9", "availability_zone": "ru-9a"}, "project", "context.json")
        state.write_text("{}")
        (root / ".local/context.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "Legacy context"):
            pools.select(root, {"region": "ru-9", "availability_zone": "ru-9a"}, "project", "context.json")
        (root / ".local/context.json").unlink()
        pools.select(root, {"region": "ru-9", "availability_zone": "ru-9a"}, "project", "context.json")
        with self.assertRaisesRegex(ValueError, "another project"):
            pools.select(root, {"region": "ru-9", "availability_zone": "ru-9a"}, "other", "context.json")
        placement = json.loads((root / ".local/placement.json").read_text())
        placement["provider"] = "other"
        (root / ".local/placement.json").write_text(json.dumps(placement))
        with self.assertRaisesRegex(ValueError, "another provider"):
            pools.select(root, {"region": "ru-9", "availability_zone": "ru-9a"}, "project", "context.json")
        self.assertFalse(pools.scheduler_unavailable(TerraformError("unrelated"), root, Mock(), "prefix"))
        state.unlink()
        self.assertFalse(pools.scheduler_unavailable(TerraformError("unrelated"), root, Mock(), "prefix"))
        cloud = Mock()
        owned = {
            "resources": [
                {
                    "mode": "managed",
                    "type": "openstack_compute_instance_v2",
                    "instances": [{"index_key": "app", "attributes": {"id": "vm", "name": "prefix-app"}}],
                }
            ]
        }
        state.write_text(
            json.dumps(
                {
                    "resources": [
                        {
                            "mode": "managed",
                            "type": "openstack_compute_instance_v2",
                            "instances": [{"index_key": "app", "attributes": {"id": "vm", "name": "other"}}],
                        }
                    ]
                }
            )
        )
        with self.assertRaisesRegex(ValueError, "Foreign instance"):
            pools.scheduler_unavailable(TerraformError("unrelated"), root, lambda: cloud, "prefix")
        state.write_text(json.dumps(owned))
        cloud.get.return_value = {
            "server": {"name": "prefix-app", "status": "ERROR", "fault": {"message": "No valid host"}}
        }
        self.assertTrue(pools.scheduler_unavailable(TerraformError("unrelated"), root, lambda: cloud, "prefix"))
        cloud.get.return_value = {"server": {"name": "other-vm"}}
        with self.assertRaisesRegex(ValueError, "Foreign server"):
            pools.scheduler_unavailable(TerraformError("unrelated"), root, lambda: cloud, "prefix")


class AdapterGapTests(unittest.TestCase):
    def test_power_reaches_the_requested_state_and_console_key_is_checked(self) -> None:
        cloud = SelectelAdapter.__new__(SelectelAdapter)
        cloud.config = {"project_id": "own"}
        key = public_key()
        get = self.enterContext(
            patch.object(
                cloud,
                "get",
                Mock(
                    side_effect=[
                        {"server": {"name": "otus-dz07-app", "tenant_id": "own"}},
                        {"server": {"name": "otus-dz07-app", "tenant_id": "own", "status": "ACTIVE"}},
                        {},
                        {"server": {"status": "SHUTOFF"}},
                        {"server": {"name": "otus-dz07-app"}},
                        {"output": "BEGIN SSH HOST KEY KEYS\nssh-ed25519 " + key + "\nEND SSH HOST KEY KEYS"},
                    ]
                ),
            )
        )
        self.assertEqual(cloud.ssh_host_key("app", "vm", "otus-dz07", key), key)
        cloud.set_power("app", "vm", running=False, prefix="otus-dz07")
        self.assertEqual(cloud.ssh_host_key("app", "vm", "otus-dz07", None), key)

        def stuck(service: str, path: str, payload: object = None) -> dict[str, object]:
            if payload is not None:
                return {}
            return {"server": {"name": "otus-dz07-app", "tenant_id": "own", "status": "ACTIVE"}}

        get.side_effect = stuck
        with patch("selectel_adapter.time.sleep"), self.assertRaisesRegex(RuntimeError, "unconfirmed"):
            cloud.set_power("app", "vm", running=False, prefix="otus-dz07")


class DeliveryGapTests(unittest.TestCase):
    def root(self) -> Path:
        root = isolated(self)
        shutil.copy(ROOT / "config/artifacts.json", root / "config/artifacts.json")
        values = json.loads((root / "config/artifacts.json").read_text())
        values["s3"]["bucket"] = "synthetic-bucket"
        (root / "config/artifacts.json").write_text(json.dumps(values))
        shutil.copy(ROOT / "config/artifact-auth.json", root / "config/artifact-auth.json")
        shutil.copy(ROOT / "config/services.json", root / "config/services.json")
        return root

    def test_settings_client_policy_and_immutable_mismatch(self) -> None:
        root = self.root()
        values = json.loads((root / "config/artifacts.json").read_text())
        for bad in ("http://s3.ru-7.storage.selcloud.ru", "https://s3.ru-7.storage.selcloud.ru/extra"):
            values["s3"]["endpoint"] = bad
            (root / "config/artifacts.json").write_text(json.dumps(values))
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                artifacts.settings(root)
        values["s3"]["endpoint"] = "https://s3.ru-7.storage.selcloud.ru"
        values["s3"]["bucket"] = "x"
        (root / "config/artifacts.json").write_text(json.dumps(values))
        with self.assertRaises(ValueError):
            artifacts.settings(root)
        values["s3"]["bucket"] = "synthetic-bucket"
        (root / "config/artifacts.json").write_text(json.dumps(values))
        config = artifacts.settings(root)
        with (
            patch.dict(
                os.environ, {"AWS_ACCESS_KEY_ID": "synthetic-access", "AWS_SECRET_ACCESS_KEY": "synthetic-secret"}
            ),
            patch("boto3.client", return_value=Mock()) as factory,
        ):
            artifacts.client(root, config)
        self.assertTrue((root / ".tools/system-trust-ca.pem").is_file())
        self.assertEqual(factory.call_args.kwargs["verify"], str(root / ".tools/system-trust-ca.pem"))
        self.assertNotIn("synthetic-secret", factory.call_args.kwargs["verify"])
        storage = Mock()
        storage.get_bucket_acl.return_value = {
            "Owner": {"ID": "owner"},
            "Grants": [{"Grantee": {"Type": "CanonicalUser", "ID": "owner"}}],
        }
        storage.get_bucket_policy.return_value = {
            "Policy": json.dumps({"Statement": [{"Effect": "Allow", "Principal": "*"}]})
        }
        with self.assertRaisesRegex(ValueError, "Broad bucket"):
            artifacts.require_private(storage, config["bucket"])
        path = root / "package.deb"
        path.write_bytes(b"package")
        sha = digest(path)
        storage.head_object.return_value = {"ContentLength": 1, "Metadata": {"sha512": "other"}}
        with self.assertRaisesRegex(ValueError, "differs"):
            artifacts.ensure(storage, config, path, "packages", sha)
        storage.head_object.return_value = {"ContentLength": path.stat().st_size, "Metadata": {"sha512": sha}}
        storage.get_object_acl.return_value = storage.get_bucket_acl.return_value
        storage.generate_presigned_url.return_value = "http://example.invalid/object"
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            artifacts.ensure(storage, config, path, "packages", sha)
        (root / "config/elastic-artifacts.json").write_text('{"unexpected": "ab"}')
        with self.assertRaises(ValueError):
            package_lock(root)

    def test_package_round_trip_and_cli_report_failures_without_printing_secrets(self) -> None:
        root = self.root()
        artifacts.settings(root)
        lock = {}
        cache = root / ".tools/elastic-packages"
        cache.mkdir(parents=True)
        for product in ("elasticsearch", "filebeat", "kibana"):
            path = cache / (product + "-8.19.0-amd64.deb")
            path.write_bytes(product.encode())
            lock[path.name] = digest(path)
        (root / "config/elastic-artifacts.json").write_text(json.dumps(lock))
        storage = Mock()
        acl = {"Owner": {"ID": "owner"}, "Grants": [{"Grantee": {"Type": "CanonicalUser", "ID": "owner"}}]}
        storage.get_bucket_acl.return_value = acl
        storage.get_object_acl.return_value = acl
        storage.get_bucket_policy.side_effect = type(
            "Missing", (Exception,), {"response": {"Error": {"Code": "NoSuchBucketPolicy"}}}
        )()

        def head(**kwargs: str) -> dict[str, object]:
            name = kwargs["Key"].rsplit("/", 1)[-1]
            return {"ContentLength": (cache / name).stat().st_size, "Metadata": {"sha512": lock[name]}}

        storage.head_object.side_effect = head
        storage.generate_presigned_url.return_value = "https://s3.ru-7.storage.selcloud.ru/object"

        def download(item: object, destination: Path, **kwargs: object) -> None:
            destination.write_bytes((cache / destination.name).read_bytes())

        with patch("remote.fetch_artifacts.download", download):
            report = artifacts.verify_packages(root, storage)
        self.assertTrue(report["success"])
        self.assertEqual(set(report["packages"]), set(lock))
        secret = "synthetic-entrypoint-secret"
        with (
            patch("sys.argv", ["s3_artifacts.py", "verify-packages"]),
            patch.object(privacy, "_values", {secret}),
            patch.object(artifacts, "verify_packages", side_effect=ValueError("probe failed " + secret)),
            self.assertRaises(SystemExit) as failure,
        ):
            exec(entrypoint_code(artifacts), {**vars(artifacts), "__name__": "__main__"})
        self.assertIn("S3 artifact operation failed", str(failure.exception))
        self.assertNotIn(secret, str(failure.exception))

    def test_range_download_retries_and_refuses_a_short_disk(self) -> None:
        url = "https://artifacts.elastic.co/downloads/file.deb"
        target = isolated(self) / "package.partial"

        class Response(io.BytesIO):
            def __init__(self, status: int, headers: Mapping[str, str], data: bytes) -> None:
                super().__init__(data)
                self.status, self.headers = status, headers

            def __enter__(self) -> Self:
                return self

            def __exit__(self, *args: object) -> None:
                return None

        calls = {"n": 0}

        def urlopen(request: urllib.request.Request, timeout: float | None = None) -> Response:
            method = request.get_method()
            if method == "HEAD":
                return Response(200, {"Content-Length": "4"}, b"")
            calls["n"] += 1
            if calls["n"] == 1:
                raise TimeoutError("synthetic")
            if request.headers.get("Range") != "bytes=0-3":
                raise AssertionError(request.headers.get("Range"))
            return Response(206, {"Content-Range": "bytes 0-3/4"}, b"abcd")

        with patch("urllib.request.urlopen", side_effect=urlopen):
            cache_packages.download(url, target)
        self.assertEqual(target.read_bytes(), b"abcd")

        def bad(request: urllib.request.Request, timeout: float | None = None) -> Response:
            if request.get_method() == "HEAD":
                return Response(200, {"Content-Length": "4"}, b"")
            return Response(200, {"Content-Range": "bytes 0-3/4"}, b"abcd")

        with (
            patch("urllib.request.urlopen", side_effect=bad),
            self.assertRaisesRegex(RuntimeError, "byte range"),
        ):
            cache_packages.download(url, target)
        root = isolated(self)
        (root / "config").mkdir(exist_ok=True)
        (root / "config/services.json").write_text(
            json.dumps({"elastic_version": "8.19.0", "artifact_delivery": "cached"})
        )
        cache = root / ".tools/elastic-packages"
        cache.mkdir(parents=True)
        with (
            patch.object(cache_packages, "ROOT", root),
            patch.object(cache_packages, "CACHE", cache),
            patch("prepare_ansible.prepare"),
            patch("shutil.disk_usage", return_value=Mock(free=1)),
            patch("urllib.request.urlopen", return_value=Mock(read=Mock(return_value=(b"ab" * 64)))),
            self.assertRaisesRegex(RuntimeError, "3 GiB"),
        ):
            cache_packages.main()


class ControllerGapTests(unittest.TestCase):
    def test_terraform_logs_redact_secrets_and_identity_is_local(self) -> None:
        root = isolated(self)
        run = root / ".local/runs"
        config = {
            "username": "synthetic-user",
            "password": "synthetic-password",
            "project_id": "project",
            "domain_name": "account",
            "region": "ru-9",
        }
        (root / ".local/secrets.json").write_text(json.dumps({"api": "synthetic-api-secret"}))
        failed = Mock(returncode=1, stdout=b"plan synthetic-password synthetic-api-secret", stderr=b"")
        with (
            patch.object(lab, "ROOT", root),
            patch.object(lab, "LOCAL", root / ".local"),
            patch.object(lab, "RUN", run),
            patch.object(lab, "_AUTH_CONFIG", config),
            patch.object(lab, "TF", root / "terraform.exe"),
            patch.object(lab, "auth_config", return_value=config),
            patch("test_runtime_support.lab.subprocess.run", return_value=failed),
            patch(
                "runtime_infrastructure.environment_for_schema",
                return_value={"OS_PASSWORD": "synthetic-password", "TF_VAR_boot": "20"},
            ),
        ):
            with self.assertRaises(TerraformError) as caught:
                lab.tf("apply")
            self.assertNotIn("synthetic-password", caught.exception.output)
            self.assertIn("<redacted>", next(run.glob("terraform-apply-*.log")).read_text(encoding="utf-8"))
            failed.returncode = 0
            failed.stdout = b'{"ok": true}'
            self.assertEqual(lab.tf("output", capture=True), '{"ok": true}')
            self.assertNotIn("TF_VAR_boot", lab.subprocess.run.call_args.kwargs["env"])
            (root / ".local/secrets.json").unlink()
            secrets = lab.prepare_identity()
            self.assertTrue((root / ".local/otus_dz07").is_file())
            self.assertEqual(len(json.loads(secrets.read_text())), 8)
            (root / ".local/otus_dz07.pub").unlink()
            with self.assertRaises(ValueError):
                lab.prepare_identity()

    def test_plan_and_state_reject_foreign_or_changed_identity(self) -> None:
        root = isolated(self)
        planned_resources: list[dict[str, object]] = []
        shown = {
            "resource_changes": [
                {
                    "mode": "managed",
                    "address": "module.vm",
                    "type": "openstack_compute_instance_v2",
                    "name": "node",
                    "index": "app",
                    "change": {"actions": ["update"], "before": {"id": "old"}, "after": {"id": "new"}},
                }
            ],
            "planned_values": {"root_module": {"resources": planned_resources}},
            "prior_state": {"values": {"root_module": {"resources": []}}},
        }
        with (
            patch.object(lab, "ROOT", root),
            patch.object(lab, "LOCAL", root / ".local"),
            patch.object(lab, "tf", side_effect=["", json.dumps(shown)]),
            patch.object(lab, "managed_ids", return_value={}),
            self.assertRaisesRegex(ValueError, "Unexpected mutation"),
        ):
            lab.checked_plan()
        planned_resources.append({"mode": "managed", "values": {"name": "other-lab"}})
        with (
            patch.object(lab, "ROOT", root),
            patch.object(lab, "LOCAL", root / ".local"),
            patch.object(lab, "tf", side_effect=["", json.dumps(shown)]),
            patch.object(lab, "managed_ids", return_value={}),
            self.assertRaisesRegex(ValueError, "Foreign resource"),
        ):
            lab.checked_plan()
        state = root / "terraform-selectel/terraform.tfstate"
        state.write_text(
            json.dumps(
                {
                    "resources": [
                        {
                            "mode": "managed",
                            "type": "openstack_compute_instance_v2",
                            "name": "node",
                            "instances": [{"index_key": "app", "attributes": {"id": "vm", "name": "other-lab"}}],
                        }
                    ]
                }
            )
        )
        with (
            patch.object(lab, "ROOT", root),
            patch.object(lab, "LOCAL", root / ".local"),
            self.assertRaisesRegex(ValueError, "Foreign resource"),
        ):
            lab.managed_ids()

    def test_remote_channel_timeout_failure_and_direct_route(self) -> None:
        root = isolated(self)
        run = root / ".local/runs"
        channel = Mock()
        channel.exit_status_ready.side_effect = [False, False, True]
        channel.recv_ready.side_effect = [True, False, False]
        channel.recv_stderr_ready.side_effect = [True, False, False]
        channel.recv.return_value = b"ok"
        channel.recv_stderr.return_value = b"warning"
        channel.recv_exit_status.return_value = 0
        client = Mock()
        client.get_transport.return_value.open_session.return_value = channel
        with (
            patch.object(lab, "ROOT", root),
            patch.object(lab, "LOCAL", root / ".local"),
            patch.object(lab, "RUN", run),
            patch.object(lab, "_AUTH_CONFIG", None),
        ):
            self.assertEqual(lab.remote(client, "true", "probe", stdin="data"), "ok")
            self.assertIn("warning", (run / "probe.log").read_text(encoding="utf-8"))
            channel.recv_exit_status.return_value = 1
            channel.exit_status_ready.side_effect = None
            channel.recv_ready.side_effect = None
            channel.recv_stderr_ready.side_effect = None
            channel.exit_status_ready.return_value = True
            channel.recv_ready.return_value = False
            channel.recv_stderr_ready.return_value = False
            with self.assertRaisesRegex(RuntimeError, "probe"):
                lab.remote(client, "false", "probe")
            channel.exit_status_ready.return_value = False
            channel.exit_status_ready.side_effect = None
            channel.recv_ready.return_value = False
            channel.recv_stderr_ready.return_value = False
            with (
                patch.object(lab.time, "monotonic", side_effect=[0, 10, 2000]),
                patch.object(lab.time, "sleep"),
                self.assertRaisesRegex(TimeoutError, "probe"),
            ):
                lab.remote(client, "sleep", "probe")
        key = b"synthetic-api-secret"
        nonce = "ab" * 24
        signature = hmac.new(key, (nonce + "|8.8.8.8").encode(), hashlib.sha256).hexdigest()
        body = json.dumps({"nonce": nonce, "ip": "8.8.8.8", "signature": signature}).encode()
        response = Mock(
            status=200,
            __enter__=Mock(return_value=Mock(read=Mock(return_value=body))),
            __exit__=Mock(return_value=False),
        )
        response.__enter__ = lambda *args: response
        response.read = Mock(return_value=body)
        (root / ".local/secrets.json").write_text(json.dumps({"api": key.decode()}))
        variables = root / "terraform-selectel/terraform.tfvars.json"
        variables.write_text(json.dumps({"admin_cidrs": ["1.1.1.1/32"]}))
        with (
            patch.object(lab, "ROOT", root),
            patch.object(lab, "LOCAL", root / ".local"),
            patch("test_runtime_support.lab.secrets.token_hex", return_value=nonce),
            patch("urllib.request.urlopen", return_value=response),
            patch.object(lab, "checked_plan", return_value=root / "plan"),
            patch.object(lab, "tf") as apply,
            patch.object(lab.pools, "phase"),
        ):
            lab.discover_route({"site_url": "http://192.0.2.20"})
        self.assertIn("8.8.8.8/32", json.loads(variables.read_text())["admin_cidrs"])
        apply.assert_called_once()
        with (
            patch.object(lab, "ROOT", root),
            patch.object(lab, "LOCAL", root / ".local"),
            patch.object(lab.time, "sleep"),
            patch("urllib.request.urlopen", side_effect=OSError("down")),
            self.assertRaisesRegex(RuntimeError, "unavailable"),
        ):
            lab.discover_route({"site_url": "http://192.0.2.20"})

    def test_preflight_accepts_sufficient_quotas_and_rejects_overlap(self) -> None:
        root = isolated(self)
        shutil.copy(ROOT / "config/cloud.json", root / "config/cloud.json")
        quota = {"limit": 100000, "in_use": 0, "reserved": 0}

        def get(service: str, path: str, payload: object = None) -> dict[str, object]:
            if path == "/servers/detail":
                return {"servers": []}
            if path == "/v2.0/networks":
                return {"networks": []}
            if path.endswith("/subnets"):
                return {"subnets": [{"cidr": "10.96.0.0/24", "ip_version": 4, "name": "foreign"}]}
            if service == "compute":
                return {
                    "quota_set": {"az_quotas": {"ru-9a": {key: dict(quota) for key in ("cores", "ram", "instances")}}}
                }
            if service == "volumev3":
                return {
                    "quota_set": {
                        "gigabytes": dict(quota),
                        "volumes": dict(quota),
                        "gigabytes_universal.ru-9a": dict(quota),
                    }
                }
            if "details.json" in path:
                return {
                    "quota": {
                        key: dict(quota)
                        for key in (
                            "floatingip",
                            "network",
                            "subnet",
                            "router",
                            "security_group",
                            "security_group_rule",
                            "port",
                        )
                    }
                }
            return {}

        cloud = Mock()
        cloud.config = {"project_id": "project"}
        cloud.get.side_effect = get
        desired = {"nodes": {"app": {"role": "app"}, "es1": {"role": "es"}}}
        placement = {
            "availability_zone": "ru-9a",
            "region": "ru-9",
            "volume_type": "universal.ru-9a",
            "boot_disk_gb": 20,
            "admin_cidrs": ["192.0.2.10/32"],
            "profiles": {"app": {"vcpus": 4, "ram_mb": 8192}, "es": {"vcpus": 2, "ram_mb": 8192}},
        }
        api = Mock(
            ROOT=root,
            STACK={"subnet_cidr": "10.96.0.0/24"},
            PREFIX="otus-dz07",
            cloud=Mock(return_value=cloud),
            placement=Mock(return_value=placement),
            desired_topology=Mock(return_value=desired),
            managed_ids=Mock(return_value={}),
            save=lambda path, value: Path(path).write_text(json.dumps(value)),
        )
        supported = {
            name: Capability(CapabilityStatus.SUPPORTED, "synthetic")
            for name in ("network_isolation", "hard_anti_affinity", "placement_hints")
        }
        with (
            patch("selectel_adapter.discover_capabilities", return_value=supported),
            self.assertRaises(pools.CandidateUnavailable),
        ):
            preflight(api)

        def clear(service: str, path: str, payload: object = None) -> dict[str, object]:
            if path.endswith("/subnets"):
                return {"subnets": []}
            return get(service, path, payload)

        cloud.get.side_effect = clear
        with patch("selectel_adapter.discover_capabilities", return_value=supported):
            preflight(api)
        self.assertTrue(json.loads((root / "docs/preflight.json").read_text())["quotas_sufficient"])


class AcceptanceGapTests(unittest.TestCase):
    def test_invalid_stack_delivery_and_infrastructure_identity(self) -> None:
        import delivery
        import stack_config
        from cloud_contract import InfrastructureResult

        root = isolated(self)
        (root / "config/services.json").write_text(json.dumps({"artifact_delivery": "usb"}))
        with self.assertRaisesRegex(ValueError, "Unknown artifact_delivery"):
            delivery.mode(root)
        value = json.loads((ROOT / "config/stack.json").read_text())
        value["name_prefix"] = "Bad"
        (root / "config").mkdir(exist_ok=True)
        (root / "config/stack.json").write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            stack_config.load(root)
        node: InfrastructureNode = {
            "roles": ["app"],
            "private_ip": "10.77.0.10",
            "id": "app",
            "volume_id": "volume",
            "port_id": "port",
            "public_ip": "8.8.8.8",
        }
        valid: dict[str, object] = {
            "provider": "selectel",
            "scope": {"project_id": "project"},
            "namespace": "otus-dz07",
            "placement": {"region": "ru-9", "zone": "ru-9a"},
            "subnet_cidr": "10.77.0.0/24",
            "nodes": {"app": node},
        }
        self.assertEqual(InfrastructureResult.from_dict(valid).namespace, "otus-dz07")
        changes: list[dict[str, object]] = [
            {"provider": "Bad"},
            {"scope": {}},
            {"namespace": "Bad"},
            {"nodes": {}},
            {"nodes": {"app": {**node, "roles": ["etcd"]}}},
            {"nodes": {"app": {**node, "volume_id": ""}}},
        ]
        for change in changes:
            with self.subTest(change=list(change)), self.assertRaises(ValueError):
                InfrastructureResult.from_dict({**valid, **change})

    def test_download_rejects_bad_transport_and_retries_a_transient_failure(self) -> None:
        from remote.download_packages import fetch
        from remote.fetch_artifacts import download

        destination = isolated(self) / "blob"
        with self.assertRaises(ValueError):
            download({"url": "http://example.invalid/a", "sha512": "a" * 128, "size": 1}, destination)
        payload = b"abc"
        digest = hashlib.sha512(payload).hexdigest()

        class Body(io.BytesIO):
            status = 200

            def geturl(self) -> str:
                return "https://example.invalid/blob"

            def read1(self, size: int | None = -1, /) -> bytes:
                return super().read(size)

            def __enter__(self) -> Self:
                return self

            def __exit__(self, *args: object) -> None:
                return None

        opener = Mock()
        opener.open.side_effect = [
            urllib.error.HTTPError("https://example.invalid", 503, "busy", Message(), None),
            Body(payload),
        ]
        download({"url": "https://example.invalid/blob", "sha512": digest, "size": len(payload)}, destination, opener)
        self.assertEqual(destination.read_bytes(), payload)
        directory = isolated(self)
        opener = Mock()
        opener.open.side_effect = AssertionError("network")
        with self.assertRaises(ValueError):
            fetch(directory, "8.19.0", "http://artifacts.elastic.co", opener)

    def test_acceptance_failover_and_log_commands_use_the_controller(self) -> None:
        import observability
        import verification

        root = isolated(self)
        lab_output = {
            "server_ids": {"app": "app-id", "es1": "es-id"},
            "private_ips": {"app": "10.77.0.10", "es1": "10.77.0.31"},
            "node_roles": {"app": "app", "es1": "es"},
            "app_public_ip": "8.8.8.8",
        }

        class Connected:
            def __enter__(self) -> Mock:
                return Mock()

            def __exit__(self, *args: object) -> Literal[False]:
                return False

        shutil.copy(ROOT / "config/services.json", root / "config/services.json")
        api = Mock()
        api.ROOT = root
        api.LOCAL = root / ".local"
        api.PREFIX = "otus-dz07"
        api.STACK = {"paths": {"remote_root": "/opt/dz07"}}
        api.lab_output.return_value = lab_output
        api.connect.return_value = Connected()
        api.remote.return_value = json.dumps({"node": "es1", "new_master": {"node": "es2"}, "sources": True})
        api.save.side_effect = lambda path, value: Path(path).write_text(json.dumps(value))
        api.cloud.return_value = Mock()
        self.assertEqual(observability.command(api, "verify")["node"], "es1")
        observability.verify(api)
        with patch("elastic_options.failover", return_value={"restored": True}) as check:
            self.assertTrue(observability.failover(api)["restored"])
        check.assert_called_once_with(api)
        api.remote.return_value = json.dumps({"node": "db1", "new_master": {"node": "es1"}})
        with (
            patch("elastic_options.failover", side_effect=ValueError("outside own")),
            self.assertRaisesRegex(ValueError, "outside own"),
        ):
            observability.failover(api)
        before = {"run_id": "ab" * 16, "server_ids": {"app": "app-id"}, "before": {"new_record": {"id": 9}}}
        api.remote.return_value = json.dumps({"preserved_records": [{"id": 1}]})
        with self.assertRaisesRegex(RuntimeError, "original canary"):
            verification.expansion(api, before)
        api.remote.side_effect = None
        api.remote.return_value = json.dumps({"database_host": "app"})
        with patch("observability.verify", return_value={"logs": True}):
            result = verification.verify(api)
        self.assertEqual(result["baseline"]["database_host"], "app")
        api.cloud.return_value.set_power.assert_not_called()
        with self.assertRaisesRegex(ValueError, "--es-failover"):
            verification.verify(api, True)

    def test_elasticsearch_failure_membership_and_process_dispatch(self) -> None:
        from remote import elk_api as elk

        base = isolated(self)
        config = {
            "ips": {"es1": "10.77.0.31", "es2": "10.77.0.32", "es3": "10.77.0.33"},
            "ip": "10.77.0.10",
            "kibana_port": 5601,
            "secrets": {"elastic": "synthetic"},
        }
        documents = {}

        def reply(path: str, payload: object = None, method: str | None = None) -> object:
            clean = path.split("?")[0]
            if clean == "/_cat/master":
                return [{"node": "es2"}]
            if method in ("PUT", "POST"):
                documents[clean] = payload
                return {"result": "created"}
            return {"found": clean in documents, "_source": documents.get(clean, {})}

        with (
            patch.object(elk, "C", config),
            patch.object(elk, "S", config["secrets"]),
            patch.object(elk, "BASE", base),
            patch.object(elk, "api", side_effect=reply),
            patch.object(elk, "wait_health", return_value={"status": "yellow"}),
        ):
            self.assertEqual(json.loads(self.capture(elk.master))["node"], "es2")
            failed = json.loads(self.capture(elk.failure))
            self.assertEqual(failed["new_master"]["node"], "es2")
            self.assertEqual(json.loads(self.capture(elk.recovery))["health"]["status"], "yellow")
            self.assertEqual(json.loads(self.capture(elk.health))["status"], "yellow")
        (base / "elk-settings.json").write_text(json.dumps(config))
        with (
            patch.object(elk, "BASE", base),
            patch.object(sys, "argv", ["elk_api.py", "health"]),
            patch.object(elk, "health") as health,
            patch.object(ssl, "create_default_context", return_value=Mock()),
        ):
            exec(entrypoint_code(elk), {**vars(elk), "__name__": "__main__"})
        health.assert_called_once()

    def capture(self, function: Callable[[], object]) -> str:
        stream = io.StringIO()
        with patch("sys.stdout", stream):
            function()
        return stream.getvalue()
