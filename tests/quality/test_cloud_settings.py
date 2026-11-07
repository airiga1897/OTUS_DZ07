"""Cloud identity is validated before the runtime can select an adapter."""

import json
from pathlib import Path

import cloud_settings
import pytest


@pytest.mark.parametrize(
    "document", [None, [], {}, {"provider": "selectel"}, {"terraform_directory": "terraform-selectel"}]
)
def test_incomplete_cloud_identity_is_rejected(tmp_path: Path, document: object) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config/cloud.json").write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="cloud"):
        cloud_settings.load(tmp_path)


def test_cloud_reader_preserves_provider_options(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    document = {"provider": "selectel", "terraform_directory": "terraform-selectel", "audit": {"read_workers": 3}}
    (tmp_path / "config/cloud.json").write_text(json.dumps(document), encoding="utf-8")
    assert cloud_settings.load(tmp_path) == document
