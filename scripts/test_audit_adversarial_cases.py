"""Ownership and absence assertions cannot accept foreign objects or malformed diagnostics."""

import copy
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import selectel_staged_audit as audit
import test_network_policy_acceptance as networks
import test_provider_audit as fixtures


class AuditAdversarialCases(unittest.TestCase):
    def test_namespace_checks_exact_prefix_suffix_and_floating_description(self) -> None:
        backend = SimpleNamespace(api=SimpleNamespace(PREFIX="otus-dz07"), region=Mock())
        for item, path in (
            ({"name": "otus-dz07"}, "/servers/detail"),
            ({"name": "otus-dz07-es1"}, "/volumes/detail"),
            ({"description": "otus-dz07-owned"}, "/v2.0/floatingips"),
        ):

            def objects(cloud, service, url, key, *, item=item, path=path):
                return [item] if url == path else []

            with (
                self.subTest(item=item),
                patch.object(audit, "objects", side_effect=objects),
                self.assertRaises(RuntimeError),
            ):
                audit.audit_namespace(backend, "ru-9")
        with patch.object(
            audit, "objects", return_value=[{"name": "otus-dz070-foreign"}, {"name": "another-project"}]
        ) as objects:
            audit.audit_namespace(backend, "ru-9")
        self.assertEqual(objects.call_count, len(audit.COLLECTIONS))

    def test_relationship_entries_are_audited_through_parent_objects(self) -> None:
        backend, entry, item = fixtures.ProviderAuditTests().backend()
        entries = [{"type": kind, "attrs": {"id": "relationship"}} for kind in audit.RELATIONSHIPS]
        backend.store.entries = lambda name: entries
        self.assertEqual(audit.inventory(backend, "es1", entries), {})
        audit.assert_stage_owned(backend, "es1")
        backend.journal["stages"]["es1"]["audit_entries"] = entries
        audit.assert_stage_absent(backend, "es1")

    def test_global_attachment_and_vm_membership_refuse_foreign_scope(self) -> None:
        backend, entry, item = fixtures.ProviderAuditTests().backend("selectel_global_router_vpc_network_v1")
        item["project_id"] = "foreign"
        with (
            patch.object(audit, "inventory", return_value={entry["type"]: {"owned": item}}),
            self.assertRaisesRegex(ValueError, "project mismatch"),
        ):
            audit.assert_stage_owned(backend, "es1")
        backend, entry, item = fixtures.ProviderAuditTests().backend("openstack_compute_servergroup_v2")
        for members in (None, "owned", ["foreign"]):
            item["members"] = members
            with (
                patch.object(audit, "inventory", return_value={entry["type"]: {"owned": item}}),
                self.assertRaises(ValueError),
            ):
                audit.assert_stage_owned(backend, "es1")

    def test_malformed_global_diagnostics_are_marked_unknown_without_raw_data(self) -> None:
        kind = "selectel_global_router_router_v1"
        backend = SimpleNamespace(
            owner="owner",
            store=SimpleNamespace(entries=lambda name: [{"type": kind, "attrs": {"id": "owned"}}]),
            global_router=Mock(),
        )
        for response in (
            None,
            [],
            {"id": "foreign", "tags": ["owner"]},
            {"id": "owned", "tags": [], "private": "synthetic-secret"},
        ):
            backend.global_router.get.return_value = response
            with patch.object(audit, "assert_stage_owned"):
                result = audit.faults(backend, "global")
            self.assertEqual(result["global_router"][kind]["diagnostic_error"], "ValueError")
            self.assertNotIn("synthetic-secret", str(result))

    def test_compute_fault_requires_name_and_project_and_skips_unrelated_entries(self) -> None:
        backend, entry, item = fixtures.ProviderAuditTests().backend()
        backend.api = SimpleNamespace(PREFIX="otus-dz07")
        backend.store.entries = lambda name: [{"type": "openstack_networking_port_v2", "attrs": {"id": "port"}}, entry]
        cloud = backend.region.return_value
        for field in ("name", "tenant_id"):
            value = copy.deepcopy(item)
            value[field] = "foreign"
            cloud.get.return_value = {"server": value}
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "ownership mismatch"):
                audit.faults(backend, "es1")
        cloud.get.return_value = {"server": item}
        result = audit.faults(backend, "es1")
        self.assertEqual(result["nodes"]["es1"]["status"], "ACTIVE")
        self.assertNotIn("metadata", result["nodes"]["es1"])

    def test_deleted_stage_does_not_require_live_network_postcheck(self) -> None:
        backend, data, pages = networks.fixture()
        backend.journal["stages"]["old-network"] = {"status": "deleted"}
        self.assertTrue(audit.verify_networks(backend)["success"])

    def test_later_collection_objects_do_not_trigger_direct_get(self) -> None:
        backend, entry, item = fixtures.ProviderAuditTests().backend()
        backend.region.return_value.get.return_value = {"servers": [item]}
        found = audit.inventory(backend, "es1", [entry])
        self.assertEqual(found[entry["type"]]["owned"], item)
        self.assertEqual(backend.region.return_value.get.call_count, 1)
