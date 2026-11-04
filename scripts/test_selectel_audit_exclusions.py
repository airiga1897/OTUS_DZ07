"""Explicit audit scope is separate from deployment scope and saved resources."""

import json
import unittest
import urllib.error

import selectel_adapter as adapter
import selectel_staged as staged
import test_empty_scope_audit as fixtures


class AuditExclusions(unittest.TestCase):
    def setUp(self) -> None:
        helper = fixtures.EmptyScopeAuditContracts()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        self.h = helper
        (helper.root / "config").mkdir()
        self.path = helper.root / "config/cloud.json"
        self.path.write_text(json.dumps({"audit": {"service_exclusions": {"volumev3": ["ru-4", "ru-5"]}}}))

    def test_expanded_audit_checks_vm_network_global_and_records_explicit_exclusions(self) -> None:
        h = self.h
        h.cloud.resource_regions.return_value = ["ru-4", "ru-5"]
        del h.regional.endpoints["volumev3"]
        with self.assertLogs("otus_dz07", "INFO") as logs:
            report = staged.audit_empty_scope(h.api)
        self.assertEqual(report["service_exclusions"], {"ru-4": ["volumev3"], "ru-5": ["volumev3"]})
        self.assertTrue(report["audit_complete_within_policy"])
        self.assertIn("отсутствие ресурсов этого типа не подтверждается", "\n".join(logs.output))
        calls = [c.args[:2] for c in h.regional.get.call_args_list]
        self.assertIn(("compute", "/servers/detail?limit=200"), calls)
        self.assertIn(("network", "/v2.0/ports?limit=200"), calls)
        self.assertEqual(h.global_api.listing.call_count, 3)

    def test_published_disk_api_is_checked_even_in_an_excluded_pool(self) -> None:
        h = self.h
        h.cloud.resource_regions.return_value = ["ru-4"]
        self.assertEqual(staged.audit_empty_scope(h.api)["service_exclusions"], {})
        h.regional.get.assert_any_call("volumev3", "/volumes/detail?limit=200")
        h.regional.get.side_effect = urllib.error.HTTPError("https://synthetic.invalid", 403, "denied", {}, None)
        with self.assertRaises(urllib.error.HTTPError):
            staged.audit_empty_scope(h.api)

    def test_saved_scope_never_uses_exclusions_and_nonempty_state_requires_saved_cleanup(self) -> None:
        h = self.h
        (h.local / "context.json").write_text(
            json.dumps({"project_id": "synthetic", "namespace": "otus-dz07", "region": "ru-4"})
        )
        del h.regional.endpoints["volumev3"]
        with self.assertRaisesRegex(ValueError, "volumev3"):
            staged.audit_empty_scope(h.api)
        (h.root / "terraform-selectel/terraform.tfstate").write_text(
            json.dumps(
                {
                    "resources": [
                        {
                            "mode": "managed",
                            "type": "openstack_blockstorage_volume_v3",
                            "instances": [{"attributes": {"id": "recorded"}}],
                        }
                    ]
                }
            )
        )
        h.cloud.read_only_pool.reset_mock()
        with self.assertRaisesRegex(ValueError, "Non-empty state"):
            staged.audit_empty_scope(h.api)
        h.cloud.read_only_pool.assert_not_called()

    def test_other_missing_services_and_unknown_pools_remain_incomplete(self) -> None:
        h = self.h
        h.cloud.resource_regions.return_value = ["ru-4"]
        del h.regional.endpoints["compute"]
        with self.assertRaisesRegex(ValueError, "compute"):
            staged.audit_empty_scope(h.api)
        h.regional.endpoints["compute"] = "https://synthetic.invalid/compute"
        del h.regional.endpoints["volumev3"]
        h.cloud.resource_regions.return_value = ["ru-9"]
        with self.assertRaisesRegex(ValueError, "volumev3"):
            staged.audit_empty_scope(h.api)

    def test_forced_recovery_search_never_excludes_recorded_resource_kinds(self) -> None:
        h = self.h
        h.cloud.resource_regions.return_value = ["ru-4"]
        del h.regional.endpoints["volumev3"]
        with self.assertRaisesRegex(ValueError, "volumev3"):
            staged.audit_empty_scope(h.api, expanded=True)

    def test_invalid_policy_never_silently_expands_exclusions(self) -> None:
        for value in (
            [],
            {"network": ["ru-4"]},
            {"volumev3": None},
            {"volumev3": ["ru-4", "ru-4"]},
            {"volumev3": [None]},
            {"volumev3": [""]},
            {"volumev3": [1]},
        ):
            with self.subTest(value=value):
                self.path.write_text(json.dumps({"audit": {"service_exclusions": value}}))
                with self.assertRaises(ValueError):
                    adapter.audit_service_exclusions(self.h.root)
        self.path.write_text('{"audit":{"service_exclusions":{"volumev3":[]}}}')
        self.assertEqual(adapter.audit_service_exclusions(self.h.root), {})
