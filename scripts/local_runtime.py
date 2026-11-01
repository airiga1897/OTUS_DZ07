"""Local lifecycle operations; never authenticate or contact a cloud service."""
import ipaddress
import json
from pathlib import Path
import shutil
import webbrowser
from cloud_settings import terraform_root


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def linked(path):
    return path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())


def checked_tree(path, root):
    if linked(path) or not path.resolve().is_relative_to(root):
        raise ValueError('Runtime path must stay inside this clone without links')
    if path.is_dir():
        for child in path.iterdir():
            checked_tree(child, root)


def cleanup_runtime(root):
    """Call only after successful state AND cloud API deletion audits."""
    root = Path(root).resolve()
    local = root / '.local'
    staged = local / 'staged-deployment.json'
    checked_tree(staged, root)
    from stage_store import require_context
    require_context(root)
    if staged.exists():
        journal = read(staged)
        if not journal.get('cleanup_verified') or any(s['status'] != 'deleted' for s in journal['stages'].values()):
            raise ValueError('Sequential resources still need their recovery files')
        for path in (local / 'stages').rglob('*.tfstate*'):
            checked_tree(path, root)
            if not path.name.endswith('.backup') and any(r.get('instances') for r in read(path).get('resources', []) if r.get('mode') == 'managed'):
                raise ValueError('Sequential managed state remains')
    state = terraform_root(root) / 'terraform.tfstate'
    alternate = root / 'terraform/terraform.tfstate'
    checked_tree(state, root)
    checked_tree(alternate, root)
    for other in root.glob('terraform*/terraform.tfstate*'):
        checked_tree(other, root)
        if other.parent != state.parent and any(r.get('mode') == 'managed' and r.get('instances')
                                                 for r in read(other).get('resources', [])):
            raise ValueError('Another provider state still needs local recovery files')
    if state.exists() and any(r.get('mode') == 'managed' and r.get('instances')
                              for r in read(state).get('resources', [])):
        raise ValueError('Managed state remains; preserve recovery files')
    paths = [p for p in local.iterdir() if p.name not in {'operator.json', 'artifact-credentials.json', 'artifact-storage.json', 'lab-config.json', 'stage-operation.lock'}] if local.exists() else []
    paths += list((terraform_root(root)).glob('terraform.tfstate*'))
    paths += list((terraform_root(root)).glob('*.tfplan'))
    paths += [p for p in (terraform_root(root)).glob('terraform.tfvars*')
              if p.name != 'terraform.tfvars.example.json' and p.suffix == '.json']
    # Validate every target before removing any recovery file.
    checked_tree(local, root)
    for path in paths:
        checked_tree(path, root)
    for path in paths:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)


def validate_state(root, config, prefix):
    """Fail closed on foreign project, pool, provider, or resource namespace."""
    state = terraform_root(Path(root)) / 'terraform.tfstate'
    checked_tree(state, Path(root).resolve())
    if not state.exists():
        return
    from cloud_contract import validate_binding
    state_value = read(state)
    binding = state_value.get('outputs', {}).get('infrastructure', {}).get('value')
    if not binding and any(r.get('mode') == 'managed' and r.get('instances') for r in state_value.get('resources', [])):
        from cloud_settings import adapter
        binding = adapter().partial_state_binding(Path(root), config, prefix)
    if binding:
        from cloud_settings import adapter
        expected = adapter().binding(config, prefix, binding['placement']['zone'])
        validate_binding(binding, expected)
        context_path = Path(root) / '.local/context.json'
        if context_path.exists():
            validate_binding(read(context_path), binding)
    from cloud_settings import adapter
    adapter().validate_managed_state(state_value, config, prefix)


def open_portal(root, homework, opener=None):
    """Validate saved identities against local state; no cloud credentials needed."""
    root = Path(root)
    local = root / '.local'
    try:
        checked_tree(terraform_root(root) / 'terraform.tfstate', root.resolve())
        checked_tree(local / {5: 'deployment.json', 6: 'context.json', 7: 'selectel-context.json'}[homework], root.resolve())
        checked_tree(local / 'lab.json', root.resolve())
        state = read(terraform_root(root) / 'terraform.tfstate')
        resources = [r for r in state.get('resources', []) if r.get('mode') == 'managed' and r.get('instances')]
        if not resources:
            raise ValueError('No active managed state')
        context = read(local / {5: 'deployment.json', 6: 'context.json', 7: 'selectel-context.json'}[homework])
        if homework == 6:
            from cloud_contract import InfrastructureResult, validate_binding
            result = InfrastructureResult.from_dict(state['outputs']['infrastructure']['value'])
            validate_binding(context, result.binding())
            context = {**context, 'project_id': result.scope['project_id'], 'region': result.placement['region'],
                       'availability_zone': result.placement['zone']}
        if homework == 5 and context.get('phase') != 'active':
            raise ValueError('Deployment is not active')
        if context.get('state_lineage') and context['state_lineage'] != state.get('lineage'):
            raise ValueError('Stale deployment lineage')
        output = state['outputs']['lab']['value']
        saved = read(local / 'lab.json') if homework != 6 else output
        if homework == 5 and saved.get('deployment_id') != context.get('deployment_id'):
            raise ValueError('Stale deployment identity')
        ids = {}
        for resource in resources:
            for item in resource['instances']:
                attrs = item.get('attributes', {})
                if attrs.get('tenant_id') and attrs['tenant_id'] != context['project_id']:
                    raise ValueError('Foreign project in state')
                if attrs.get('region') and attrs['region'] != context['region']:
                    raise ValueError('Foreign region in state')
                name = attrs.get('name')
                prefix = context.get('namespace', context.get('config', {}).get('name_prefix', f'otus-dz0{homework}'))
                if name and name != prefix and not name.startswith(prefix + '-'):
                    raise ValueError('Foreign resource in state')
                if resource['type'] == 'openstack_compute_instance_v2':
                    ids[item['index_key']] = attrs['id']
        expected = {name: node['id'] for name, node in saved.items()} if homework == 7 else saved['server_ids']
        output_ids = {name: node['id'] for name, node in output.items()} if homework == 7 else output['server_ids']
        if not ids or ids != expected or ids != output_ids:
            raise ValueError('Stale VM identities')
        address = saved['es3']['public_ip'] if homework == 7 else saved['app_public_ip']
        current_address = output['es3']['public_ip'] if homework == 7 else output['app_public_ip']
        if address != current_address:
            raise ValueError('Stale portal address')
        ipaddress.IPv4Address(address)
        url = 'http://' + address
        region = context['region']
        zone = context.get('availability_zone')
        if not zone:
            variables = read(terraform_root(root) / 'terraform.tfvars.json')
            zone = variables['availability_zone']
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise ValueError('Нет действующего локального контекста портала: проверьте context, state и завершение deploy; сохраните данные для восстановления.') from error
    print(f'Placement: {region}/{zone}\nPortal: {url}', flush=True)
    if 'kibana' in saved.get('server_ids', {}):
        from topology import services
        print('Kibana: ' + url + ':' + str(services(root)['kibana_port']), flush=True)
    if not (opener or webbrowser.open)(url):
        print('Браузер не открылся автоматически; используйте URL выше.', flush=True)
    return url
