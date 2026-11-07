"""Private, content-addressed development artifacts; no stand lifecycle operations."""

import argparse
import json
import os
import re
import ssl
import tempfile
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, cast
from urllib.parse import parse_qsl, urlsplit

import privacy
from artifact_content import digest, package_lock, source_archive
from artifact_types import ArtifactLocation, DeliveryManifest, PackageReport, S3Settings
from remote.fetch_artifacts import DownloadItem

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client

ROOT = Path(__file__).resolve().parents[1]


def settings(root: Path) -> S3Settings:
    document = json.loads((root / "config/artifacts.json").read_text())
    values = document.get("s3") if isinstance(document, dict) else None
    if (
        not isinstance(values, dict)
        or any(not isinstance(values.get(field), str) for field in ("endpoint", "region", "bucket", "prefix"))
        or type(values.get("expires_seconds")) is not int
    ):
        raise ValueError("Invalid S3 artifact configuration shape")
    url = urlsplit(values["endpoint"])
    if (
        url.scheme != "https"
        or not url.hostname
        or not url.hostname.endswith(".storage.selcloud.ru")
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path not in ("", "/")
        or url.port not in (None, 443)
    ):
        raise ValueError("Require a Selectel S3 HTTPS endpoint")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", values.get("bucket") or ""):
        raise ValueError("Configure the private bucket in config/artifacts.json before deployment")
    if values["prefix"] != "otus-dz07" or not 300 <= values["expires_seconds"] <= 3600:
        raise ValueError("Invalid artifact namespace or URL lifetime")
    return cast(S3Settings, values)


def client(root: Path, config: S3Settings) -> "S3Client":
    import boto3
    from botocore.config import Config

    schema = json.loads((root / "config/artifact-auth.json").read_text())["fields"]
    credentials = {name: os.environ.get(field["env"], "") for name, field in schema.items()}
    if not all(credentials.values()):
        raise ValueError("S3 credentials missing; dot-source scripts/env.ps1 -Artifacts")
    privacy.remember(credentials.values())
    verify: bool | str = True
    if os.name == "nt":
        # Match Python HTTPS/Windows trust rather than silently disabling TLS
        # when botocore's bundled CA list differs from the operator OS store.
        anchors = ssl.create_default_context().get_ca_certs(binary_form=True)
        if not anchors:
            raise ValueError("No system trust anchors available for S3 HTTPS")
        bundle = root / ".tools/system-trust-ca.pem"
        from local_runtime import checked_tree

        checked_tree(bundle, root.resolve())
        bundle.parent.mkdir(parents=True, exist_ok=True)
        bundle.write_text("".join(ssl.DER_cert_to_PEM_cert(certificate) for certificate in anchors))
        verify = str(bundle)
    return boto3.client(
        "s3",
        endpoint_url=config["endpoint"],
        region_name=config["region"],
        aws_access_key_id=credentials["access_key"],
        aws_secret_access_key=credentials["secret_key"],
        verify=verify,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "virtual"},
            connect_timeout=15,
            read_timeout=120,
            retries={"max_attempts": 3},
        ),
    )


def private_acl(acl: object) -> None:
    if not isinstance(acl, dict) or not isinstance(acl.get("Owner"), dict):
        raise ValueError("Artifacts require an owner-only private ACL")
    owner = acl["Owner"].get("ID")
    grants = acl.get("Grants")
    if (
        not isinstance(owner, str)
        or not owner
        or not isinstance(grants, list)
        or not grants
        or any(
            not isinstance(grant, dict)
            or not isinstance(grant.get("Grantee"), dict)
            or grant["Grantee"].get("Type") != "CanonicalUser"
            or grant["Grantee"].get("ID") != owner
            for grant in grants
        )
    ):
        raise ValueError("Artifacts require an owner-only private ACL")


def require_private(storage: "S3Client", bucket: str) -> None:
    private_acl(storage.get_bucket_acl(Bucket=bucket))
    try:
        policy = json.loads(storage.get_bucket_policy(Bucket=bucket)["Policy"])
    except Exception as error:
        if getattr(error, "response", {}).get("Error", {}).get("Code") != "NoSuchBucketPolicy":
            raise
    else:
        for statement in policy.get("Statement", []):
            if statement.get("Effect") == "Allow" and (
                statement.get("NotPrincipal") is not None or "*" in json.dumps(statement.get("Principal", {}))
            ):
                raise ValueError("Broad bucket policy is not allowed for development artifacts")


def ensure(storage: "S3Client", config: ArtifactLocation, path: Path, kind: str, sha: str) -> DownloadItem:
    key = "/".join((config["prefix"], kind, sha, path.name))
    try:
        head = storage.head_object(Bucket=config["bucket"], Key=key)
    except Exception as error:
        if getattr(error, "response", {}).get("Error", {}).get("Code") not in ("404", "NoSuchKey", "NotFound"):
            raise
        from boto3.s3.transfer import TransferConfig

        print(f"Uploading {path.name}: {path.stat().st_size // (1024 * 1024)} MiB", flush=True)
        storage.upload_file(
            str(path),
            config["bucket"],
            key,
            ExtraArgs={"ACL": "private", "Metadata": {"sha512": sha}},
            Config=TransferConfig(max_concurrency=1, use_threads=False),
        )
        head = storage.head_object(Bucket=config["bucket"], Key=key)
    if head.get("Metadata", {}).get("sha512") != sha or head["ContentLength"] != path.stat().st_size:
        raise ValueError("Existing immutable artifact differs; refusing overwrite")
    private_acl(storage.get_object_acl(Bucket=config["bucket"], Key=key))
    url = storage.generate_presigned_url(
        "get_object", Params={"Bucket": config["bucket"], "Key": key}, ExpiresIn=config["expires_seconds"]
    )
    if urlsplit(url).scheme != "https":
        raise ValueError("Presigned artifact URL must use HTTPS")
    privacy.remember([url])
    privacy.remember(
        value
        for name, value in parse_qsl(urlsplit(url).query)
        if name.lower() in ("x-amz-signature", "x-amz-credential", "x-amz-security-token")
    )
    return {"url": url, "sha512": sha, "size": path.stat().st_size}


def package_progress(name: str, count: int, total: int) -> None:
    print(f"{name}: {count // 2**20}/{total // 2**20} MiB", flush=True)


def verify_packages(root: Path = ROOT, storage: "S3Client | None" = None) -> PackageReport:
    """Read-only round trip of every pinned package using the VM downloader."""
    from remote.fetch_artifacts import download

    config = settings(root)
    lock = package_lock(root)
    storage = storage or client(root, config)
    require_private(storage, config["bucket"])
    report: PackageReport = {"success": False, "packages": {}, "virtual_machines_created": 0}
    work = root / ".tools/artifact-build"
    work.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(dir=work) as directory:
            for name, sha in lock.items():
                key = "/".join((config["prefix"], "packages", sha, name))
                head = storage.head_object(Bucket=config["bucket"], Key=key)
                if head.get("Metadata", {}).get("sha512") != sha:
                    raise ValueError("S3 package metadata does not match the pinned digest")
                private_acl(storage.get_object_acl(Bucket=config["bucket"], Key=key))
                url = storage.generate_presigned_url(
                    "get_object", Params={"Bucket": config["bucket"], "Key": key}, ExpiresIn=config["expires_seconds"]
                )
                privacy.remember([url])
                size = head["ContentLength"]
                download(
                    {"url": url, "size": size, "sha512": sha},
                    Path(directory) / name,
                    progress=partial(package_progress, name),
                )
                report["packages"][name] = {"size": size, "sha512_verified": True, "private_acl_verified": True}
                print(name + ": full HTTPS download and pinned SHA512 verified", flush=True)
            report["success"] = True
    finally:
        destination = root / "docs/s3-package-downloads.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report, indent=2) + "\n")
    return report


def prepare(root: Path = ROOT, storage: "S3Client | None" = None) -> DeliveryManifest:
    config = settings(root)
    lock = package_lock(root)
    cache = root / ".tools/elastic-packages"
    for name, sha in lock.items():
        if not (cache / name).is_file() or digest(cache / name) != sha:
            raise ValueError("A verified package is missing; prepare the explicit local cache first")
    storage = storage or client(root, config)
    require_private(storage, config["bucket"])
    from artifact_lifecycle import lease

    lease(root, storage, config)
    packages = {name: ensure(storage, config, cache / name, "packages", sha) for name, sha in lock.items()}
    work = root / ".tools/artifact-build"
    work.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=work) as directory:
        source = Path(directory) / "source.tar.gz"
        sha = source_archive(root, source)
        manifest: DeliveryManifest = {"source": ensure(storage, config, source, "sources", sha), "packages": packages}
    local = root / ".local"
    local.mkdir(exist_ok=True)
    destination = local / "artifact-delivery.json"
    destination.write_text(json.dumps(manifest))
    print("Private S3 artifacts verified; temporary delivery manifest prepared", flush=True)
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", default="prepare", choices=["prepare", "verify", "verify-packages"])
    action = parser.parse_args().action
    try:
        if action == "verify":
            from artifact_lifecycle import probe

            probe(ROOT)
        elif action == "verify-packages":
            verify_packages()
        else:
            prepare()
    except Exception as error:
        raise SystemExit("S3 artifact operation failed: " + privacy.redact(str(error))) from None
