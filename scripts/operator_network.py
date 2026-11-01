"""Confirm the operator address without widening administrative access."""
import ipaddress
import hashlib
import hmac
import json
import urllib.parse
import urllib.request


def read_address(url: str) -> str:
    with urllib.request.urlopen(url,timeout=15) as response:
        return response.read(128).decode('ascii').strip()


def discover_cidrs(urls: list[str]) -> list[str]:
    if len(urls) != 2 or len({urllib.parse.urlsplit(u).hostname for u in urls}) != 2:
        raise ValueError('Configure two independent public IPv4 HTTPS services')
    for url in urls:
        parsed=urllib.parse.urlsplit(url)
        if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('Operator IP discovery requires HTTPS endpoints without credentials')
    cidrs=[]
    for url in urls:
        address=ipaddress.ip_address(read_address(url))
        if address.version!=4 or not address.is_global:
            raise ValueError('Operator discovery must return a global IPv4 address')
        cidr=str(address)+'/32'
        if cidr not in cidrs:
            cidrs.append(cidr)
    if len(cidrs) != 1:
        raise ValueError('Operator IPv4 is unconfirmed: HTTPS services disagree; check routing, VPN and proxy')
    return cidrs


def allowed_cidrs(configured, observed, existing=()):
    """Preserve existing access and require explicit confirmation of a changed route."""
    for cidr in [*configured, *existing, *observed]:
        network = ipaddress.ip_network(cidr, strict=True)
        if network.version != 4 or network.prefixlen != 32 or not network.network_address.is_global:
            raise ValueError('Administrative access requires public IPv4 /32 entries')
    result = list(dict.fromkeys(existing or configured or observed))
    if observed[0] not in result:
        raise ValueError('Confirmed operator IPv4 is absent from admin_cidrs; explicitly review access')
    return result


def verify_ssh_source(connection, cidrs):
    fields = connection.split()
    if len(fields) != 4:
        raise ValueError('SSH client source could not be confirmed')
    allowed_cidrs(cidrs, [str(ipaddress.ip_address(fields[0])) + '/32'])


def signed_route_cidr(result, nonce, key):
    """Only a signed, fresh TCP source may extend this stand's allowlist."""
    if not isinstance(result, dict) or not all(isinstance(result.get(k), str) for k in ('nonce', 'ip', 'signature')):
        raise ValueError('Invalid route probe response')
    expected = hmac.new(key, (nonce + '|' + result['ip']).encode(), hashlib.sha256).hexdigest()
    if result['nonce'] != nonce or not hmac.compare_digest(expected, result['signature']):
        raise ValueError('Untrusted route probe response')
    cidr = str(ipaddress.IPv4Address(result['ip'])) + '/32'
    allowed_cidrs([], [cidr])
    return cidr


def validate_access_plan(change, cidr, security_group_id):
    """A route confirmation may add only exact SSH/Kibana operator /32 rules."""
    after = change['change'].get('after') or {}
    port = {'ssh': 22, 'kibana': 5601}.get(change.get('name'))
    if (not security_group_id or after.get('security_group_id') != security_group_id
            or change.get('type') != 'openstack_networking_secgroup_rule_v2'
            or port is None or change.get('index') != cidr
            or any(after.get(k) != v for k, v in {
                'direction': 'ingress', 'ethertype': 'IPv4', 'protocol': 'tcp',
                'port_range_min': port, 'port_range_max': port, 'remote_ip_prefix': cidr}.items())):
        raise ValueError('Route plan may only add the confirmed SSH/22 rule')


def check_saved_access(variables_path, services, configured):
    """Reconfirm the current route without rewriting a running stand's rules."""
    observed = discover_cidrs(services)
    if variables_path.exists():
        existing = json.loads(variables_path.read_text())['admin_cidrs']
        allowed_cidrs(configured, observed, existing)
    else:
        allowed_cidrs(configured, observed)
    return observed
