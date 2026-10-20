"""Check SSH host-key compatibility and the unauthenticated HTTP entry point."""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import yc_lab
import selectel
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class AccessTest(unittest.TestCase):
    def test_standard_ssh_port_keeps_strict_host_key_policy(self):
        node = {'public_ip': '192.0.2.10'}
        with patch.object(yc_lab.paramiko, 'SSHClient') as factory:
            client = yc_lab.connect(node)
            self.assertEqual(client.connect.call_args.kwargs['port'], 22)
            self.assertEqual(client.connect.call_args.args[0], node['public_ip'])
            self.assertIsInstance(client.set_missing_host_key_policy.call_args.args[0],
                                  yc_lab.paramiko.RejectPolicy)

    def test_console_host_key_matches_paramiko_default_port_lookup(self):
        public = Ed25519PrivateKey.generate().public_key().public_bytes(
            serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
        node = {'id': 'test-server', 'public_ip': '192.0.2.10'}
        with patch.object(selectel, 'Cloud') as cloud, patch.object(Path, 'write_text') as write:
            cloud.return_value.get.side_effect = [
                {'server': {'name': 'otus-dz07-es3'}},
                {'output': 'BEGIN SSH HOST KEY KEYS\n' + public + '\nEND SSH HOST KEY KEYS'}]
            selectel.trust({'es3': node})
            entry = yc_lab.paramiko.hostkeys.HostKeyEntry.from_line(write.call_args.args[0].strip())
            self.assertEqual(entry.hostnames, ['192.0.2.10'])

    def test_public_login_check_does_not_send_credentials(self):
        response = Mock(status=200)
        response.read.return_value = b'<title>Kibana</title>'
        with patch.object(yc_lab.urllib.request, 'urlopen') as request:
            request.return_value.__enter__.return_value = response
            result = yc_lab.verify_kibana_http('192.0.2.10')
            self.assertEqual(request.call_args.args, ('http://192.0.2.10:5601/login',))
            self.assertFalse(result['authentication_sent'])

    def test_unrelated_http_page_is_not_kibana_success(self):
        response = Mock(status=200)
        response.read.return_value = b'<title>Default site</title>'
        with patch.object(yc_lab.urllib.request, 'urlopen') as request:
            request.return_value.__enter__.return_value = response
            with self.assertRaisesRegex(RuntimeError, 'Kibana login'):
                yc_lab.verify_kibana_http('192.0.2.10')


if __name__ == '__main__':
    unittest.main()
