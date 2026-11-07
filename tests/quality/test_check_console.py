"""The local runner reports progress without mixing test diagnostics into it."""

import io
import logging
import sys
from pathlib import Path
from typing import TextIO
from unittest.mock import Mock, patch

import check


def test_group_diagnostics_go_to_log_and_failed_result_remains_visible() -> None:
    console, journal = io.StringIO(), io.StringIO()
    handler: logging.StreamHandler[TextIO] = logging.StreamHandler(console)
    logger = logging.Logger("test-check-console", level=logging.INFO)
    logger.addHandler(handler)

    def unit_group(folder: str) -> dict[str, int | bool]:
        print("synthetic stdout")
        print("synthetic stderr", file=sys.stderr)
        logger.error("synthetic exception diagnostic")
        return {"tests": 1, "success": folder == "scripts"}

    passed = {"tests": 1, "success": True}
    with (
        patch.object(check, "LOGGER", logger),
        patch.object(check, "unit_group", side_effect=unit_group),
        patch.object(check, "django_group", return_value=passed),
        patch.object(check, "pytest_group", return_value=passed),
    ):
        results = check.measured_groups(Mock(), handler, journal)

    assert results["tests"]["success"] is False
    assert "Тесты tests: есть ошибки" in console.getvalue()
    for message in ("synthetic stdout", "synthetic stderr", "synthetic exception diagnostic"):
        assert message in journal.getvalue()
        assert message not in console.getvalue()


def test_interrupt_during_tests_does_not_write_a_partial_coverage_report(tmp_path: Path) -> None:
    (tmp_path / ".tools").mkdir()
    meter = Mock()
    with (
        patch.object(check, "ROOT", tmp_path),
        patch.object(check, "prepare_process", return_value=tmp_path),
        patch.object(check, "measurement", return_value=meter),
        patch.object(check, "measured_groups", side_effect=KeyboardInterrupt),
        patch.object(check, "save_measurement") as save,
    ):
        assert check.main() == 130
    meter.stop.assert_called_once_with()
    save.assert_not_called()
