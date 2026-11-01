"""Server configuration backend, independent of infrastructure creation."""
import json
import paramiko
from cloud_settings import terraform_root


def operation_inventory(api, existing=False):
    from ansible_operation import pending, read
    from render import ansible_inventory
    passwords = json.loads((api.LOCAL / 'secrets.json').read_text())
    inventory = ansible_inventory(api.infrastructure_output(), passwords)
    inventory['all']['vars']['dz07_bootstrap'] = read(api)['bootstrap'] if pending(api) else not existing
    return inventory


def resume(api, existing=False):
    from ansible_operation import pending, resume
    return pending(api) and resume(api, operation_inventory(api, existing))


def configure(api, existing=False):
    from stage_store import operation
    with operation(api.LOCAL):
        return _configure(api, existing)


def _configure(api, existing=False):
    from elastic_options import require_no_failover
    require_no_failover(api)
    if resume(api, existing):
        print('Saved Ansible operation observed and acknowledged', flush=True)
        return
    ROOT, LOCAL = api.ROOT, api.LOCAL
    lab_output, trust, connect = api.lab_output, api.trust, api.connect
    remote, upload_tree, put = api.remote, api.upload_tree, api.put
    discover_route = api.discover_route
    lab = lab_output()
    api.check_placement(lab)
    from stage_store import deployment_exists as exists
    if not exists(ROOT):
        from cloud_settings import adapter
        variables = json.loads((terraform_root(ROOT) / 'terraform.tfvars.json').read_text())
        api.save(ROOT / 'docs/network-result.json', adapter().check_network(api.Cloud(), api.infrastructure_output(), variables['admin_cidrs']))
    trust(lab)
    if not existing:
        from network_diagnostics import require_bootstrap_network
        require_bootstrap_network(api, lab)
    try:
        with connect(lab):
            pass
    except (paramiko.AuthenticationException, paramiko.BadHostKeyException) as error:
        raise RuntimeError('SSH authentication or host-key verification failed; preserve context and check credentials and trust') from error
    except (OSError, paramiko.SSHException):
        if existing:
            raise RuntimeError('SSH to the existing stand is unavailable; check routing and allowed /32 before a separate network change')
        discover_route(lab)
    passwords = json.loads((LOCAL / 'secrets.json').read_text())
    with connect(lab) as app:
        remote(app, 'sudo python3 -', 'network-app', stdin=(ROOT / 'scripts/remote/network_diagnostics.py').read_text())
        remote(app, 'sudo python3 -', 'cloud-init-app', stdin=(ROOT / 'scripts/remote/check_cloud_init.py').read_text())
        from ansible_operation import reserve
        inventory = operation_inventory(api, existing)
        reserve(api, lab, inventory)
        upload_tree(app)
        put(app, api.STACK['paths']['remote_root'] + '/settings.json', json.dumps({'lab': lab, 'passwords': passwords}))
        import observability
        observability.stage(api, app, lab, passwords)
        from ansible_backend import configure as configure_ansible
        observability.package_cache(api, app, lab, start=True)
        configure_ansible(api, app, inventory)
    print('Cluster and portal configured with Ansible', flush=True)
