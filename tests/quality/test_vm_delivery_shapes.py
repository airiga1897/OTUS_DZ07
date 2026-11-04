"""VM delivery rejects malformed descriptors before replacing local files."""

import hashlib
import tarfile
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest
from remote import fetch_artifacts as worker


@pytest.mark.parametrize(
    "item",
    [
        None,
        [],
        "PRIVATE",
        {},
        {"url": None},
        {"url": []},
        {"url": "http://host.invalid/private"},
        {"url": "https://user:PRIVATE@host.invalid/private"},
    ],
)
def test_invalid_download_descriptor_never_opens_transport_or_replaces_file(tmp_path: Path, item: object) -> None:
    destination = tmp_path / "artifact.deb"
    destination.write_bytes(b"KEEP")
    opener = Mock()
    with pytest.raises(ValueError) as error:
        worker.download(item, destination, opener)
    assert "PRIVATE" not in str(error.value)
    opener.open.assert_not_called()
    assert destination.read_bytes() == b"KEEP"
    assert not destination.with_suffix(".partial").exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("sha512", None),
        ("sha512", []),
        ("sha512", "a" * 127),
        ("sha512", "A" * 128),
        ("size", None),
        ("size", []),
        ("size", True),
        ("size", "1"),
        ("size", 1.0),
        ("size", 0),
        ("size", -1),
        ("size", 2 * 2**30 + 1),
    ],
)
def test_digest_and_length_have_exact_types_and_bounds(tmp_path: Path, field: str, value: object) -> None:
    descriptor: dict[str, object] = {"url": "https://host.invalid/object", "sha512": "a" * 128, "size": 1}
    descriptor[field] = value
    opener = Mock()
    with pytest.raises(ValueError, match="digest or length"):
        worker.download(descriptor, tmp_path / "artifact.deb", opener)
    opener.open.assert_not_called()
    assert list(tmp_path.iterdir()) == []


def test_valid_cached_download_uses_the_same_digest_and_avoids_network(tmp_path: Path) -> None:
    content = b"synthetic immutable artifact"
    path = tmp_path / "artifact.deb"
    path.write_bytes(content)
    descriptor = {
        "url": "https://host.invalid/object",
        "sha512": hashlib.sha512(content).hexdigest(),
        "size": len(content),
    }
    opener = Mock()
    worker.download(descriptor, path, opener)
    opener.open.assert_not_called()
    assert path.read_bytes() == content


def test_missing_archive_payload_does_not_truncate_existing_application_file(tmp_path: Path) -> None:
    root = tmp_path / "destination"
    (root / "app").mkdir(parents=True)
    target = root / "app/content.txt"
    target.write_bytes(b"KEEP")
    member = tarfile.TarInfo("app/content.txt")
    member.size = 1
    opened = MagicMock()
    archive = opened.__enter__.return_value
    archive.getmembers.return_value = [member]
    archive.extractfile.return_value = None
    with patch.object(tarfile, "open", return_value=opened), pytest.raises(ValueError, match="payload unavailable"):
        worker.extract(tmp_path / "source.tar.gz", root)
    assert target.read_bytes() == b"KEEP"
    archive.extractfile.assert_called_once_with(member)


def test_package_progress_closures_keep_their_own_package_names(capsys: pytest.CaptureFixture[str]) -> None:
    first = worker._package_progress("filebeat.deb")
    second = worker._package_progress("kibana.deb")
    first(2**20, 2 * 2**20)
    second(3 * 2**20, 4 * 2**20)
    assert capsys.readouterr().out.splitlines() == ["filebeat.deb: 1/2 MiB", "kibana.deb: 3/4 MiB"]
