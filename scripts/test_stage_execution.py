"""Exercise real Terraform stage sequencing and guards with synthetic CLI results."""

import contextlib
import json
import tempfile
import unittest
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from stage_store import StageStore, resources

ROOT = Path(__file__).resolve().parents[1]


class StageExecutionTests(unittest.TestCase):
    @contextlib.contextmanager
    def store(
        self, root: Path, before: dict[str, str], after: dict[str, str], plan: Mapping[str, object]
    ) -> Iterator[tuple[StageStore, Mock]]:
        api = SimpleNamespace(ROOT=root)
        store = StageStore(api, {})
        tf_mock = Mock()

        def cli(name: str, operation: str, *args: str) -> str:
            if operation == "show":
                return json.dumps(plan)
            if operation == "output":
                return '{"id":"new"}'
            return ""

        tf_mock.side_effect = cli
        with (
            patch.object(store, "identities", Mock(side_effect=[before, after])),
            patch.object(store, "tf", tf_mock),
        ):
            yield store, tf_mock

    def test_safe_plan_is_applied_once_and_id_changes_after_apply_are_rejected(self) -> None:
        before = {"existing": "old"}
        plan = {
            "resource_changes": [
                {"address": "existing", "change": {"actions": ["no-op"], "before": {"id": "old"}}},
                {"address": "new", "change": {"actions": ["create"]}},
            ]
        }
        with tempfile.TemporaryDirectory(dir=ROOT / ".tools") as d:
            with self.store(Path(d), before, {**before, "new": "new-id"}, plan) as payload:
                store, tf_mock = payload
                self.assertEqual(store.apply("es1"), {"id": "new"})
                self.assertEqual(
                    [c.args[1] for c in tf_mock.call_args_list], ["init", "plan", "show", "apply", "output"]
                )
            with self.store(Path(d), before, {"existing": "replacement"}, plan) as payload:
                store, _ = payload
                with self.assertRaisesRegex(RuntimeError, "identity changed"):
                    store.apply("es1")

    def test_repeat_and_extra_validation_block_changes_before_apply(self) -> None:
        plan = {"resource_changes": [{"address": "new", "change": {"actions": ["create"]}}]}
        with tempfile.TemporaryDirectory(dir=ROOT / ".tools") as d:
            with self.store(Path(d), {}, {}, plan) as payload:
                store, tf_mock = payload
                with self.assertRaises(ValueError):
                    store.apply("es1", unchanged=True)
                self.assertNotIn("apply", [c.args[1] for c in tf_mock.call_args_list])
            with self.store(Path(d), {}, {}, plan) as payload:
                store, tf_mock = payload
                guard = Mock(side_effect=ValueError("unsafe provider change"))
                with self.assertRaises(ValueError):
                    store.apply("es1", validate_extra=guard)
                self.assertNotIn("apply", [c.args[1] for c in tf_mock.call_args_list])

    def test_destroy_requires_empty_final_state_and_nested_resource_walk(self) -> None:
        plan = {"resource_changes": [{"address": "one", "change": {"actions": ["delete"], "before": {"id": "id"}}}]}
        with tempfile.TemporaryDirectory(dir=ROOT / ".tools") as d:
            with self.store(Path(d), {"one": "id"}, {}, plan) as payload:
                store, _ = payload
                self.assertEqual(store.apply("es1", destroy=True), {})
            with self.store(Path(d), {"one": "id"}, {"one": "id"}, plan) as payload:
                store, _ = payload
                with self.assertRaisesRegex(RuntimeError, "not empty"):
                    store.apply("es1", destroy=True)
        tree = {
            "resources": [{"mode": "data"}],
            "child_modules": [{"resources": [{"mode": "managed", "address": "owned"}]}],
        }
        self.assertEqual(resources(tree), [{"mode": "managed", "address": "owned"}])
