"""Operator routes may expose different public IPv4 addresses per destination."""
import unittest
from unittest.mock import patch
from operator_network import discover_cidrs, allowed_cidrs, verify_ssh_source, signed_route_cidr, validate_access_plan, check_saved_access
import hashlib
import hmac
import json
from pathlib import Path
import tempfile


class OperatorNetworkTests(unittest.TestCase):
    def test_cli_blocks_verify_before_cloud_actions_when_address_is_unconfirmed(self):
        import lab
        with patch.object(lab.sys, 'argv', ['lab.py', 'verify']), \
             patch('test_cycle.require_access'), patch.object(lab, 'placement', return_value={
                 'operator_ip_services': ['https://one.invalid', 'https://two.invalid']}), \
             patch('operator_network.discover_cidrs', side_effect=ValueError('unconfirmed')), \
             patch.object(lab, 'verify') as verify, patch.object(lab, 'Cloud') as cloud:
            with self.assertRaisesRegex(ValueError, 'unconfirmed'): lab.main()
            verify.assert_not_called()
            cloud.assert_not_called()

    def test_signed_route_rejects_tampering_replay_private_addresses_and_invalid_schema(self):
        key, nonce = b'synthetic-key', 'fresh-nonce'
        def response(address, token=nonce):
            return {'nonce': token, 'ip': address,
                    'signature': hmac.new(key, (token + '|' + address).encode(), hashlib.sha256).hexdigest()}
        self.assertEqual(signed_route_cidr(response('1.1.1.1'), nonce, key), '1.1.1.1/32')
        for value in [None, {}, response('10.0.0.1'), response('1.1.1.1', 'old'),
                      {**response('1.1.1.1'), 'ip': '8.8.8.8'}]:
            with self.assertRaises(ValueError):
                signed_route_cidr(value, nonce, key)

    def test_access_plan_cannot_create_vm_or_widen_ssh_rule(self):
        cidr = '1.1.1.1/32'
        change = {'type': 'openstack_networking_secgroup_rule_v2', 'name': 'ssh', 'index': cidr,
                  'change': {'after': {'direction': 'ingress', 'ethertype': 'IPv4', 'protocol': 'tcp',
                                      'port_range_min': 22, 'port_range_max': 22, 'remote_ip_prefix': cidr,
                                      'security_group_id': 'own-group'}}}
        validate_access_plan(change, cidr, 'own-group')
        with self.assertRaises(ValueError): validate_access_plan(change, cidr, 'foreign-group')
        for bad in [{**change, 'type': 'openstack_compute_instance_v2'},
                    {**change, 'change': {'after': {**change['change']['after'], 'remote_ip_prefix': '0.0.0.0/0'}}}]:
            with self.assertRaises(ValueError): validate_access_plan(bad, cidr, 'own-group')

    def test_saved_rules_remain_unchanged_when_current_route_no_longer_matches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'variables.json'
            original = json.dumps({'admin_cidrs': ['8.8.8.8/32']})
            path.write_text(original)
            with patch('operator_network.discover_cidrs', return_value=['1.1.1.1/32']):
                with self.assertRaises(ValueError):
                    check_saved_access(path, [], [])
            self.assertEqual(path.read_text(), original)

    def test_two_services_must_agree(self):
        urls=['https://one.invalid','https://two.invalid']
        with patch('operator_network.read_address',return_value='1.1.1.1'):
            self.assertEqual(discover_cidrs(urls), ['1.1.1.1/32'])
        with patch('operator_network.read_address',side_effect=['1.1.1.1','8.8.8.8']):
            with self.assertRaises(ValueError): discover_cidrs(urls)

    def test_failed_or_non_public_source_blocks_preparation(self):
        for response in ['127.0.0.1','192.168.1.2','::1','not-an-ip']:
            with patch('operator_network.read_address',return_value=response):
                with self.assertRaises(ValueError): discover_cidrs(['https://one.invalid','https://two.invalid'])
        with patch('operator_network.read_address',side_effect=TimeoutError()):
            with self.assertRaises(TimeoutError): discover_cidrs(['https://one.invalid','https://two.invalid'])

    def test_sources_must_be_explicit_https_endpoints(self):
        for urls in [[],['http://one.invalid'],['https://user:password@one.invalid']]:
            with self.assertRaises(ValueError): discover_cidrs(urls)

    def test_preserves_rules_and_rejects_implicit_route_changes(self):
        observed=['1.1.1.1/32']
        self.assertEqual(allowed_cidrs([], observed), observed)
        existing=['8.8.8.8/32','1.1.1.1/32']
        self.assertEqual(allowed_cidrs([], observed, existing), existing)
        for rules in [['8.8.8.8/32'],['0.0.0.0/0'],['192.168.0.1/32'],['::1/128']]:
            with self.assertRaises(ValueError): allowed_cidrs(rules,observed)

    def test_checks_actual_ssh_source_not_private_destination(self):
        verify_ssh_source('1.1.1.1 50000 10.77.0.10 22',['1.1.1.1/32'])
        for connection in ['', '8.8.8.8 50000 10.77.0.10 22', '10.77.0.10 50000 10.77.0.10 22']:
            with self.assertRaises(ValueError): verify_ssh_source(connection,['1.1.1.1/32'])
