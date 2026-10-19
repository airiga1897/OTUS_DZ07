"""Download official Elastic packages on the controller, verify SHA512."""
import hashlib
import concurrent.futures
import json
from pathlib import Path
import shutil
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / '.local/packages'
VERSION = '8.19.0'


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


def main():
    CACHE.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(CACHE).free < 3 * 2**30:
        raise RuntimeError('At least 3 GiB free controller disk space required')
    manifest = {}
    for product in ['filebeat', 'elasticsearch', 'kibana']:
        filename = product + '-' + VERSION + '-amd64.deb'
        directory = 'beats/filebeat' if product == 'filebeat' else product
        url = 'https://artifacts.elastic.co/downloads/' + directory + '/' + filename
        expected = urllib.request.urlopen(url + '.sha512', timeout=30).read().decode().split()[0]
        destination = CACHE / filename
        if not destination.exists() or hashlib.file_digest(destination.open('rb'), 'sha512').hexdigest() != expected:
            print('Downloading official package: ' + product, flush=True)
            partial = destination.with_suffix('.partial')
            download(url, partial)
            if hashlib.file_digest(partial.open('rb'), 'sha512').hexdigest() != expected:
                raise RuntimeError('SHA512 mismatch: ' + product)
            partial.replace(destination)
        manifest[filename] = expected
        print(product + ': SHA512 verified', flush=True)
    (CACHE / 'manifest.json').write_text(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    main()
