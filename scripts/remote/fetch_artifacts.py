"""Receive immutable files over temporary HTTPS URLs; never log those URLs."""
import hashlib
import http.client
import json
from pathlib import Path, PurePosixPath
import shutil
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

BASE = Path('/home/otus/dz07')


class HTTPSRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        if urlsplit(newurl).scheme != 'https':
            raise ValueError('Artifact redirect must remain HTTPS')
        return super().redirect_request(request, response, code, message, headers, newurl)


def download(item, destination, opener=None, *, progress=None, timeout=30, deadline=1800, attempts=3):
    import re
    url = urlsplit(item['url'])
    if url.scheme != 'https' or not url.hostname or url.username or url.password:
        raise ValueError('HTTPS artifact URL required')
    if not re.fullmatch('[a-f0-9]{128}', item['sha512']) or not 0 < item['size'] <= 2 * 2**30:
        raise ValueError('Invalid artifact digest or length')
    if destination.exists():
        with destination.open('rb') as stream:
            if destination.stat().st_size == item['size'] and hashlib.file_digest(stream, 'sha512').hexdigest() == item['sha512']:
                return
    temporary = destination.with_suffix('.partial')
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), HTTPSRedirect())
    expires = time.monotonic() + deadline
    try:
        for attempt in range(attempts):
            digest, count, reported = hashlib.sha512(), 0, 0
            try:
                remaining = expires - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Artifact download deadline exceeded')
                with opener.open(item['url'], timeout=min(timeout, remaining)) as response, temporary.open('wb') as stream:
                    if response.status != 200 or response.geturl().split(':', 1)[0] != 'https':
                        raise ValueError('Invalid artifact response')
                    while True:
                        if time.monotonic() >= expires:
                            raise TimeoutError('Artifact download deadline exceeded')
                        # read() may wait to fill the entire requested buffer while
                        # trickled bytes keep resetting a socket's idle timeout.
                        block = response.read1(65536)
                        if not block:
                            break
                        count += len(block)
                        if count > item['size']:
                            raise ValueError('Artifact exceeds declared length')
                        stream.write(block)
                        digest.update(block)
                        if progress and (not reported or count - reported >= 32 * 2**20):
                            progress(count, item['size'])
                            reported = count
                if count != item['size'] or digest.hexdigest() != item['sha512']:
                    raise ValueError('Artifact checksum or length mismatch')
                temporary.replace(destination)
                if progress:
                    progress(count, item['size'])
                return
            except (OSError, urllib.error.URLError, http.client.IncompleteRead) as error:
                if isinstance(error, urllib.error.HTTPError) and error.code not in (429, 500, 502, 503, 504):
                    raise
                if attempt + 1 == attempts or time.monotonic() >= expires:
                    raise
                # Start from zero: never append a potentially different response.
                if progress:
                    progress(0, item['size'])
    finally:
        temporary.unlink(missing_ok=True)


def extract(source, destination):
    with tarfile.open(source, 'r:gz') as archive:
        members = archive.getmembers()
        if sum(m.size for m in members) > 128 * 2**20:
            raise ValueError('Source bundle too large')
        for member in members:
            path = PurePosixPath(member.name)
            if (not member.isfile() or path.is_absolute() or '..' in path.parts or not path.parts
                    or path.parts[0] not in ('app', 'scripts', 'config', 'ansible', 'terraform-selectel', 'README.md', 'requirements-local.txt', '.env.example')
                    or (member.name != '.env.example' and any(part.startswith('.') for part in path.parts))
                    or '\\' in member.name or ':' in member.name):
                raise ValueError('Unsafe source bundle member')
            target = destination / member.name
            if target.is_symlink() or any(p.is_symlink() for p in target.parents if p != destination.parent):
                raise ValueError('Source destination contains a link')
        for member in members:
            target = destination / member.name
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as stream, target.open('wb') as output:
                shutil.copyfileobj(stream, output)
            target.chmod(0o644)


def main():
    path = BASE / 'artifact-delivery.json'
    phase = 'read-manifest'
    try:
        manifest = json.loads(path.read_text())
        with tempfile.TemporaryDirectory(dir=BASE) as directory:
            source = Path(directory) / 'source.tar.gz'
            phase = 'download-source'
            download(manifest['source'], source)
            phase = 'extract-source'
            extract(source, BASE)
        phase = 'read-node-settings'
        if json.loads((BASE / 'artifact-node.json').read_text())['name'] == 'es1':
            packages = BASE / 'packages'
            packages.mkdir(exist_ok=True)
            expected = {}
            version = json.loads((BASE / 'artifact-node.json').read_text())['version']
            names = {p + '-' + version + '-amd64.deb' for p in ('elasticsearch', 'filebeat', 'kibana')}
            if set(manifest['packages']) != names:
                raise ValueError('Unexpected package manifest')
            for name, item in manifest['packages'].items():
                phase = 'download-package'
                download(item, packages / name, progress=lambda count, size:
                         print(f'{name}: {count // 2**20}/{size // 2**20} MiB', flush=True))
                expected[name] = item['sha512']
                print(name + ': downloaded and SHA512 verified', flush=True)
            (packages / 'manifest.json').write_text(json.dumps(expected))
        print('Source version downloaded and SHA512 verified', flush=True)
    except Exception as error:
        detail = 'phase=' + phase + '; type=' + type(error).__name__
        if isinstance(error, urllib.error.HTTPError):
            detail += '; http=' + str(error.code)
        raise SystemExit('Private artifact retrieval failed; ' + detail + '; URL suppressed') from None
    finally:
        path.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
