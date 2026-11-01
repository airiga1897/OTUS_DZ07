"""Scoped S3 binding, run ownership, real probe and audited artifact cleanup."""
import datetime
import hashlib
import json
from pathlib import Path
import tempfile
import uuid

import s3_artifacts as artifacts


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2))


def bind(root, config, require_existing=False):
    import os
    from local_runtime import checked_tree
    schema = json.loads((root / 'config/selectel-auth.json').read_text())
    project = os.environ.get(schema['fields']['project_id']['env'])
    if not project:
        raise ValueError('Load the OpenStack project environment to confirm artifact ownership')
    expected = {'provider': 'selectel-s3', 'project_id': project,
                **{key: config[key] for key in ('endpoint', 'region', 'bucket', 'prefix')}}
    path = root / '.local/artifact-storage.json'
    checked_tree(path, root.resolve())
    if path.exists():
        if json.loads(path.read_text()) != expected:
            raise ValueError('Artifact storage binding changed; preserve recovery data')
    elif require_existing:
        raise ValueError('No confirmed artifact storage binding; purge refused')
    else:
        save(path, expected)
    return expected


def run_key(config, run_id):
    if uuid.UUID(run_id).hex != run_id:
        raise ValueError('Invalid artifact run identity')
    return config['prefix'] + '/runs/' + run_id + '/lease.json'


def lease(root, storage, config):
    bind(root, config)
    path = root / '.local/artifact-run.json'
    run_id = json.loads(path.read_text())['run_id'] if path.exists() else uuid.uuid4().hex
    key = run_key(config, run_id)
    save(path, {'run_id': run_id})
    storage.put_object(Bucket=config['bucket'], Key=key, ACL='private',
                       Body=json.dumps({'run_id': run_id}).encode())


def missing(error):
    return getattr(error, 'response', {}).get('Error', {}).get('Code') in ('404', 'NoSuchKey', 'NotFound')


def confirm_absent(storage, config, key):
    try:
        storage.head_object(Bucket=config['bucket'], Key=key)
    except Exception as error:
        if not missing(error):
            raise
    else:
        raise RuntimeError('Artifact deletion not confirmed')


def release(root):
    path = root / '.local/artifact-run.json'
    probe_path = root / '.local/artifact-probe.json'
    if not path.exists() and not probe_path.exists():
        return
    config = artifacts.settings(root)
    bind(root, config, require_existing=True)
    storage = artifacts.client(root, config)
    entries = []
    if path.exists():
        entries.append((path, run_key(config, json.loads(path.read_text())['run_id'])))
    if probe_path.exists():
        key = json.loads(probe_path.read_text())['key']
        if not key.startswith(config['prefix'] + '/probes/') or uuid.UUID(key.rsplit('/', 1)[1]).hex != key.rsplit('/', 1)[1]:
            raise ValueError('Invalid probe recovery identity')
        entries.append((probe_path, key))
    for context, key in entries:
        storage.delete_object(Bucket=config['bucket'], Key=key)
        confirm_absent(storage, config, key)
        context.unlink()


def probe(root, storage=None):
    config = artifacts.settings(root)
    bind(root, config)
    storage = storage or artifacts.client(root, config)
    artifacts.require_private(storage, config['bucket'])
    key = config['prefix'] + '/probes/' + uuid.uuid4().hex
    body = b'OTUS_DZ07 private S3 integrity probe\n' + uuid.uuid4().bytes
    sha = hashlib.sha512(body).hexdigest()
    result = {'success': False, 'upload_verified': False, 'download_hash_verified': False,
              'deletion_verified': False, 'region': config['region']}
    recovery = root / '.local/artifact-probe.json'
    if recovery.exists():
        raise ValueError('An earlier probe needs confirmed cleanup before another probe')
    save(recovery, {'key': key})
    try:
        storage.put_object(Bucket=config['bucket'], Key=key, Body=body, ACL='private', Metadata={'sha512': sha})
        result['upload_verified'] = True
        url = storage.generate_presigned_url('get_object', Params={'Bucket': config['bucket'], 'Key': key}, ExpiresIn=300)
        import privacy
        privacy.remember([url])
        from remote.fetch_artifacts import download
        work = root / '.tools/artifact-build'
        work.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=work) as directory:
            destination = Path(directory) / 'probe'
            download({'url': url, 'size': len(body), 'sha512': sha}, destination)
            result['download_hash_verified'] = destination.read_bytes() == body
    finally:
        try:
            storage.delete_object(Bucket=config['bucket'], Key=key)
            confirm_absent(storage, config, key)
            result['deletion_verified'] = True
            recovery.unlink()
            result['success'] = result['upload_verified'] and result['download_hash_verified']
        finally:
            save(root / 'docs/s3-probe.json', result)
    if not result['success']:
        raise RuntimeError('S3 round-trip integrity probe failed')
    print('S3 upload, HTTPS download, SHA512 and deletion verified', flush=True)


def inventory(storage, config):
    prefix = config['prefix'] + '/'
    options = {'Bucket': config['bucket'], 'Prefix': prefix}
    objects, versions, uploads = [], [], []
    for page in storage.get_paginator('list_objects_v2').paginate(**options):
        objects.extend({'Key': value['Key']} for value in page.get('Contents', []))
    status = storage.get_bucket_versioning(Bucket=config['bucket']).get('Status')
    if status not in (None, 'Enabled', 'Suspended'):
        raise ValueError('Unknown bucket versioning status')
    if status in ('Enabled', 'Suspended'):
        for page in storage.get_paginator('list_object_versions').paginate(**options):
            versions.extend({'Key': v['Key'], 'VersionId': v['VersionId']}
                            for v in page.get('Versions', []) + page.get('DeleteMarkers', []))
    for page in storage.get_paginator('list_multipart_uploads').paginate(**options):
        uploads.extend({'Key': v['Key'], 'UploadId': v['UploadId']} for v in page.get('Uploads', []))
    if any(not value['Key'].startswith(prefix) for value in objects + versions + uploads):
        raise ValueError('S3 listing escaped the confirmed namespace')
    return objects, versions, uploads


def purge(root, storage=None):
    from test_cycle import assert_empty
    assert_empty(root)  # Never purge files while own stand/recovery state exists.
    config = artifacts.settings(root)
    bind(root, config, require_existing=True)
    storage = storage or artifacts.client(root, config)
    artifacts.require_private(storage, config['bucket'])
    pending = root / '.local/artifact-purge-pending.json'
    report = {'success': False, 'bucket_deleted': False, 'prefix': config['prefix'], 'region': config['region']}
    save(pending, {'binding': bind(root, config, require_existing=True)})
    try:
        objects, versions, uploads = inventory(storage, config)
        if any(v['Key'].startswith(config['prefix'] + '/runs/') for v in objects):
            raise RuntimeError('An artifact run lease remains; purge refused')
        targets = versions if versions else objects
        for offset in range(0, len(targets), 1000):
            result = storage.delete_objects(Bucket=config['bucket'], Delete={'Objects': targets[offset:offset + 1000], 'Quiet': False})
            if result.get('Errors'):
                raise RuntimeError('S3 returned object deletion errors')
        for upload in uploads:
            storage.abort_multipart_upload(Bucket=config['bucket'], **upload)
        remaining = inventory(storage, config)
        report.update(objects_remaining=len(remaining[0]), versions_remaining=len(remaining[1]),
                      multipart_remaining=len(remaining[2]),
                      checked_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat())
        if any(remaining):
            raise RuntimeError('S3 namespace cleanup incomplete')
        report['success'] = True
        pending.unlink()
    finally:
        save(root / 'docs/s3-cleanup.json', report)
    print('S3 ARTIFACT PURGE CONFIRMED; BUCKET PRESERVED', flush=True)
