"""SSH trust and route checks use real controller logic without contacting a host."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import paramiko
import lab

ROOT=Path(__file__).resolve().parents[1]


class SshConnectionContracts(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory(dir=ROOT/'.tools');self.addCleanup(folder.cleanup)
        self.root=Path(folder.name);self.local=self.root/'.local';self.local.mkdir()
        (self.local/'staged-deployment.json').write_text(json.dumps({'admin_cidrs':['8.8.8.8/32']}))
        setting=patch.multiple(lab,ROOT=self.root,LOCAL=self.local);setting.start();self.addCleanup(setting.stop)
        self.value={'app_public_ip':'192.0.2.10','private_ips':{'app':'10.77.0.10','es1':'10.77.0.31'}}

    def test_public_ssh_uses_strict_known_hosts_port22_and_confirmed_source(self):
        client=Mock();client.exec_command.return_value=(None,io.BytesIO(b'8.8.8.8 4000 10.77.0.10 22'),None)
        with patch.object(lab.paramiko,'SSHClient',return_value=client):
            self.assertIs(lab.connect(self.value),client)
        self.assertEqual(client.connect.call_args.kwargs['port'],22)
        self.assertFalse(client.connect.call_args.kwargs['allow_agent'])
        self.assertIsInstance(client.set_missing_host_key_policy.call_args.args[0],paramiko.RejectPolicy)
        client.close.assert_not_called()

    def test_unapproved_route_and_failed_connect_close_resources(self):
        client=Mock();client.exec_command.return_value=(None,io.BytesIO(b'1.1.1.1 4000 10.77.0.10 22'),None)
        with patch.object(lab.paramiko,'SSHClient',return_value=client):
            with self.assertRaises(ValueError):lab.connect(self.value)
        client.close.assert_called_once()
        client=Mock();client.connect.side_effect=TimeoutError()
        front=Mock();sock=front.get_transport.return_value.open_channel.return_value
        with patch.object(lab.paramiko,'SSHClient',return_value=client):
            with self.assertRaises(TimeoutError):lab.connect(self.value,'es1',front)
        client.close.assert_called_once();sock.close.assert_called_once()

    def test_forwarded_es_connection_uses_private_address(self):
        client=Mock();front=Mock()
        with patch.object(lab.paramiko,'SSHClient',return_value=client):
            self.assertIs(lab.connect(self.value,'es1',front),client)
        front.get_transport.return_value.open_channel.assert_called_once_with('direct-tcpip',('10.77.0.31',22),('127.0.0.1',0))
        client.exec_command.assert_not_called()
