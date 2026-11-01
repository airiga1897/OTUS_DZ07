"""Build local release assets or consume a pinned public release; never publish."""
import argparse
import gzip
import io
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile

from local_runtime import checked_tree
from remote.fetch_artifacts import download
from s3_artifacts import digest, package_lock

ROOT = Path(__file__).resolve().parents[1]


def checked_settings(value: dict) -> dict:
    required = {'repository', 'tag', 'commit', 'manifest_sha512', 'manifest_size'}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError('Configure a published, pinned GitHub Release before deployment')
    base(value['repository'], value['tag'])
    if not isinstance(value['commit'], str) or not re.fullmatch('[a-f0-9]{40}', value['commit']):
        raise ValueError('Invalid release commit identity')
    if not isinstance(value['manifest_sha512'], str) or not re.fullmatch('[a-f0-9]{128}', value['manifest_sha512']):
        raise ValueError('Invalid release manifest SHA512')
    if type(value['manifest_size']) is not int or not 0 < value['manifest_size'] <= 2**20:
        raise ValueError('Invalid release manifest size')
    return dict(value)


def base(repository: str, tag: str) -> str:
    if (not isinstance(repository, str) or not re.fullmatch(r'[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+', repository)
            or not isinstance(tag, str) or not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?', tag)):
        raise ValueError('A repository and exact version tag are required')
    return 'https://github.com/' + repository + '/releases/download/' + tag + '/'


def validate(manifest: dict, settings: dict, lock: dict) -> dict:
    url = base(settings['repository'], settings['tag'])
    if (manifest.get('schema') != 1 or manifest.get('repository') != settings['repository']
            or manifest.get('tag') != settings['tag'] or manifest.get('commit') != settings['commit']
            or not re.fullmatch('[a-f0-9]{40}', settings['commit'])):
        raise ValueError('Release identity does not match the pinned configuration')
    if set(manifest.get('packages', {})) != set(lock):
        raise ValueError('Release package set differs from the pinned Elastic version')
    for name, item in [('source.tar.gz', manifest['source']), *manifest['packages'].items()]:
        if (item.get('url') != url + name or not re.fullmatch('[a-f0-9]{128}', item.get('sha512', ''))
                or type(item.get('size')) is not int or not 0 < item['size'] <= 2 * 2**30
                or (name in lock and item['sha512'] != lock[name])):
            raise ValueError('Invalid release asset URL, SHA512 or size')
    return {'source': manifest['source'], 'packages': manifest['packages']}


def prepare(root: Path = ROOT) -> dict:
    settings = checked_settings(json.loads((root / 'config/artifacts.json').read_text()).get('github_release', {}))
    url = base(settings['repository'], settings['tag']) + 'manifest.json'
    local = root / '.local'
    destination = local / 'artifact-delivery.json'
    checked_tree(destination, root.resolve())
    local.mkdir(exist_ok=True)
    # An old manifest must not survive a failed attempt to select another release.
    destination.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(dir=local) as folder:
        path = Path(folder) / 'manifest.json'
        download({'url': url, 'sha512': settings['manifest_sha512'], 'size': settings['manifest_size']}, path)
        manifest = validate(json.loads(path.read_text()), settings, package_lock(root))
    from stage_store import atomic_json
    atomic_json(destination, manifest, root)
    print('Pinned public GitHub Release manifest verified; no S3 credentials required', flush=True)
    return manifest


def git(root: Path, *args: str) -> bytes:
    return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True).stdout


def checked_archive(raw: bytes) -> None:
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:') as archive:
        for entry in archive.getmembers():
            path = PurePosixPath(entry.name)
            if (path.is_absolute() or '..' in path.parts or '\\' in entry.name or ':' in entry.name
                    or not (entry.isfile() or entry.isdir())
                    or any(p in ('.local', '.tools', '.git', 'media', '__pycache__') for p in path.parts)
                    or path.name in ('.env', 'operator.json', 'secrets.json', 'artifact-credentials.json')
                    or '.tfstate' in path.name or '.tfvars' in path.name
                    or path.suffix.lower() in ('.pem', '.key', '.sqlite3', '.tfplan')):
                raise ValueError('Private data or unsafe path in tracked release source')


def runtime_archive(raw: bytes) -> bytes:
    """Match the existing VM extractor contract without weakening its allowlist."""
    result = io.BytesIO()
    directories = ('app', 'scripts', 'config', 'ansible', 'terraform-selectel')
    files = ('README.md', 'requirements-local.txt', '.env.example')
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:') as source, tarfile.open(fileobj=result, mode='w') as target:
        for entry in source.getmembers():
            path = PurePosixPath(entry.name)
            if (not entry.isfile() or (path.parts[0] not in directories and entry.name not in files)
                    or (entry.name != '.env.example' and any(p.startswith('.') for p in path.parts))):
                continue
            data = source.extractfile(entry).read()
            normalized = tarfile.TarInfo(entry.name)
            normalized.size, normalized.mode = len(data), 0o644
            target.addfile(normalized, io.BytesIO(data))
    return result.getvalue()


def build(root: Path, tag: str, repository: str) -> Path:
    url = base(repository, tag)
    if git(root, 'status', '--porcelain').strip():
        raise ValueError('Release assets require a clean, committed working tree')
    commit = git(root, 'rev-parse', 'HEAD').decode().strip()
    lock = package_lock(root)
    cache = root / '.tools/elastic-packages'
    for name, sha in lock.items():
        if digest(cache / name) != sha:
            raise ValueError('A cached Elastic package does not match its official SHA512')
    raw = git(root, 'archive', '--format=tar', commit)
    checked_archive(raw)
    raw = runtime_archive(raw)
    output = root / '.tools/releases' / tag
    checked_tree(output, root.resolve())
    if output.exists():
        raise ValueError('Release assets already exist; refusing overwrite')
    output.parent.mkdir(parents=True, exist_ok=True)
    # Publish a completed local directory only, never a partial build.
    with tempfile.TemporaryDirectory(dir=output.parent) as folder:
        temporary = Path(folder) / tag
        temporary.mkdir()
        source = temporary / 'source.tar.gz'
        source.write_bytes(gzip.compress(raw, mtime=0))
        assets = {}
        for name in ['source.tar.gz', *lock]:
            path = temporary / name
            if name in lock:
                shutil.copyfile(cache / name, path)
            sha = digest(path)
            if name in lock and sha != lock[name]:
                raise ValueError('Package changed during release assembly')
            assets[name] = {'url': url + name, 'size': path.stat().st_size, 'sha512': sha}
        manifest = {'schema': 1, 'repository': repository, 'tag': tag, 'commit': commit,
                    'source': assets['source.tar.gz'], 'packages': {n: assets[n] for n in lock}}
        path = temporary / 'manifest.json'
        path.write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
        settings = {'repository': repository, 'tag': tag, 'commit': commit,
                    'manifest_sha512': digest(path), 'manifest_size': path.stat().st_size}
        (temporary / 'release-settings.json').write_text(json.dumps(settings, indent=2) + '\n')
        names = ['source.tar.gz', *lock, 'manifest.json', 'release-settings.json']
        (temporary / 'SHA512SUMS').write_text(''.join(digest(temporary / n) + '  ' + n + '\n' for n in names))
        if git(root, 'status', '--porcelain').strip() or git(root, 'rev-parse', 'HEAD').decode().strip() != commit:
            raise ValueError('Repository changed during release assembly')
        temporary.rename(output)
    return output


def configure(root: Path, assets: Path) -> dict:
    """Pin a locally verified bundle; never upload files or create a stand."""
    from stage_store import operation
    with operation(root / '.local'):
        return configure_locked(root, assets)


def configure_locked(root: Path, assets: Path) -> dict:
    root = root.resolve()
    checked_tree(assets, root)
    assets = assets.resolve()
    from test_cycle import assert_empty
    assert_empty(root)
    if git(root, 'status', '--porcelain').strip():
        raise ValueError('Release configuration requires a clean, committed working tree')
    settings = checked_settings(json.loads((assets / 'release-settings.json').read_text()))
    if git(root, 'rev-parse', 'HEAD').decode().strip() != settings['commit']:
        raise ValueError('Release configuration must follow its exact source commit')
    manifest_path = assets / 'manifest.json'
    if manifest_path.stat().st_size != settings['manifest_size'] or digest(manifest_path) != settings['manifest_sha512']:
        raise ValueError('Local release manifest size or SHA512 mismatch')
    manifest = validate(json.loads(manifest_path.read_text()), settings, package_lock(root))
    for name, item in [('source.tar.gz', manifest['source']), *manifest['packages'].items()]:
        asset = assets / name
        if asset.stat().st_size != item['size'] or digest(asset) != item['sha512']:
            raise ValueError('Local release asset size or SHA512 mismatch')
    artifacts_path, services_path = root / 'config/artifacts.json', root / 'config/services.json'
    for path in (artifacts_path, services_path):
        checked_tree(path, root)
    artifacts = json.loads(artifacts_path.read_text())
    services = json.loads(services_path.read_text())
    if (git(root, 'status', '--porcelain').strip()
            or git(root, 'rev-parse', 'HEAD').decode().strip() != settings['commit']):
        raise ValueError('Repository changed during release verification; configuration preserved')
    artifacts['github_release'] = settings
    services['artifact_delivery'] = 'release'
    from stage_store import atomic_json
    atomic_json(artifacts_path, artifacts, root)
    atomic_json(services_path, services, root)
    return settings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['build', 'configure', 'prepare'])
    parser.add_argument('--tag')
    parser.add_argument('--repository', default='airiga1897/OTUS_DZ07')
    parser.add_argument('--assets', type=Path, help='Local bundle directory for configure; no publication')
    args = parser.parse_args()
    if args.action == 'build':
        print(build(ROOT, args.tag, args.repository))
    elif args.action == 'configure':
        if args.assets is None:
            parser.error('configure requires --assets')
        configure(ROOT, args.assets)
        print('Local release binding configured; publication was not performed')
    else:
        prepare(ROOT)


if __name__ == '__main__':
    main()
