"""Check cloud-provided IPv4 before starting services; never rewrite networking."""

import ipaddress
import json
import subprocess
import sys
from typing import cast


def object_rows(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list) or any(
        not isinstance(row, dict) or any(not isinstance(key, str) for key in row) for row in value
    ):
        raise ValueError("Invalid IPv4 command response shape")
    return cast(list[dict[str, object]], value)


def validate(expected: str, gateway: str, addresses: object, routes: object) -> None:
    wanted = ipaddress.IPv4Interface(expected)
    if ipaddress.IPv4Address(gateway) not in wanted.network:
        raise ValueError("IPv4 gateway is outside the private subnet")
    interfaces: list[str] = []
    for interface in object_rows(addresses):
        flags = interface.get("flags", [])
        if not isinstance(flags, list) or any(not isinstance(flag, str) for flag in flags):
            raise ValueError("Invalid IPv4 interface flags")
        if "UP" not in flags:
            continue
        if any(
            address.get("family") == "inet"
            and address.get("local") == str(wanted.ip)
            and type(address.get("prefixlen")) is int
            and address.get("prefixlen") == wanted.network.prefixlen
            for address in object_rows(interface.get("addr_info", []))
        ):
            name = interface.get("ifname")
            if not isinstance(name, str) or not name:
                raise ValueError("Invalid IPv4 interface identity")
            interfaces.append(name)
    if len(interfaces) != 1:
        raise ValueError("Expected private IPv4/prefix is missing or ambiguous")
    defaults = [route for route in object_rows(routes) if route.get("dst") == "default"]
    if not defaults or any(route.get("gateway") != gateway or route.get("dev") != interfaces[0] for route in defaults):
        raise ValueError("Default IPv4 route does not match the private interface and gateway")


def main() -> None:
    try:
        addresses = json.loads(
            subprocess.run(["ip", "-j", "-4", "address", "show"], capture_output=True, text=True, check=True).stdout
        )
        routes = json.loads(
            subprocess.run(
                ["ip", "-j", "-4", "route", "show", "default"], capture_output=True, text=True, check=True
            ).stdout
        )
        validate(sys.argv[1], sys.argv[2], addresses, routes)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit("IPv4 bootstrap check failed: " + str(error)) from error
    print("Private IPv4 and default gateway verified")


if __name__ == "__main__":
    main()
