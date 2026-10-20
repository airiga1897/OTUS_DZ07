"""Single credential contract for PowerShell, Terraform and Selectel API."""
import argparse
import json
import os
from pathlib import Path

import privacy

ROOT = Path(__file__).resolve().parents[1]


def settings() -> dict:
    return json.loads((ROOT / 'config/selectel.json').read_text(encoding='utf-8'))


def _environment(policy: dict) -> dict:
    fields = policy['credentials']
    if not all(os.environ.get(field['environment']) for field in fields):
        raise RuntimeError('Selectel credentials incomplete; dot-source scripts/env.ps1 in this PowerShell session')
    return {field['key']: os.environ[field['environment']] for field in fields}


def load(local: Path) -> dict:
    policy = settings()
    fields = policy['credentials']
    # Any credential in the session selects environment mode. Incomplete input
    # is an error, never a reason to switch silently to a saved identity.
    if any(os.environ.get(field['environment']) for field in fields):
        config = _environment(policy)
    elif os.environ.get('OTUS_SELECTEL_AUTH'):
        try:
            config = json.loads(Path(os.environ['OTUS_SELECTEL_AUTH']).read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            raise RuntimeError('Cannot read explicitly selected development credentials') from None
    else:
        raise RuntimeError('Selectel credentials unavailable; dot-source scripts/env.ps1')
    if not isinstance(config, dict) or not all(isinstance(config.get(field['key']), str)
                                               and config[field['key']].strip() for field in fields):
        raise RuntimeError('Selectel credentials incomplete; dot-source scripts/env.ps1')
    privacy.remember(config[field['key']] for field in fields)
    config = {**config, 'auth_url': config.get('auth_url', os.environ.get('OS_AUTH_URL', policy['auth_url'])),
              'region': config.get('region', os.environ.get('OS_REGION_NAME', policy['region']))}
    if config['auth_url'].rstrip('/') != policy['auth_url'] or config['region'] != policy['region']:
        raise RuntimeError('Unexpected Selectel authentication endpoint or region')
    _validate_binding(local, config)
    return config


def _validate_binding(local: Path, config: dict) -> None:
    path = local / 'selectel-context.json'
    if path.exists():
        context = json.loads(path.read_text(encoding='utf-8'))
        if any(context.get(key) != config[key] for key in ('project_id', 'region')):
            raise RuntimeError('Credentials do not match the deployment project binding; retain state and context')
    else:
        state = local.parent / 'terraform-selectel/terraform.tfstate'
        if state.exists():
            resources = json.loads(state.read_text(encoding='utf-8')).get('resources', [])
            if any(r.get('mode') == 'managed' and r.get('instances') for r in resources):
                raise RuntimeError('Existing deployment has no project binding; retain state and verify its project before proceeding')


def bind(local: Path, config: dict) -> None:
    _validate_binding(local, config)
    local.mkdir(parents=True, exist_ok=True)
    context = {key: config[key] for key in ('project_id', 'region')}
    (local / 'selectel-context.json').write_text(json.dumps(context, indent=2) + '\n', encoding='utf-8')


def terraform_environment(config: dict) -> dict:
    env = dict(os.environ)
    for field in settings()['credentials']:
        env[field['environment']] = config[field['key']]
    env.update(OS_AUTH_URL=config['auth_url'], OS_REGION_NAME=config['region'])
    for name in ('TF_LOG', 'TF_LOG_CORE', 'TF_LOG_PROVIDER', 'TF_LOG_PATH', 'OS_DEBUG'):
        env.pop(name, None)
    return env


def save_development(local: Path) -> None:
    # File ACL is restricted by save-operator.ps1 before invoking this command.
    policy = settings()
    config = {**_environment(policy), 'auth_url': policy['auth_url'], 'region': policy['region']}
    local.mkdir(parents=True, exist_ok=True)
    (local / 'operator.json').write_text(json.dumps(config, indent=2) + '\n', encoding='utf-8')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['save-development'])
    parser.parse_args()
    try:
        save_development(ROOT / '.local')
    except Exception:
        raise SystemExit('Could not save development credentials; check session input and local file permissions') from None
    print('Development credentials saved locally; excluded from Git. Delete after development and cleanup.')


if __name__ == '__main__':
    main()
