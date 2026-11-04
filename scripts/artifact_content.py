"""Provider-independent artifact hashes, pinned packages and source snapshots."""

import gzip
import hashlib
import io
import json
import re
import tarfile
from pathlib import Path


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha512").hexdigest()


def source_archive(root: Path, destination: Path) -> str:
    """Allowlist code only, with stable gzip/tar metadata. Never include runtime data."""
    directories = ("app", "ansible", "scripts", "config", "terraform-selectel")
    paths = []
    for directory in directories:
        for path in (root / directory).rglob("*"):
            relative = path.relative_to(root)
            if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                raise ValueError("Source links are not allowed")
            if not path.is_file() or any(
                part.startswith(".") or part in ("__pycache__", "media") for part in relative.parts
            ):
                continue
            if (
                path.suffix.lower() in (".pyc", ".pem", ".key", ".sqlite3", ".tfplan", ".env")
                or ".tfstate" in path.name
                or ".tfvars" in path.name
                or path.name in ("operator.json", "settings.json", "secrets.json")
            ):
                continue
            paths.append(path)
    paths += [
        root / name for name in ("README.md", "requirements-local.txt", ".env.example") if (root / name).is_file()
    ]
    with (
        destination.open("wb") as raw,
        gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w") as archive,
    ):
        for path in sorted(paths):
            if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                raise ValueError("Source links are not allowed")
            content = path.read_bytes()
            info = tarfile.TarInfo(path.relative_to(root).as_posix())
            info.size, info.mode = len(content), 0o644
            archive.addfile(info, io.BytesIO(content))
    return digest(destination)


def package_lock(root: Path) -> dict[str, str]:
    lock = json.loads((root / "config/elastic-artifacts.json").read_text())
    version = json.loads((root / "config/services.json").read_text())["elastic_version"]
    expected_names = {p + "-" + version + "-amd64.deb" for p in ("elasticsearch", "filebeat", "kibana")}
    if not isinstance(lock, dict) or set(lock) != expected_names:
        raise ValueError("Invalid pinned Elastic artifact lock")
    verified: dict[str, str] = {}
    for name in lock:
        checksum = lock[name]
        if not isinstance(checksum, str) or not re.fullmatch("[a-f0-9]{128}", checksum):
            raise ValueError("Invalid pinned Elastic artifact lock")
        verified[name] = checksum
    return verified
