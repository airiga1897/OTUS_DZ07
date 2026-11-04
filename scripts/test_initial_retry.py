"""Initial-placement retries and partial-apply ownership, using isolated roots."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import pool_selection as pools
from cloud_contract import TerraformError
from local_runtime import validate_state
from selectel_adapter import binding
from test_runtime_support import lab

ROOT = Path(__file__).resolve().parents[1]
FAULT = "Exceeded maximum number of retries. Exhausted all retry attempts for shelving preemptible instances to build instance synthetic-vm."


class InitialRetryTests(unittest.TestCase):
    def test_first_deploy_advances_only_after_state_and_api_cleanup(self) -> None:
        for api_failure in (False, True):
            with self.subTest(api_failure=api_failure), tempfile.TemporaryDirectory(dir=ROOT / ".tools") as directory:
                root = Path(directory)
                (root / ".local").mkdir()
                identities, events = {}, []

                def apply(api, desired, allowed_new_nodes=None, *, events=events, identities=identities) -> None:
                    events.append("apply")
                    identities["node"] = "owned"
                    if events.count("apply") == 1:
                        raise TerraformError("NoValidHost")

                def cleanup(clean_local, *, api_failure=api_failure, events=events, identities=identities) -> None:
                    self.assertFalse(clean_local)
                    events.append("state-empty")
                    if api_failure:
                        raise RuntimeError("API audit failed")
                    events.append("api-empty")
                    identities.clear()

                with (
                    patch.multiple(lab, ROOT=root, LOCAL=root / ".local", _TEST_CYCLE_ID=None),
                    patch.object(lab, "managed_ids", side_effect=lambda *, identities=identities: dict(identities)),
                    patch.object(lab.pools, "selected", return_value=None),
                    patch.object(lab, "deployment_candidates", return_value=[1, 2]),
                    patch.object(
                        lab,
                        "select_candidate",
                        side_effect=lambda choice, *, events=events: events.append("select-" + str(choice)),
                    ),
                    patch.object(lab, "prepare"),
                    patch.object(lab, "desired_topology", return_value={}),
                    patch("infrastructure.apply", side_effect=apply),
                    patch.object(lab, "cleanup", side_effect=cleanup),
                    patch.object(lab, "configure"),
                    patch.object(lab, "save"),
                    patch.object(lab.pools, "phase"),
                    patch.object(lab, "placement", return_value={"region": "ru-7", "availability_zone": "ru-7a"}),
                ):
                    if api_failure:
                        with self.assertRaisesRegex(RuntimeError, "API audit"):
                            lab.deploy_selected()
                        self.assertNotIn("select-2", events)
                    else:
                        lab.deploy_selected()
                        self.assertLess(events.index("state-empty"), events.index("api-empty"))
                        self.assertLess(events.index("api-empty"), events.index("select-2"))
                        self.assertEqual(events.count("apply"), 2)

    def fixture(self, root):
        local = root / ".local"
        local.mkdir()
        (root / "terraform-selectel").mkdir()
        config = {"project_id": "synthetic-project", "region": "ru-7"}
        context = binding(config, "otus-dz07", "ru-7a")
        files = {
            "context.json": context,
            "placement.json": {
                "provider": "selectel",
                "namespace": "otus-dz07",
                "project_id": config["project_id"],
                "region": "ru-7",
                "availability_zone": "ru-7a",
                "phase": "creating",
            },
        }
        for name, value in files.items():
            (local / name).write_text(json.dumps(value))
        attrs = {"id": "synthetic-vm", "name": "otus-dz07-db1", "tenant_id": config["project_id"], "region": "ru-7"}
        state = {
            "resources": [
                {
                    "mode": "managed",
                    "type": "openstack_compute_instance_v2",
                    "name": "node",
                    "provider": 'provider["registry.terraform.io/terraform-provider-openstack/openstack"]',
                    "instances": [{"index_key": "db1", "attributes": attrs}],
                }
            ]
        }
        (root / "terraform-selectel/terraform.tfstate").write_text(json.dumps(state))
        variables = {"region": "ru-7", "availability_zone": "ru-7a", "name_prefix": "otus-dz07"}
        (root / "terraform-selectel/terraform.tfvars.json").write_text(json.dumps(variables))
        return config, state

    def test_partial_apply_can_be_validated_without_outputs_and_without_writing_state(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tools") as directory:
            root = Path(directory)
            config, _ = self.fixture(root)
            path = root / "terraform-selectel/terraform.tfstate"
            original = path.read_bytes()
            validate_state(root, config, "otus-dz07")
            self.assertEqual(path.read_bytes(), original)

    def test_destroy_partial_apply_runs_only_mock_terraform_and_mock_api_then_cleans_own_root(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tools") as directory:
            root = Path(directory)
            config, _ = self.fixture(root)
            (root / ".local/key").write_text("synthetic")
            path = root / "terraform-selectel/terraform.tfstate"

            def terraform(*args, **kwargs):
                if args[0] == "apply":
                    path.write_text(json.dumps({"resources": []}))
                return "{}"

            with (
                patch.multiple(lab, ROOT=root, LOCAL=root / ".local"),
                patch.object(lab, "auth_config", return_value=config),
                patch.object(lab, "checked_plan"),
                patch.object(lab, "tf", side_effect=terraform) as tf,
                patch.object(lab, "cloud") as cloud,
                patch("selectel_adapter.audit_cleanup", return_value=([], 0)) as audit,
            ):
                cloud.return_value.config = config
                lab.cleanup()
                audit.assert_called_once()
                self.assertEqual(tf.call_args_list[0].args[0], "apply")
            self.assertFalse(path.exists())
            self.assertFalse((root / ".local/key").exists())

    def test_partial_apply_rejects_missing_or_conflicting_metadata_and_foreign_state(self) -> None:
        for mutation in (
            "missing_context",
            "foreign_scope",
            "wrong_zone",
            "wrong_namespace",
            "foreign_provider",
            "foreign_tenant",
        ):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(dir=ROOT / ".tools") as directory:
                root = Path(directory)
                config, state = self.fixture(root)
                path = root / ".local/context.json"
                context = json.loads(path.read_text())
                if mutation == "missing_context":
                    path.unlink()
                elif mutation == "foreign_scope":
                    context["scope"]["project_id"] = "other"
                    path.write_text(json.dumps(context))
                elif mutation == "wrong_zone":
                    context["placement"]["zone"] = "ru-7b"
                    path.write_text(json.dumps(context))
                elif mutation == "wrong_namespace":
                    context["namespace"] = "otus-dz05"
                    path.write_text(json.dumps(context))
                elif mutation == "foreign_provider":
                    state["resources"][0]["type"] = "other_instance"
                else:
                    state["resources"][0]["instances"][0]["attributes"]["tenant_id"] = "other"
                (root / "terraform-selectel/terraform.tfstate").write_text(json.dumps(state))
                with self.assertRaises(ValueError):
                    validate_state(root, config, "otus-dz07")

    def test_selectel_scheduling_fault_is_retryable_but_generic_retry_error_is_not(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tools") as directory:
            root = Path(directory)
            config, _ = self.fixture(root)
            cloud = Mock()
            for message, expected in [
                (FAULT, True),
                ("Exceeded maximum number of retries.", False),
                ("Network creation failed", False),
            ]:
                cloud.get.return_value = {
                    "server": {"name": "otus-dz07-db1", "status": "ERROR", "fault": {"message": message}}
                }
                self.assertEqual(
                    pools.scheduler_unavailable(
                        TerraformError("unexpected state ERROR"), root, lambda: cloud, "otus-dz07"
                    ),
                    expected,
                )

    def test_initial_capacity_failure_cleans_before_retry_and_cleanup_failure_stops(self) -> None:
        for failed_cleanup in (False, True):
            with (
                self.subTest(failed_cleanup=failed_cleanup),
                tempfile.TemporaryDirectory(dir=ROOT / ".tools") as directory,
            ):
                root = Path(directory)
                (root / ".local").mkdir()
                with (
                    patch.multiple(lab, ROOT=root, LOCAL=root / ".local", _TEST_CYCLE_ID=None),
                    patch.object(lab, "managed_ids", return_value={}),
                    patch.object(lab.pools, "selected", return_value=None),
                    patch.object(lab, "desired_topology", return_value={}),
                    patch("infrastructure.apply", side_effect=TerraformError("NoValidHost")),
                    patch.object(lab.pools, "scheduler_unavailable", return_value=True),
                    patch.object(lab, "placement", return_value={"region": "ru-7", "availability_zone": "ru-7a"}),
                    patch.object(lab, "save"),
                    patch.object(lab, "diagnostics"),
                    patch("placement_diagnostics.preserve") as fault,
                    patch.object(
                        lab, "cleanup", side_effect=RuntimeError("API audit failed") if failed_cleanup else None
                    ) as cleanup,
                ):
                    calls = Mock()
                    calls.attach_mock(fault, "fault")
                    calls.attach_mock(cleanup, "cleanup")
                    with self.assertRaises(RuntimeError if failed_cleanup else pools.CapacityRetry):
                        lab.deploy(initial_creation=True)
                    cleanup.assert_called_once_with(clean_local=False)
                    self.assertEqual([call[0] for call in calls.mock_calls], ["fault", "cleanup"])

    def test_existing_resources_never_cleaned_even_if_initial_flag_is_passed(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tools") as directory:
            root = Path(directory)
            (root / ".local").mkdir()
            with (
                patch.multiple(lab, ROOT=root, LOCAL=root / ".local"),
                patch.object(lab, "managed_ids", return_value={"owned": "vm"}),
                patch.object(lab.pools, "selected", return_value={"phase": "creating"}),
                patch.object(lab, "desired_topology", return_value={}),
                patch("infrastructure.apply", side_effect=TerraformError("NoValidHost")),
                patch.object(lab, "diagnostics"),
                patch.object(lab, "cleanup") as cleanup,
            ):
                with self.assertRaises(TerraformError):
                    lab.deploy(initial_creation=True)
                cleanup.assert_not_called()

    def test_initial_bootstrap_failure_retries_only_after_cleanup_and_preserves_existing(self) -> None:
        from network_diagnostics import BootstrapNetworkError

        for existing, failed_cleanup in [(False, False), (False, True), (True, False)]:
            with (
                self.subTest(existing=existing, failed_cleanup=failed_cleanup),
                tempfile.TemporaryDirectory(dir=ROOT / ".tools") as directory,
            ):
                root = Path(directory)
                (root / ".local").mkdir()
                failure = BootstrapNetworkError("IPv4 bootstrap failed")
                with (
                    patch.multiple(lab, ROOT=root, LOCAL=root / ".local", _TEST_CYCLE_ID=None),
                    patch.object(lab, "managed_ids", return_value={"own": "vm"} if existing else {}),
                    patch.object(lab.pools, "selected", return_value={"phase": "creating"}),
                    patch.object(lab, "desired_topology", return_value={}),
                    patch("infrastructure.apply", side_effect=failure),
                    patch.object(lab, "placement", return_value={"region": "ru-9", "availability_zone": "ru-9a"}),
                    patch.object(lab, "save"),
                    patch.object(lab, "diagnostics"),
                    patch.object(
                        lab, "cleanup", side_effect=RuntimeError("API audit failed") if failed_cleanup else None
                    ) as cleanup,
                ):
                    expected = (
                        BootstrapNetworkError if existing else RuntimeError if failed_cleanup else pools.CapacityRetry
                    )
                    with self.assertRaises(expected):
                        lab.deploy(initial_creation=True)
                    if existing:
                        cleanup.assert_not_called()
                    else:
                        cleanup.assert_called_once_with(clean_local=False)
