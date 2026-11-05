"""Conditional routing uses real stage orchestration and isolated provider doubles."""

import copy
import unittest
from unittest.mock import Mock, patch

import selectel_stage_operations as operations
import test_selectel_retained_contracts as fixtures
from selectel_global_router import GlobalRouter, GlobalRouterCapacityError
from selectel_staged_audit import audit_deletion, cleanup_order
from stage_journal import StageOutput


class ConditionalGlobalRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        helper = fixtures.RetainedSelectelContracts()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        self.b = helper.backend
        self.router = Mock(spec_set=GlobalRouter)
        self.b.global_router = self.router
        self.b.store = Mock(entries=Mock(return_value=[]))
        self.b.journal["stages"] = {
            "app": {"status": "active", "candidate": {"region": "ru-9"}, "output": {"id": "app-id"}},
            "network-ru-9": {
                "status": "active",
                "candidate": {"region": "ru-9"},
                "output": {"network_id": "net-id"},
            },
        }

    def test_single_pool_never_connects_or_authenticates_even_when_router_api_is_forbidden(self) -> None:
        for error in (PermissionError("forbidden"), ValueError("unknown API")):
            with self.subTest(error=type(error).__name__):
                self.b.global_router = Mock(preflight=Mock(side_effect=error))
                before = copy.deepcopy(self.b.journal)
                with patch.object(operations, "place") as place:
                    self.b.connect_pool("ru-9")
                place.assert_not_called()
                self.assertEqual(self.b.global_router.mock_calls, [])
                self.assertEqual(before, self.b.journal)

    def test_second_pool_checks_full_capacity_and_router_before_creating_network(self) -> None:
        events: list[str] = []

        def allocate(backend: object, name: str, choices: object) -> None:
            events.append(name)
            self.b.journal["stages"][name] = {"status": "active", "output": {"network_id": "new-net"}}

        with (
            patch.object(self.b, "check_complete_capacity", side_effect=lambda: events.append("capacity")),
            patch.object(self.b.global_router, "preflight", side_effect=lambda *args: events.append("router")),
            patch("selectel_staged_capacity.require_network_capacity"),
            patch.object(operations, "place", side_effect=allocate),
        ):
            result = self.b.ensure_pool(
                {"region": "ru-3", "network_availability_zone": "ru-3a", "router_availability_zone": "ru-3a"}
            )
        self.assertEqual(events, ["capacity", "router", "network-ru-3"])
        self.assertEqual(result, {"network_id": "new-net"})
        self.assertEqual(self.b.journal["stages"]["app"]["output"]["id"], "app-id")
        self.assertEqual(self.b.journal["stages"]["network-ru-9"]["output"]["network_id"], "net-id")

    def test_all_four_nodes_finish_and_repeat_without_router_or_identity_changes(self) -> None:
        for name in self.b.desired["nodes"]:
            self.b.journal["stages"][name] = {
                "status": "active",
                "candidate": {"region": "ru-9"},
                "kind": "node",
                "inputs": {"name": name},
                "output": {"id": name + "-id"},
            }
        before = {name: self.b.journal["stages"][name]["output"]["id"] for name in self.b.desired["nodes"]}
        store = Mock(
            state=Mock(return_value={"lineage": "same"}),
            identities=Mock(side_effect=lambda name: {"vm": before[name]}),
            apply=Mock(side_effect=lambda name, **kwargs: {"id": before[name]}),
        )
        self.b.store = store
        with patch("selectel_staged_audit.assert_stage_owned"):
            for name in self.b.desired["nodes"]:
                self.b.finish(name)
                self.b.reapply(name)
        self.assertEqual(self.router.mock_calls, [])
        self.assertEqual(store.apply.call_count, 4)
        self.assertTrue(all(call.kwargs == {"unchanged": True} for call in store.apply.call_args_list))
        self.assertEqual(before, {name: self.b.journal["stages"][name]["output"]["id"] for name in before})

    def test_access_quota_and_unknown_router_results_block_second_pool_before_creation(self) -> None:
        for error in (PermissionError("forbidden"), GlobalRouterCapacityError("quota"), ValueError("unknown")):
            with self.subTest(error=type(error).__name__):
                before = copy.deepcopy(self.b.journal)
                with (
                    patch.object(self.b, "check_complete_capacity", return_value={"ru-9", "ru-3"}),
                    patch.object(self.b.global_router, "preflight", side_effect=error),
                    patch.object(operations, "place") as place,
                    self.assertRaises(type(error)),
                ):
                    self.b.ensure_pool({"region": "ru-3"})
                place.assert_not_called()
                self.assertEqual(before, self.b.journal)

    def test_partial_vm_counts_but_deleted_and_unallocated_attempts_do_not(self) -> None:
        self.b.journal["stages"].update(
            {
                "es1": {"status": "pending", "candidate": {"region": "ru-3"}, "output": {"id": "partial"}},
                "es2": {"status": "deleted", "candidate": {"region": "unused"}},
                "es3": {"status": "pending", "candidate": {"region": "unused"}},
            }
        )
        self.assertEqual(operations.node_pools(self.b), {"ru-9", "ru-3"})

    def test_interrupted_attachment_resumes_same_stages_and_preserves_node_data(self) -> None:
        self.b.journal["stages"]["es1"] = {
            "status": "active",
            "candidate": {"region": "ru-3"},
            "output": {"id": "es-id"},
        }
        node_before = copy.deepcopy(self.b.journal["stages"]["app"])
        created: list[str] = []
        repeated: list[str] = []
        failed = False

        def allocate(backend: object, name: str, candidates: object) -> None:
            nonlocal failed
            if name == "connection-ru-9" and not failed:
                failed = True
                self.b.journal["stages"][name] = {"status": "pending", "candidate": {"region": "ru-9"}}
                raise ConnectionError("interrupted attachment")
            if self.b.completed(name):
                repeated.append(name)
                return
            created.append(name)
            output: StageOutput = {"router_id": "same-router"} if name == "global" else {"network_id": name}
            self.b.journal["stages"][name] = {
                "status": "active",
                "output": output,
                "candidate": {"region": name.removeprefix("connection-")},
            }

        with patch.object(operations, "place", side_effect=allocate):
            with self.assertRaisesRegex(ConnectionError, "interrupted"):
                self.b.connect_pool("ru-3")
            self.assertEqual(self.b.journal["stages"]["connection-ru-9"]["status"], "pending")
            self.b.connect_pool("ru-3")
            self.b.connect_pool("ru-9")
        self.assertEqual(created, ["global", "connection-ru-3", "connection-ru-9"])
        self.assertEqual(repeated.count("global"), 2)
        self.assertEqual(self.b.journal["stages"]["global"]["output"]["router_id"], "same-router")
        self.assertEqual(node_before, self.b.journal["stages"]["app"])
        preflight = self.b.global_router.preflight
        if not isinstance(preflight, Mock):
            self.fail("Expected external router double")
        self.assertEqual(preflight.call_args.args, (["ru-3", "ru-9"], self.b.api.PREFIX, "same-router"))

    def test_preexisting_router_in_single_pool_is_retained_and_reapplied(self) -> None:
        self.b.journal["stages"]["global"] = {"status": "active", "output": {"router_id": "old-router"}}
        with patch.object(operations, "place") as place:
            self.b.connect_pool("ru-9")
        self.assertEqual([call.args[1] for call in place.call_args_list], ["global", "connection-ru-9"])
        self.assertEqual(self.b.journal["stages"]["global"]["output"]["router_id"], "old-router")

    def test_audit_without_router_skips_api_but_retained_or_partial_router_requires_it(self) -> None:
        for stage in (None, "global", "connection-ru-9"):
            with self.subTest(stage=stage):
                self.b.journal["stages"] = {"app": {"status": "deleted", "candidate": {"region": "ru-9"}}}
                if stage is not None:
                    self.b.journal["stages"][stage] = {"status": "deleted", "candidate": {"region": "ru-9"}}
                with (
                    patch("selectel_staged_audit.assert_stage_absent"),
                    patch("selectel_staged_audit.audit_namespace"),
                    patch.object(self.b.global_router, "assert_absent") as shared,
                ):
                    audit_deletion(self.b)
                self.assertEqual(shared.call_count, int(stage is not None))
        self.assertLess(cleanup_order(self.b).index("app"), cleanup_order(self.b).index("connection-ru-9"))

    def test_failed_router_audit_never_confirms_cleanup(self) -> None:
        self.b.journal["stages"]["global"] = {"status": "deleted", "candidate": {"region": "ru-9"}}
        with (
            patch("selectel_staged_audit.assert_stage_absent"),
            patch("selectel_staged_audit.audit_namespace"),
            patch.object(self.b.global_router, "assert_absent", side_effect=PermissionError("audit unavailable")),
            self.assertRaises(PermissionError),
        ):
            audit_deletion(self.b)
        self.assertNotIn("cleanup_verified", self.b.journal)
