"""Malformed local tool output cannot be interpreted as complete coverage."""

import json
from pathlib import Path
from unittest.mock import Mock

import check
import pytest


@pytest.mark.parametrize(
    "document",
    [
        None,
        [],
        {},
        {"totals": None},
        {"totals": []},
        {"totals": {}},
        {"totals": {"missing_lines": True, "missing_branches": 0}},
        {"totals": {"missing_lines": 0.0, "missing_branches": 0}},
        {"totals": {"missing_lines": 0, "missing_branches": None}},
        {"totals": {"missing_lines": 0, "missing_branches": -1}},
    ],
)
def test_invalid_coverage_summary_cannot_pass(tmp_path: Path, document: object) -> None:
    meter = Mock()

    def report(*, outfile: str) -> None:
        Path(outfile).write_text(json.dumps(document))

    meter.json_report.side_effect = report
    with pytest.raises(ValueError, match="coverage totals"):
        check.save_measurement(meter, tmp_path, {})


def test_extra_totals_survive_validation(tmp_path: Path) -> None:
    meter = Mock()

    def report(*, outfile: str) -> None:
        Path(outfile).write_text(
            json.dumps({"totals": {"missing_lines": 0, "missing_branches": 0, "num_statements": 10}})
        )

    meter.json_report.side_effect = report
    result = check.save_measurement(meter, tmp_path, {})
    assert result == {"missing_lines": 0, "missing_branches": 0, "num_statements": 10}
