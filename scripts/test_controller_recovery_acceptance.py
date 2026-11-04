"""Exercise retained legacy-controller paths with real files and synthetic transports."""

import json
import unittest
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import patch

import test_controller_boundaries as fixtures
from cloud_contract import InfrastructureResult
from test_cursor_coverage import entrypoint_code
from test_runtime_support import lab


class ControllerRecoveryAcceptance(unittest.TestCase):
    def setUp(self) -> None:
        helper = fixtures.ControllerBoundaries()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        self.root, self.local, self.config = helper.root, helper.local, helper.config
        self.tfroot = self.root / "terraform-selectel"

    def result(self) -> InfrastructureResult:
        return InfrastructureResult(
            "selectel",
            {"project_id": "synthetic"},
            lab.PREFIX,
            {"region": "ru-9", "zone": "ru-9a"},
            {
                "app": {
                    "roles": ["app"],
                    "id": "app-id",
                    "private_ip": "10.77.0.10",
                    "public_ip": "8.8.8.8",
                    "volume_id": "disk",
                    "port_id": "port",
                }
            },
            "10.77.0.0/24",
        )

    def test_checked_plan_limits_route_changes_to_confirmed_rules(self) -> None:
        cidr = "8.8.8.8/32"
        rule = {
            "mode": "managed",
            "type": "openstack_networking_secgroup_rule_v2",
            "name": "ssh",
            "index": cidr,
            "address": "rule.ssh",
            "change": {
                "actions": ["create"],
                "after": {
                    "security_group_id": "owned-sg",
                    "direction": "ingress",
                    "ethertype": "IPv4",
                    "protocol": "tcp",
                    "port_range_min": 22,
                    "port_range_max": 22,
                    "remote_ip_prefix": cidr,
                },
            },
        }
        data = {
            "resource_changes": [{"mode": "data"}, rule],
            "planned_values": {"root_module": {"resources": [{"mode": "data"}, {"mode": "managed", "values": {}}]}},
        }
        identities = {("openstack_networking_secgroup_v2", "lab", ""): "owned-sg"}
        with (
            patch.object(lab, "managed_ids", return_value=identities),
            patch.object(lab, "tf", side_effect=lambda op, *a, **kw: json.dumps(data) if op == "show" else ""),
        ):
            self.assertEqual(lab.checked_plan(access_cidr=cidr), self.local / "create.tfplan")
            rule["change"]["actions"] = ["update"]
            with self.assertRaises(ValueError):
                lab.checked_plan(access_cidr=cidr)
            rule["change"]["actions"] = ["create"]
            rule["change"]["after"]["port_range_max"] = 443
            with self.assertRaises(ValueError):
                lab.checked_plan(access_cidr=cidr)

    def test_plan_extension_only_allows_resource_triplet_of_new_nodes(self) -> None:
        change = {
            "mode": "managed",
            "address": "node.es2",
            "type": "openstack_compute_instance_v2",
            "name": "node",
            "index": "es2",
            "change": {"actions": ["create"]},
        }
        data = {"resource_changes": [change]}
        with (
            patch.object(lab, "managed_ids", return_value={}),
            patch.object(lab, "tf", side_effect=lambda op, *a, **kw: json.dumps(data) if op == "show" else ""),
        ):
            lab.checked_plan(allowed_new_nodes={"es2"})
            change["index"] = "es1"
            with self.assertRaises(ValueError):
                lab.checked_plan(allowed_new_nodes={"es2"})
            change["change"]["actions"] = ["delete"]
            self.assertEqual(lab.checked_plan(destroy=True), self.local / "destroy.tfplan")

    def test_legacy_output_binding_and_ids_ignore_data_and_empty_instances(self) -> None:
        result = self.result()
        (self.local / "context.json").write_text(json.dumps(result.binding()))
        with patch.object(lab, "tf", return_value=json.dumps(asdict(result))):
            self.assertEqual(lab.infrastructure_output(), result)
            self.assertEqual(lab.lab_output()["site_url"], "http://8.8.8.8")
        state = {
            "resources": [
                {"mode": "data"},
                {"mode": "managed", "type": "vm", "name": "empty", "instances": []},
                {
                    "mode": "managed",
                    "type": "vm",
                    "name": "app",
                    "instances": [{"attributes": {"id": "owned", "name": lab.PREFIX}}],
                },
            ]
        }
        (self.tfroot / "terraform.tfstate").write_text(json.dumps(state))
        self.assertEqual(lab.managed_ids(), {("vm", "app", ""): "owned"})

    def legacy_cleanup(self, *, remaining=(), remaining_state=False, recovery=False, clean_local=True) -> None:
        result = self.result()
        (self.local / "context.json").write_text(json.dumps(result.binding()))
        if recovery:
            (self.local / "cleanup-fips.json").write_text(json.dumps({**result.binding(), "ids": ["owned-fip"]}))
        else:
            state = {
                "resources": [
                    {
                        "mode": "managed",
                        "type": "openstack_networking_floatingip_v2",
                        "instances": [{"attributes": {"id": "owned-fip"}}],
                    }
                ]
            }
            (self.tfroot / "terraform.tfstate").write_text(json.dumps(state))
        key = self.local / "key"
        key.write_text("synthetic recovery key")

        def tf(op, *args, **kwargs):
            if op == "apply":
                (self.tfroot / "terraform.tfstate").write_text("{}")
            return (
                json.dumps({"values": {"root_module": {"resources": [{"mode": "managed"}] if remaining_state else []}}})
                if op == "show"
                else ""
            )

        cloud = SimpleNamespace(config={"region": "ru-9"})
        with (
            patch("cloud_settings.sequential_enabled", return_value=False),
            patch.object(lab, "auth_config", return_value=self.config),
            patch("local_runtime.validate_state"),
            patch.object(lab.pools, "phase"),
            patch.object(lab, "cloud", return_value=cloud),
            patch.object(lab, "checked_plan", return_value=self.local / "destroy.tfplan"),
            patch.object(lab, "tf", side_effect=tf),
            patch("selectel_adapter.audit_cleanup", return_value=(list(remaining), 0)) as audit,
            patch("artifact_lifecycle.release") as release,
        ):
            if remaining or remaining_state:
                with self.assertRaises(RuntimeError):
                    lab.cleanup(clean_local)
                self.assertTrue(key.exists())
                release.assert_not_called()
            else:
                lab.cleanup(clean_local)
                self.assertEqual(key.exists(), not clean_local)
                self.assertEqual(release.call_count, int(clean_local))
            if remaining_state:
                audit.assert_not_called()
            else:
                self.assertEqual(audit.call_args.args[-1], {"owned-fip"})

    def test_cleanup_records_and_recovers_own_fips_before_removing_private_files(self) -> None:
        self.legacy_cleanup()

    def test_cleanup_reuses_bound_fips_when_state_outputs_are_already_gone(self) -> None:
        self.legacy_cleanup(recovery=True, clean_local=False)
        self.assertTrue((self.local / "cleanup-fips.json").exists())

    def test_remaining_state_or_api_objects_preserve_recovery_data(self) -> None:
        self.legacy_cleanup(remaining_state=True)
        self.legacy_cleanup(remaining=["owned-resource"])

    def test_foreign_recovery_scope_is_rejected_before_apply(self) -> None:
        binding = self.result().binding()
        (self.local / "context.json").write_text(json.dumps(binding))
        (self.local / "cleanup-fips.json").write_text(
            json.dumps({**binding, "scope": {"project_id": "foreign"}, "ids": ["foreign-fip"]})
        )
        with (
            patch("cloud_settings.sequential_enabled", return_value=False),
            patch.object(lab, "auth_config", return_value=self.config),
            patch("local_runtime.validate_state"),
            patch.object(lab.pools, "phase"),
            patch.object(lab, "tf") as tf,
            self.assertRaises(ValueError),
        ):
            lab.cleanup()
        tf.assert_not_called()

    def test_delegations_and_entrypoint_do_not_start_unrequested_work(self) -> None:
        with (
            patch("selectel_adapter.preflight") as preflight,
            patch("verification.check_placement") as placement,
            patch("verification.verify") as verify,
            patch.object(lab, "main") as main,
        ):
            lab.preflight()
            lab.check_placement({"synthetic": True})
            lab.verify(False)
            exec(entrypoint_code(lab), {**vars(lab), "__name__": "__main__"})
        preflight.assert_called_once_with(lab)
        placement.assert_called_once_with(lab, {"synthetic": True})
        verify.assert_called_once_with(lab, False)
        main.assert_called_once_with()
        with (
            patch.object(lab, "lab_output", side_effect=ValueError("missing stand")),
            patch("builtins.print") as output,
        ):
            lab.diagnostics()
        self.assertIn("diagnostics unavailable", str(output.call_args).lower())
