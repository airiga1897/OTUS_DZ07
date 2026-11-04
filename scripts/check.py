"""One local command: isolated unittest, Django, pytest, coverage and static tools."""

import json
import logging
import os
import socket
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import TypedDict, cast
from unittest.mock import patch

import coverage

ROOT = Path(__file__).resolve().parents[1]
LOGGER = logging.getLogger(__name__)


class CoverageTotals(TypedDict):
    missing_lines: int
    missing_branches: int


def offline_connection(*args: object, **kwargs: object) -> None:
    raise AssertionError("External networking is forbidden in local checks; mock the transport")


def prepare_process() -> Path:
    os.chdir(ROOT)
    for key in list(os.environ):
        if key.startswith("OS_") or key == "OTUS_SELECTEL_AUTH":
            os.environ.pop(key)
    os.environ.update(DJANGO_SECRET_KEY="local-tests-only", DJANGO_SETTINGS_MODULE="config.test_settings")
    sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "app")]
    output = ROOT / ".tools/coverage"
    output.mkdir(parents=True, exist_ok=True)
    return output


def measurement(output: Path) -> coverage.Coverage:
    omitted = [str(p) for p in (ROOT / "scripts").glob("test_*.py") if p.name != "test_cycle.py"]
    omitted += [
        str(ROOT / "tests/*"),
        str(ROOT / "app/knowledge/test*.py"),
        str(ROOT / "app/knowledge/migrations/*"),
        str(Path(__file__).resolve()),
    ]
    result = coverage.Coverage(
        branch=True,
        source=[str(ROOT / "scripts"), str(ROOT / "app")],
        omit=omitted,
        data_file=str(output / "latest.data"),
    )
    result.set_option("report:include_namespace_packages", True)
    return result


def unit_group(folder: str) -> dict[str, int | bool]:
    suite = unittest.TestLoader().discover(str(ROOT / folder), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return {"tests": result.testsRun, "success": result.wasSuccessful()}


def django_group() -> dict[str, int | bool]:
    import django

    django.setup()
    from django.test.runner import DiscoverRunner

    runner = DiscoverRunner(verbosity=1)
    count = runner.build_suite(["knowledge"]).countTestCases()
    return {"tests": count, "success": runner.run_tests(["knowledge"]) == 0}


def pytest_group() -> dict[str, int | bool]:
    import pytest

    class Counts:
        collected: int = 0

        def pytest_collection_finish(self, session: pytest.Session) -> None:
            self.collected = len(session.items)

    counts = Counts()
    code = pytest.main(
        [str(ROOT / "tests/quality"), "-q", "--basetemp", str(Path(tempfile.gettempdir()) / "pytest")], plugins=[counts]
    )
    return {"tests": counts.collected, "success": code == 0}


def measured_groups(meter: coverage.Coverage) -> dict[str, dict[str, int | bool]]:
    results = {}
    groups: list[tuple[str, Callable[[], dict[str, int | bool]]]] = [
        ("scripts", lambda: unit_group("scripts")),
        ("tests", lambda: unit_group("tests")),
        ("django", django_group),
        ("pytest", pytest_group),
    ]
    with (
        patch.object(socket.socket, "connect", offline_connection),
        patch.object(socket, "create_connection", offline_connection),
    ):
        for name, run in groups:
            meter.switch_context(name)
            results[name] = run()
    return results


def save_measurement(meter: coverage.Coverage, output: Path, results: object) -> CoverageTotals:
    meter.stop()
    meter.save()
    meter.json_report(outfile=str(output / "latest.json"))
    with (output / "latest.txt").open("w") as stream:
        meter.report(file=stream, show_missing=True)
    (output / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    document = json.loads((output / "latest.json").read_text())
    totals = document.get("totals") if isinstance(document, dict) else None
    if not isinstance(totals, dict) or any(
        type(totals.get(field)) is not int or totals[field] < 0 for field in ("missing_lines", "missing_branches")
    ):
        raise ValueError("Invalid local coverage totals")
    return cast(CoverageTotals, totals)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    output = prepare_process()
    meter = measurement(output)
    results = {}
    meter.start()
    try:
        with (
            tempfile.TemporaryDirectory(prefix="local-check-", dir=ROOT / ".tools") as folder,
            patch.object(tempfile, "tempdir", folder),
            patch.dict(os.environ, {"TEMP": folder, "TMP": folder}),
        ):
            results = measured_groups(meter)
    finally:
        totals = save_measurement(meter, output, results)
    from quality import run_checks

    tools = run_checks(ROOT)
    LOGGER.info("%s", json.dumps({"results": results, "coverage": totals}, indent=2))
    passed = all(item["success"] for item in results.values()) and all(tool.exit_code == 0 for tool in tools)
    complete = totals["missing_lines"] == 0 and totals["missing_branches"] == 0
    return 0 if results and passed and complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
