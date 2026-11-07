"""Composition and import contracts, with filesystem and process boundaries."""

import ast
import json
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from runtime import create_runtime

ROOT = Path(__file__).resolve().parents[2]


def _protocols(tree: ast.AST) -> Iterator[ast.ClassDef]:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and any(
            isinstance(base, ast.Name) and base.id == "Protocol" for base in node.bases
        ):
            yield node


def _empty_statement(statement: ast.stmt, allow_ellipsis: bool = False) -> bool:
    return isinstance(statement, ast.Pass) or (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Constant)
        and (isinstance(statement.value.value, str) or allow_ellipsis and statement.value.value is Ellipsis)
    )


def _declaration(member: ast.stmt) -> bool:
    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return all(_empty_statement(statement, allow_ellipsis=True) for statement in member.body)
    if isinstance(member, ast.AnnAssign):
        return member.value is None
    return _empty_statement(member)


def test_protocols_omitted_by_coverage_contain_only_declarations() -> None:
    for path in (ROOT / "scripts").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in _protocols(tree):
            for member in node.body:
                assert _declaration(member), (path.name, node.name, ast.dump(member))


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    shutil.copytree(ROOT / "config", tmp_path / "config")
    shutil.copy(ROOT / ".env.example", tmp_path / ".env.example")
    return tmp_path


def test_factory_does_not_authenticate_create_state_or_read_credentials(clone: Path) -> None:
    before = sorted(p.name for p in clone.iterdir())
    with patch("credentials.load_credentials", side_effect=AssertionError("unexpected authentication")):
        dependencies = create_runtime(clone)
    assert clone == dependencies.ROOT
    assert clone / ".local" == dependencies.LOCAL
    assert dependencies._AUTH_CONFIG is None
    assert sorted(p.name for p in clone.iterdir()) == before


def test_instances_do_not_share_credentials_or_cycle_state(clone: Path) -> None:
    first, second = create_runtime(clone), create_runtime(clone)
    first._AUTH_CONFIG = {"password": "synthetic-private-value"}
    first._TEST_CYCLE_ID = "synthetic-cycle"
    assert second._AUTH_CONFIG is None
    assert second._TEST_CYCLE_ID is None
    assert "synthetic-private-value" not in repr(first)


def test_mutated_remote_root_is_rejected_before_use(clone: Path) -> None:
    runtime = create_runtime(clone)
    runtime.STACK["paths"].update(json.loads('{"remote_root":null}'))
    with pytest.raises(ValueError, match="Remote root"):
        _ = runtime.remote_root


@pytest.mark.parametrize("checksum", [None, 7, "unknown", "A" * 128])
def test_package_lock_rejects_unverified_checksums(clone: Path, checksum: object) -> None:
    from artifact_content import package_lock

    path = clone / "config/elastic-artifacts.json"
    value = json.loads(path.read_text())
    value[next(iter(value))] = checksum
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid pinned"):
        package_lock(clone)


def test_package_catalog_preserves_configured_order_for_repeatable_release_assembly(clone: Path) -> None:
    from artifact_content import package_lock

    path = clone / "config/elastic-artifacts.json"
    original = json.loads(path.read_text())
    names = list(original)
    for order in (names, list(reversed(names))):
        path.write_text(json.dumps({name: original[name] for name in order}), encoding="utf-8")
        assert list(package_lock(clone)) == order


def test_imports_do_not_load_configuration_or_write_runtime_files(clone: Path) -> None:
    script = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
original = Path.read_text
def forbidden(path, *args, **kwargs):
    if path.parent.name == 'config' and path.suffix == '.json' or '.local' in path.parts:
        raise AssertionError('configuration read during import')
    return original(path, *args, **kwargs)
Path.read_text = forbidden
import lab, runtime, render, ansible_backend
from remote import ansible_control
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(ROOT / "scripts")], cwd=clone, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    assert not (clone / ".local").exists()


@pytest.mark.parametrize("document", [None, [], {}, {"ansible": None}, {"ansible": {"version": 7, "venv": "/tmp"}}])
def test_controller_settings_reject_malformed_config_before_subprocess(clone: Path, document: object) -> None:
    from remote import ansible_control

    (clone / "config/stack.json").write_text(json.dumps(document), encoding="utf-8")
    with patch("subprocess.run") as run, pytest.raises(ValueError):
        ansible_control.load_settings(clone)
    run.assert_not_called()


@pytest.mark.parametrize("value", [None, [], {}, 42, "invalid"])
def test_factory_rejects_unknown_config_without_creating_state(clone: Path, value: object) -> None:
    (clone / "config/stack.json").write_text(json.dumps(value))
    with pytest.raises((ValueError, KeyError, TypeError)):
        create_runtime(clone)
    assert not (clone / ".local").exists()


@pytest.mark.parametrize(
    "group,key,value",
    [
        ("paths", "remote_root", None),
        ("paths", "unexpected", "path"),
        ("postgresql", "major", True),
        ("postgresql", "major", "10"),
        ("ansible", "version", None),
        ("web", "nginx_port", []),
        ("portal", "service_user", 42),
        ("postgresql", "data_dir", None),
        ("postgresql", "port", False),
    ],
)
def test_stack_field_types_are_checked_before_runtime_creation(
    clone: Path, group: str, key: str, value: object
) -> None:
    path = clone / "config/stack.json"
    source = json.loads(path.read_text())
    source[group][key] = value
    path.write_text(json.dumps(source))
    with pytest.raises(ValueError):
        create_runtime(clone)
    assert not (clone / ".local").exists()


@pytest.mark.parametrize("document", [None, [], {}, {"nodes": []}, {"nodes": {"app": None}}])
def test_topology_rejects_malformed_external_shapes(clone: Path, document: object) -> None:
    import topology

    (clone / "config/topology.json").write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="schema"):
        topology.load(clone)
    assert not (clone / ".local").exists()


def test_topology_requires_all_configured_elasticsearch_masters(clone: Path) -> None:
    import topology

    path = clone / "config/services.json"
    services = json.loads(path.read_text())
    services["initial_masters"].append("es5")
    path.write_text(json.dumps(services), encoding="utf-8")
    with pytest.raises(ValueError, match="masters"):
        topology.load(clone)
