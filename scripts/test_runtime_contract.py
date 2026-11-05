"""Synthetic local state and cleanup contracts; no cloud services."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import TypedDict
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from local_runtime import cleanup_runtime, open_portal, validate_state

ROOT = Path(__file__).resolve().parents[1]


class StateOutput(TypedDict):
    value: object


class RuntimeContract(unittest.TestCase):
    def fixture(self, root: Path, homework: int) -> tuple[Path, Path, dict[str, object]]:
        local = root / ".local"
        local.mkdir()
        tf = root / "terraform-selectel"
        tf.mkdir()
        prefix = f"otus-dz0{homework}"
        name = "es3" if homework == 7 else "app"
        attrs = {"id": "vm-1", "name": prefix + "-" + name, "tenant_id": "project", "region": "ru-9"}
        if homework == 7:
            lab_value: dict[str, object] = {name: {"id": "vm-1", "public_ip": "192.0.2.10"}}
        else:
            lab_value = {"server_ids": {name: "vm-1"}, "app_public_ip": "192.0.2.10"}
        resources: list[dict[str, object]] = [
            {
                "mode": "managed",
                "type": "openstack_compute_instance_v2",
                "instances": [{"index_key": name, "attributes": attrs}],
            }
        ]
        outputs: dict[str, object] = {"lab": StateOutput(value=lab_value)}
        state: dict[str, object] = {
            "lineage": "own",
            "resources": resources,
            "outputs": outputs,
        }
        context: dict[str, object] = {
            "project_id": "project",
            "region": "ru-9",
            "availability_zone": "ru-9a",
            "phase": "active",
            "deployment_id": "deployment",
            "state_lineage": "own",
        }
        if homework == 6:
            from dataclasses import asdict

            from selectel_adapter import infrastructure_result

            full = {
                **lab_value,
                "private_ips": {"app": "10.77.0.10"},
                "subnet_cidr": "10.77.0.0/24",
                "volume_ids": {"app": "v1"},
                "port_ids": {"app": "p1"},
            }
            contract = infrastructure_result(full, {"project_id": "project", "region": "ru-9"}, prefix, "ru-9a")
            outputs["infrastructure"] = StateOutput(value=asdict(contract))
            context.update(contract.binding())
        (tf / "terraform.tfstate").write_text(json.dumps(state))
        (tf / "terraform.tfvars.json").write_text(json.dumps({"availability_zone": "ru-9a"}))
        context_name = {5: "deployment.json", 6: "context.json", 7: "selectel-context.json"}[homework]
        (local / context_name).write_text(json.dumps(context))
        if homework == 5:
            lab_value["deployment_id"] = "deployment"
        (local / "lab.json").write_text(json.dumps(lab_value))
        return local, tf, state

    def test_open_needs_no_auth_and_refuses_stale_or_missing_state(self) -> None:
        for homework in (5, 6, 7):
            with self.subTest(homework=homework), tempfile.TemporaryDirectory(dir=ROOT / ".tools") as d:
                root = Path(d)
                local, tf, state = self.fixture(root, homework)
                opener = Mock(return_value=True)
                self.assertEqual(open_portal(root, homework, opener), "http://192.0.2.10")
                opener.assert_called_once()
                resources = state["resources"]
                if not isinstance(resources, list):
                    raise AssertionError("Expected state resources")
                first = resources[0]
                if not isinstance(first, dict):
                    raise AssertionError("Expected resource object")
                instances = first["instances"]
                if not isinstance(instances, list):
                    raise AssertionError("Expected state instances")
                instance = instances[0]
                if not isinstance(instance, dict):
                    raise AssertionError("Expected instance object")
                attributes = instance["attributes"]
                if not isinstance(attributes, dict):
                    raise AssertionError("Expected instance attributes")
                attributes["id"] = "replacement"
                (tf / "terraform.tfstate").write_text(json.dumps(state))
                with self.assertRaisesRegex(ValueError, "контекста"):
                    open_portal(root, homework, Mock())
                (tf / "terraform.tfstate").unlink()
                with self.assertRaisesRegex(ValueError, "контекста"):
                    open_portal(root, homework, Mock())

    def test_cleanup_keeps_inputs_and_credentials_only_after_empty_state(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tools") as d:
            root = Path(d)
            local, tf, state = self.fixture(root, 6)
            for name in ("operator.json", "lab-config.json", "secrets.json", "key"):
                (local / name).write_text("{}")
            with self.assertRaises(ValueError):
                cleanup_runtime(root)
            self.assertTrue((local / "key").exists())
            state["resources"] = []
            (tf / "terraform.tfstate").write_text(json.dumps(state))
            (tf / "terraform.tfstate.backup").write_text("{}")
            cleanup_runtime(root)
            self.assertEqual({p.name for p in local.iterdir()}, {"operator.json", "lab-config.json"})
            self.assertFalse((tf / "terraform.tfstate").exists())
            self.assertFalse((tf / "terraform.tfvars.json").exists())

    def test_foreign_state_refused(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tools") as d:
            root = Path(d)
            local, tf, state = self.fixture(root, 6)
            for config, prefix in [
                ({"project_id": "other", "region": "ru-9"}, "otus-dz07"),
                ({"project_id": "project", "region": "ru-7"}, "otus-dz07"),
                ({"project_id": "project", "region": "ru-9"}, "otus-dz07"),
            ]:
                with self.assertRaises(ValueError):
                    validate_state(root, config, prefix)

    def test_cleanup_rejects_links_before_removing_recovery_data(self) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory(dir=ROOT / ".tools") as d:
            root = Path(d)
            (root / ".local").mkdir()
            (root / "terraform-selectel").mkdir()
            key = root / ".local/key"
            key.write_text("synthetic")
            with patch("local_runtime.linked", side_effect=lambda p: p == key), self.assertRaises(ValueError):
                cleanup_runtime(root)
            self.assertTrue(key.exists())


if __name__ == "__main__":
    unittest.main()
