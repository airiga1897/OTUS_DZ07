"""One credential schema; Python receives credentials only from the environment."""
import json
import os
from pathlib import Path
from typing import Any


def auth_settings() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[1] / 'config/selectel-auth.json'
    settings = json.loads(path.read_text(encoding='utf-8-sig'))
    from pool_selection import policy
    settings['region'] = policy(path.parents[1], 'placement.json', 'context.json')['region']
    return settings


def config_from_env() -> dict[str, str]:
    settings = auth_settings()
    return {**{k: settings[k] for k in ['auth_url', 'region']},
            **{k: os.environ.get(v['env'], '') for k, v in settings['fields'].items()}}


def load_credentials() -> dict[str, str]:
    settings = auth_settings()
    supplied = config_from_env()
    config = {**{k: settings[k] for k in ['auth_url', 'region']}, **supplied}
    if any(not isinstance(config.get(k), str) or not config[k].strip() for k in settings['fields']):
        raise ValueError('Incomplete Selectel credentials; run scripts/env.ps1')
    if config['auth_url'] != settings['auth_url'] or config['region'] != settings['region']:
        raise ValueError('Unsupported Selectel endpoint or region')
    return config


def to_environment(config: dict[str, str]) -> dict[str, str]:
    settings = auth_settings()
    return {**{v['env']: config[k] for k, v in settings['fields'].items()},
            'OS_AUTH_URL': settings['auth_url'], 'OS_REGION_NAME': settings['region']}


def credential_values(config: dict[str, str] | None) -> list[str]:
    return [v for k in auth_settings()['fields'] if (v := (config or {}).get(k))]
