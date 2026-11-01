"""Download official Elastic packages on the controller, verify SHA512."""
import hashlib
import argparse
import concurrent.futures
import json
from pathlib import Path
import shutil
import re
import urllib.request
import topology

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / '.tools/elastic-packages'


def sha512(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha512').hexdigest()


def download(url, target):
    with urllib.request.urlopen(urllib.request.Request(url, method='HEAD'), timeout=30) as response:
        size = int(response.headers['Content-Length'])
    chunk_size = 2**20
    def chunk(start):
        end = min(start + chunk_size, size) - 1
        for attempt in range(3):
            try:
                request = urllib.request.Request(url + f'?otus_dz07_range={start}', headers={'Range': f'bytes={start}-{end}'})
                with urllib.request.urlopen(request, timeout=20) as response:
                    if response.status != 206:
                        raise RuntimeError('Official server did not honor byte range')
                    if response.headers.get('Content-Range') != f'bytes {start}-{end}/{size}':
                        raise RuntimeError('Unexpected artifact byte range')
                    data = response.read(end - start + 2)
                if len(data) != end - start + 1:
                    raise RuntimeError('Incomplete artifact range')
                return start, data
            except Exception:
                if attempt == 2:
                    raise
    with target.open('wb') as output:
        output.truncate(size)
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=8)
        futures = []
        try:
            futures = [pool.submit(chunk, start) for start in range(0, size, chunk_size)]
            completed = 0
            for future in concurrent.futures.as_completed(futures):
                start, data = future.result()
                output.seek(start)
                output.write(data)
                futures.remove(future)
                completed += len(data)
                if completed // (128 * 2**20) != (completed - len(data)) // (128 * 2**20):
                    print(f'Artifact received: {completed // 2**20}/{size // 2**20} MiB', flush=True)
        finally:
            for future in futures:
                future.cancel()
            pool.shutdown(wait=True, cancel_futures=True)


def main(source=None):
    import delivery
    if source is None and delivery.mode(ROOT) in ('s3', 'release'):
        delivery.prepare(ROOT)
        return
    if source is None and delivery.internet(ROOT):
        print('Elastic packages and Ansible dependencies will be downloaded by the VM', flush=True)
        return
    from prepare_ansible import prepare
    prepare(ROOT)
    version = topology.services(ROOT)['elastic_version']
    CACHE.mkdir(parents=True, exist_ok=True)
    previous_path = CACHE / 'manifest.json'
    previous = json.loads(previous_path.read_text()) if previous_path.exists() else {}
    manifest = {}
    for product in ['filebeat', 'elasticsearch', 'kibana']:
        filename = product + '-' + version + '-amd64.deb'
        directory = 'beats/filebeat' if product == 'filebeat' else product
        url = 'https://artifacts.elastic.co/downloads/' + directory + '/' + filename
        destination = CACHE / filename
        expected = previous.get(filename)
        if expected and re.fullmatch(r'[a-fA-F0-9]{128}', expected) and destination.exists():
            if sha512(destination) == expected.lower():
                manifest[filename] = expected.lower()
                print(product + ': cached SHA512 verified offline', flush=True)
                continue
        if source is not None:
            folder = Path(source).resolve()
            expected = (folder / (filename + '.sha512')).read_text().split()[0]
            if not re.fullmatch(r'[a-fA-F0-9]{128}', expected):
                raise ValueError('Invalid SHA512 for ' + product)
            if sha512(folder / filename) != expected.lower():
                raise ValueError('Source SHA512 mismatch for ' + product)
            if (folder / filename).resolve() != destination.resolve():
                shutil.copyfile(folder / filename, destination)
        else:
            expected = urllib.request.urlopen(url + '.sha512', timeout=30).read().decode().split()[0]
        if not re.fullmatch(r'[a-fA-F0-9]{128}', expected):
            raise ValueError('Invalid published SHA512 for ' + product)
        expected = expected.lower()
        if not destination.exists() or sha512(destination) != expected:
            if shutil.disk_usage(CACHE).free < 3 * 2**30:
                raise RuntimeError('At least 3 GiB free controller disk space required')
            print('Downloading official package: ' + product, flush=True)
            partial = destination.with_suffix('.partial')
            try:
                download(url, partial)
                if sha512(partial) != expected:
                    raise RuntimeError('SHA512 mismatch: ' + product)
                partial.replace(destination)
            finally:
                partial.unlink(missing_ok=True)
        manifest[filename] = expected
        previous_path.write_text(json.dumps({**previous, **manifest}, indent=2))
        print(product + ': SHA512 verified', flush=True)
    (CACHE / 'manifest.json').write_text(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', help='Explicit directory with the three deb files and their SHA512 files')
    main(parser.parse_args().source)
