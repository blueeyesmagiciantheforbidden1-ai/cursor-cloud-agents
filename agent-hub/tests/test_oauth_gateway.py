from http.client import HTTPConnection
import hashlib
import base64
import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.parse import urlencode, parse_qs, urlsplit
from urllib.request import Request, urlopen
from unittest.mock import Mock, patch

from agent_hub.oauth_gateway import make_server
from agent_hub.oauth_runtime import https_origin, load_oauth_service
from agent_hub.oauth_service import OAuthClient, OAuthConfig, OAuthError, OAuthService, PinnedIdentity
from agent_hub.oauth_store import MemoryOAuthStore

ISSUER = 'https://gateway.example.test'
RESOURCE = ISSUER+'/mcp'
AUTH_ORIGIN = 'https://identity.example.test'
CALLBACK = 'https://chatgpt.com/connector/oauth/private-test'
IDENTITY = {'iss': 'https://cloud.google.com/iap', 'sub': 'owner-a'}
VERIFIER = 'a' * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b'=').decode()


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.service = OAuthService(OAuthConfig(
            ISSUER, RESOURCE, (OAuthClient('private-client', (CALLBACK,)),
                              OAuthClient('second-client', (CALLBACK + '-second',))),
            (PinnedIdentity(IDENTITY['iss'], 'owner-a'), PinnedIdentity(IDENTITY['iss'], 'owner-b'))),
            MemoryOAuthStore())
        self.upstream = Mock()
        self.server = make_server('127.0.0.1', 0, service=self.service, upstream=self.upstream,
                                  authorization_origin=AUTH_ORIGIN)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:'+str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, path, body=None, headers=None):
        try:
            response = urlopen(Request(self.url+path, data=body, headers=headers or {}), timeout=5)
        except HTTPError as error:
            response = error
        with response:
            payload = response.read()
            return response.code, json.loads(payload) if payload else None, response.headers

    def code(self):
        pending = self.service.begin_authorization({
            'response_type': 'code', 'client_id': 'private-client', 'redirect_uri': CALLBACK,
            'scope': 'hub:manage', 'state': 'state', 'resource': RESOURCE,
            'code_challenge': CHALLENGE, 'code_challenge_method': 'S256'}, IDENTITY)
        callback = self.service.approve(pending['id'], IDENTITY, pending['csrf'])
        return parse_qs(urlsplit(callback).query)['code'][0]

    def token(self):
        form = {'grant_type': 'authorization_code', 'client_id': 'private-client',
                'redirect_uri': CALLBACK, 'code': self.code(),
                'code_verifier': VERIFIER, 'resource': RESOURCE}
        status, result, _ = self.request('/oauth/token', urlencode(form).encode(),
                                         {'Content-Type': 'application/x-www-form-urlencoded'})
        self.assertEqual(status, 200, result)
        return result['access_token']

    def test_discovery_public_but_tools_require_oauth(self):
        status, metadata, _ = self.request('/.well-known/oauth-protected-resource/mcp')
        self.assertEqual(status, 200)
        self.assertEqual(metadata['resource'], RESOURCE)
        status, metadata, _ = self.request('/.well-known/oauth-authorization-server')
        self.assertEqual(metadata['authorization_endpoint'], AUTH_ORIGIN+'/oauth/authorize')
        status, _, headers = self.request('/mcp', b'{"jsonrpc":"2.0","id":1,"method":"ping"}',
                                          {'Content-Type': 'application/json'})
        self.assertEqual(status, 401)
        self.assertIn('/.well-known/oauth-protected-resource', headers['WWW-Authenticate'])
        self.upstream.rpc.assert_not_called()

    def test_verified_request_forwards_only_message_and_adds_scope_metadata(self):
        token = self.token()
        message = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}
        self.upstream.rpc.return_value = {'jsonrpc': '2.0', 'id': 1, 'result': {
            'tools': [{'name': 'hub_status', 'inputSchema': {}}]}}
        status, result, _ = self.request('/mcp', json.dumps(message).encode(), {
            'Content-Type': 'application/json', 'Authorization': 'Bearer '+token,
            'X-Hub-Token': 'attacker-manager-token', 'X-Hub-Agent': 'grok',
            'X-Goog-IAP-JWT-Assertion': 'untrusted', 'X-Serverless-Authorization': 'untrusted'})
        self.assertEqual(status, 200, result)
        self.upstream.rpc.assert_called_once_with(message)
        tool = result['result']['tools'][0]
        self.assertEqual(tool['securitySchemes'], [{'type': 'oauth2', 'scopes': ['hub:manage']}])
        self.assertTrue(tool['annotations']['readOnlyHint'])
        self.assertNotIn('attacker', json.dumps(result))

    def test_worker_tool_and_bad_origin_never_reach_upstream(self):
        token = self.token()
        headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer '+token}
        message = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'hub_complete'}}
        status, _, _ = self.request('/mcp', json.dumps(message).encode(), headers)
        self.assertEqual(status, 403)
        status, _, _ = self.request('/mcp', b'{}', {**headers, 'Origin': 'https://evil.example'})
        self.assertEqual(status, 403)
        self.upstream.rpc.assert_not_called()

    def test_revoked_token_denied(self):
        token = self.token()
        self.service.revoke(token)
        status, _, _ = self.request('/mcp', b'{}', {'Content-Type': 'application/json',
                                                   'Authorization': 'Bearer '+token})
        self.assertEqual(status, 401)
        self.upstream.rpc.assert_not_called()


    def test_revocation_is_bound_to_registered_client(self):
        token = self.token()
        headers = {'Content-Type': 'application/x-www-form-urlencoded'}
        for client_id in ('second-client', 'private-client'):
            status, result, _ = self.request('/oauth/revoke', urlencode({
                'token': token, 'client_id': client_id}).encode(), headers)
            self.assertEqual((status, result), (200, {}))
            if client_id == 'second-client':
                self.service.authenticate(token)
        with self.assertRaises(OAuthError):
            self.service.authenticate(token)
        self.upstream.rpc.assert_not_called()

    def test_unsupported_empty_and_duplicate_protocol_headers_are_rejected(self):
        token = self.token()
        body = b'{"jsonrpc":"2.0","id":1,"method":"ping"}'
        for versions in (('unsupported',), ('2024-11-05',), ('',),
                         ('2025-06-18, 2025-03-26',),
                         ('2025-06-18', '2025-06-18'),
                         ('2025-06-18', '2025-03-26')):
            with self.subTest(versions=versions):
                connection = HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
                try:
                    connection.putrequest('POST', '/mcp')
                    connection.putheader('Content-Type', 'application/json')
                    connection.putheader('Content-Length', str(len(body)))
                    connection.putheader('Authorization', 'Bearer ' + token)
                    for version in versions:
                        connection.putheader('MCP-Protocol-Version', version)
                    connection.endheaders(body)
                    response = connection.getresponse()
                    self.assertEqual(response.status, 400, response.read())
                    response.read()
                finally:
                    connection.close()
        self.upstream.rpc.assert_not_called()

    def test_supported_protocol_headers_and_missing_header_are_accepted(self):
        token = self.token()
        message = {'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}
        self.upstream.rpc.return_value = {'jsonrpc': '2.0', 'id': 1, 'result': {}}
        for version in (None, '2025-03-26', '2025-06-18'):
            with self.subTest(version=version):
                headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token}
                if version is not None:
                    headers['MCP-Protocol-Version'] = version
                status, result, _ = self.request('/mcp', json.dumps(message).encode(), headers)
                self.assertEqual(status, 200, result)
        self.assertEqual(self.upstream.rpc.call_count, 3)
        self.upstream.rpc.assert_called_with(message)

    def test_token_body_limits_duplicate_fields_and_unexpected_auth(self):
        for body, headers, expected in (
            (b'client_id=a&client_id=b', {'Content-Type': 'application/x-www-form-urlencoded'}, 400),
            (b'x=' + b'a'*16_384, {'Content-Type': 'application/x-www-form-urlencoded'}, 400),
            (b'{}', {'Content-Type': 'application/json'}, 415),
            (b'client_id=private-client', {'Content-Type': 'application/x-www-form-urlencoded',
                                         'Authorization': 'Basic untrusted'}, 401),
        ):
            status, result, _ = self.request('/oauth/token', body, headers)
            self.assertEqual(status, expected, result)
        self.upstream.rpc.assert_not_called()

    def test_storage_failure_never_reveals_diagnostics(self):
        with patch.object(self.service, 'exchange', side_effect=RuntimeError('sensitive backend detail')):
            status, result, _ = self.request('/oauth/token', b'client_id=private-client',
                                             {'Content-Type': 'application/x-www-form-urlencoded'})
        self.assertEqual(status, 503)
        self.assertNotIn('sensitive', json.dumps(result))


class BootstrapTests(unittest.TestCase):

    def test_origin_rejects_controls_and_whitespace_before_parsing(self):
        for bad in (None, '', ' ' + ISSUER, ISSUER + ' ', ISSUER + '\t',
                    ISSUER + '\r\n', 'https://gate\nway.example.test',
                    ISSUER + '\x00', ISSUER + '\x1f', ISSUER + '\x7f',
                    ISSUER + '\x80', ISSUER + '\u00a0'):
            with self.subTest(origin=repr(bad)):
                with self.assertRaises(ValueError):
                    https_origin(bad)
        self.assertEqual(https_origin(ISSUER), ISSUER)
        self.assertEqual(https_origin(ISSUER + '/'), ISSUER)
        self.assertEqual(https_origin(ISSUER + ':443/'), ISSUER + ':443')

    def test_missing_configuration_does_not_import_optional_dependencies(self):
        with patch.dict('os.environ', {}, clear=True):
            self.assertIsNone(load_oauth_service())

    def test_unconfigured_gateway_has_no_tool_access(self):
        server = make_server('127.0.0.1', 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with self.assertRaises(HTTPError) as caught:
                urlopen(Request('http://127.0.0.1:'+str(server.server_port)+'/mcp', b'{}'), timeout=3)
            self.assertEqual(caught.exception.code, 503)
            caught.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)
