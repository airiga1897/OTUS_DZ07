"""Terraform boundary: topology inputs, append-only plan and resource creation."""
import json
from cloud_settings import terraform_root


def check_replication_capacity(snapshot: dict, desired_nodes: int) -> None:
    capacity = snapshot['before']['replication_capacity']
    if desired_nodes > min(capacity['senders'], capacity['slots']):
        raise ValueError('Running PostgreSQL replication capacity requires a separate migration before expansion')


def validate_existing_identity(change: dict, existing: dict) -> None:
    key = (change.get('type'), change.get('name'), str(change.get('index', '')))
    if key in existing and (change['change'].get('before') or {}).get('id') != existing[key]:
        raise ValueError('Existing resource identity changed before apply: ' + change['address'])


def validate_node_creation(change: dict, allowed: set[str]) -> None:
    from cloud_settings import adapter
    adapter().validate_node_creation(change, allowed)


def apply(api, desired: dict, allowed_new_nodes=None) -> None:
    variables = terraform_root(api.ROOT) / 'terraform.tfvars.json'
    if variables.exists():
        value = json.loads(variables.read_text(encoding='utf-8'))
        from stack_config import load
        settings = load()
        value.setdefault('name_prefix', settings['name_prefix'])
        value.setdefault('subnet_cidr', settings['subnet_cidr'])
        value.update(desired)
        api.save(variables, value)
    api.preflight()
    plan = api.checked_plan() if allowed_new_nodes is None else api.checked_plan(allowed_new_nodes=allowed_new_nodes)
    api.pools.phase(api.ROOT, 'creating')
    api.tf('apply', '-input=false', '-no-color', str(plan))
