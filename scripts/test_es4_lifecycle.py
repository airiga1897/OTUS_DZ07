"""Optional expansion drives the real staged controller with a mock provider boundary."""

import copy
import json
import shutil
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import Mock, patch

import elastic_options as options
import run_settings
import stack_config
import test_staged_lifecycle as fixtures
import topology
from cloud_contract import InfrastructureResult

ROOT = Path(__file__).resolve().parents[1]


class Es4LifecycleTests(unittest.TestCase):
    def exercise(self, configuration_failure=False, broken_snapshot=False) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tools") as directory:
            helper = fixtures.RetainedLifecycleTest()
            api, provider = helper.fixture(Path(directory))
            api.verify = Mock(return_value={"logs": True})
            shutil.copytree(ROOT / "config", api.ROOT / "config", dirs_exist_ok=True)
            shutil.copyfile(ROOT / ".env.example", api.ROOT / ".env.example")
            provider.journal.update(
                configured=True,
                topology=copy.deepcopy(provider.desired),
                namespace=api.PREFIX,
                deployment_id="a" * 32,
                scope={"project_id": "synthetic-project"},
                stack_sha256=stack_config.digest(api.STACK),
            )
            for name in provider.desired["nodes"]:
                provider.journal["stages"][name] = {
                    "status": "active",
                    "candidate": provider.choices["app" if name == "app" else "es"][0],
                }
                provider.objects[name] = "original-" + name
            original_ids = dict(provider.objects)
            original_result = provider.result()
            api.save(api.LOCAL / "infrastructure.json", asdict(original_result))
            provider.save()
            api.desired_topology = lambda: options.desired(api.ROOT, topology.load(api.ROOT))
            api.lab_output = lambda: InfrastructureResult.from_dict(
                json.loads((api.LOCAL / "infrastructure.json").read_text())
            ).application()
            data = {"document": "committed-before-extension", "media": "original-media"}
            snapshots = []
            supplied_snapshot = copy.deepcopy(original_result)
            if broken_snapshot:
                supplied_snapshot.nodes.pop("es3")

            def snapshot(api, lab):
                value = {"ids": dict(lab["server_ids"]), "data": dict(data)}
                snapshots.append(value)
                return value

            def preserved(api, before) -> None:
                topology.assert_preserved(before["ids"], api.lab_output()["server_ids"])
                self.assertEqual(data, before["data"])

            def backend(api, cleanup=False):
                if not cleanup:
                    provider.desired = api.desired_topology()
                    provider.policy = run_settings.placement(ROOT)
                    options.node_order(provider.policy, provider.journal, provider.desired)
                return provider

            with (
                patch("staged_runtime.backend_factory", side_effect=backend),
                patch("operator_network.discover_cidrs", return_value=["8.8.8.8/32"]),
                patch("staged_runtime.prepare_files"),
                patch("selectel_staged_audit.assert_stage_owned"),
                patch("verification.snapshot", side_effect=snapshot),
                patch("verification.expansion", side_effect=preserved),
                patch("staged_runtime.output", return_value=supplied_snapshot),
            ):
                if broken_snapshot:
                    with self.assertRaisesRegex(ValueError, "unexpected infrastructure snapshot"):
                        options.add_es4(api)
                    self.assertEqual(provider.objects, original_ids)
                    self.assertFalse(any(event[0] == "apply" for event in provider.events))
                    return
                if configuration_failure:
                    api.configure.side_effect = RuntimeError("synthetic configuration failure")
                    with self.assertRaisesRegex(RuntimeError, "configuration failure"):
                        options.add_es4(api)
                    self.assertEqual(set(provider.objects), set(original_ids) | {"es4"})
                    self.assertTrue(provider.path.exists())
                    api.configure.side_effect = None
                options.add_es4(api)
                after = dict(provider.objects)
                options.add_es4(api)
            topology.assert_preserved(original_ids, after)
            self.assertEqual(provider.objects, after)
            self.assertEqual([event[1] for event in provider.events if event[0] == "apply"], ["es4"])
            self.assertFalse(any(event[0] == "cleanup" for event in provider.events))
            self.assertEqual(data, {"document": "committed-before-extension", "media": "original-media"})
            self.assertEqual(len(api.desired_topology()["nodes"]), 5)
            self.assertEqual(len(topology.load(api.ROOT)["nodes"]), 4)
            self.assertTrue(all(call.kwargs["existing"] for call in api.configure.call_args_list))
            self.assertTrue(snapshots)

    def test_addition_and_following_deploy_reuse_existing_identities_and_data(self) -> None:
        self.exercise()

    def test_failed_configuration_keeps_es4_and_retry_does_not_allocate_it_again(self) -> None:
        self.exercise(configuration_failure=True)

    def test_incomplete_extension_refuses_a_corrupt_original_snapshot(self) -> None:
        self.exercise(broken_snapshot=True)
