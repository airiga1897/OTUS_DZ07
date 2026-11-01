"""Exercise the real HTTPS client; only its transport is synthetic."""
import http.client
import io
import json
import ssl
import unittest
from unittest.mock import Mock, patch
import urllib.error

import selectel_api as api
import pool_selection
from selectel_global_router import GlobalRouter


def response(value):
    stream = io.BytesIO(json.dumps(value).encode() if value is not None else b'')
    stream.headers = {'X-Test': 'present'}
    return stream


class SelectelHttpTests(unittest.TestCase):
    def test_invalid_transport_settings_fail_before_network(self):
        valid={'read_attempts':3,'timeout_seconds':15,'retry_delay_seconds':2}
        for key,values in [('read_attempts',[0,6,True]),('timeout_seconds',[0,61,'15']),('retry_delay_seconds',[-1,11,None])]:
            for value in values:
                document={'api':{**valid,key:value}}
                with patch.object(api.Path,'read_text',return_value=json.dumps(document)):
                    with self.assertRaises(ValueError):api.transport_policy()
    def invoke(self, outcomes, payload=None):
        opener = Mock()
        opener.open.side_effect = outcomes
        with patch.object(api.urllib.request, 'build_opener', return_value=opener), patch.object(api.time, 'sleep'):
            result = api.request_json('https://example.invalid/resources', token='synthetic-token', payload=payload)
        return result, opener

    def test_successful_get_empty_response_and_post_contract(self):
        for payload, value in [(None, {'ok': True}), ({'action': None}, None)]:
            result, opener = self.invoke([response(value)], payload)
            self.assertEqual(result[0], value or {})
            self.assertEqual(result[1]['X-Test'], 'present')
            request = opener.open.call_args.args[0]
            self.assertEqual(request.get_method(), 'GET' if payload is None else 'POST')
            self.assertEqual(request.get_header('X-auth-token'), 'synthetic-token')
            if payload is not None: self.assertEqual(json.loads(request.data), payload)

    def test_invalid_or_credentialed_endpoints_never_reach_transport(self):
        for url in ('http://example.invalid', 'https:///missing', 'https://user:password@example.invalid', 'file:///tmp/x'):
            with self.subTest(url=url), patch.object(api.urllib.request, 'build_opener') as opener:
                with self.assertRaises(ValueError): api.request_json(url)
                opener.assert_not_called()

    def test_read_retries_cover_connect_read_and_truncated_responses(self):
        errors = [TimeoutError(), ConnectionResetError(), urllib.error.URLError('temporary'),
                  http.client.IncompleteRead(b'partial'),
                  urllib.error.HTTPError('https://example.invalid', 503, 'Unavailable', {}, None),
                  urllib.error.HTTPError('https://example.invalid', 429, 'Limited', {}, None)]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                result, opener = self.invoke([error, response({'ok': True})])
                self.assertTrue(result[0]['ok'])
                self.assertEqual(opener.open.call_count, 2)
        failing = Mock()
        failing.__enter__ = Mock(return_value=failing)
        failing.__exit__ = Mock(return_value=False)
        failing.read.side_effect = TimeoutError('read timeout')
        result, opener = self.invoke([failing, response({'complete': True})])
        self.assertTrue(result[0]['complete'])
        failing.__exit__.assert_called_once()

    def test_exhausted_reads_stop_after_bounded_attempts(self):
        opener = Mock()
        opener.open.side_effect = TimeoutError()
        with patch.object(api.urllib.request, 'build_opener', return_value=opener), patch.object(api.time, 'sleep') as sleep:
            with self.assertRaises(TimeoutError) as caught: api.request_json('https://example.invalid')
        self.assertEqual(opener.open.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(caught.exception.api_error_context['attempts'],3)
        self.assertEqual(caught.exception.api_error_context['host'],'example.invalid')

    def test_post_permission_redirect_and_certificate_failures_are_not_retried(self):
        certificate = ssl.SSLCertVerificationError('synthetic certificate failure')
        errors = [(None, certificate), (None, urllib.error.URLError(certificate))]
        errors += [(None, urllib.error.HTTPError('https://example.invalid', status, 'Rejected', {}, None))
                   for status in (302, 400, 401, 403, 404)]
        errors += [({'action': None}, error) for error in (TimeoutError(), http.client.IncompleteRead(b''),
            urllib.error.HTTPError('https://example.invalid', 503, 'Unavailable', {}, None))]
        for payload, error in errors:
            with self.subTest(payload=payload, error=type(error).__name__):
                opener = Mock();opener.open.side_effect = error
                with patch.object(api.urllib.request, 'build_opener', return_value=opener), patch.object(api.time, 'sleep') as sleep:
                    with self.assertRaises(type(error)):
                        api.request_json('https://example.invalid', payload=payload)
                self.assertEqual(opener.open.call_count, 1)
                sleep.assert_not_called()

    def test_invalid_json_is_not_hidden_by_retry_or_partial_parsing(self):
        opener = Mock();opener.open.return_value = io.BytesIO(b'log\n{"ok":true}')
        with patch.object(api.urllib.request, 'build_opener', return_value=opener):
            with self.assertRaises(json.JSONDecodeError): api.request_json('https://example.invalid')
        self.assertEqual(opener.open.call_count, 1)
        self.assertIsNone(api.NoRedirect().redirect_request(None, None, 302, None, {}, 'https://foreign.invalid'))

    def test_catalog_and_global_router_use_same_retry_policy(self):
        for consumer, payload in [(lambda: pool_selection.image_catalog('https://example.invalid/v2', 'synthetic'), {'images': []}),
                                  (self.global_read, [])]:
            opener = Mock();opener.open.side_effect = [TimeoutError(), response(payload)]
            with patch.object(api.urllib.request, 'build_opener', return_value=opener), patch.object(api.time, 'sleep'):
                self.assertEqual(consumer(), payload.get('images') if isinstance(payload, dict) else payload)
            self.assertEqual(opener.open.call_count, 2)

    def global_read(self):
        client = GlobalRouter.__new__(GlobalRouter);client.token = 'synthetic'
        return client.get('/networks')

    def test_authentication_scope_and_nova_endpoint_are_validated(self):
        config = {'auth_url': 'https://identity.invalid/v3', 'username': 'synthetic', 'password': 'synthetic',
                  'domain_name': 'synthetic', 'project_id': 'own-project', 'region': 'ru-3'}
        catalog = [{'type': 'image', 'endpoints': []}, {'type': 'compute', 'endpoints': [
            {'interface': 'private', 'region': 'ru-3', 'url': 'https://private.invalid'},
            {'interface': 'public', 'region': 'ru-9', 'url': 'https://other.invalid'},
            {'interface': 'public', 'region': 'ru-3', 'url': 'http://insecure.invalid'},
            {'interface': 'public', 'region': 'ru-3', 'url': 'https://compute.invalid/'}]}]
        reply = ({'token': {'project': {'id': 'own-project'}, 'catalog': catalog}}, {'X-Subject-Token': 'synthetic-token'})
        with patch.object(api, 'auth_settings', return_value={'auth_url': config['auth_url']}), \
             patch.object(api, 'request_json', return_value=reply) as request:
            client = api.NovaClient(config)
            self.assertEqual(client.endpoint, 'https://compute.invalid')
            self.assertEqual(request.call_args.kwargs['payload']['auth']['scope'], {'project': {'id': 'own-project'}})
            with self.assertRaises(ValueError): api.authenticate({**config, 'auth_url': 'https://foreign.invalid'})
            request.return_value = ({'token': {'project': {'id': 'foreign'}}}, {})
            with self.assertRaises(ValueError): api.authenticate(config)
        with patch.object(api, 'authenticate', return_value=('synthetic', [])):
            with self.assertRaises(ValueError): api.NovaClient(config)
        with patch.object(api, 'request_json', return_value=({'ok': True}, {})) as request:
            self.assertTrue(client.request('/servers/owned')['ok'])
            for path in ('/volumes/x', '/servers/../foreign', '/servers/x?token=y'):
                with self.assertRaises(ValueError): client.request(path)
            self.assertEqual(request.call_count, 1)
