"""Small, clone-local contracts shared by lifecycle and cloud adapters."""
from dataclasses import dataclass
from enum import Enum
import ipaddress
import re


class TerraformError(RuntimeError):
    def __init__(self, output):
        super().__init__('Terraform failed; inspect the private local log')
        self.output = output


class CapabilityStatus(str, Enum):
    SUPPORTED = 'supported'
    UNSUPPORTED = 'unsupported'
    UNKNOWN = 'unknown'


@dataclass(frozen=True)
class Capability:
    status: CapabilityStatus
    reason: str


def require_capabilities(found: dict[str, Capability], required: list[str]) -> None:
    for name in required:
        value = found.get(name, Capability(CapabilityStatus.UNKNOWN, 'No discovery result'))
        if value.status != CapabilityStatus.SUPPORTED:
            raise ValueError(f'Required capability {name}: {value.status.value}; {value.reason}')


def validate_binding(saved: dict, expected: dict) -> None:
    for key in ('provider', 'scope', 'namespace', 'placement'):
        if not saved.get(key) or saved[key] != expected.get(key):
            raise ValueError('Stand ownership mismatch: ' + key)


@dataclass(frozen=True)
class InfrastructureResult:
    provider: str
    scope: dict
    namespace: str
    placement: dict
    nodes: dict
    subnet_cidr: str

    @classmethod
    def from_dict(cls, value: dict) -> 'InfrastructureResult':
        result = cls(**{key: value[key] for key in cls.__dataclass_fields__})
        result.validate()
        return result

    def validate(self) -> None:
        if not re.fullmatch(r'[a-z][a-z0-9-]+', self.provider) or not self.scope or not all(self.scope.values()):
            raise ValueError('Provider and resource scope are required')
        if not re.fullmatch(r'[a-z][a-z0-9-]+', self.namespace) or not all(self.placement.get(k) for k in ('region', 'zone')):
            raise ValueError('Namespace and placement are required')
        subnet = ipaddress.ip_network(self.subnet_cidr)
        ids, addresses = set(), set()
        apps = []
        if not self.nodes:
            raise ValueError('Infrastructure has no nodes')
        for name, node in self.nodes.items():
            if not re.fullmatch(r'[a-z][a-z0-9-]*', name) or not node.get('roles') or set(node['roles']) - {'app', 'db', 'etcd', 'es', 'kibana'}:
                raise ValueError('Invalid node name or roles')
            address = ipaddress.ip_address(node['private_ip'])
            node_subnet = ipaddress.ip_network(node.get('subnet_cidr', self.subnet_cidr))
            if node.get('subnet_cidr') and (self.placement.get('pool_subnets', {}).get(node.get('region')) != node['subnet_cidr'] or self.placement.get('node_placements', {}).get(name) != {'region': node.get('region'), 'zone': node.get('zone')}):
                raise ValueError('Node placement/subnet is outside the saved network contract')
            if address not in node_subnet or str(address) in addresses or not node.get('id') or node['id'] in ids:
                raise ValueError('Duplicate or invalid node identity/address')
            if not node.get('volume_id') or not node.get('port_id'):
                raise ValueError('Node storage and network identities are required')
            ids.add(node['id'])
            addresses.add(str(address))
            if 'etcd' in node['roles'] and 'db' not in node['roles']:
                raise ValueError('This stack requires etcd on database hosts')
            if 'app' in node['roles']:
                ipaddress.IPv4Address(node['public_ip'])
                apps.append(name)
        if apps != ['app']:
            raise ValueError('This stack requires one application/controller node named app')

    def binding(self) -> dict:
        return {key: getattr(self, key) for key in ('provider', 'scope', 'namespace', 'placement')}

    def application(self) -> dict:
        """Technology settings use addresses and roles, never provider resource types."""
        return dict(private_ips={n: v['private_ip'] for n, v in self.nodes.items()},
                    node_roles={n: next(r for r in v['roles'] if r != 'etcd') for n, v in self.nodes.items()},
                    app_public_ip=self.nodes['app']['public_ip'], subnet_cidr=self.subnet_cidr,
                    server_ids={n: v['id'] for n, v in self.nodes.items()},
                    volume_ids={n: v['volume_id'] for n, v in self.nodes.items()},
                    port_ids={n: v['port_id'] for n, v in self.nodes.items()},
                    etcd_members=[n for n, v in self.nodes.items() if 'etcd' in v['roles']],
                    node_regions={n: v.get('region', self.placement['region']) for n, v in self.nodes.items()},
                    node_zones={n: v.get('zone', self.placement['zone']) for n, v in self.nodes.items()})
