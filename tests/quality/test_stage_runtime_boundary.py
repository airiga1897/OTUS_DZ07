"""Terraform inputs use the actual command runtime, without test-only module attributes."""

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

from runtime import create_runtime
from selectel_stage_operations import inputs

ROOT = Path(__file__).resolve().parents[2]


def test_network_inputs_use_clone_service_settings_with_actual_runtime(tmp_path: Path) -> None:
    shutil.copytree(ROOT / "config", tmp_path / "config")
    runtime = create_runtime(tmp_path)
    settings = json.loads((tmp_path / "config/services.json").read_text())
    settings["kibana_port"] = 5602
    (tmp_path / "config/services.json").write_text(json.dumps(settings))
    policy = json.loads((tmp_path / "config/placement.json").read_text())["sequential_placement"]
    backend = SimpleNamespace(
        api=runtime,
        root=tmp_path,
        owner="synthetic-owner",
        journal={
            "stages": {
                "network-ru-2": {
                    "candidate": {
                        "region": "ru-2",
                        "network_availability_zone": "ru-2a",
                        "router_availability_zone": "ru-2a",
                    }
                }
            },
            "address_policy": policy["addresses"],
            "pool_subnets": {"ru-2": "10.96.0.0/24", "ru-9": "10.77.0.0/24"},
            "admin_cidrs": ["8.8.8.8/32"],
        },
    )
    kind, variables = inputs(backend, "network-ru-2")
    assert kind == "network"
    assert variables["kibana_port"] == 5602
    assert variables["cidr"] == "10.96.0.0/24"
    assert variables["peer_cidrs"] == ["10.77.0.0/24"]
    assert variables["admin_cidrs"] == ["8.8.8.8/32"]
