"""Check cloud-provided IPv4 before starting services; never rewrite networking."""
import ipaddress
import json
import subprocess
import sys


def validate(expected: str, gateway: str, addresses: list[dict], routes: list[dict]) -> None:
    wanted = ipaddress.IPv4Interface(expected)
    if ipaddress.IPv4Address(gateway) not in wanted.network:
        raise ValueError('IPv4 gateway is outside the private subnet')
    interfaces = [interface['ifname'] for interface in addresses
                  if 'UP' in interface.get('flags', []) and any(
                      address.get('family') == 'inet'
                      and address.get('local') == str(wanted.ip)
                      and address.get('prefixlen') == wanted.network.prefixlen
                      for address in interface.get('addr_info', []))]
    if len(interfaces) != 1:
        raise ValueError('Expected private IPv4/prefix is missing or ambiguous')
    defaults = [route for route in routes if route.get('dst') == 'default']
    if not defaults or any(route.get('gateway') != gateway or route.get('dev') != interfaces[0]
                           for route in defaults):
        raise ValueError('Default IPv4 route does not match the private interface and gateway')



def main() -> None:
    try:
        addresses = json.loads(subprocess.run(['ip', '-j', '-4', 'address', 'show'],
                                             capture_output=True, text=True, check=True).stdout)
        routes = json.loads(subprocess.run(['ip', '-j', '-4', 'route', 'show', 'default'],
                                          capture_output=True, text=True, check=True).stdout)
        validate(sys.argv[1], sys.argv[2], addresses, routes)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit('IPv4 bootstrap check failed: ' + str(error)) from error
    print('Private IPv4 and default gateway verified')


if __name__ == '__main__':
    main()
