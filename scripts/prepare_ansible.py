"""Prepare a hashed Linux control-node wheelhouse from Windows, without a cloud."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def prepare(root: Path) -> Path:
    source = root / 'ansible/requirements-control.txt'
    lock_data = source.read_bytes()
    version = next(line.split('==', 1)[1].split()[0] for line in source.read_text().splitlines()
                   if line.startswith('ansible-core=='))
    destination = root / '.local/ansible-wheels'
    destination.mkdir(parents=True, exist_ok=True)
    marker = destination / 'manifest.json'
    if marker.exists():
        manifest = json.loads(marker.read_text())
        if (manifest['version'] == version and (destination / 'requirements.lock').exists()
                and (destination / 'requirements.lock').read_bytes() == lock_data
                and all((destination / name).exists() and
                hashlib.sha256((destination / name).read_bytes()).hexdigest() == digest
                for name, digest in manifest['files'].items())):
            return destination
    with tempfile.TemporaryDirectory(prefix='ansible-download-', dir=destination.parent) as temporary:
        env = dict(os.environ, TEMP=temporary, TMP=temporary, TMPDIR=temporary)
        subprocess.run([sys.executable, '-m', 'pip', 'download', '--only-binary=:all:',
            '--platform', 'manylinux2014_x86_64', '--python-version', '3.12', '--implementation', 'cp',
            '--abi', 'cp312', '--dest', str(destination), '--require-hashes', '-r', str(source)],
            check=True, env=env)
    hashes = {}
    for wheel in sorted(destination.glob('*.whl')):
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        hashes[wheel.name] = digest
    lock = destination / 'requirements.lock'
    lock.write_bytes(lock_data)
    hashes[lock.name] = hashlib.sha256(lock.read_bytes()).hexdigest()
    marker.write_text(json.dumps({'version': version, 'files': hashes}, indent=2))
    return destination


if __name__ == '__main__':
    import delivery
    root = Path(__file__).resolve().parents[1]
    if delivery.internet(root):
        print('Ansible dependencies will be installed by the VM from the hashed lock file')
    else:
        prepare(root)
