"""Private, content-addressed development artifacts; no stand lifecycle operations."""
import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tarfile
import tempfile
import ssl
from urllib.parse import urlsplit, parse_qsl

import privacy

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha512').hexdigest()


def source_archive(root, destination):
    """Allowlist code only, with stable gzip/tar metadata. Never include runtime data."""
    directories = ('app', 'ansible', 'scripts', 'config', 'terraform-selectel')
    paths = []
    for directory in directories:
        for path in (root / directory).rglob('*'):
            relative = path.relative_to(root)
            if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
                raise ValueError('Source links are not allowed')
            if not path.is_file() or any(part.startswith('.') or part in ('__pycache__', 'media') for part in relative.parts):
                continue
            if (path.suffix.lower() in ('.pyc', '.pem', '.key', '.sqlite3', '.tfplan', '.env')
                    or '.tfstate' in path.name or '.tfvars' in path.name or path.name in ('operator.json', 'settings.json', 'secrets.json')):
                continue
            paths.append(path)
    paths += [root / name for name in ('README.md', 'requirements-local.txt', '.env.example') if (root / name).is_file()]
    with destination.open('wb') as raw, gzip.GzipFile(fileobj=raw, mode='wb', filename='', mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode='w') as archive:
            for path in sorted(paths):
                if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
                    raise ValueError('Source links are not allowed')
                content = path.read_bytes()
                info = tarfile.TarInfo(path.relative_to(root).as_posix())
                info.size, info.mode = len(content), 0o644
                archive.addfile(info, io.BytesIO(content))
    return digest(destination)


def settings(root):
    values = json.loads((root / 'config/artifacts.json').read_text())['s3']
    url = urlsplit(values['endpoint'])
    if (url.scheme != 'https' or not url.hostname or not url.hostname.endswith('.storage.selcloud.ru')
            or url.username or url.password or url.query or url.fragment or url.path not in ('', '/')
            or url.port not in (None, 443)):
        raise ValueError('Require a Selectel S3 HTTPS endpoint')
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{1,61}[a-z0-9]', values.get('bucket') or ''):
        raise ValueError('Configure the private bucket in config/artifacts.json before deployment')
    if values['prefix'] != 'otus-dz07' or not 300 <= values['expires_seconds'] <= 3600:
        raise ValueError('Invalid artifact namespace or URL lifetime')
    return values


def client(root, config):
    import boto3
    from botocore.config import Config
    schema = json.loads((root / 'config/artifact-auth.json').read_text())['fields']
    credentials = {name: os.environ.get(field['env']) for name, field in schema.items()}
    if not all(credentials.values()):
        raise ValueError('S3 credentials missing; dot-source scripts/env.ps1 -Artifacts')
    privacy.remember(credentials.values())
    verify = True
    if os.name == 'nt':
        # Match Python HTTPS/Windows trust rather than silently disabling TLS
        # when botocore's bundled CA list differs from the operator OS store.
        anchors = ssl.create_default_context().get_ca_certs(binary_form=True)
        if not anchors:
            raise ValueError('No system trust anchors available for S3 HTTPS')
        bundle = root / '.tools/system-trust-ca.pem'
        from local_runtime import checked_tree
        checked_tree(bundle, root.resolve())
        bundle.parent.mkdir(parents=True, exist_ok=True)
        bundle.write_text(''.join(ssl.DER_cert_to_PEM_cert(certificate) for certificate in anchors))
        verify = str(bundle)
    return boto3.client('s3', endpoint_url=config['endpoint'], region_name=config['region'],
                        aws_access_key_id=credentials['access_key'], aws_secret_access_key=credentials['secret_key'],
                        verify=verify, config=Config(signature_version='s3v4', s3={'addressing_style': 'virtual'},
                                      connect_timeout=15, read_timeout=120, retries={'max_attempts': 3}))


def private_acl(acl):
    owner = acl['Owner']['ID']
    if not acl.get('Grants') or any(g['Grantee'].get('Type') != 'CanonicalUser' or
                                   g['Grantee'].get('ID') != owner for g in acl['Grants']):
        raise ValueError('Artifacts require an owner-only private ACL')


def require_private(storage, bucket):
    private_acl(storage.get_bucket_acl(Bucket=bucket))
    try:
        policy = json.loads(storage.get_bucket_policy(Bucket=bucket)['Policy'])
    except Exception as error:
        if getattr(error, 'response', {}).get('Error', {}).get('Code') != 'NoSuchBucketPolicy':
            raise
    else:
        for statement in policy.get('Statement', []):
            if statement.get('Effect') == 'Allow' and (statement.get('NotPrincipal') is not None
                    or '*' in json.dumps(statement.get('Principal', {}))):
                raise ValueError('Broad bucket policy is not allowed for development artifacts')


def ensure(storage, config, path, kind, sha):
    key = '/'.join((config['prefix'], kind, sha, path.name))
    try:
        head = storage.head_object(Bucket=config['bucket'], Key=key)
    except Exception as error:
        if getattr(error, 'response', {}).get('Error', {}).get('Code') not in ('404', 'NoSuchKey', 'NotFound'):
            raise
        from boto3.s3.transfer import TransferConfig
        print(f'Uploading {path.name}: {path.stat().st_size // (1024 * 1024)} MiB', flush=True)
        storage.upload_file(str(path), config['bucket'], key,
            ExtraArgs={'ACL': 'private', 'Metadata': {'sha512': sha}},
            Config=TransferConfig(max_concurrency=1, use_threads=False))
        head = storage.head_object(Bucket=config['bucket'], Key=key)
    if head.get('Metadata', {}).get('sha512') != sha or head['ContentLength'] != path.stat().st_size:
        raise ValueError('Existing immutable artifact differs; refusing overwrite')
    private_acl(storage.get_object_acl(Bucket=config['bucket'], Key=key))
    url = storage.generate_presigned_url('get_object', Params={'Bucket': config['bucket'], 'Key': key},
                                         ExpiresIn=config['expires_seconds'])
    if urlsplit(url).scheme != 'https':
        raise ValueError('Presigned artifact URL must use HTTPS')
    privacy.remember([url])
    privacy.remember(value for name, value in parse_qsl(urlsplit(url).query)
                     if name.lower() in ('x-amz-signature', 'x-amz-credential', 'x-amz-security-token'))
    return {'url': url, 'sha512': sha, 'size': path.stat().st_size}


def package_lock(root):
    lock = json.loads((root / 'config/elastic-artifacts.json').read_text())
    version = json.loads((root / 'config/services.json').read_text())['elastic_version']
    expected_names = {p + '-' + version + '-amd64.deb' for p in ('elasticsearch', 'filebeat', 'kibana')}
    if set(lock) != expected_names or any(not re.fullmatch('[a-f0-9]{128}', h) for h in lock.values()):
        raise ValueError('Invalid pinned Elastic artifact lock')
    return lock


def verify_packages(root=ROOT, storage=None):
    """Read-only round trip of every pinned package using the VM downloader."""
    from remote.fetch_artifacts import download
    config = settings(root)
    lock = package_lock(root)
    storage = storage or client(root, config)
    require_private(storage, config['bucket'])
    report = {'success': False, 'packages': {}, 'virtual_machines_created': 0}
    work = root / '.tools/artifact-build'
    work.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(dir=work) as directory:
            for name, sha in lock.items():
                key = '/'.join((config['prefix'], 'packages', sha, name))
                head = storage.head_object(Bucket=config['bucket'], Key=key)
                if head.get('Metadata', {}).get('sha512') != sha:
                    raise ValueError('S3 package metadata does not match the pinned digest')
                private_acl(storage.get_object_acl(Bucket=config['bucket'], Key=key))
                url = storage.generate_presigned_url('get_object',
                    Params={'Bucket': config['bucket'], 'Key': key}, ExpiresIn=config['expires_seconds'])
                privacy.remember([url])
                size = head['ContentLength']
                download({'url': url, 'size': size, 'sha512': sha}, Path(directory) / name,
                         progress=lambda count, total: print(f'{name}: {count // 2**20}/{total // 2**20} MiB', flush=True))
                report['packages'][name] = {'size': size, 'sha512_verified': True, 'private_acl_verified': True}
                print(name + ': full HTTPS download and pinned SHA512 verified', flush=True)
            report['success'] = True
    finally:
        destination = root / 'docs/s3-package-downloads.json'
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report, indent=2) + '\n')
    return report


def prepare(root=ROOT, storage=None):
    config = settings(root)
    lock = package_lock(root)
    cache = root / '.tools/elastic-packages'
    for name, sha in lock.items():
        if not (cache / name).is_file() or digest(cache / name) != sha:
            raise ValueError('A verified package is missing; prepare the explicit local cache first')
    storage = storage or client(root, config)
    require_private(storage, config['bucket'])
    from artifact_lifecycle import lease
    lease(root, storage, config)
    manifest = {'packages': {name: ensure(storage, config, cache / name, 'packages', sha) for name, sha in lock.items()}}
    work = root / '.tools/artifact-build'
    work.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=work) as directory:
        source = Path(directory) / 'source.tar.gz'
        sha = source_archive(root, source)
        manifest['source'] = ensure(storage, config, source, 'sources', sha)
    local = root / '.local'
    local.mkdir(exist_ok=True)
    destination = local / 'artifact-delivery.json'
    destination.write_text(json.dumps(manifest))
    print('Private S3 artifacts verified; temporary delivery manifest prepared', flush=True)
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', nargs='?', default='prepare', choices=['prepare', 'verify', 'verify-packages'])
    action = parser.parse_args().action
    try:
        if action == 'verify':
            from artifact_lifecycle import probe
            probe(ROOT)
        elif action == 'verify-packages':
            verify_packages()
        else:
            prepare()
    except Exception as error:
        raise SystemExit('S3 artifact operation failed: ' + privacy.redact(str(error))) from None
