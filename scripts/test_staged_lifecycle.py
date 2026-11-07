"""Complete CLI orchestration with synthetic retained resources and mock APIs."""

import ipaddress
import json
import tempfile
import unittest
from collections.abc import Mapping
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import stack_config
import staged_runtime
import topology
from cloud_contract import InfrastructureNode, InfrastructurePlacement, InfrastructureResult, TerraformError
from placement_config import checked
from selectel_adapter import SelectelAdapter
from selectel_global_router import GlobalRouter
from selectel_stage_operations import candidate_region
from selectel_staged import Backend
from stage_journal import StageJournal, StageOutput

ROOT = Path(__file__).resolve().parents[1]


class MemoryBackend(Backend):
    """A deterministic provider boundary, not a substitute for a cloud trial."""

    def prepare_tools(self) -> None:
        pass  # Provider installation is tested separately without cloud resources.

    preflight: Mock

    def __init__(self, api: SimpleNamespace) -> None:
        self.api = api
        self.desired = topology.load(ROOT)
        from run_settings import placement

        self.policy = placement(ROOT)
        self.journal: StageJournal = {
            "stages": {},
            "configured": False,
            "admin_cidrs": [],
            "pool_subnets": {"ru-9": "10.77.0.0/24", "ru-3": "10.78.0.0/24"},
        }
        self.path = api.LOCAL / "staged-deployment.json"
        self.events: list[tuple[str, ...]] = []
        self.objects: dict[str, str] = {}
        self.attempts: dict[str, int] = {}
        self.choices = {
            role: [{"region": "ru-9", "availability_zone": "ru-9a"}, {"region": "ru-3", "availability_zone": "ru-3b"}]
            for role in ("app", "es")
        }
        self.preflight = Mock()
        self.global_audit = Mock()
        self.global_router = Mock(spec_set=GlobalRouter, assert_absent=self.global_audit)
        self.cloud = Mock()
        self.cloud.get.return_value = {"floatingips": []}

    @property
    def owner(self) -> str:
        return "otus-dz07-synthetic"

    def region(self, pool: str) -> SelectelAdapter:
        return self.cloud

    def save(self) -> None:
        self.api.save(self.path, self.journal)

    def completed(self, name: str) -> bool:
        return self.journal["stages"].get(name, {}).get("status") == "active"

    def pending(self, name: str) -> None:
        return None

    def identities(self, exclude_pending: bool = False) -> dict[tuple[str, str], str]:
        return {(n, "vm"): i for n, i in self.objects.items() if not exclude_pending or self.completed(n)}

    def assert_preserved(self, before: Mapping[tuple[str, str], str]) -> None:
        if any(self.objects.get(n) != i for (n, _), i in before.items()):
            raise RuntimeError("Identity changed")

    def begin(self, name: str, candidate: Mapping[str, object]) -> None:
        self.journal["stages"][name] = {"candidate": checked(candidate), "status": "pending"}

    def apply(self, name: str) -> StageOutput:
        self.events.append(("apply", name, candidate_region(self.journal["stages"][name]["candidate"])))
        self.attempts[name] = self.attempts.get(name, 0) + 1
        self.objects[name] = name + "-id-" + str(self.attempts[name])
        if name == "es2" and self.attempts[name] == 1:
            raise TerraformError("No valid host was found")
        return {}

    def capture_fault(self, name: str) -> None:
        self.events.append(("fault", name))

    def is_capacity_error(self, error: Exception, name: str) -> bool:
        return isinstance(error, TerraformError)

    def cleanup_attempt(self, name: str) -> None:
        self.events.append(("cleanup", name))
        self.objects.pop(name, None)
        self.journal["stages"][name]["status"] = "deleted"

    def finish(self, name: str) -> None:
        self.journal["stages"][name]["status"] = "active"

    def reapply(self, name: str) -> None:
        self.events.append(("repeat", name))

    def result(self) -> InfrastructureResult:
        nodes: dict[str, InfrastructureNode] = {}
        for name, v in self.desired["nodes"].items():
            candidate = self.journal["stages"][name]["candidate"]
            pool, zone = candidate_region(candidate), candidate["availability_zone"]
            if not isinstance(zone, str):
                raise ValueError("Synthetic candidate has no zone")
            cidr = self.journal["pool_subnets"][pool]
            nodes[name] = {
                "id": self.objects[name],
                "volume_id": name + "-disk",
                "port_id": name + "-port",
                "roles": [v["role"]],
                "private_ip": str(ipaddress.ip_network(cidr)[v["host"]]),
                "public_ip": "192.0.2.1" if name == "app" else None,
                "region": pool,
                "zone": zone,
                "subnet_cidr": cidr,
            }
        placement: InfrastructurePlacement = {
            "region": nodes["app"]["region"],
            "zone": nodes["app"]["zone"],
            "pool_subnets": self.journal["pool_subnets"],
            "node_placements": {n: {"region": v["region"], "zone": v["zone"]} for n, v in nodes.items()},
        }
        return InfrastructureResult(
            "selectel", {"project_id": "synthetic-project"}, "otus-dz07", placement, nodes, nodes["app"]["subnet_cidr"]
        )


class RetainedLifecycleTest(unittest.TestCase):
    def fixture(self, root: Path) -> tuple[SimpleNamespace, MemoryBackend]:
        local = root / ".local"
        local.mkdir()
        (root / "config").mkdir()
        (root / "config/services.json").write_text((ROOT / "config/services.json").read_text())
        api = SimpleNamespace(
            ROOT=root,
            LOCAL=local,
            PREFIX="otus-dz07",
            STACK=stack_config.load(),
            _TEST_CYCLE_ID=None,
            prepare_identity=Mock(),
            configure=Mock(),
        )

        def save(path: Path, value: object) -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value))

        api.save = save
        provider = MemoryBackend(api)
        api.lab_output = lambda: provider.result().application()
        return api, provider

    def patches(self, provider: MemoryBackend) -> ExitStack:
        stack = ExitStack()
        stack.enter_context(patch("staged_runtime.backend_factory", return_value=provider))
        stack.enter_context(patch("operator_network.discover_cidrs", return_value=["8.8.8.8/32"]))
        stack.enter_context(patch("delivery.prepare"))
        stack.enter_context(patch("selectel_staged_audit.assert_stage_owned"))
        return stack

    def test_failed_es_attempt_keeps_app_and_other_nodes_then_repeat_preserves_all_ids(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            api, provider = self.fixture(Path(folder))
            with (
                self.patches(provider),
                patch("verification.snapshot", return_value={"canary": "saved"}) as snapshot,
                patch("verification.expansion") as preservation,
            ):
                staged_runtime.deploy(api)
                first = dict(provider.objects)
                self.assertEqual(len(first), 4)
                self.assertEqual(provider.journal["stages"]["es2"]["candidate"]["region"], "ru-3")
                self.assertEqual([e for e in provider.events if e[0] == "cleanup"], [("cleanup", "es2")])
                staged_runtime.deploy(api)
                self.assertEqual(provider.objects, first)
                self.assertEqual(sum(e[0] == "apply" for e in provider.events), 5)
                self.assertEqual(sum(e[0] == "repeat" for e in provider.events), 4)
                snapshot.assert_called_once()
                preservation.assert_called_once_with(api, {"canary": "saved"})
                self.assertEqual([c.kwargs["existing"] for c in api.configure.call_args_list], [False, True])

    def test_configuration_error_does_not_destroy_created_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            api, provider = self.fixture(Path(folder))
            api.configure.side_effect = RuntimeError("configuration failed")
            with self.patches(provider), self.assertRaisesRegex(RuntimeError, "configuration failed"):
                staged_runtime.deploy(api)
            self.assertEqual(len(provider.objects), 4)
            self.assertTrue(provider.path.exists())
            self.assertFalse(provider.journal["configured"])

    def test_destroy_audits_api_before_local_cleanup_and_preserves_files_on_api_failure(self) -> None:
        for fails in (False, True):
            with tempfile.TemporaryDirectory() as folder:
                api, provider = self.fixture(Path(folder))
                with self.patches(provider):
                    staged_runtime.deploy(api)
                with (
                    self.patches(provider),
                    patch("selectel_staged_audit.assert_stage_absent") as audit,
                    patch("selectel_adapter.audit_cleanup", return_value=([], 0)),
                    patch(
                        "selectel_staged_audit.audit_namespace",
                        side_effect=RuntimeError("audit failed") if fails else None,
                    ),
                    patch("artifact_lifecycle.release") as release,
                    patch("local_runtime.cleanup_runtime") as local,
                ):
                    if fails:
                        with self.assertRaisesRegex(RuntimeError, "audit failed"):
                            staged_runtime.cleanup(api)
                        local.assert_not_called()
                        release.assert_not_called()
                        self.assertTrue(provider.path.exists())
                    else:
                        staged_runtime.cleanup(api)
                        self.assertEqual(audit.call_count, 4)
                        local.assert_called_once_with(api.ROOT)
                        release.assert_called_once_with(api.ROOT)
                        self.assertTrue(provider.journal["cleanup_verified"])
                    self.assertFalse(provider.objects)
