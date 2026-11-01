"""Guest IPv4 readiness using synthetic iproute2 responses; no cloud calls."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('check_ipv4', Path(__file__).parent / 'remote/check_ipv4.py')
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


class IPv4BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.addresses = [dict(ifname='ens3', flags=['UP'], addr_info=[
            dict(family='inet', local='10.77.0.10', prefixlen=24)])]
        self.routes = [dict(dst='default', gateway='10.77.0.1', dev='ens3')]

    def validate(self, addresses=None, routes=None, gateway='10.77.0.1'):
        check.validate('10.77.0.10/24', gateway,
                       self.addresses if addresses is None else addresses,
                       self.routes if routes is None else routes)

    def test_private_address_and_gateway_accept_actual_interface_name(self):
        self.validate()
        self.addresses[0]['ifname'] = self.routes[0]['dev'] = 'eth0'
        self.validate()

    def test_link_local_only_does_not_count_as_ipv4(self):
        self.addresses[0]['addr_info'] = [dict(family='inet6', local='fe80::1', prefixlen=64)]
        with self.assertRaisesRegex(ValueError, 'private IPv4'):
            self.validate()

    def test_missing_wrong_prefix_down_or_duplicate_address_rejected(self):
        for variant in ('missing', 'wrong_address', 'prefix', 'down', 'duplicate'):
            addresses = copy.deepcopy(self.addresses)
            if variant == 'missing': addresses = []
            if variant == 'wrong_address': addresses[0]['addr_info'][0]['local'] = '10.77.0.11'
            if variant == 'prefix': addresses[0]['addr_info'][0]['prefixlen'] = 16
            if variant == 'down': addresses[0]['flags'] = []
            if variant == 'duplicate': addresses += copy.deepcopy(addresses)
            with self.subTest(variant=variant), self.assertRaises(ValueError):
                self.validate(addresses=addresses)

    def test_missing_wrong_or_competing_default_route_rejected(self):
        for routes in [[], [dict(dst='default', gateway='10.77.0.2', dev='ens3')],
                       [dict(dst='default', gateway='10.77.0.1', dev='ens4')],
                       self.routes + [dict(dst='default', gateway='10.77.0.2', dev='ens4')]]:
            with self.subTest(routes=routes), self.assertRaises(ValueError):
                self.validate(routes=routes)
        with self.assertRaisesRegex(ValueError, 'outside'):
            self.validate(gateway='10.67.0.1')

    def test_command_fails_on_missing_ipv4_and_iproute_errors(self):
        with patch.object(check.sys, 'argv', ['check_ipv4.py', '10.77.0.10/24', '10.77.0.1']):
            with patch.object(check.subprocess, 'run', side_effect=[
                    subprocess.CompletedProcess([], 0, '[]'),
                    subprocess.CompletedProcess([], 0, json.dumps(self.routes))]):
                with self.assertRaisesRegex(SystemExit, 'private IPv4'):
                    check.main()
            for error in (FileNotFoundError('ip unavailable'), subprocess.CalledProcessError(1, ['ip']),
                          json.JSONDecodeError('invalid output', '', 0)):
                with patch.object(check.subprocess, 'run', side_effect=error):
                    with self.assertRaises(SystemExit): check.main()
