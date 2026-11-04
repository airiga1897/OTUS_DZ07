"""Deliver public code and verified packages; permanent S3 keys stay local."""

import contextlib
import json
import shlex

from artifact_content import digest, package_lock
from paramiko import SSHClient
from runtime_ports import DeliveryPort


def source(api: DeliveryPort, client: SSHClient, name: str) -> None:
    if name == "app":
        import delivery

        delivery.prepare(api.ROOT)
    target = api.STACK["paths"]["remote_root"]
    api.remote(client, "install -d -m 700 " + shlex.quote(target), "source-directory-" + name)
    driver = target + "/artifact-fetch.py"
    manifest = target + "/artifact-delivery.json"
    try:
        api.put(client, driver, (api.ROOT / "scripts/remote/fetch_artifacts.py").read_bytes())
        api.put(client, manifest, (api.LOCAL / "artifact-delivery.json").read_bytes())
        api.put(
            client,
            target + "/artifact-node.json",
            json.dumps(
                {
                    "name": name,
                    "version": json.loads((api.ROOT / "config/services.json").read_text())["elastic_version"],
                }
            ),
        )
        api.remote(client, "python3 " + shlex.quote(driver), "verified-source-" + name)
    finally:
        with client.open_sftp() as sftp:
            for path in (driver, manifest):
                with contextlib.suppress(FileNotFoundError):
                    sftp.remove(path)


def cached_packages(api: DeliveryPort, client: SSHClient) -> None:
    """Explicit offline alternative; never a fallback from S3 failure."""
    lock = package_lock(api.ROOT)
    directory = api.STACK["paths"]["remote_root"] + "/packages"
    api.remote(client, "mkdir -p " + shlex.quote(directory), "package-directory")
    with client.open_sftp() as sftp:
        for name, sha in lock.items():
            path = api.ROOT / ".tools/elastic-packages" / name
            if digest(path) != sha:
                raise ValueError("Pinned package digest mismatch")
            sftp.put(str(path), directory + "/" + name)
    api.put(client, directory + "/manifest.json", json.dumps(lock), mode=0o644)
