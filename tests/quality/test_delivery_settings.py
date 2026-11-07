"""Malformed delivery configuration must fail before either connector runs."""

import json
from pathlib import Path
from unittest.mock import patch

import delivery
import pytest


@pytest.mark.parametrize("document", [None, [], "release", 42, {"artifact_delivery": None}, {"artifact_delivery": []}])
def test_invalid_delivery_document_does_not_start_a_connector(tmp_path: Path, document: object) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config/services.json").write_text(json.dumps(document), encoding="utf-8")
    with (
        patch("release_artifacts.prepare") as release,
        patch("s3_artifacts.prepare") as s3,
        pytest.raises(ValueError, match="artifact_delivery"),
    ):
        delivery.prepare(tmp_path)
    release.assert_not_called()
    s3.assert_not_called()
