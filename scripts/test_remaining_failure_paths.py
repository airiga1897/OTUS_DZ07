"""Remaining recovery guards use synthetic resources and disposable roots only."""

import http.client
import io
import json
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock, patch

import cloud_contract
import configuration
import local_runtime
import network_diagnostics
import placement_diagnostics as diagnostics
import pool_selection
import s3_artifacts
import selectel_adapter as adapter
import selectel_staged_audit
import staged_deploy
import terraform_plugins
import test_cycle as cycle
import test_cycle_safety as cycle_fixtures
import test_provider_postcheck_matrix as network_fixtures
import test_runtime_contract as runtime_fixtures
import test_staged_backend as staged_fixtures
import test_terraform_plugins as provider_fixtures
from cloud_contract import InfrastructureNode, StandDescription
from placement_diagnostics import PlacementFaultReport
from remote import verify as remote_verify
from runtime_ports import PlacementIdentity
from selectel_adapter import SelectelAdapter
from selectel_staged import Backend
from stage_store import StageStore
from terraform_state_types import StateEntry
from topology import Topology


def obj(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AssertionError("expected object")
    return cast(dict[str, object], value)


def rows(value: object) -> list[object]:
    if not isinstance(value, list):
        raise AssertionError("expected list")
    return cast(list[object], value)


class RecordedFailureError(RuntimeError):
    api_error_context: dict[str, object]
    failure_evidence: str


class ApiPort:
    def __init__(self, root: Path | None = None, prefix: str = "synthetic") -> None:
        base = root or Path(".")
        self.ROOT = base
        self.LOCAL = base / ".local"
        self.PREFIX = prefix
        self.TF = base / "terraform"
        self.RUN = base / "logs"

    def save(self, path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def redact(self, text: str) -> str:
        return text

    def desired_topology(self) -> Topology:
        return {"nodes": {}}


class PlacementProbe:
    def __init__(self, root: Path) -> None:
        self.ROOT = root
        self.LOCAL = root / ".local"
        self.PREFIX = "otus-dz07"

    def save(self, path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def redact(self, text: str) -> str:
        return text

    def placement(self) -> PlacementIdentity:
        return {"region": "ru-9"}

    def cloud(self) -> SelectelAdapter:
        raise AssertionError("cloud read is patched")


class RemainingFailurePaths(unittest.TestCase):
    def setUp(self) -> None:
        values: set[str] = set()
        secrets = patch("privacy._values", values)
        secrets.start()
        self.addCleanup(secrets.stop)

    def test_direct_lookup_permission_error_cannot_prove_resource_absence(self) -> None:
        backend = Backend.__new__(Backend)
        backend.journal = {"stages": {"app": {"candidate": {"region": "ru-9"}}}}
        cloud = Mock()
        cloud.get.side_effect = urllib.error.HTTPError(
            "https://example.invalid", 403, "Forbidden", http.client.HTTPMessage(), None
        )
        entry: StateEntry = {"type": "openstack_compute_instance_v2", "attrs": {"id": "own-id"}}
        with (
            patch.object(backend, "region", return_value=cloud),
            patch.object(selectel_staged_audit, "objects", return_value=[]),
            self.assertRaises(urllib.error.HTTPError),
        ):
            selectel_staged_audit.inventory(backend, "app", [entry])
        cloud.get.assert_called_once_with("compute", "/servers/own-id")

    def test_open_prints_kibana_for_a_saved_dedicated_kibana_node(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local, tf, state = runtime_fixtures.RuntimeContract().fixture(root, 5)
            value = obj(obj(obj(state["outputs"])["lab"])["value"])
            server_ids = obj(value["server_ids"])
            server_ids["kibana"] = "kibana-id"
            rows(obj(rows(state["resources"])[0])["instances"]).append(
                {
                    "index_key": "kibana",
                    "attributes": {
                        "id": "kibana-id",
                        "name": "otus-dz05-kibana",
                        "tenant_id": "project",
                        "region": "ru-9",
                    },
                }
            )
            (tf / "terraform.tfstate").write_text(json.dumps(state))
            saved = json.loads((local / "lab.json").read_text())
            saved["server_ids"] = value["server_ids"]
            (local / "lab.json").write_text(json.dumps(saved))
            with (
                patch("service_config.kibana_port", return_value=5601),
                patch("sys.stdout", new_callable=io.StringIO) as output,
            ):
                local_runtime.open_portal(root, 5, Mock(return_value=True))
            self.assertIn("Kibana: http://192.0.2.10:5601", output.getvalue())

    def test_invalid_node_role_is_rejected_before_address_processing(self) -> None:
        node: InfrastructureNode = {
            "id": "app",
            "roles": ["foreign"],
            "private_ip": "10.77.0.10",
            "volume_id": "volume",
            "port_id": "port",
        }
        value = cloud_contract.InfrastructureResult(
            "selectel",
            {"project_id": "own"},
            "otus-dz07",
            {"region": "ru-9", "zone": "ru-9a"},
            {"app": node},
            "10.77.0.0/24",
        )
        with self.assertRaisesRegex(ValueError, "Invalid node name or roles"):
            value.validate()

    def test_network_port_with_disabled_filtering_is_rejected(self) -> None:
        result, data = network_fixtures.ProviderPostcheckMatrix().network_fixture()

        def reply(service: str, path: str) -> object:
            return data[path]

        cloud = Mock()
        cloud.get.side_effect = reply
        obj(obj(data["/v2.0/ports/ep"])["port"])["port_security_enabled"] = False
        with self.assertRaisesRegex(RuntimeError, "filtering or identity"):
            adapter.check_network(cloud, result, ["8.8.8.8/32"])

    def test_s3_preparation_requires_verified_local_package_before_connecting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(s3_artifacts, "settings", return_value={}),
                patch.object(s3_artifacts, "package_lock", return_value={"package.deb": "a" * 128}),
                patch.object(s3_artifacts, "client") as connect,
                self.assertRaisesRegex(ValueError, "verified package is missing"),
            ):
                s3_artifacts.prepare(Path(directory))
            connect.assert_not_called()

    def test_saved_placement_overrides_preferences_and_saved_context_is_retained(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / ".local").mkdir()
            (root / "config/custom.json").write_text('{"pools":["ru-9","ru-3"]}')
            (root / ".local/context.json").write_text('{"placement":{"region":"ru-3"}}')
            self.assertEqual(pool_selection.policy(root, "custom.json", "context.json")["region"], "ru-3")
            (root / ".local/placement.json").write_text('{"region":"ru-8","availability_zone":"ru-8a"}')
            result = pool_selection.policy(root, "custom.json", "context.json")
            self.assertEqual((result["region"], result["availability_zone"]), ("ru-8", "ru-8a"))

    def test_cleanup_audit_retries_until_eventual_resource_absence(self) -> None:
        cloud = Mock()
        responses = iter([[{"name": "otus-dz07-app"}], [], [], []])
        with (
            patch.object(adapter, "OWNED_COLLECTIONS", [("compute", "/servers", "servers")]),
            patch.object(adapter, "collection", side_effect=lambda *args: next(responses)),
            patch("time.sleep") as sleep,
        ):
            self.assertEqual(adapter.audit_cleanup(cloud, "otus-dz07", set()), ([], 0))
        sleep.assert_called_once_with(3)

    def test_extension_access_denied_and_malformed_response_are_not_support(self) -> None:
        cloud = Mock()
        cloud.get.side_effect = urllib.error.HTTPError(
            "https://example.invalid", 403, "Forbidden", http.client.HTTPMessage(), None
        )
        with self.assertRaises(urllib.error.HTTPError):
            adapter._extensions(cloud, "network", "/v2.0/extensions")
        cloud.get.side_effect = None
        cloud.get.return_value = {"extensions": [{"alias": None}]}
        with self.assertRaisesRegex(ValueError, "Malformed"):
            adapter._extensions(cloud, "network", "/v2.0/extensions")

    def test_stage_recovery_requires_finishing_destroy_and_unambiguous_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            backend, _ = staged_fixtures.StagedBackendTests().backend(Path(directory))
            backend.journal["destroy_requested"] = True
            with self.assertRaisesRegex(ValueError, "finish destroy"):
                backend.validate_existing()
            backend.journal["stages"]["app"] = {"status": "deleting"}
            entries: list[StateEntry] = [{"type": "kind", "attrs": {"id": value}} for value in ("one", "two")]
            with patch.object(backend, "cleanup_attempt") as cleanup:
                backend.recover_attempt("app")
                cleanup.assert_called_once_with("app")
            with (
                patch.object(backend.store, "entries", return_value=entries),
                self.assertRaisesRegex(ValueError, "Ambiguous"),
            ):
                backend.saved_id("app", "kind", "id")

    def test_destroy_refuses_lost_stage_configuration_with_remaining_resources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            backend, _ = staged_fixtures.StagedBackendTests().backend(Path(directory))
            backend.journal["stages"]["app"] = {"status": "failed", "candidate": {"region": "ru-9"}}
            with (
                patch.object(backend.store, "folder", return_value=Path(directory) / "missing-stage"),
                patch.object(backend.store, "identities", return_value={"vm": "preserve"}),
                patch.object(backend, "save"),
                patch("selectel_staged_audit.assert_stage_owned"),
                self.assertRaisesRegex(ValueError, "configuration missing"),
            ):
                backend.cleanup_attempt("app")
            self.assertEqual(backend.journal["stages"]["app"]["status"], "deleting")

    def test_console_and_key_queries_use_the_saved_node_pool(self) -> None:
        backend = Backend.__new__(Backend)
        backend.api = ApiPort(prefix="otus-dz07")
        cloud = SelectelAdapter.__new__(SelectelAdapter)
        lab: StandDescription = {"node_regions": {"es1": "ru-3"}, "server_ids": {"es1": "saved-id"}}
        with (
            patch.object(backend, "region", return_value=cloud) as region,
            patch.object(cloud, "ssh_host_key", return_value="ssh-ed25519 key") as key,
            patch.object(cloud, "get", return_value={"output": "console evidence"}),
        ):
            self.assertEqual(backend.ssh_host_key(lab, "es1", None), "ssh-ed25519 key")
            self.assertEqual(backend.console_output(lab, "es1"), "console evidence")
            self.assertEqual([call.args for call in region.call_args_list], [("ru-3",), ("ru-3",)])
            key.assert_called_once_with("es1", "saved-id", "otus-dz07", None)

    def test_stage_provider_requirements_are_preserved_in_generated_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "module"
            source.mkdir()
            api = SimpleNamespace(ROOT=root, LOCAL=root / ".local")
            store = StageStore(api, {"stages": {}}, environment=lambda candidate: {}, cli_config=root / "cli.rc")
            providers = {"openstack": {"source": "terraform-provider-openstack/openstack", "version": "= 3.4.0"}}
            store.configure("app", source, {"name": "otus-dz07-app"}, providers=providers)
            config = json.loads((store.folder("app") / "main.tf.json").read_text())
            self.assertEqual(config["terraform"]["required_providers"], providers)
            self.assertEqual(config["module"]["stage"]["name"], "otus-dz07-app")

    def test_ipv4_summary_reads_only_address_evidence_and_handles_missing_checks(self) -> None:
        record = {
            "checks": {
                "addresses": {
                    "returncode": 0,
                    "stdout": json.dumps(
                        [
                            {
                                "addr_info": [
                                    {"family": "inet6", "local": "::1"},
                                    {"family": "inet", "local": "10.77.0.10"},
                                ]
                            }
                        ]
                    ),
                }
            }
        }
        self.assertTrue(network_diagnostics.summary(record, "10.77.0.10")["expected_private_ipv4_present"])
        self.assertFalse(network_diagnostics.summary(record, "10.77.0.11")["expected_private_ipv4_present"])
        self.assertIsNone(network_diagnostics.summary({}, "10.77.0.10")["expected_private_ipv4_present"])

    def test_remote_verifier_reexecutes_in_the_application_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "settings.json").write_text('{"lab":{"private_ips":{}},"passwords":{"app":"synthetic"}}')
            with (
                patch.object(remote_verify, "BASE", root),
                patch.object(remote_verify, "lab", {}),
                patch.object(remote_verify, "passwords", {}),
                patch("sys.prefix", "controller"),
                patch("os.execv") as execute,
            ):
                remote_verify.initialize()
                self.assertEqual(remote_verify.passwords["app"], "synthetic")
                execute.assert_called_once_with(
                    remote_verify.PYTHON, [remote_verify.PYTHON, remote_verify.__file__, *sys.argv[1:]]
                )

    def test_remote_verifier_detects_changed_committed_record(self) -> None:
        connection = Mock()
        connection.execute.return_value.fetchone.return_value = (7, "slug", "changed")
        with self.assertRaisesRegex(AssertionError, "absent or changed"):
            remote_verify.check_record(connection, {"id": 7, "slug": "slug", "body": "original"})
        connection.execute.assert_called_once_with("SELECT id,slug,body FROM knowledge_article WHERE id=%s", (7,))

    def test_remote_verifier_rejects_invalid_mode_before_writing_probe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(remote_verify, "initialize"),
                patch.object(remote_verify, "APP", Path(directory)),
                patch("sys.argv", ["verify.py", "invalid", "a" * 32]),
                self.assertRaisesRegex(ValueError, "Invalid verification context"),
            ):
                remote_verify.main()
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_no_placement_candidates_never_allocates_or_cleans_an_existing_node(self) -> None:
        backend = Mock(unsafe=True)
        backend.completed.return_value = False
        backend.pending.return_value = None
        backend.identities.return_value = {"es1": "preserved"}
        with self.assertRaisesRegex(staged_deploy.PlacementExhausted, "app"):
            staged_deploy.place(backend, "app", [])
        backend.begin.assert_not_called()
        backend.apply.assert_not_called()
        backend.cleanup_attempt.assert_not_called()
        backend.assert_preserved.assert_called_once_with({"es1": "preserved"})

    def test_configuration_resume_does_not_rewrite_inputs_or_connect_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            api = Mock(ROOT=Path(directory), LOCAL=Path(directory) / ".local")
            with patch.object(configuration, "resume", return_value=True):
                configuration._configure(api, existing=True)
        api.lab_output.assert_not_called()
        api.connect.assert_not_called()
        api.put.assert_not_called()

    def test_provider_download_success_still_requires_offline_lock_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            api = provider_fixtures.ProviderMirrorTests().fixture(Path(directory))
            results = [
                subprocess.CompletedProcess([], code, b"", message)
                for code, message in [(1, b"cache empty"), (0, b""), (1, b"lock mismatch")]
            ]
            with (
                patch("subprocess.run", side_effect=results) as run,
                self.assertRaises(cloud_contract.TerraformError) as caught,
            ):
                terraform_plugins.prepare(api)
            self.assertEqual(caught.exception.output, "lock mismatch")
            self.assertNotIn("TF_PLUGIN_CACHE_DIR", run.call_args.kwargs["env"])
            self.assertEqual(list((api.ROOT / ".tools").glob("provider-check-*")), [])

    def test_partial_state_without_segment_is_not_a_valid_cleanup_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in (".local/context.json", ".local/placement.json", "terraform-selectel/terraform.tfvars.json"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}")
            with self.assertRaisesRegex(ValueError, "no segment"):
                adapter.partial_state_binding(root, {"region": "ru-9"}, "otus-dz07")

    def test_managed_state_rejects_project_and_pool_independently(self) -> None:
        for attrs, expected in (({"tenant_id": "foreign"}, "project"), ({"region": "ru-3"}, "pool")):
            state = {
                "resources": [
                    {"mode": "managed", "type": "openstack_compute_instance_v2", "instances": [{"attributes": attrs}]}
                ]
            }
            with self.subTest(attrs=attrs), self.assertRaisesRegex(ValueError, expected):
                adapter.validate_managed_state(state, {"project_id": "own", "region": "ru-9"}, "otus-dz07")

    def test_power_transition_timeout_never_reports_success(self) -> None:
        cloud = adapter.SelectelAdapter.__new__(adapter.SelectelAdapter)
        cloud.config = {"project_id": "own"}
        with (
            patch.object(
                cloud,
                "get",
                return_value={"server": {"name": "otus-dz07-es1", "tenant_id": "own", "status": "BUILD"}},
            ) as get,
            patch("time.sleep"),
            self.assertRaisesRegex(RuntimeError, "unconfirmed"),
        ):
            cloud.set_power("es1", "id", running=True, prefix="otus-dz07")
        self.assertEqual(get.call_count, 62)

    def test_cached_host_key_is_returned_only_after_identity_check(self) -> None:
        cloud = adapter.SelectelAdapter.__new__(adapter.SelectelAdapter)
        with patch.object(cloud, "get", return_value={"server": {"name": "otus-dz07-app"}}) as get:
            self.assertEqual(cloud.ssh_host_key("app", "id", "otus-dz07", "ssh-ed25519 cached"), "ssh-ed25519 cached")
            get.assert_called_once()
            get.return_value = {"server": {"name": "foreign-app"}}
            with self.assertRaises(ValueError):
                cloud.ssh_host_key("app", "id", "otus-dz07", "ssh-ed25519 cached")

    def test_placement_fault_lookup_error_is_saved_without_exception_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "terraform-selectel/terraform.tfstate"
            state.parent.mkdir()
            state.write_text(
                json.dumps(
                    {
                        "resources": [
                            {
                                "mode": "managed",
                                "type": "openstack_compute_instance_v2",
                                "instances": [{"index_key": "app"}],
                            }
                        ]
                    }
                )
            )

            api = PlacementProbe(root)
            with patch.object(diagnostics, "collect_nodes", side_effect=OSError("PRIVATE-SECRET")):
                report = json.loads(diagnostics.capture(api, RuntimeError()).read_text())
            self.assertEqual(report["provider_error"]["type"], "OSError")
            self.assertNotIn("PRIVATE", json.dumps(report))

    def test_missing_server_attributes_are_recorded_per_node(self) -> None:
        cloud = SelectelAdapter.__new__(SelectelAdapter)
        cloud.config = {"project_id": "own"}
        report: PlacementFaultReport = {
            "region": "ru-9",
            "availability_zone": None,
            "error_type": "RuntimeError",
            "nodes": {},
            "read_only": True,
        }
        api = PlacementProbe(Path("."))
        instances: list[object] = [{"index_key": "app", "attributes": {"name": "otus-dz07-app", "id": "id"}}]
        with (
            patch.object(cloud, "get", side_effect=KeyError("PRIVATE")),
            patch.object(api, "cloud", return_value=cloud),
        ):
            diagnostics.collect_nodes(api, instances, report)
        self.assertEqual(report["nodes"]["app"]["diagnostic_error"], "KeyError")
        self.assertNotIn("PRIVATE", json.dumps(report))

    def test_cycle_secondary_local_guards_remain_read_only(self) -> None:
        for mutation, message in (
            ("managed", "empty stand"),
            ("state", "nonempty state"),
            ("pending", "existing context"),
        ):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                api = cycle_fixtures.CycleSafetyTests().fixture(Path(directory))
                if mutation == "managed":
                    api.managed_ids.return_value = {"app": "id"}
                elif mutation == "state":
                    (api.ROOT / "terraform-selectel/terraform.tfstate").write_text('{"outputs":{"x":1}}')
                else:
                    (api.LOCAL / "context.json").write_text("{}")
                with patch.object(cycle, "assert_empty"), self.assertRaisesRegex(RuntimeError, message):
                    cycle.reject_existing(api)
                api.cloud.assert_not_called()
                api.cleanup.assert_not_called()

    def test_cycle_attaches_only_structured_error_evidence_before_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            api = cycle_fixtures.CycleSafetyTests().fixture(Path(directory))
            error = RecordedFailureError("PRIVATE")
            error.api_error_context = {"service": "network", "http_status": 409}
            error.failure_evidence = "docs/fault.json"
            with (
                patch.object(cycle, "reject_existing"),
                patch.object(cycle, "stages", side_effect=error),
                self.assertRaises(RuntimeError) as caught,
            ):
                cycle.run(api)
            self.assertIs(caught.exception, error)
            report = json.loads((api.ROOT / "docs/test-cycle-result.json").read_text())
            self.assertEqual(report["api_error_context"]["http_status"], 409)
            self.assertEqual(report["failure_evidence"], "docs/fault.json")
            self.assertNotIn("PRIVATE", json.dumps(report))

    def test_cleanup_failure_links_evidence_and_keeps_recovery_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            api = cycle_fixtures.CycleSafetyTests().fixture(Path(directory))
            error = RecordedFailureError("PRIVATE")
            error.failure_evidence = "docs/cleanup-fault.json"
            api.cleanup.side_effect = error
            with (
                patch.object(cycle, "reject_existing"),
                patch.object(cycle, "stages", return_value={}),
                self.assertRaises(RuntimeError),
            ):
                cycle.run(api)
            report = json.loads((api.ROOT / "docs/test-cycle-result.json").read_text())
            self.assertEqual(report["cleanup_failure_evidence"], error.failure_evidence)
            self.assertFalse(report["state_and_api_cleanup_verified"])
            self.assertEqual(json.loads((api.LOCAL / "test-cycle.json").read_text())["status"], "cleanup_failed")
