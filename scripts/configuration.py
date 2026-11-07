"""Server configuration backend, independent of infrastructure creation."""

import json

import paramiko
from cloud_settings import terraform_root
from inventory_types import Inventory
from runtime_ports import ApplicationPort, ConfigurationPort


def operation_inventory(api: ApplicationPort, existing: bool = False) -> Inventory:
    from ansible_operation import read
    from render import ansible_inventory

    passwords = json.loads((api.LOCAL / "secrets.json").read_text())
    inventory = ansible_inventory(api.infrastructure_output(), passwords, api.STACK)
    saved = read(api)
    inventory["all"]["vars"]["dz07_bootstrap"] = (
        saved["bootstrap"] if saved is not None and not saved.get("finalized", False) else not existing
    )
    return inventory


def resume(api: ApplicationPort, existing: bool = False) -> bool:
    from ansible_operation import pending
    from ansible_operation import resume as resume_operation

    return pending(api) and resume_operation(api, operation_inventory(api, existing))


def configure(api: ConfigurationPort, existing: bool = False) -> None:
    from stage_store import operation

    with operation(api.LOCAL):
        return _configure(api, existing)


def _configure(api: ConfigurationPort, existing: bool = False) -> None:
    from elastic_options import require_no_failover

    require_no_failover(api)
    if resume(api, existing):
        print("Saved Ansible operation observed and acknowledged", flush=True)
        return
    project_root, local_data = api.ROOT, api.LOCAL
    lab_output, trust, connect = api.lab_output, api.trust, api.connect
    remote, upload_tree, put = api.remote, api.upload_tree, api.put
    discover_route = api.discover_route
    lab = lab_output()
    api.check_placement(lab)
    from stage_store import deployment_exists as exists

    if not exists(project_root):
        from cloud_settings import adapter

        variables = json.loads((terraform_root(project_root) / "terraform.tfvars.json").read_text())
        api.save(
            project_root / "docs/network-result.json",
            adapter().check_network(api.cloud(), api.infrastructure_output(), variables["admin_cidrs"]),
        )
    trust(lab)
    if not existing:
        from network_diagnostics import require_bootstrap_network

        require_bootstrap_network(api, lab)
    try:
        with connect(lab):
            pass
    except (paramiko.AuthenticationException, paramiko.BadHostKeyException) as error:
        raise RuntimeError(
            "SSH authentication or host-key verification failed; preserve context and check credentials and trust"
        ) from error
    except (OSError, paramiko.SSHException) as error:
        if existing:
            raise RuntimeError(
                "SSH to the existing stand is unavailable; check routing and allowed /32 before a separate network change"
            ) from error
        discover_route(lab)
    passwords = json.loads((local_data / "secrets.json").read_text())
    with connect(lab) as app:
        remote(
            app,
            "sudo python3 -",
            "network-app",
            stdin=(project_root / "scripts/remote/network_diagnostics.py").read_text(),
        )
        remote(
            app,
            "sudo python3 -",
            "cloud-init-app",
            stdin=(project_root / "scripts/remote/check_cloud_init.py").read_text(),
        )
        from ansible_operation import reserve

        inventory = operation_inventory(api, existing)
        reserve(api, lab, inventory)
        upload_tree(app)
        put(app, api.STACK["paths"]["remote_root"] + "/settings.json", json.dumps({"lab": lab, "passwords": passwords}))
        import observability

        observability.stage(api, app, lab, passwords)
        from ansible_backend import configure as configure_ansible

        observability.package_cache(api, app, lab, start=True, remote=api.remote)
        configure_ansible(api, app, inventory)
    print("Cluster and portal configured with Ansible", flush=True)
