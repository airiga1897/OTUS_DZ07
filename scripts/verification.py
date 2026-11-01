"""Cluster checks and failover orchestration; does not create infrastructure."""
import json
import time
import uuid
import topology


def snapshot(api, previous: dict) -> dict:
    """Keep the canary across retries, including a partially joined replica."""
    path = api.LOCAL / 'expansion-pending.json'
    if path.exists():
        return json.loads(path.read_text(encoding='utf-8'))
    run_id = uuid.uuid4().hex
    with api.connect(previous) as app:
        api.upload_tree(app)
        raw = api.remote(app, 'sudo python3 ' + api.STACK['paths']['remote_root'] + '/scripts/remote/verify.py baseline ' + run_id,
                         'expansion-before')
    result = {'run_id': run_id, 'before': json.loads(raw), 'server_ids': previous['server_ids']}
    if topology.elastic_names(previous):
        import observability
        observability.command(api, 'checkpoint')
        result['elastic_checkpoint'] = True
    api.save(path, result)
    return result


def expansion(api, before: dict) -> None:
    lab = api.lab_output()
    from topology import assert_preserved
    assert_preserved(before['server_ids'], lab['server_ids'])
    with api.connect(lab) as app:
        raw = api.remote(app, 'sudo python3 ' + api.STACK['paths']['remote_root'] + '/scripts/remote/verify.py expanded ' + before['run_id'],
                         'expansion-after')
    result = json.loads(raw)
    if before.get('elastic_checkpoint'):
        import observability
        result['elasticsearch'] = observability.command(api, 'preservation')
    if result['preserved_records'] != [before['before']['new_record']]:
        raise RuntimeError('Expansion did not verify the original canary')
    added = sorted(set(lab['server_ids']) - set(before['server_ids']))
    report = {
        'success': True, 'resource_ids_preserved': True,
        'added_nodes': added, 'verification': result}
    api.save(api.ROOT / 'docs/expansion-result.json', report)
    if added:
        api.save(api.ROOT / 'docs/node-addition-result.json', report)
    (api.LOCAL / 'expansion-pending.json').unlink(missing_ok=True)


def verify(api, failover=False):
    lab_output, connect, remote, save = api.lab_output, api.connect, api.remote, api.save
    ROOT, Cloud = api.ROOT, api.Cloud
    lab = lab_output()
    run_id = uuid.uuid4().hex
    with connect(lab) as app:
        command = 'sudo python3 ' + api.STACK['paths']['remote_root'] + '/scripts/remote/verify.py '
        raw = remote(app, command + 'baseline ' + run_id, 'verify-baseline')
        baseline = json.loads(raw)
        save(ROOT / 'docs/verification.json', baseline)
        if not failover:
            import observability
            return {'baseline': baseline, 'logs': observability.verify(api)}
        leader = baseline['leader']
        server_id = lab['server_ids'][leader]
        cloud = Cloud()
        try:
            cloud.set_power(leader, server_id, running=False, prefix=api.PREFIX)
            raw = remote(app, command + 'failover ' + run_id + ' ' + leader, 'verify-failover')
            result = json.loads(raw)
            result['stopped_node'] = leader
            result['shutdown_confirmed'] = True
            save(ROOT / 'docs/failover-result.json', result)
        finally:
            cloud.set_power(leader, server_id, running=True, prefix=api.PREFIX)
            raw = remote(app, command + 'recovered ' + run_id, 'verify-recovered')
            recovery = json.loads(raw)
            save(ROOT / 'docs/recovery-result.json', recovery)
    print('Failover, committed data, HTTP and recovered replica verified', flush=True)
    if topology.elastic_names(lab):
        import observability
        observability.failover(api)
        observability.verify(api)
    return {'run_id': run_id, 'baseline': baseline, 'failover': result, 'recovery': recovery}




def check_placement(api, lab):
    from cloud_settings import adapter
    return adapter().check_placement(api, lab)
