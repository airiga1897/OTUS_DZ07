"""Find a quota-feasible assignment before allocating any stage."""

from pool_selection import CandidateUnavailable


def require_named_quota(item, needed, label, credit=0):
    from pool_selection import capacity
    try:
        capacity(with_credit(item, credit), needed)
    except CandidateUnavailable as error:
        raise CandidateUnavailable(label + ': ' + str(error)) from error

NETWORK_PORT_RESERVE = 6  # Conservative service-port reserve, excluding VM ports.


def network_port_demand(backend, pool):
    if backend.completed('network-' + pool):
        return 0
    entries = backend.store.entries('network-' + pool)
    if any(e['type'] == 'openstack_networking_network_v2' for e in entries):
        return 0  # Preserve the existing resume policy for an allocated network.
    return NETWORK_PORT_RESERVE


def free(item):
    if item is None:
        raise CandidateUnavailable('Required quota is unknown')
    if item['limit'] == -1:
        return 10**15
    return item['limit'] - item.get('in_use', item.get('used', 0)) - item.get('reserved', 0)


def pending_credits(backend, pool):
    credits = {}
    for name, node in backend.desired['nodes'].items():
        if backend.completed(name) or name not in backend.journal['stages']:
            continue
        record = backend.journal['stages'][name]
        candidate = record['candidate']
        if candidate['region'] != pool:
            continue
        profile = backend.policy['profiles'][node['role']]
        for entry in backend.store.entries(name):
            if entry['type'] == 'openstack_compute_instance_v2':
                for key, amount in [('instances', 1), ('cores', profile['vcpus']), ('ram', profile['ram_mb'])]:
                    index = (candidate['availability_zone'], key)
                    credits[index] = credits.get(index, 0) + amount
            if entry['type'] == 'openstack_blockstorage_volume_v3':
                size = entry['attrs']['size']
                for key, amount in [('volumes', 1), ('gigabytes', size), ('gigabytes_' + candidate['volume_type'], size)]:
                    credits[key] = credits.get(key, 0) + amount
            if entry['type'] in ('openstack_networking_port_v2', 'openstack_networking_floatingip_v2'):
                key = 'port' if entry['type'] == 'openstack_networking_port_v2' else 'floatingip'
                credits[key] = credits.get(key, 0) + 1
    return credits


def with_credit(item, credit):
    if item is None:
        raise CandidateUnavailable('Required quota is unknown')
    value = dict(item)
    value['in_use'] = max(0, value.get('in_use', value.get('used', 0)) - credit)
    return value


def require_capacity(backend):
    pending = [n for n in backend.policy['sequential_placement']['node_order'] if not backend.completed(n)]
    if not pending:
        return
    project = backend.credentials['project_id']
    budgets = {}
    for pool in backend.policy['pools']:
        cloud = backend.region(pool)
        compute = cloud.get('compute', '/os-quota-sets/' + project + '/detail')['quota_set'].get('az_quotas', {})
        volumes = cloud.get('volumev3', '/os-quota-sets/' + project + '?usage=True')['quota_set']
        network = cloud.get('network', '/v2.0/quotas/' + project + '/details.json')['quota']
        credits = pending_credits(backend, pool)
        for zone, values in compute.items():
            for key in ('instances', 'cores', 'ram'):
                budgets[(pool, zone, key)] = free(with_credit(values.get(key), credits.get((zone, key), 0)))
        for key, value in volumes.items():
            if isinstance(value, dict) and 'limit' in value:
                budgets[(pool, key)] = free(with_credit(value, credits.get(key, 0)))
        try:
            require_network_capacity(backend, pool)
            budgets[(pool, 'network_available')] = True
        except CandidateUnavailable:
            budgets[(pool, 'network_available')] = False
        budgets[(pool, 'ports')] = free(with_credit(network.get('port'), credits.get('port', 0)))
        budgets[(pool, 'floatingip')] = free(with_credit(network.get('floatingip'), credits.get('floatingip', 0)))
        budgets[(pool, 'network_ports')] = network_port_demand(backend, pool)
    pending = [n for n in backend.policy['sequential_placement']['node_order'] if not backend.completed(n)]
    disk = backend.policy['boot_disk_gb']

    def search(index, available, networks):
        if index == len(pending):
            return True
        name = pending[index]
        role = backend.desired['nodes'][name]['role']
        profile = backend.policy['profiles'][role]
        for choice in backend.choices[role]:
            pool, zone = choice['region'], choice['availability_zone']
            demand = {(pool, zone, 'instances'): 1, (pool, zone, 'cores'): profile['vcpus'],
                      (pool, zone, 'ram'): profile['ram_mb'], (pool, 'gigabytes'): disk,
                      (pool, 'volumes'): 1, (pool, 'gigabytes_' + choice['volume_type']): disk,
                      (pool, 'ports'): 1, (pool, 'floatingip'): int(role == 'app')}
            if pool not in networks:
                demand[(pool, 'ports')] += available[(pool, 'network_ports')]
            if not available[(pool, 'network_available')]:
                continue
            if any(key not in available or available[key] < amount for key, amount in demand.items()):
                continue
            following = {**available, **{key: available[key] - value for key, value in demand.items()}}
            if search(index + 1, following, networks | {pool}):
                return True
        return False

    if not search(0, budgets, set()):
        raise CandidateUnavailable('No quota-feasible assignment for all remaining nodes')


def require_network_capacity(backend, pool):
    if backend.completed('network-' + pool):
        return
    cloud = backend.region(pool)
    project = backend.credentials['project_id']
    network = cloud.get('network', '/v2.0/quotas/' + project + '/details.json')['quota']
    needs = dict(network=1, subnet=1, router=1, security_group=1,
                 security_group_rule=len(backend.journal['pool_subnets']) + 3 + 2 * len(backend.journal.get('admin_cidrs', [])), port=network_port_demand(backend, pool))
    kinds = {'network': 'openstack_networking_network_v2', 'subnet': 'openstack_networking_subnet_v2',
             'router': 'openstack_networking_router_v2', 'security_group': 'openstack_networking_secgroup_v2',
             'security_group_rule': 'openstack_networking_secgroup_rule_v2'}
    entries = backend.store.entries('network-' + pool)
    for key, kind in kinds.items():
        needs[key] = max(0, needs[key] - sum(e['type'] == kind for e in entries))
    if any(e['type'] == kinds['security_group'] for e in entries):
        needs['security_group_rule'] = max(0, needs['security_group_rule'] - 2)
    for key, amount in needs.items():
        if free(network.get(key)) < amount:
            raise CandidateUnavailable('Network quota insufficient in ' + pool + ': ' + key)
    compute = cloud.get('compute', '/os-quota-sets/' + project + '/detail')['quota_set']
    group_needed = int(not any(e['type'] == 'openstack_compute_servergroup_v2' for e in entries))
    members = compute.get('server_group_members')
    required_members = backend.journal.get('require_distinct_hosts', True)
    if free(compute.get('server_groups')) < group_needed or (required_members and (members is None or (members['limit'] != -1 and members['limit'] < 3))):
        raise CandidateUnavailable('Elasticsearch server-group quota unknown or insufficient in ' + pool)
