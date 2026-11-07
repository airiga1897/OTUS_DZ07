"""Optional ES settings reject malformed shapes before requesting infrastructure."""

import json
from pathlib import Path

import pytest
from elastic_options import settings


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {},
        {"es4": None, "restore_attempts": 2, "restore_delay_seconds": 1},
        {"es4": [], "restore_attempts": 2, "restore_delay_seconds": 1},
    ],
)
def test_optional_settings_require_nested_objects(tmp_path: Path, value: object) -> None:
    (tmp_path / "config").mkdir()
    path = tmp_path / "config/optional-elasticsearch.json"
    original = json.dumps(value)
    path.write_text(original)
    with pytest.raises(ValueError, match="Invalid optional Elasticsearch settings"):
        settings(tmp_path)
    assert path.read_text() == original
    assert not (tmp_path / ".local").exists()
