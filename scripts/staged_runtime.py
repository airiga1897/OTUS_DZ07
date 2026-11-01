"""User lifecycle integration for retained, sequential infrastructure stages."""
from dataclasses import asdict
import json
from pathlib import Path
import time

from cloud_contract import InfrastructureResult
from local_runtime import checked_tree
from cloud_settings import staged_backend as Backend
from stage_store import JOURNAL, StageStore, atomic_json, operation


def read_journal(root):
    path = Path(root) / '.local' / JOURNAL
    checked_tree(path, Path(root).resolve())
    return json.loads(path.read_text())


def local_store(api):
    journal = read_journal(api.ROOT)
    return StageStore(api, journal)


def managed_ids(api):
    store = local_store(api)
    return {(name, address): identifier for name in store.journal['stages']
            for address, identifier in store.identities(name).items()}


def output(api):
    path = api.LOCAL / 'infrastructure.json'
    checked_tree(path, Path(api.ROOT).resolve())
    result = InfrastructureResult.from_dict(json.loads(path.read_text()))
    journal = read_journal(api.ROOT)
    if (result.provider != journal['provider'] or result.scope != journal['scope']
            or result.namespace != journal['namespace']):
        raise ValueError('Infrastructure result belongs to another deployment')
    store = local_store(api)
    for name, node in result.nodes.items():
        saved = journal['stages'][name]
        if saved['status'] != 'active' or saved['output'] != node:
            raise ValueError('Infrastructure output is stale')
        if saved.get('lineage') and store.state(name).get('lineage') != saved['lineage']:
            raise ValueError('Stage lineage differs from its saved context')
        if 'identities' in saved and store.identities(name) != saved['identities']:
            raise ValueError('Stage resource identities differ from saved context')
        stored_ids = set(store.identities(name).values())
        if not {node['id'], node['port_id'], node['volume_id']} <= stored_ids:
            raise ValueError('Infrastructure identity differs from its stage state')
    from cloud_settings import adapter
    adapter().validate_staged_local(api, result, journal, store)
    return result


def open_portal(api, opener=None):
    """Open the saved site using local state only; no provider authentication."""
    import webbrowser
    result = output(api)
    url = 'http://' + result.nodes['app']['public_ip']
    for name, node in result.nodes.items():
        print(name + ': ' + node['region'] + '/' + node['zone'], flush=True)
    from topology import services
    print('Portal: ' + url, flush=True)
    print('Kibana: ' + url + ':' + str(services(api.ROOT)['kibana_port']), flush=True)
    if not (opener or webbrowser.open)(url):
        print('Use the URL above to open the portal.', flush=True)
    return url


def backend(api):
    active = vars(api).get('_STAGED_BACKEND')
    return active if active is not None else Backend(api)


def reject_shared_namespace(api):
    from cloud_settings import adapter
    return adapter().reject_shared_namespace(api)


def cleanup_empty(api, clean_local=True):
    from cloud_settings import adapter
    from test_cycle import require_access
    require_access(api, cleanup=True)
    with operation(api.LOCAL):
        report = adapter().audit_empty_stages(api)
        api.save(api.ROOT / 'docs/cleanup-result.json', {'success': True, 'staged': True, **report})
        if clean_local:
            from artifact_lifecycle import release
            release(api.ROOT)
            from local_runtime import cleanup_runtime
            cleanup_runtime(api.ROOT)


def checked_backend(api):
    from operator_network import discover_cidrs, allowed_cidrs
    provider = Backend(api)
    observed = discover_cidrs(provider.policy['operator_ip_services'])
    provider.journal['admin_cidrs'] = allowed_cidrs(provider.policy['admin_cidrs'], observed, provider.journal.get('admin_cidrs', []))
    provider.validate_existing()
    provider.preflight()
    return provider


def prepare_files(api):
    from topology import services
    api.prepare_identity()
    import delivery
    delivery.prepare(api.ROOT)


def preflight(api):
    from test_cycle import require_access
    require_access(api)
    with operation(api.LOCAL):
        return checked_backend(api)


def prepare(api):
    from test_cycle import require_access
    require_access(api)
    with operation(api.LOCAL):
        provider = checked_backend(api)
        provider.prepare_tools()
        provider.save()
        prepare_files(api)


def deploy(api):
    from test_cycle import require_access
    from staged_deploy import place
    import stack_config
    import topology
    require_access(api)
    with operation(api.LOCAL):
        from elastic_options import require_no_failover
        require_no_failover(api)
        from configuration import resume
        if resume(api):
            journal = read_journal(api.ROOT)
            checkpoint = api.LOCAL / 'expansion-pending.json'
            if checkpoint.exists():
                from verification import expansion
                expansion(api, json.loads(checkpoint.read_text()))
            journal.update(configured=True, stack_sha256=stack_config.digest(api.STACK))
            atomic_json(api.LOCAL / JOURNAL, journal, api.ROOT)
            from ansible_operation import finalize
            finalize(api)
            return
        provider = checked_backend(api)
        provider.prepare_tools()
        api._STAGED_BACKEND = provider
        try:
            journal = provider.journal
            if journal.get('configured') and journal['stack_sha256'] != stack_config.digest(api.STACK):
                raise ValueError('Changing a running stack requires a separate migration')
            provider.save()
            prepare_files(api)
            snapshot = None
            if journal.get('configured'):
                if journal.get('optional_es4') and not provider.completed('es4'):
                    previous = output(api)
                    if set(previous.nodes) != set(provider.desired['nodes']) - {'es4'}:
                        raise ValueError('Incomplete es4 extension has an unexpected infrastructure snapshot')
                else:
                    previous = provider.result()
                atomic_json(api.LOCAL / 'infrastructure.json', asdict(previous), Path(api.ROOT))
                from verification import snapshot as checkpoint
                snapshot = checkpoint(api, api.lab_output())
            before = provider.identities(exclude_pending=True)
            for name in provider.deployment_order():
                place(provider, name, provider.candidates_for(name))
            provider.assert_preserved(before)
            result = provider.result()
            atomic_json(api.LOCAL / 'infrastructure.json', asdict(result), Path(api.ROOT))
            api.configure(existing=journal.get('configured', False))
            if snapshot:
                from verification import expansion
                expansion(api, snapshot)
            journal.update(configured=True, stack_sha256=stack_config.digest(api.STACK))
            provider.save()
            from ansible_operation import finalize
            finalize(api)
        finally:
            api._STAGED_BACKEND = None


def cleanup(api, clean_local=True):
    from test_cycle import require_access
    require_access(api, cleanup=True)
    with operation(api.LOCAL):
        provider = Backend(api, cleanup=True)
        provider.journal['destroy_requested'] = True
        provider.save()
        try:
            from ansible_operation import before_destroy
            before_destroy(api, provider)
        except Exception as error:
            api.save(api.ROOT / 'docs/cleanup-result.json', {'success': False, 'staged': True,
                'ansible_stop_error_type': type(error).__name__, 'recovery_files_preserved': True})
            raise
        from cleanup_dependencies import children
        dependencies = children(provider.journal['stages'], provider.desired['nodes'])
        ordered = provider.cleanup_order()
        errors = {}
        for name in ordered:
            if provider.journal['stages'][name]['status'] != 'deleted':
                # A parent is removable only after each own child passed its
                # state/API deletion audit. Independent pools can still finish.
                if any(provider.journal['stages'][child]['status'] != 'deleted'
                       for child in dependencies[name]):
                    errors[name] = 'BlockedByCleanupDependency'
                    continue
                try:
                    provider.cleanup_attempt(name)
                except Exception as error:
                    errors[name] = type(error).__name__
        if errors:
            api.save(api.ROOT / 'docs/cleanup-result.json', {'success': False, 'staged': True,
                     'stage_errors': errors, 'recovery_files_preserved': True})
            raise RuntimeError('Some stages could not be removed; preserve recovery data: ' + ', '.join(errors))
        if provider.identities():
            raise RuntimeError('Managed resources remain in stage states')
        audit_report = provider.audit_deletion()
        provider.journal['cleanup_verified'] = True
        provider.save()
        api.save(api.ROOT / 'docs/cleanup-result.json', {'success': True, 'staged': True,
                 'managed_remaining': 0, **audit_report})
        if clean_local:
            from artifact_lifecycle import release
            release(api.ROOT)
            from local_runtime import cleanup_runtime
            cleanup_runtime(api.ROOT)


def discover_route(api, lab):
    import secrets
    import urllib.request
    from operator_network import signed_route_cidr
    key = json.loads((api.LOCAL / 'secrets.json').read_text())['api'].encode()
    nonce = secrets.token_hex(24)
    for _ in range(40):
        try:
            with urllib.request.urlopen(lab['site_url'] + '/?nonce=' + nonce, timeout=3) as response:
                cidr = signed_route_cidr(json.load(response), nonce, key)
            break
        except (OSError, json.JSONDecodeError):
            time.sleep(2)
    else:
        raise RuntimeError('Signed direct HTTP route unavailable; preserve the deployment')
    provider = backend(api)
    provider.extend_admin_access(lab, cidr)
    api.save(api.LOCAL / 'operator-network.json', {'tcp_http_cidr': cidr, 'admin_cidrs': provider.journal['admin_cidrs']})
