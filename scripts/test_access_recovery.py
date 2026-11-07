"""An interrupted checked /32 rule addition must be retried, never mistaken for applied."""

import copy
import unittest
from collections.abc import Callable
from unittest.mock import Mock

from cloud_contract import StandDescription
from selectel_staged import Backend
from stage_journal import StageJournal


class RecoveryBackend(Backend):
    journal: StageJournal
    store: Mock
    configure_stage: Mock
    finish: Mock
    snapshots: list[StageJournal]

    def save(self) -> None:
        self.snapshots.append(copy.deepcopy(self.journal))


class AdministrativeAccessRecoveryTests(unittest.TestCase):
    def backend(self) -> RecoveryBackend:
        backend = RecoveryBackend.__new__(RecoveryBackend)
        backend.journal = {
            "admin_cidrs": ["8.8.8.8/32"],
            "stages": {"network-ru-3": {"inputs": {}, "output": {"security_group_id": "own-security-group"}}},
        }
        backend.store = Mock()
        backend.configure_stage = Mock()
        backend.finish = Mock()
        backend.snapshots = []
        return backend

    def test_failed_apply_remains_pending_then_retry_applies_once_without_duplicate_rules(self) -> None:
        backend = self.backend()
        backend.store.apply.side_effect = [RuntimeError("temporary failure"), {}]
        lab: StandDescription = {"node_regions": {"app": "ru-3"}}
        with self.assertRaises(RuntimeError):
            backend.extend_admin_access(lab, "1.1.1.1/32")
        self.assertEqual(backend.snapshots[-1]["admin_access_pending"], "1.1.1.1/32")
        backend.extend_admin_access(lab, "1.1.1.1/32")
        self.assertEqual(backend.store.apply.call_count, 2)
        self.assertEqual(backend.journal["admin_cidrs"], ["8.8.8.8/32", "1.1.1.1/32"])
        self.assertNotIn("admin_access_pending", backend.journal)
        backend.extend_admin_access(lab, "1.1.1.1/32")
        self.assertEqual(backend.store.apply.call_count, 2)

    def test_different_request_cannot_overwrite_unfinished_access_change(self) -> None:
        backend = self.backend()
        backend.journal["admin_access_pending"] = "1.1.1.1/32"
        with self.assertRaises(ValueError):
            backend.extend_admin_access({}, "9.9.9.9/32")
        backend.configure_stage.assert_not_called()
        backend.store.apply.assert_not_called()

    def test_checked_plan_rejects_existing_resource_changes(self) -> None:
        backend = self.backend()

        def unsafe_plan(name: str, validate_extra: Callable[[dict[str, object]], None]) -> None:
            validate_extra(
                {
                    "resource_changes": [
                        {
                            "mode": "managed",
                            "type": "openstack_compute_instance_v2",
                            "change": {"actions": ["delete"], "before": {"id": "existing-app"}},
                        }
                    ]
                }
            )

        backend.store.apply.side_effect = unsafe_plan
        with self.assertRaises(ValueError):
            backend.extend_admin_access({"node_regions": {"app": "ru-3"}}, "1.1.1.1/32")
        backend.finish.assert_not_called()
        self.assertEqual(backend.journal["admin_access_pending"], "1.1.1.1/32")
