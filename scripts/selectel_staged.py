"""Selectel backend for independently retained networks, attachments and VMs."""
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import uuid
import urllib.error

from cloud_contract import InfrastructureResult, require_capabilities
from credentials import credential_values, load_credentials, to_environment
from local_runtime import checked_tree
import pool_selection as pools
import privacy
from selectel_adapter import SelectelAdapter, discover_capabilities, reject_cloud_resources
from selectel_global_router import GlobalRouter
from stage_store import JOURNAL, StageStore, atomic_json, deployment_exists as exists, require_context
from staged_deploy import place, PlacementExhausted
from run_settings import placement as placement_settings, snapshot as settings_snapshot, pool_subnet

LOGGER = logging.getLogger(__name__)


def enabled(root: Path) -> bool:
    path = Path(root) / 'config/placement.json'
    return path.exists() and json.loads(path.read_text()).get('sequential_placement', {}).get('enabled') is True




def validate_policy(policy: dict, desired: dict) -> None:
    if type(policy.get('require_distinct_hosts', True)) is not bool:
        raise ValueError('require_distinct_hosts must be boolean')
    config = policy['sequential_placement']
    if set(config['node_order']) != set(desired['nodes']) or len(config['node_order']) != len(desired['nodes']):
        raise ValueError('Sequential node order must contain each desired node once')
    subnets = config['pool_subnets']
    if set(policy['pools']) & set(policy['excluded_pools']):
        raise ValueError('Requested pool is explicitly excluded')
    if ('auto' not in policy['pools'] and set(subnets) != set(policy['pools'])) or set(subnets) & set(policy['excluded_pools']):
        raise ValueError('Pool subnet configuration is inconsistent with pool policy')
    nets = [ipaddress.ip_network(c) for c in subnets.values()]
    if any(n.version != 4 or n.prefixlen != 24 or not n.is_private for n in nets):
        raise ValueError('Sequential networks require private IPv4 /24 subnets')
    if any(a.overlaps(b) for i, a in enumerate(nets) for b in nets[i + 1:]):
        raise ValueError('Global Router subnets must not overlap')

    slots = config['addresses']
    reserved = [slots['cloud_gateway'], slots['global_gateway'], *slots['service_hosts']]
    if (len(slots['service_hosts']) != 2 or len(set(reserved)) != len(reserved)
            or any(type(h) is not int or not 1 <= h <= 254 for h in reserved)
            or not 1 <= slots['dhcp_first'] <= slots['dhcp_last'] <= 254):
        raise ValueError('Invalid network service address reservations')
    dhcp = set(range(slots['dhcp_first'], slots['dhcp_last'] + 1))
    if dhcp & set(reserved) or any(node['host'] in dhcp | set(reserved) for node in desired['nodes'].values()):
        raise ValueError('Node, DHCP and gateway addresses must not overlap')


def terraform_environment(credentials, candidate):
    region = candidate.get('region', credentials['region'])
    env = {k:v for k,v in os.environ.items() if not k.startswith('OS_') and k not in
           {'INFRA_REGION', 'INFRA_PROJECT_ID', 'OTUS_SELECTEL_AUTH'}}
    env.update(to_environment(credentials))
    env.update(OS_REGION_NAME=region, OS_DOMAIN_NAME=credentials['domain_name'])
    return env


def validate_local_result(api, result, journal, store):
    for name, node in result.nodes.items():
        for entry in store.entries(name):
            attrs = entry['attrs']
            if attrs.get('region') not in (None, '', node['region']) or attrs.get('tenant_id') not in (None, '', journal['project_id']):
                raise ValueError('Foreign Selectel pool or project in local state')



def reject_shared_namespace(api, credentials=None):
    from credentials import load_credentials
    from selectel_global_router import GlobalRouter
    client = GlobalRouter(credentials or load_credentials())
    for kind in ('routers', 'networks', 'subnets'):
        if any(item.get('name') == api.PREFIX or item.get('name', '').startswith(api.PREFIX + '-')
               or any(tag.startswith(api.PREFIX + '-') for tag in item.get('tags', []))
               for item in client.listing(kind)):
            raise RuntimeError('Global Router namespace already exists; recover its context before continuing')


def audit_empty_scope(api):
    from selectel_adapter import SelectelAdapter, reject_cloud_resources
    credentials = load_credentials()
    for filename in ('context.json', 'selectel-context.json', 'infrastructure.json'):
        path = api.LOCAL / filename
        checked_tree(path, Path(api.ROOT).resolve())
        if path.exists():
            saved = json.loads(path.read_text())
            project = saved.get('project_id', saved.get('scope', {}).get('project_id'))
            if project != credentials['project_id']:
                raise ValueError('Existing local context has a different or unknown project')
    cloud = SelectelAdapter(credentials)
    pools = cloud.resource_regions()
    for pool in pools:
        regional = cloud.read_only_pool(pool)
        reject_cloud_resources(api, regional)
        if 'network' in regional.endpoints:
            from selectel_staged_audit import objects
            try:
                floating = objects(regional, 'network', '/v2.0/floatingips', 'floatingips')
            except urllib.error.HTTPError as error:
                if error.code != 404:
                    raise
                aliases = {e['alias'] for e in regional.get('network', '/v2.0/extensions')['extensions']}
                if 'router' in aliases:
                    raise
                floating = []
            if any(f.get('description', '').startswith(api.PREFIX + '-') for f in floating):
                raise RuntimeError('Untracked deployment floating IP remains in ' + pool)
    reject_shared_namespace(api, credentials)
    return {'empty_without_context': True, 'pools_audited': pools, 'global_router_remaining': 0}


class Backend:
    def __init__(self, api, cleanup=False):
        self.api, self.root = api, Path(api.ROOT).resolve()
        self.path = self.root / '.local' / JOURNAL
        checked_tree(self.path, self.root)
        self.credentials = load_credentials()
        privacy.remember(credential_values(self.credentials))
        saved = json.loads(self.path.read_text()) if self.path.exists() else None
        self.policy = placement_settings(self.root) if not cleanup or not saved or not saved.get('run_settings') else {
            **json.loads((self.root / 'config/placement.json').read_text()), **saved['run_settings'],
            'pools': list(saved['pool_subnets'])}
        self.desired = saved['topology'] if cleanup and saved else api.desired_topology()
        from elastic_options import node_order
        if not cleanup:
            node_order(self.policy, saved or {}, self.desired)
            validate_policy(self.policy, self.desired)
        if not self.path.exists():
            legacy = self.root / 'terraform-selectel'
            if any((self.root / '.local' / n).exists() for n in ('context.json', 'placement.json', 'configured.json')) or (legacy / 'terraform.tfvars.json').exists() or (self.root / '.local/stages').exists():
                raise ValueError('Existing legacy context or orphan stages require separate recovery')
        self.journal = saved if saved is not None else {
            'provider': 'selectel', 'scope': {'project_id': self.credentials['project_id']}, 'project_id': self.credentials['project_id'],
            'namespace': api.PREFIX, 'deployment_id': uuid.uuid4().hex, 'stages': {}, 'configured': False,
            'run_settings': settings_snapshot(self.policy), 'bootstrap_region': self.credentials['region'],
            'require_distinct_hosts': self.policy.get('require_distinct_hosts', True),
            'pool_subnets': self.policy['sequential_placement']['pool_subnets'],
            'topology': self.desired, 'address_policy': self.policy['sequential_placement']['addresses']}
        self.validate_journal(cleanup)
        self.cloud = SelectelAdapter(self.credentials)
        self.global_router = GlobalRouter(self.credentials)
        from terraform_plugins import cli_path
        self.store = StageStore(api, self.journal, environment=lambda candidate: terraform_environment(self.credentials, candidate), cli_config=cli_path(self.root), drift_validator=self.allow_refresh)
        self.choices = {}
        self._temporary_networks = set()

    def prepare_tools(self):
        from terraform_plugins import prepare
        self.store.cli_config = prepare(self.api)

    def validate_journal(self, cleanup=False):
        if (self.journal['provider'] != 'selectel' or self.journal.get('scope') != {'project_id': self.credentials['project_id']} or self.journal['project_id'] != self.credentials['project_id']
                or self.journal['namespace'] != self.api.PREFIX or (not cleanup and self.journal['topology'] != self.desired)):
            raise ValueError('Sequential deployment scope or topology mismatch; preserve recovery files')
        if not re.fullmatch('[0-9a-f]{32}', self.journal['deployment_id']):
            raise ValueError('Invalid deployment identity')
        for name, record in self.journal['stages'].items():
            region = record['candidate'].get('region')
            allowed = set(self.journal['topology']['nodes']) | {'global'}
            allowed |= {'network-' + p for p in self.journal['pool_subnets']}
            allowed |= {'connection-' + p for p in self.journal['pool_subnets']}
            global_region = self.journal.get('bootstrap_region', self.credentials['region'])
            valid_region = region == global_region if name == 'global' else region in self.journal['pool_subnets']
            if name not in allowed or not valid_region:
                raise ValueError('Stage is outside the saved deployment scope')
        if cleanup:
            self.desired = self.journal['topology']
            return
        if self.journal.get('require_distinct_hosts', True) != self.policy.get('require_distinct_hosts', True):
            raise ValueError('Existing physical-host policy cannot change')
        previous = self.journal.get('run_settings', settings_snapshot(self.policy))
        current = settings_snapshot(self.policy)
        if any(previous.get(key) != current.get(key) for key in ('profiles', 'boot_disk_gb', 'disk_type_patterns', 'require_distinct_hosts')):
            raise ValueError('Existing VM and physical-host settings cannot change; preserve the current stand')
        if self.journal['address_policy'] != self.policy['sequential_placement']['addresses']:
            raise ValueError('Existing network address reservations cannot change')
        if any(self.journal['pool_subnets'].get(p) != cidr for p,cidr in self.policy['sequential_placement']['pool_subnets'].items() if p in self.journal['pool_subnets']):
            raise ValueError('Existing pool networks cannot be readdressed')
        if not all(('auto' in self.policy['pools'] or k in self.policy['pools']) and k not in self.policy['excluded_pools'] for k in self.journal['pool_subnets']):
            raise ValueError('Existing deployment uses a now forbidden pool; explicit recovery required')

    @property
    def owner(self):
        return self.api.PREFIX + '-' + self.journal['deployment_id']

    def save(self):
        # Persist the write-ahead record before every operation, including failed cleanup.
        atomic_json(self.path, self.journal, self.root)

    def region(self, region):
        return self.cloud.read_only_pool(region)

    def allow_refresh(self, name, drift):
        """Accept only proven, read-only changes derived from our saved resources."""
        kind = drift['type']
        if not kind.startswith('openstack_'):
            return False
        before, after = drift['change'].get('before') or {}, drift['change'].get('after') or {}
        changed = {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}
        from selectel_refresh import NODE_KINDS, allow_node_refresh
        if kind in NODE_KINDS and changed - {'tags', 'all_tags'} and name in self.desired['nodes']:
            return allow_node_refresh(self, name, drift)
        if kind == 'openstack_blockstorage_volume_v3' and changed == {'attachment'} and name in self.desired['nodes']:
            entries = self.store.entries(name)
            servers = [e['attrs']['id'] for e in entries if e['type'] == 'openstack_compute_instance_v2']
            volumes = [e for e in entries if e['type'] == kind and e['address'] == drift['address']]
            if len(servers) != 1 or len(volumes) != 1 or volumes[0]['attrs']['id'] != after.get('id'):
                return False
            old, new = before.get('attachment') or [], after.get('attachment') or []
            # Cinder records the volume before Nova attaches it as the boot disk.
            # Refresh may discover that attachment; it must never detach it or
            # point to another VM. Planned writes are checked independently.
            return (isinstance(old, list) and isinstance(new, list) and len(new) == 1
                and len(old) <= 1 and all(isinstance(a, dict) and a.get('instance_id') == servers[0]
                    for a in old + new))
        if kind == 'openstack_compute_servergroup_v2' and changed == {'members'} and name.startswith('network-'):
            pool = name.removeprefix('network-')
            expected, known = set(), set()
            for node, specification in self.desired['nodes'].items():
                record = self.journal['stages'].get(node, {})
                if specification['role'] != 'es' or record.get('candidate', {}).get('region') != pool:
                    continue
                entries = self.store.entries(node)
                if self.journal.get('require_distinct_hosts', True):
                    expected.update(e['attrs']['id'] for e in entries if e['type'] == 'openstack_compute_instance_v2')
                known.update(e['attrs']['id'] for e in entries + record.get('audit_entries', []) if e['type'] == 'openstack_compute_instance_v2')
            return set(after.get('members') or []) == expected and set(before.get('members') or []) <= known
        for key in changed:
            old, new = before.get(key), after.get(key)
            if key in ('tags', 'all_tags') and old in (None, []) and new in (None, []):
                continue
            if kind == 'openstack_networking_network_v2' and key == 'all_tags' and name.startswith('network-'):
                pool = name.removeprefix('network-')
                attachment = self.journal['stages'].get('connection-' + pool, {}).get('output', {}).get('network_id')
                expected = {self.owner, 'l3vpn-network', 'l3vpn-network-' + attachment} if attachment else set()
                if expected and set(old or []) in ({self.owner}, expected) and set(new or []) in ({self.owner}, expected):
                    continue
            return False
        return bool(changed)

    def preflight(self):
        self.resolve_pools()
        required = json.loads((self.root / 'config/cloud.json').read_text())['required_capabilities'] + ['ownership_tags']
        finished = all(self.completed(n) for n in self.desired['nodes'])
        pools_to_check = sorted({s['candidate']['region'] for n,s in self.journal['stages'].items() if n.startswith('network-') and s['status'] == 'active'}) if finished else self.policy['pools']
        eligible = []
        for pool in pools_to_check:
            cloud = self.region(pool)
            require_capabilities(discover_capabilities(cloud), required)
            subnets = cloud.get('network', '/v2.0/subnets')['subnets']
            wanted = ipaddress.ip_network(self.journal['pool_subnets'][pool])
            own_net = self.saved_id('network-' + pool, 'openstack_networking_network_v2', 'network_id')
            if any(ipaddress.ip_network(s['cidr']).overlaps(wanted) and s['network_id'] != own_net for s in subnets):
                if own_net:
                    raise ValueError('Subnet conflicts with an existing network in ' + pool)
                LOGGER.warning('%s: subnet conflict; skipped', pool)
                continue
            if not self.path.exists():
                reject_cloud_resources(self.api, cloud)
                from selectel_staged_audit import audit_namespace
                audit_namespace(self, pool)
            eligible.append(pool)
        self.policy['pools'] = eligible
        if not eligible:
            raise ValueError('No compatible network pools')
        router = self.saved_id('global', 'selectel_global_router_router_v1', 'router_id')
        # Discover each role separately: app availability must not eliminate ES placements.
        for role in {n['role'] for n in self.desired['nodes'].values()}:
            if all(self.completed(name) for name, node in self.desired['nodes'].items() if node['role'] == role):
                self.choices[role] = []
                continue
            config = {**self.policy, 'profiles': {role: self.policy['profiles'][role]}}
            candidates = list(pools.candidates(config, self.credentials))
            accepted = []
            flavors = {}
            for candidate in candidates:
                cloud = self.region(candidate['region'])
                flavor_key = (candidate['region'], candidate['flavor_ids'][role])
                if flavor_key not in flavors:
                    flavors[flavor_key] = cloud.get('compute', '/flavors/' + candidate['flavor_ids'][role])['flavor']
                flavor = flavors[flavor_key]
                if not pools.flavor_available_in_zone(flavor, candidate['availability_zone']):
                    LOGGER.warning('%s: flavor availability in the segment is unconfirmed', candidate['availability_zone'])
                    continue
                try:
                    self.check_node_quota(candidate, role)
                except pools.CandidateUnavailable as error:
                    LOGGER.warning('%s/%s rejected: %s', candidate['region'], candidate['availability_zone'], error)
                    continue
                accepted.append(candidate)
            self.choices[role] = accepted
        if not finished:
            usable = {c['region'] for items in self.choices.values() for c in items}
            usable |= {s['candidate']['region'] for name,s in self.journal['stages'].items()
                       if name.startswith('network-') and s['status'] != 'deleted'}
            for region in eligible:
                if region not in usable:
                    LOGGER.warning('%s: no compatible remaining VM role; skipped', region)
            eligible = [r for r in eligible if r in usable]
            self.policy['pools'] = eligible
        if not finished and getattr(self, '_auto_discovery', False):
            from selectel_global_router import GlobalRouterCapacityError
            original = {role:list(items) for role,items in self.choices.items()}
            retained = {s['candidate']['region'] for name,s in self.journal['stages'].items()
                        if name.startswith('network-') and s['status'] != 'deleted'}
            original_subnets = dict(self.journal['pool_subnets'])
            def accept(regions):
                self.policy['pools'] = regions
                self.choices = {role:[c for c in items if c['region'] in regions] for role,items in original.items()}
                if not self.path.exists():
                    self.journal['pool_subnets'] = {r:original_subnets[r] for r in regions}
                if any(not self.choices[role] for role in original
                       if not all(self.completed(n) for n,v in self.desired['nodes'].items() if v['role'] == role)):
                    return False
                try:
                    self.check_complete_capacity()
                    self.global_router.preflight(regions, self.api.PREFIX, router, max_networks=len(self.desired['nodes']))
                except (pools.CandidateUnavailable, GlobalRouterCapacityError) as error:
                    LOGGER.warning('Zone group rejected: %s', error)
                    return False
                return True
            self.global_router.compatible_regions(eligible, retained, accept=accept)
        else:
            for role,items in self.choices.items():
                if not items and not all(self.completed(n) for n,v in self.desired['nodes'].items() if v['role'] == role):
                    raise ValueError('No complete compatible candidates for ' + role)
            self.check_complete_capacity()
            if not finished:
                self.global_router.preflight(eligible, self.api.PREFIX, router, max_networks=len(self.desired['nodes']))
        self.api.save(self.root / 'docs/staged-preflight.json', {'success': True, 'read_only': True,
                      'candidate_counts': {r: len(v) for r, v in self.choices.items()},
                      'global_router_discovery': True})

    def resolve_pools(self):
        if 'auto' not in self.policy['pools']:
            return
        self._auto_discovery = True
        from cloud_contract import CapabilityStatus
        regions = pools.ordered_pools(self.policy, self.cloud.catalog)
        if self.path.exists():
            regions = [r for r in regions if r in self.journal['pool_subnets']]
        retained = {s['candidate']['region'] for name,s in self.journal['stages'].items()
                    if name.startswith('network-') and s['status'] != 'deleted'}
        if not retained <= set(regions):
            raise ValueError('Saved network pool is not available in the confirmed catalog')
        required = json.loads((self.root/'config/cloud.json').read_text())['required_capabilities'] + ['ownership_tags']
        supported = []
        for region in regions:
            found = discover_capabilities(self.region(region))
            # An unknown API result is never converted into "unsupported".
            unknown = [key for key in required if key not in found or found[key].status == CapabilityStatus.UNKNOWN]
            if unknown:
                require_capabilities(found, required)
            unavailable = [key for key in required if found[key].status == CapabilityStatus.UNSUPPORTED]
            if unavailable:
                if region in retained:
                    raise ValueError('Saved pool capabilities changed: ' + region)
                LOGGER.warning('%s: unsupported capabilities %s; skipped', region, ', '.join(unavailable))
                continue
            supported.append(region)
        if not supported:
            raise ValueError('No compatible discovered pools')
        self.policy['pools'] = supported
        if not self.path.exists():
            assigned = {}
            for region in supported:
                assigned[region] = pool_subnet(self.policy, region, assigned)
            self.journal['pool_subnets'] = assigned

    def check_node_quota(self, candidate, role):
        cloud = self.region(candidate['region'])
        project = self.credentials['project_id']
        compute = cloud.get('compute', '/os-quota-sets/' + project + '/detail')['quota_set'].get('az_quotas', {}).get(candidate['availability_zone'])
        if compute is None:
            raise pools.CandidateUnavailable('Compute segment quota unknown')
        from selectel_staged_capacity import pending_credits, require_named_quota
        credits = pending_credits(self, candidate['region'])
        profile = self.policy['profiles'][role]
        for key, needed in [('instances', 1), ('cores', profile['vcpus']), ('ram', profile['ram_mb'])]:
            require_named_quota(compute.get(key), needed, role + ': compute.' + key,
                                credits.get((candidate['availability_zone'], key), 0))
        volume = cloud.get('volumev3', '/os-quota-sets/' + project + '?usage=True')['quota_set']
        for key, needed in [('gigabytes', self.policy['boot_disk_gb']), ('volumes', 1),
                            ('gigabytes_' + candidate['volume_type'], self.policy['boot_disk_gb'])]:
            require_named_quota(volume.get(key), needed, role + ': volume.' + key, credits.get(key, 0))
        network = cloud.get('network', '/v2.0/quotas/' + project + '/details.json')['quota']
        for key, needed in [('port', 1), ('floatingip', int(role == 'app'))]:
            if needed:
                require_named_quota(network.get(key), needed, role + ': network.' + key, credits.get(key, 0))

    def check_complete_capacity(self):
        from selectel_staged_capacity import require_capacity
        require_capacity(self)

    def saved_id(self, name, kind, output_key):
        record = self.journal['stages'].get(name, {})
        if record.get('status') == 'deleted':
            return None
        if record.get('output', {}).get(output_key):
            return record['output'][output_key]
        ids = [e['attrs']['id'] for e in self.store.entries(name) if e['type'] == kind]
        if len(ids) > 1:
            raise ValueError('Ambiguous partial stage identity')
        return ids[0] if ids else None

    def completed(self, name):
        return self.journal['stages'].get(name, {}).get('status') == 'active'

    def validate_existing(self):
        if self.journal.get('destroy_requested'):
            raise ValueError('A full destroy was started; finish destroy before creating resources')
        from selectel_staged_audit import assert_stage_owned
        for name, stage in self.journal['stages'].items():
            if stage['status'] != 'deleted':
                assert_stage_owned(self, name, active=stage['status'] == 'active')

    def recover_attempt(self, name):
        if self.journal['stages'].get(name, {}).get('status') == 'deleting':
            self.cleanup_attempt(name)

    def deployment_order(self):
        return self.policy['sequential_placement']['node_order']

    def candidates_for(self, name):
        return self.choices[self.desired['nodes'][name]['role']]

    def pending(self, name):
        item = self.journal['stages'].get(name)
        return item['candidate'] if item and item['status'] != 'deleted' else None

    def identities(self, exclude_pending=False):
        return {(name, address): identifier for name, record in self.journal['stages'].items()
                if not exclude_pending or record['status'] == 'active'
                for address, identifier in self.store.identities(name).items()}

    def assert_preserved(self, before):
        now = self.identities()
        if any(now.get(key) != identifier for key, identifier in before.items()):
            raise RuntimeError('Previously created resource identity changed')

    def begin(self, name, candidate):
        if self.store.identities(name):
            raise ValueError('Cannot change candidate with resources remaining in its stage')
        self.journal['stages'][name] = {'candidate': candidate, 'status': 'pending'}
        self.save()

    def ensure_pool(self, candidate):
        pool = candidate['region']
        network = 'network-' + pool
        new_network = not self.completed(network)
        if new_network:
            from selectel_staged_capacity import require_network_capacity
            require_network_capacity(self, pool)
        choice = {k: candidate[k] for k in ('region', 'network_availability_zone', 'router_availability_zone')}
        network_choices = [choice]
        for options in self.choices.values():
            for item in options:
                if item['region'] == pool:
                    domain = {k: item[k] for k in ('region', 'network_availability_zone', 'router_availability_zone')}
                    if domain not in network_choices:
                        network_choices.append(domain)
        try:
            place(self, network, network_choices)
        except PlacementExhausted as error:
            if self.store.identities(network):
                raise
            raise pools.CandidateUnavailable('No network scheduling candidate succeeded in ' + pool) from error
        if new_network:
            self._temporary_networks.add(network)
        return self.journal['stages'][network]['output']

    def connect_pool(self, pool):
        connected = {record['candidate']['region'] for name,record in self.journal['stages'].items()
                     if name.startswith('connection-') and record['status'] != 'deleted'}
        router = self.saved_id('global', 'selectel_global_router_router_v1', 'router_id')
        self.global_router.preflight(sorted(connected | {pool}), self.api.PREFIX, router)
        place(self, 'global', [{'region': self.credentials['region']}])
        place(self, 'connection-' + pool, [{'region': pool}])

    def inputs(self, name):
        candidate = self.journal['stages'][name]['candidate']
        pool = candidate['region']
        label = self.api.PREFIX + '-' + name
        slots = self.journal['address_policy']
        if name == 'global':
            return 'global', {'name': label, 'owner': self.owner}
        if name.startswith('network-'):
            cidr = self.journal['pool_subnets'][pool]
            return 'network', dict(region=pool, name=label, cidr=cidr, owner=self.owner,
                cloud_gateway_host=slots['cloud_gateway'], global_gateway_host=slots['global_gateway'],
                dhcp_first_host=slots['dhcp_first'], dhcp_last_host=slots['dhcp_last'],
                network_zone=candidate['network_availability_zone'], router_zone=candidate['router_availability_zone'],
                admin_cidrs=list(self.journal['admin_cidrs']), kibana_port=self.api.topology.services(self.root)['kibana_port'],
                peer_cidrs=[v for k, v in self.journal['pool_subnets'].items() if k != pool])
        if name.startswith('connection-'):
            network = self.journal['stages']['network-' + pool]['output']
            return 'connection', dict(region=pool, name=label, owner=self.owner, project_id=self.credentials['project_id'],
                router_id=self.journal['stages']['global']['output']['router_id'],
                os_network_id=network['network_id'], os_subnet_id=network['subnet_id'], cidr=network['cidr'],
                global_gateway_host=slots['global_gateway'], service_hosts=slots['service_hosts'])
        network = self.ensure_pool(candidate)
        role = self.desired['nodes'][name]['role']
        if not self.saved_id(name, 'openstack_compute_instance_v2', 'id'):
            self.check_node_quota(candidate, role)
        secrets_path = self.root / '.local/secrets.json'
        passwords = json.loads(secrets_path.read_text())
        return 'node', dict(region=pool, owner=self.owner, node_name=name, role=role, name_prefix=self.api.PREFIX,
            require_distinct_hosts=self.journal.get('require_distinct_hosts', True),
            zone=candidate['availability_zone'], private_ip=str(ipaddress.ip_network(network['cidr'])[self.desired['nodes'][name]['host']]),
            subnet_cidr=network['cidr'], subnet_gateway=network['gateway_ip'], image_id=candidate['image_id'], flavor_id=candidate['flavor_ids'][role],
            volume_type=candidate['volume_type'], boot_disk_gb=self.policy['boot_disk_gb'],
            **{k: network[k] for k in ('network_id', 'subnet_id', 'security_group_id', 'server_group_id', 'external_network_name')},
            ssh_public_key_path=str(self.root / '.local/otus_dz07.pub'), route_probe_key=passwords['api'])

    def configure_stage(self, name, kind, inputs):
        if kind not in ('network', 'node', 'global', 'connection'):
            raise ValueError('Unknown Selectel stage module')
        provider = 'selectel' if kind in ('global', 'connection') else 'openstack'
        source = 'selectel/selectel' if provider == 'selectel' else 'terraform-provider-openstack/openstack'
        self.store.configure(name, self.root / 'terraform-selectel/staged' / kind, inputs,
                             providers={provider: {'source': source}})

    def extend_admin_access(self, lab, cidr):
        pending = self.journal.get('admin_access_pending')
        if pending is not None and pending != cidr:
            raise ValueError('Finish the pending administrative access change first')
        if cidr in self.journal['admin_cidrs'] and pending is None:
            return self.journal['admin_cidrs']
        name = 'network-' + lab['node_regions']['app']
        record = self.journal['stages'][name]
        if cidr not in self.journal['admin_cidrs']:
            self.journal['admin_cidrs'].append(cidr)
        self.journal['admin_access_pending'] = cidr
        record['inputs']['admin_cidrs'] = list(self.journal['admin_cidrs'])
        self.save()
        self.configure_stage(name, 'network', record['inputs'])
        def validate(plan):
            from operator_network import validate_access_plan
            for change in plan.get('resource_changes', []):
                if change.get('mode') != 'data' and change['change']['actions'] != ['no-op']:
                    validate_access_plan(change, cidr, record['output']['security_group_id'])
        self.store.apply(name, validate_extra=validate)
        self.finish(name)
        self.journal.pop('admin_access_pending', None)
        self.save()
        return self.journal['admin_cidrs']

    def ssh_host_key(self, lab, name, cached):
        return self.region(lab['node_regions'][name]).ssh_host_key(name, lab['server_ids'][name], self.api.PREFIX, cached)

    def console_output(self, lab, name):
        return self.region(lab['node_regions'][name]).get('compute', '/servers/' + lab['server_ids'][name] + '/action',
               {'os-getConsoleOutput': {'length': 1000}})['output']

    def verify_placement(self):
        from selectel_staged_audit import verify_networks
        return verify_networks(self)

    def cleanup_order(self):
        names = list(self.journal['stages'])
        nodes = [n for n in names if n in self.desired['nodes']]
        ordered = list(reversed(nodes)) + [n for n in reversed(names) if n.startswith('connection-')]
        ordered += [n for n in reversed(names) if n.startswith('network-')]
        return ordered + [n for n in names if n == 'global']

    def audit_deletion(self):
        from selectel_staged_audit import assert_stage_absent, audit_namespace, objects
        from selectel_adapter import audit_cleanup
        for name in self.journal['stages']:
            assert_stage_absent(self, name)
        self.global_router.assert_absent({}, self.owner)
        regions = {stage['candidate']['region'] for stage in self.journal['stages'].values()}
        for region in regions:
            remaining, floating = audit_cleanup(self.region(region), self.api.PREFIX, set())
            own_floating = [f for f in objects(self.region(region), 'network', '/v2.0/floatingips', 'floatingips')
                            if self.owner in f.get('description', '')]
            if remaining or floating or own_floating:
                raise RuntimeError('Named deployment resources remain in ' + region)
            audit_namespace(self, region)
        return {'pools_audited': sorted(regions), 'global_router_remaining': 0}

    def apply(self, name):
        if name in self.desired['nodes']:
            from selectel_staged_audit import faults
            known_fault = json.dumps(faults(self, name)).lower()
            if any(marker in known_fault for marker in ('no valid host', 'novalidhost', 'not enough hosts')):
                raise pools.TerraformError('No valid host in the saved failed attempt')
        kind, inputs = self.inputs(name)
        record = self.journal['stages'][name]
        record.update(kind=kind, inputs=inputs)
        self.save()
        self.configure_stage(name, kind, inputs)
        output = self.store.apply(name)
        record['output'] = output
        self.save()
        from selectel_staged_audit import assert_stage_owned
        assert_stage_owned(self, name, active=True)
        return output

    def finish(self, name):
        record = self.journal['stages'][name]
        record['identities'] = self.store.identities(name)
        record['lineage'] = self.store.state(name).get('lineage')
        record['status'] = 'active'
        self.save()
        if name in self.desired['nodes']:
            self.connect_pool(record['candidate']['region'])

    def reapply(self, name):
        from selectel_staged_audit import assert_stage_owned
        assert_stage_owned(self, name, active=True)
        record = self.journal['stages'][name]
        self.configure_stage(name, record['kind'], record['inputs'])
        record['output'] = self.store.apply(name, unchanged=True)
        self.save()
        if name in self.desired['nodes']:
            self.connect_pool(record['candidate']['region'])

    def capture_fault(self, name):
        from selectel_staged_audit import faults
        report = {'stage': name, 'read_only': True}
        try:
            report.update(faults(self, name))
        except (OSError, ValueError, KeyError, RuntimeError) as error:
            report['diagnostic_error'] = {'type': type(error).__name__, 'http_status': getattr(error, 'code', None)}
        try:
            logs = list(getattr(self.api, 'RUN', self.root / '.local/runs').glob('stage-' + name + '-*.log'))
            if logs:
                last = max(logs, key=lambda p: p.stat().st_mtime_ns)
                checked_tree(last, self.root)
                report['terraform_error'] = self.api.redact(last.read_text())
            self.api.save(self.root / 'docs/placement-faults' / (name + '-' + uuid.uuid4().hex + '.json'), report)
        except (OSError, ValueError, KeyError) as error:
            LOGGER.error('Cannot preserve stage fault: %s', type(error).__name__)

    def is_capacity_error(self, error, name):
        if isinstance(error, pools.CandidateUnavailable) and not self.store.identities(name):
            return True
        if not isinstance(error, pools.TerraformError):
            return False
        text = getattr(error, 'output', '')
        from selectel_staged_audit import faults
        if name in self.desired['nodes']:
            try:
                text += json.dumps(faults(self, name))
            except (OSError, ValueError, KeyError, RuntimeError) as diagnostic_error:
                LOGGER.error('Scheduler diagnostics unavailable (%s); preserve original error and resources',
                             type(diagnostic_error).__name__)
                return False
        normalized = ' '.join(text.lower().split())
        return any(x in normalized for x in
                   ('no valid host', 'novalidhost', 'not enough hosts', 'exhausted all retry attempts'))

    def cleanup_attempt(self, name):
        from selectel_staged_audit import assert_stage_absent, assert_stage_owned
        record = self.journal['stages'][name]
        assert_stage_owned(self, name, active=False)
        record['audit_entries'] = record.get('audit_entries') or self.store.entries(name)
        record['status'] = 'deleting'
        self.save()
        if record.get('kind') and record.get('inputs'):
            self.configure_stage(name, record['kind'], record['inputs'])
        if not (self.store.folder(name) / 'main.tf.json').exists():
            if self.store.identities(name):
                raise ValueError('Stage configuration missing while resources remain')
        else:
            self.store.apply(name, destroy=True)
        assert_stage_absent(self, name)
        record['status'] = 'deleted'
        self.save()
        if name in self.desired['nodes'] and not self.journal.get('destroy_requested'):
            self.prune_unused_network(record['candidate']['region'])

    def prune_unused_network(self, pool):
        network = 'network-' + pool
        if network not in getattr(self, '_temporary_networks', set()):
            return
        for node in self.desired['nodes']:
            record = self.journal['stages'].get(node, {})
            if record.get('candidate', {}).get('region') == pool and self.store.identities(node):
                return
        connection = 'connection-' + pool
        if connection in self.journal['stages'] and self.journal['stages'][connection]['status'] != 'deleted':
            self.cleanup_attempt(connection)
        if self.journal['stages'][network]['status'] != 'deleted':
            self.cleanup_attempt(network)
        self._temporary_networks.discard(network)

    def result(self):
        nodes = {n: self.journal['stages'][n]['output'] for n in self.desired['nodes']}
        app = nodes['app']
        result = InfrastructureResult(provider='selectel', scope={'project_id': self.credentials['project_id']},
            namespace=self.api.PREFIX, placement={'region': app['region'], 'zone': app['zone'],
            'node_placements': {n: {'region': v['region'], 'zone': v['zone']} for n, v in nodes.items()},
            'pool_subnets': self.journal['pool_subnets']}, nodes=nodes, subnet_cidr=app['subnet_cidr'])
        result.validate()
        return result
