"""A real clean clone of current project files consumes Releases without S3."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import release_artifacts as release

ROOT = Path(__file__).resolve().parents[1]

CONSUMER = r'''
import hashlib, io, json, os, socket, sys
from pathlib import Path
from unittest.mock import Mock, patch
clone, assets, vm = map(Path, sys.argv[1:])
sys.path.insert(0, str(clone / 'scripts'))
import delivery, release_artifacts, run_settings, topology, render
from cloud_contract import InfrastructureResult
from remote import fetch_artifacts as worker
assert Path(release_artifacts.__file__).resolve().is_relative_to(clone)
assert delivery.mode(clone) == 'release'
assert not (clone / '.local').exists() and not (clone / '.tools').exists()
def forbidden(*args, **kwargs):
    raise AssertionError('Network, S3 or cloud access is forbidden in this local rehearsal')
class Response(io.BytesIO):
    status = 200
    def __init__(self, body, url):
        super().__init__(body); self.url = url
    def geturl(self): return self.url
    def read1(self, count): return self.read(count)
requested = []
def open_asset(url, **kwargs):
    assert url.startswith('https://github.com/example/repository/releases/download/v0.0.1-local/')
    requested.append(url.rsplit('/', 1)[1])
    return Response((assets / requested[-1]).read_bytes(), url)
opener = Mock(open=open_asset)
original = worker.download
def fetch(item, destination, **kwargs):
    return original(item, destination, opener=opener, **kwargs)
vm.mkdir()
with patch.object(socket.socket, 'connect', forbidden), patch.object(socket, 'create_connection', forbidden), \
        patch('boto3.client', side_effect=forbidden), patch('s3_artifacts.client', side_effect=forbidden), \
        patch.object(release_artifacts, 'download', side_effect=fetch), patch.object(worker, 'download', side_effect=fetch):
    manifest = delivery.prepare(clone)
    (vm / 'artifact-delivery.json').write_text(json.dumps(manifest))
    (vm / 'artifact-node.json').write_text('{"name":"es1","version":"8.19.0"}')
    with patch.object(worker, 'BASE', vm): worker.main()
assert requested == ['manifest.json', 'source.tar.gz', *manifest['packages']]
assert not (vm / 'artifact-delivery.json').exists()
for path in ('ansible/site.yml', 'scripts/remote/ansible_job.py', 'config/placement.json',
             'app/knowledge/fixtures/initial_data.json'):
    if path.endswith('initial_data.json'):
        assert list((vm / 'app/knowledge/fixtures').glob('*.json'))
    else: assert (vm / path).is_file(), path
for name, item in manifest['packages'].items():
    assert hashlib.sha512((vm / 'packages' / name).read_bytes()).hexdigest() == item['sha512']
assert (clone / 'terraform-selectel/staged/node/.terraform.lock.hcl').is_file()
assert run_settings.placement(clone)['preferred_pools'][:3] == ['ru-2','ru-9','ru-3']
nodes = {}
for name, node in topology.load(clone)['nodes'].items():
    nodes[name] = dict(id='test-' + name, roles=[node['role']], private_ip='10.77.0.' + str(node['host']),
                       volume_id='volume-' + name, port_id='port-' + name)
nodes['app']['public_ip'] = '8.8.8.8'
result = InfrastructureResult('selectel', {'project_id':'synthetic'}, 'otus-dz07',
    {'region':'ru-2','zone':'ru-2a'}, nodes, '10.77.0.0/24')
passwords = {key: 'synthetic-only' for key in ('django','app','postgres','api','replicator','elastic','shipper','kibana','kibana_encryption')}
inventory = render.ansible_inventory(result, passwords)
assert set(inventory['all']['children']['elasticsearch']['hosts']) == {'es1','es2','es3'}
assert 'app' in inventory['all']['children']['kibana']['hosts']
assert not (clone / '.local/artifact-credentials.json').exists()
print('CURRENT_PROJECT_CLEAN_CLONE_RELEASE_OK')
'''


class CurrentProjectReleaseClone(unittest.TestCase):
    def commit(self, root):
        release.git(root, 'add', '.')
        release.git(root, '-c', 'user.name=Local Test', '-c', 'user.email=local@example.invalid',
                    '-c', 'commit.gpgsign=false', 'commit', '-qm', 'isolated rehearsal snapshot')

    def test_current_sources_two_commits_clean_clone_and_guest_delivery(self):
        original_head = release.git(ROOT, 'rev-parse', 'HEAD')
        with tempfile.TemporaryDirectory(dir=ROOT / '.tools') as directory:
            work = Path(directory)
            origin = work / 'origin'
            origin.mkdir()
            files = release.git(ROOT, 'ls-files', '-z', '--cached', '--others', '--exclude-standard').decode().split('\0')
            for name in filter(None, files):
                source = ROOT / name
                if not source.exists():
                    continue  # Already deleted files in the uncommitted transition.
                self.assertFalse(source.is_symlink())
                self.assertTrue(source.resolve().is_relative_to(ROOT))
                target = origin / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
            cache = origin / '.tools/elastic-packages'
            cache.mkdir(parents=True)
            lock = {}
            version = json.loads((origin / 'config/services.json').read_text())['elastic_version']
            for component in ('elasticsearch', 'filebeat', 'kibana'):
                name = component + '-' + version + '-amd64.deb'
                body = (component + '-synthetic-local-only').encode()
                (cache / name).write_bytes(body)
                lock[name] = hashlib.sha512(body).hexdigest()
            (origin / 'config/elastic-artifacts.json').write_text(json.dumps(lock))
            release.git(origin, 'init', '-q')
            self.commit(origin)
            assets = release.build(origin, 'v0.0.1-local', 'example/repository')
            release.configure(origin, assets)
            self.commit(origin)
            clone = work / 'reviewer'
            subprocess.run(['git', 'clone', '-q', '--local', str(origin), str(clone)], check=True, capture_output=True)
            self.assertFalse(release.git(clone, 'status', '--porcelain').strip())
            env = {k:v for k,v in os.environ.items() if not k.startswith(('OS_', 'AWS_', 'S3_', 'OTUS_'))}
            result = subprocess.run([sys.executable, '-c', CONSUMER, str(clone), str(assets), str(work / 'vm')],
                                    env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('CURRENT_PROJECT_CLEAN_CLONE_RELEASE_OK', result.stdout)
            self.assertEqual(release.git(ROOT, 'rev-parse', 'HEAD'), original_head)
