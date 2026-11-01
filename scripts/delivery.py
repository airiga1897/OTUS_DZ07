"""The explicit package delivery policy for this clone."""
import json


class ArtifactUnavailable(RuntimeError):
    pass


def mode(root):
    path = root / 'config/services.json'
    mode = json.loads(path.read_text()).get('artifact_delivery', 'cached') if path.exists() else 'cached'
    if mode not in ('internet', 'cached', 's3', 'release'):
        raise ValueError('Unknown artifact_delivery mode')
    return mode


def internet(root):
    return mode(root) in ('internet', 's3', 'release')


def prepare(root):
    selected = mode(root)
    if selected == 'release':
        from release_artifacts import prepare
        return prepare(root)
    if selected == 's3':
        from s3_artifacts import prepare
        return prepare(root)
