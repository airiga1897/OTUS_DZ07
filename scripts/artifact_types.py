"""Validated controller delivery records; secrets are never part of these types."""

from typing import NotRequired, TypedDict

from remote.fetch_artifacts import DownloadItem


class DeliveryManifest(TypedDict):
    source: DownloadItem
    packages: dict[str, DownloadItem]


class ReleaseSettings(TypedDict):
    repository: str
    tag: str
    commit: str
    manifest_sha512: str
    manifest_size: int


class S3Settings(TypedDict):
    endpoint: str
    region: str
    bucket: str
    prefix: str
    expires_seconds: int


class ArtifactLocation(TypedDict):
    bucket: str
    prefix: str
    expires_seconds: int


class PackageVerification(TypedDict):
    size: int
    sha512_verified: bool
    private_acl_verified: bool


class PackageReport(TypedDict):
    success: bool
    packages: dict[str, PackageVerification]
    virtual_machines_created: int


class ArtifactBinding(TypedDict):
    provider: str
    project_id: str
    endpoint: str
    region: str
    bucket: str
    prefix: str


class ArtifactProbe(TypedDict):
    success: bool
    upload_verified: bool
    download_hash_verified: bool
    deletion_verified: bool
    region: str


class ArtifactPurge(TypedDict):
    success: bool
    bucket_deleted: bool
    prefix: str
    region: str
    objects_remaining: NotRequired[int]
    versions_remaining: NotRequired[int]
    multipart_remaining: NotRequired[int]
    checked_at_utc: NotRequired[str]


class MultipartUpload(TypedDict):
    Key: str
    UploadId: str
