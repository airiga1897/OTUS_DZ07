"""Download public Elastic artifacts on the VM; never accept an unverified file."""

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = Path("/home/otus/dz07")


def fetch(directory: Path, version: str, base_url: str, opener: urllib.request.OpenerDirector | None = None) -> None:
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.hostname != "artifacts.elastic.co":
        raise ValueError("Require the official HTTPS Elastic artifact source")
    directory.mkdir(parents=True, exist_ok=True)
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
    manifest = {}
    for product in ("elasticsearch", "filebeat", "kibana"):
        name = product + "-" + version + "-amd64.deb"
        url = base_url.rstrip("/") + "/" + ("beats/filebeat" if product == "filebeat" else product) + "/" + name
        with opener.open(url + ".sha512", timeout=30) as response:
            expected = response.read(512).decode().split()[0].lower()
        if not re.fullmatch("[a-f0-9]{128}", expected):
            raise ValueError("Invalid official SHA512")
        target = directory / name
        if target.exists():
            with target.open("rb") as stream:
                matched = hashlib.file_digest(stream, "sha512").hexdigest() == expected
            if matched:
                manifest[name] = expected
                continue
        temporary = target.with_suffix(".partial")
        try:
            digest = hashlib.sha512()
            with opener.open(url, timeout=120) as response, temporary.open("wb") as stream:
                while block := response.read(2**20):
                    digest.update(block)
                    stream.write(block)
            if digest.hexdigest() != expected:
                raise ValueError("Downloaded SHA512 mismatch: " + product)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        manifest[name] = expected
        print(product + ": VM HTTPS download and SHA512 verified", flush=True)
    (directory / "manifest.json").write_text(json.dumps(manifest))


if __name__ == "__main__":
    settings = json.loads((BASE / "elk-settings.json").read_text())
    try:
        fetch(BASE / "packages", settings["version"], settings["elastic_download_base"])
    except urllib.error.HTTPError as error:
        if error.code == 403:
            raise SystemExit("ARTIFACT_SOURCE_UNAVAILABLE: official HTTPS source returned HTTP 403") from None
        raise
