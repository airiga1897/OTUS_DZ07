"""Actual API ownership, placement and deletion checks for one retained stage."""
import ipaddress
import time
import urllib.error
import urllib.parse

GLOBAL = {'selectel_global_router_router_v1': 'routers', 'selectel_global_router_vpc_network_v1': 'networks',
          'selectel_global_router_vpc_subnet_v1': 'subnets'}
COLLECTIONS = {
    'openstack_compute_instance_v2': ('compute', '/servers/detail', 'servers'),
    'openstack_compute_servergroup_v2': ('compute', '/os-server-groups', 'server_groups'),
    'openstack_blockstorage_volume_v3': ('volumev3', '/volumes/detail', 'volumes'),
    'openstack_networking_port_v2': ('network', '/v2.0/ports', 'ports'),
    'openstack_networking_network_v2': ('network', '/v2.0/networks', 'networks'),
    'openstack_networking_subnet_v2': ('network', '/v2.0/subnets', 'subnets'),
    'openstack_networking_router_v2': ('network', '/v2.0/routers', 'routers'),
    'openstack_networking_secgroup_v2': ('network', '/v2.0/security-groups', 'security_groups'),
    'openstack_networking_secgroup_rule_v2': ('network', '/v2.0/security-group-rules', 'security_group_rules'),
    'openstack_networking_floatingip_v2': ('network', '/v2.0/floatingips', 'floatingips'),
}
RELATIONSHIPS = {'openstack_networking_router_interface_v2', 'openstack_networking_subnet_route_v2',
                 'openstack_networking_floatingip_associate_v2'}


def objects(cloud, service, path, key):
    result, seen = [], set()
    url = path + '?limit=200'
    while url:
        if url in seen:
            raise ValueError('Cyclic OpenStack pagination')
        seen.add(url)
        page = cloud.get(service, url)
        values = page[key]
        if not isinstance(values, list):
            raise ValueError('Unknown OpenStack collection shape')
        result.extend(values)
        links = [link['href'] for link in page.get(key + '_links', []) if link.get('rel') == 'next']
        if len(links) > 1 or (len(values) >= 200 and not links):
            raise ValueError('Collection completeness is unconfirmed')
        url = None
        if links:
            endpoint = urllib.parse.urlsplit(cloud.endpoints[service])
            following = urllib.parse.urlsplit(urllib.parse.urljoin(cloud.endpoints[service] + '/', links[0]))
            if following.scheme != 'https' or following.netloc != endpoint.netloc or not following.path.startswith(endpoint.path.rstrip('/') + '/'):
                raise ValueError('Unsafe OpenStack pagination URL')
            url = following.path[len(endpoint.path.rstrip('/')):] + '?' + following.query
    return result


def inventory(backend, name, entries):
    cloud = backend.region(backend.journal['stages'][name]['candidate']['region'])
    found = {}
    for kind in {e['type'] for e in entries}:
        if kind in COLLECTIONS:
            service, url, key = COLLECTIONS[kind]
            found[kind] = {x['id']: x for x in objects(cloud, service, url, key)}
            # The collection proves API support; a documented GET by saved ID
            # distinguishes actual absence from a record outside a collection page.
            for entry in (e for e in entries if e['type'] == kind and e['attrs']['id'] not in found[kind]):
                identifier = entry['attrs']['id']
                direct = url.removesuffix('/detail') + '/' + identifier
                try:
                    item = cloud.get(service, direct)[key[:-1]]
                    found[kind][identifier] = item
                except urllib.error.HTTPError as error:
                    if error.code != 404:
                        raise
        elif kind in GLOBAL:
            found[kind] = {x['id']: x for x in backend.global_router.listing(GLOBAL[kind])}
        elif kind not in RELATIONSHIPS:
            raise ValueError('Unknown stage resource cannot be audited: ' + kind)
    return found


def audit_namespace(backend, pool):
    cloud = backend.region(pool)
    for kind, (service, path, key) in COLLECTIONS.items():
        for item in objects(cloud, service, path, key):
            name = item.get('name', '')
            if name == backend.api.PREFIX or name.startswith(backend.api.PREFIX + '-') or (kind == 'openstack_networking_floatingip_v2' and backend.api.PREFIX in item.get('description', '')):
                raise RuntimeError('Deployment namespace is not empty in ' + pool)


def assert_stage_owned(backend, name, active=False):
    record = backend.journal['stages'][name]
    if record.get('lineage') and backend.store.state(name).get('lineage') != record['lineage']:
        raise ValueError('Stage state lineage changed')
    if active and 'identities' in record and backend.store.identities(name) != record['identities']:
        raise ValueError('Saved stage identities differ from state')
    entries = backend.store.entries(name)
    found = inventory(backend, name, entries)
    for entry in entries:
        kind, attrs = entry['type'], entry['attrs']
        if kind in RELATIONSHIPS:
            continue  # The associated objects are independently checked below/in their own stage.
        item = found[kind].get(attrs['id'])
        if item is None:
            known_deleted = (record['status'] == 'deleting' and any(e['type'] == kind and e['attrs'].get('id') == attrs['id']
                             for e in record.get('audit_entries', [])))
            if not active and known_deleted:
                continue
            raise ValueError('Stage resource missing in API; explicit recovery required')
        if kind in GLOBAL:
            if backend.owner not in item.get('tags', []):
                raise ValueError('Global resource tag mismatch')
            if kind == 'selectel_global_router_vpc_subnet_v1':
                # The API can return project_id=null for a subnet. Its project
                # is then proven through the tagged parent VPC network.
                project = backend.credentials['project_id']
                parents = found.get('selectel_global_router_vpc_network_v1')
                if parents is None:
                    parents = {p['id']: p for p in backend.global_router.listing('networks')}
                parent = parents.get(item.get('network_id'), {})
                if (item.get('project_id') not in (None, '', project)
                        or item.get('network_id') != attrs.get('network_id')
                        or item.get('os_subnet_id') != attrs.get('os_subnet_id')
                        or parent.get('project_id') != project
                        or backend.owner not in parent.get('tags', [])):
                    raise ValueError('Global subnet parent/project mismatch')
            elif kind == 'selectel_global_router_vpc_network_v1' and item.get('project_id') != backend.credentials['project_id']:
                raise ValueError('Global attachment project mismatch')
        else:
            if attrs.get('region') not in (None, '', backend.journal['stages'][name]['candidate']['region']):
                raise ValueError('Stage pool mismatch')
            tenant = item.get('tenant_id', item.get('project_id'))
            # Nova server groups and Cinder volumes omit tenant in project-scoped lists.
            if tenant is not None and tenant != backend.credentials['project_id']:
                raise ValueError('Stage project mismatch')
            if attrs.get('name') and item.get('name') != attrs['name']:
                raise ValueError('Stage resource name mismatch')
            if kind in ('openstack_compute_instance_v2', 'openstack_blockstorage_volume_v3') and item.get('metadata', {}).get('dz07_owner') != backend.owner:
                raise ValueError('VM or volume deployment marker mismatch')
            if kind in ('openstack_networking_network_v2', 'openstack_networking_router_v2') and backend.owner not in item.get('tags', []):
                raise ValueError('Network deployment marker mismatch')
            if kind == 'openstack_networking_secgroup_v2' and item.get('description') != backend.owner:
                raise ValueError('Security group deployment marker mismatch')
            if kind == 'openstack_compute_servergroup_v2':
                known = set()
                for node, specification in backend.desired['nodes'].items():
                    saved = backend.journal['stages'].get(node, {})
                    if specification['role'] != 'es' or saved.get('candidate', {}).get('region') != record['candidate']['region']:
                        continue
                    node_entries = backend.store.entries(node) + saved.get('audit_entries', [])
                    known.update(e['attrs']['id'] for e in node_entries if e['type'] == 'openstack_compute_instance_v2')
                if not isinstance(item.get('members'), list) or not set(item['members']) <= known:
                    raise ValueError('Unknown VM membership in the deployment server group')
        if active and kind in GLOBAL and item.get('status') != 'ACTIVE':
            raise RuntimeError('Global Router object is not ACTIVE')
        if active and kind == 'openstack_compute_instance_v2':
            candidate = backend.journal['stages'][name]['candidate']
            if item.get('status') != 'ACTIVE' or item.get('OS-EXT-AZ:availability_zone') != candidate['availability_zone']:
                raise RuntimeError('VM state or segment is unconfirmed')


def assert_stage_absent(backend, name):
    entries = backend.journal['stages'][name].get('audit_entries', [])
    deadline = time.monotonic() + 90
    while True:
        found = inventory(backend, name, entries)
        remaining = [e['attrs']['id'] for e in entries if e['type'] in found and e['attrs'].get('id') in found[e['type']]]
        if not remaining:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError('Stage resources remain in API after Terraform deletion')
        time.sleep(2)


def faults(backend, name):
    result = {'stage': name, 'nodes': {}, 'read_only': True}
    entries = backend.store.entries(name)
    if any(e['type'] in GLOBAL for e in entries):
        assert_stage_owned(backend, name)
    for entry in entries:
        if entry['type'] in GLOBAL:
            kind, attrs = entry['type'], entry['attrs']
            try:
                item = backend.global_router.get('/' + GLOBAL[kind] + '/' + urllib.parse.quote(attrs['id'], safe=''))
                if not isinstance(item, dict):
                    raise ValueError('Unknown Global Router diagnostic response')
                if item.get('id') != attrs['id'] or backend.owner not in item.get('tags', []):
                    raise ValueError('Global diagnostic ownership mismatch')
                result.setdefault('global_router', {})[kind] = {k: item[k] for k in
                    ('status', 'created_at', 'updated_at', 'fault', 'error', 'error_message', 'last_error', 'details') if k in item}
            except (OSError, ValueError, KeyError) as error:
                result.setdefault('global_router', {})[kind] = {
                    'diagnostic_error': type(error).__name__, 'http_status': getattr(error, 'code', None)}
            continue
        if entry['type'] != 'openstack_compute_instance_v2':
            continue
        cloud = backend.region(backend.journal['stages'][name]['candidate']['region'])
        server = cloud.get('compute', '/servers/' + entry['attrs']['id'])['server']
        if server.get('name') != backend.api.PREFIX + '-' + name or server.get('tenant_id') != backend.credentials['project_id']:
            raise ValueError('Fault diagnostic ownership mismatch')
        result['nodes'][name] = {key: server.get(key) for key in ('status', 'fault', 'OS-EXT-AZ:availability_zone',
                                   'OS-EXT-STS:vm_state', 'OS-EXT-STS:task_state')}
    return result


def verify_networks(backend):
    nodes = backend.result().nodes
    require_distinct = backend.journal.get('require_distinct_hosts', True)
    physical = []
    for name, node in nodes.items():
        cloud = backend.region(node['region'])
        server = cloud.get('compute', '/servers/' + node['id'])['server']
        network = backend.journal['stages']['network-' + node['region']]['output']
        port = cloud.get('network', '/v2.0/ports/' + node['port_id'])['port']
        if (port.get('port_security_enabled') is not True or port.get('security_groups') != [network['security_group_id']]
                or port.get('network_id') != network['network_id']
                or not any(ip['ip_address'] == node['private_ip'] for ip in port['fixed_ips'])):
            raise RuntimeError('Private VM port filtering/address unconfirmed')
        if name != 'app' and any(f.get('port_id') == node['port_id'] for f in objects(cloud, 'network', '/v2.0/floatingips', 'floatingips')):
            raise RuntimeError('An internal Elasticsearch node has a public address')
        if 'es' in node['roles']:
            groups = objects(cloud, 'compute', '/os-server-groups', 'server_groups')
            matches = [g for g in groups if g['id'] == network['server_group_id']]
            if len(matches) != 1 or matches[0].get('policies') != ['anti-affinity']:
                raise RuntimeError('Owned Elasticsearch placement group unconfirmed')
            member = node['id'] in matches[0].get('members', [])
            if require_distinct and (not member or not server.get('hostId')):
                raise RuntimeError('Elasticsearch hard anti-affinity unconfirmed')
            if not require_distinct and member:
                raise RuntimeError('Diagnostic VM unexpectedly belongs to the hard placement group')
            if server.get('hostId'):
                physical.append((node['region'], server['hostId']))
    if require_distinct and (len(physical) != 3 or len(set(physical)) != 3):
        raise RuntimeError('Three physically separate Elasticsearch nodes required')
    for name, record in backend.journal['stages'].items():
        if record['status'] != 'active':
            continue
        assert_stage_owned(backend, name, active=True)
        if name.startswith('network-'):
            cloud = backend.region(record['candidate']['region'])
            net = cloud.get('network', '/v2.0/networks/' + record['output']['network_id'])['network']
            if net.get('shared') is not False or net.get('router:external') is not False or net.get('port_security_enabled') is not True:
                raise RuntimeError('Private network isolation unconfirmed')
            subnet = cloud.get('network', '/v2.0/subnets/' + record['output']['subnet_id'])['subnet']
            expected = {(cidr, str(ipaddress.ip_network(record['output']['cidr'])[record['inputs']['global_gateway_host']]))
                        for cidr in record['inputs']['peer_cidrs']}
            if expected != {(r['destination'], r['nexthop']) for r in subnet.get('host_routes', [])}:
                raise RuntimeError('Global Router subnet routes unconfirmed')
            routers = objects(cloud, 'network', '/v2.0/routers', 'routers')
            routers = [r for r in routers if r.get('name') == record['inputs']['name']]
            if len(routers) != 1 or any(set(item.get('availability_zone_hints', [])) != {zone} or set(item.get('availability_zones', [])) != {zone}
                    for item, zone in ((net, record['inputs']['network_zone']), (routers[0], record['inputs']['router_zone']))):
                raise RuntimeError('Actual network/router scheduling domains unconfirmed')
            group = cloud.get('network', '/v2.0/security-groups/' + record['output']['security_group_id'])['security_group']
            actual = {(r.get('protocol'), r.get('port_range_min'), r.get('port_range_max'), r.get('remote_ip_prefix'), r.get('remote_group_id'), r.get('ethertype'))
                      for r in group['security_group_rules'] if r['direction'] == 'ingress'}
            expected = {(None, None, None, cidr, None, 'IPv4') for cidr in [record['inputs']['cidr'], *record['inputs']['peer_cidrs']]}
            expected.add(('tcp', 80, 80, '0.0.0.0/0', None, 'IPv4'))
            expected |= {('tcp', port, port, cidr, None, 'IPv4') for port in (22, record['inputs']['kibana_port']) for cidr in record['inputs']['admin_cidrs']}
            if actual != expected:
                raise RuntimeError('Actual network ingress differs from saved policy')
    return {'success': True, 'es_distinct_hosts': len(set(physical)) if len(physical) == 3 else None,
            'require_distinct_hosts': require_distinct, 'physical_host_separation_verified': require_distinct,
            'private_ports_filtered': True, 'global_routes_confirmed': True}
