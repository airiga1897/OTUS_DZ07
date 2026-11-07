"""Optional development delivery rejects unknown configuration and ACL shapes."""

import json
from pathlib import Path

import pytest
import s3_artifacts as artifacts
from pytest import CaptureFixture


@pytest.mark.parametrize("value", [None, [], "PRIVATE", {}, {"s3": None}, {"s3": []}])
def test_invalid_s3_document_fails_before_sdk_creation(tmp_path: Path, value: object) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config/artifacts.json").write_text(json.dumps(value))
    with pytest.raises(ValueError, match="configuration shape") as error:
        artifacts.settings(tmp_path)
    assert "PRIVATE" not in str(error.value)
    assert not (tmp_path / ".local").exists()


@pytest.mark.parametrize("field", ["endpoint", "region", "bucket", "prefix", "expires_seconds"])
@pytest.mark.parametrize("value", [None, [], {}, True])
def test_invalid_s3_field_types_are_rejected(tmp_path: Path, field: str, value: object) -> None:
    document: dict[str, object] = {
        "endpoint": "https://s3.ru-7.storage.selcloud.ru",
        "region": "ru-7",
        "bucket": "example-private",
        "prefix": "otus-dz07",
        "expires_seconds": 3600,
    }
    document[field] = value
    (tmp_path / "config").mkdir()
    (tmp_path / "config/artifacts.json").write_text(json.dumps({"s3": document}))
    with pytest.raises(ValueError, match="configuration shape"):
        artifacts.settings(tmp_path)


@pytest.mark.parametrize(
    "acl",
    [
        None,
        [],
        {},
        {"Owner": None},
        {"Owner": {}},
        {"Owner": {"ID": ""}},
        {"Owner": {"ID": 1}},
        {"Owner": {"ID": "owner"}},
        {"Owner": {"ID": "owner"}, "Grants": None},
        {"Owner": {"ID": "owner"}, "Grants": {}},
        {"Owner": {"ID": "owner"}, "Grants": [None]},
        {"Owner": {"ID": "owner"}, "Grants": [{"Grantee": None}]},
        {"Owner": {"ID": "owner"}, "Grants": [{"Grantee": {"Type": "CanonicalUser", "ID": "other"}}]},
    ],
)
def test_unknown_acl_cannot_confirm_private_storage(acl: object) -> None:
    with pytest.raises(ValueError, match="owner-only private ACL"):
        artifacts.private_acl(acl)


def test_owner_only_acl_is_accepted() -> None:
    artifacts.private_acl({"Owner": {"ID": "owner"}, "Grants": [{"Grantee": {"Type": "CanonicalUser", "ID": "owner"}}]})


def test_package_progress_reports_only_asset_name_and_mib(capsys: CaptureFixture[str]) -> None:
    artifacts.package_progress("elasticsearch.deb", 2**20, 3 * 2**20)
    assert capsys.readouterr().out == "elasticsearch.deb: 1/3 MiB\n"
