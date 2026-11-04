"""Invalid service configuration must fail before writing Ansible inventory."""

import json
import shutil
from pathlib import Path
from typing import cast

import pytest
import topology
from service_config import elastic_version, kibana_port

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def services_path(tmp_path: Path) -> Path:
    (tmp_path / "config").mkdir()
    path = tmp_path / "config/services.json"
    shutil.copyfile(ROOT / "config/services.json", path)
    return path


@pytest.mark.parametrize("value", [None, [], {}, 42])
def test_service_document_requires_a_complete_object(services_path: Path, value: object) -> None:
    services_path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="service"):
        topology.services(services_path.parents[1])


@pytest.mark.parametrize(
    "path,value",
    [
        (("artifact_delivery",), None),
        (("artifact_delivery",), "unknown"),
        (("elastic_version",), 819),
        (("elastic_heap",), []),
        (("elastic_download_base",), None),
        (("kibana_host",), []),
        (("kibana_port",), True),
        (("kibana_port",), "5601"),
        (("initial_masters",), None),
        (("initial_masters",), [None]),
        (("logs",), None),
        (("logs", "all"), None),
        (("logs", "all"), [None]),
        (("logs", "all"), [{"service": None}]),
        (("logs", "roles"), None),
        (("logs", "roles"), {"app": None}),
        (("logs", "roles"), {"app": [{"service": "nginx", "paths": None}]}),
        (("logs", "roles"), {"app": [{"service": "django", "journal_units": [42]}]}),
    ],
)
def test_service_field_shapes(services_path: Path, path: tuple[str, ...], value: object) -> None:
    raw = json.loads(services_path.read_text(encoding="utf-8"))
    parent = cast(dict[str, object], raw)
    for key in path[:-1]:
        parent = cast(dict[str, object], parent[key])
    parent[path[-1]] = value
    services_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="service"):
        topology.services(services_path.parents[1])


def test_service_configuration_preserves_unicode_and_source_order(services_path: Path) -> None:
    raw = json.loads(services_path.read_text(encoding="utf-8"))
    source = {"service": "учебный-журнал", "paths": ["/var/log/учебный.log"]}
    raw["logs"]["all"] = [source]
    services_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    result = topology.services(services_path.parents[1])
    assert result["logs"]["all"] == [source]
    assert result["initial_masters"] == ["es1", "es2", "es3"]


@pytest.mark.parametrize("value", [None, [], {}, {"elastic_version": 819}, {"kibana_port": False}])
def test_narrow_service_readers_reject_missing_or_invalid_fields(services_path: Path, value: object) -> None:
    services_path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="service"):
        elastic_version(services_path.parents[1])
    with pytest.raises(ValueError, match="service"):
        kibana_port(services_path.parents[1])


def test_narrow_readers_do_not_require_inventory_configuration(services_path: Path) -> None:
    services_path.write_text('{"elastic_version":"8.19.0", "kibana_port":5601}', encoding="utf-8")
    assert elastic_version(services_path.parents[1]) == "8.19.0"
    assert kibana_port(services_path.parents[1]) == 5601
