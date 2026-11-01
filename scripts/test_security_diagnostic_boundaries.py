"""Address, certificate and diagnostic boundaries use synthetic identities only."""
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

import artifact_transport
import elk_tls
import network_diagnostics
import operator_network
import privacy
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

ROOT=Path(__file__).resolve().parents[1]


class SecurityDiagnosticBoundaries(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory(dir=ROOT/'.tools')
        self.addCleanup(directory.cleanup)
        self.root=Path(directory.name)

    def test_address_reading_and_service_authentication_guard(self):
        with patch.object(operator_network.urllib.request,'urlopen',return_value=io.BytesIO(b' 8.8.8.8\n')):
            self.assertEqual(operator_network.read_address('https://one.invalid'),'8.8.8.8')
        for first in ('http://one.invalid','https://user:password@one.invalid'):
            with patch.object(operator_network,'read_address') as read,self.assertRaisesRegex(ValueError,'HTTPS'):
                operator_network.discover_cidrs([first,'https://two.invalid'])
            read.assert_not_called()
        with patch.object(operator_network,'discover_cidrs',return_value=['8.8.8.8/32']):
            self.assertEqual(operator_network.check_saved_access(self.root/'absent',[],[]),['8.8.8.8/32'])
            saved=self.root/'vars.json';saved.write_text('{"admin_cidrs":["8.8.8.8/32"]}')
            self.assertEqual(operator_network.check_saved_access(saved,[],[]),['8.8.8.8/32'])

    def test_certificate_authority_mismatched_key_is_not_rotated_or_overwritten(self):
        elk_tls.certificates(self.root,{'es1':'10.77.0.31'})
        ca=self.root/'certs/ca.crt'
        before=ca.read_bytes()
        unrelated=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        key=self.root/'certs/ca.key'
        key.write_bytes(unrelated.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
        with self.assertRaisesRegex(RuntimeError,'CA key mismatch'):
            elk_tls.certificates(self.root,{'es1':'10.77.0.31'})
        self.assertEqual(ca.read_bytes(),before)

    def test_privacy_handles_optional_values_invalid_ip_and_secret_file(self):
        with patch.object(privacy,'_values',set()):
            privacy.remember([None,'',42,'synthetic-secret'])
            self.assertEqual(privacy.redact('synthetic-secret'),'[REDACTED]')
            (self.root/'secrets.json').write_text('{"app":"synthetic-app-secret"}')
            privacy.remember_application(self.root)
            self.assertEqual(privacy.redact('synthetic-app-secret'),'[REDACTED]')
            self.assertEqual(privacy.sanitize('999.999.999.999 10.0.0.1 8.8.8.8'),'999.999.999.999 10.0.0.1 <public-ip>')
            self.assertEqual(privacy.sanitize([None,True,7]),[None,True,7])

    def test_staged_console_error_is_summarized_without_raw_exception_or_console(self):
        api=SimpleNamespace(ROOT=self.root,RUN=self.root/'.local/runs',Cloud=Mock(),save=Mock())
        provider=Mock()
        provider.console_output.side_effect=PermissionError('synthetic-secret')
        with patch('stage_store.deployment_exists',return_value=True),patch('staged_runtime.backend',return_value=provider):
            report=network_diagnostics.console_evidence(api,{'server_ids':{'app':'own'}})
        self.assertEqual(report['nodes']['app'],{'guest_evidence_found':False,'error_type':'PermissionError'})
        api.Cloud.assert_not_called()
        self.assertNotIn('synthetic-secret',str(api.save.call_args_list))
        self.assertIsNone(network_diagnostics.summary({'checks':{'addresses':{'stdout':'invalid'}}},'10.0.0.1')['expected_private_ipv4_present'])

    def test_source_delivery_cleans_missing_remote_files_on_failure_without_preparing_other_nodes(self):
        (self.root/'scripts/remote').mkdir(parents=True)
        (self.root/'scripts/remote/fetch_artifacts.py').write_text('synthetic source')
        (self.root/'config').mkdir()
        (self.root/'config/services.json').write_text('{"elastic_version":"8.19.0"}')
        (self.root/'artifact-delivery.json').write_text('{}')
        api=SimpleNamespace(ROOT=self.root,LOCAL=self.root,STACK={'paths':{'remote_root':'/synthetic'}},put=Mock(),remote=Mock(side_effect=[None,OSError('SSH interrupted')]))
        client=MagicMock()
        client.open_sftp.return_value.__enter__.return_value.remove.side_effect=FileNotFoundError()
        with patch.object(artifact_transport.s3_artifacts,'prepare') as prepare,self.assertRaises(OSError):
            artifact_transport.source(api,client,'es2')
        prepare.assert_not_called()
        removed=[call.args[0] for call in client.open_sftp.return_value.__enter__.return_value.remove.call_args_list]
        self.assertEqual(removed,['/synthetic/artifact-fetch.py','/synthetic/artifact-delivery.json'])
