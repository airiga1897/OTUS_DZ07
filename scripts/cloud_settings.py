"""Select an implemented adapter without changing an existing state backend."""
import json
from pathlib import Path


def load(root: Path) -> dict:
    value = json.loads((root / 'config/cloud.json').read_text(encoding='utf-8'))
    if value['provider'] != 'selectel' or value['terraform_directory'] != 'terraform-selectel':
        raise ValueError('Only Selectel is implemented; another cloud requires an independent stand and state')
    return value


def adapter():
    """Only the implemented provider is registered; no implicit cross-cloud fallback."""
    load(Path(__file__).resolve().parents[1])
    import selectel_adapter
    return selectel_adapter


def staged_backend(api, cleanup=False):
    return adapter().staged_backend(api, cleanup=cleanup)


def terraform_root(root: Path) -> Path:
    settings = load(Path(__file__).resolve().parents[1])
    return root / settings['terraform_directory']


def sequential_enabled(root: Path) -> bool:
    return adapter().sequential_enabled(root)
