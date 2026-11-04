"""Malformed release metadata never becomes an accepted delivery manifest."""

import io
import tarfile
from collections.abc import Mapping
from unittest.mock import patch

import pytest
import release_artifacts as release
from artifact_types import ReleaseSettings


def settings() -> ReleaseSettings:
    return {
        "repository": "example/project",
        "tag": "v1.0.0",
        "commit": "a" * 40,
        "manifest_sha512": "b" * 128,
        "manifest_size": 100,
    }


def manifest() -> dict[str, object]:
    pinned = settings()
    return {
        "schema": 1,
        "repository": pinned["repository"],
        "tag": pinned["tag"],
        "commit": pinned["commit"],
        "source": {
            "url": release.base(pinned["repository"], pinned["tag"]) + "source.tar.gz",
            "sha512": "c" * 128,
            "size": 1,
        },
        "packages": {},
    }


@pytest.mark.parametrize("value", [None, [], "PRIVATE", 1])
def test_non_object_release_is_rejected_without_body(value: object) -> None:
    with pytest.raises(ValueError, match="Release identity") as error:
        release.validate(value, settings(), {})
    assert "PRIVATE" not in str(error.value)


@pytest.mark.parametrize("value", [None, [], "PRIVATE", 1])
def test_non_object_package_map_is_rejected(value: object) -> None:
    document = manifest()
    document["packages"] = value
    with pytest.raises(ValueError, match="package set"):
        release.validate(document, settings(), {})


@pytest.mark.parametrize("value", [None, [], "PRIVATE", 1, {"sha512": None}, {"sha512": []}])
def test_missing_or_malformed_source_is_rejected(value: object) -> None:
    document = manifest()
    document["source"] = value
    with pytest.raises(ValueError, match="Invalid release asset"):
        release.validate(document, settings(), {})


def test_verified_manifest_keeps_asset_metadata_and_does_not_mutate_input() -> None:
    document = manifest()
    result = release.validate(document, settings(), {})
    assert result["source"] == document["source"]
    assert result["packages"] == document["packages"]
    assert set(document) == {"schema", "repository", "tag", "commit", "source", "packages"}


@pytest.mark.parametrize("checksum", [None, [], True, "PRIVATE"])
def test_malformed_digest_on_exact_release_url_is_rejected(checksum: object) -> None:
    document = manifest()
    source = document["source"]
    assert isinstance(source, Mapping)
    document["source"] = {**source, "sha512": checksum}
    with pytest.raises(ValueError, match="Invalid release asset"):
        release.validate(document, settings(), {})


def test_runtime_archive_reports_missing_file_payload() -> None:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        entry = tarfile.TarInfo("app/public.py")
        archive.addfile(entry, io.BytesIO())
    with (
        patch.object(tarfile.TarFile, "extractfile", return_value=None),
        pytest.raises(ValueError, match="no readable payload"),
    ):
        release.runtime_archive(stream.getvalue())
