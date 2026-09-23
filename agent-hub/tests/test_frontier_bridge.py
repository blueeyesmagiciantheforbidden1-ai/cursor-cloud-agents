"""No credentials or external requests; verify authority and ambiguous transport."""
import io
import json
from unittest import TestCase, mock
from urllib.error import HTTPError, URLError

from agent_hub.core import AGENTS, Hub, HubError
from agent_hub import mcp
from agent_hub.frontier_client import FrontierClient, NoRedirect, MAX_RESPONSE_BYTES


class BridgeTests(TestCase):
    def setUp(self):
        self.client = mock.Mock()
        self.client.call.return_value = {'status': 'succeeded', 'model_calls': 0}
        self.hub = Hub(mock.Mock(), frontier=self.client)

    def rpc(self, actor, tool, args):
        return mcp.handle_rpc(self.hub, actor, {'jsonrpc': '2.0', 'id': 1,
            'method': 'tools/call', 'params': {'name': tool, 'arguments': args}})

    def test_only_manager_can_start_or_read_research(self):
        for actor in (*AGENTS, 'status', 'unknown'):
            for tool in ('hub_frontier', 'hub_frontier_get'):
                self.assertTrue(self.rpc(actor, tool, {'request_id': 'check'})['result']['isError'])
        self.client.call.assert_not_called()
        result = self.rpc('manager', 'hub_frontier', {'request_id': 'check'})
        self.assertFalse(result['result']['isError'])
        self.client.call.assert_called_once_with('run', {'request_id': 'check'})

    def test_invalid_parameters_and_arbitrary_destinations_never_reach_service(self):
        for args in ({'request_id': '../secret'}, {'request_id': 'x', 'units': 9},
                     {'request_id': 'x', 'budget': True}, {'request_id': 'x', 'url': 'https://evil.invalid'},
                     {'request_id': 'x', 'seed': 2147483648}):
            self.assertIn('error', self.rpc('manager', 'hub_frontier', args))
        self.client.call.assert_not_called()

    def test_missing_service_fails_honestly(self):
        hub = Hub(mock.Mock())
        with self.assertRaisesRegex(HubError, 'not configured'):
            hub.frontier_call('manager', 'run', {'request_id': 'x'})

    def test_no_notification_can_dispatch_research(self):
        self.assertIs(mcp.handle_rpc(self.hub, 'manager', {'jsonrpc': '2.0', 'method': 'tools/call',
            'params': {'name': 'hub_frontier', 'arguments': {'request_id': 'x'}}}), mcp.NO_CONTENT)
        self.client.call.assert_not_called()

    def test_get_remains_read_only_in_gateway(self):
        from agent_hub.oauth_gateway import READ_ONLY
        self.assertIn('hub_frontier_get', READ_ONLY)
        self.assertNotIn('hub_frontier', READ_ONLY)


class TransportTests(TestCase):
    def client(self, opener):
        return FrontierClient('https://research-test.run.app', opener=opener,
                              identity=lambda audience: 'stub-id')

    def test_no_retry_after_unknown_call_and_no_diagnostic_leak(self):
        opener = mock.Mock()
        opener.open.side_effect = URLError('credential-sensitive detail')
        with self.assertRaisesRegex(HubError, 'same request ID') as caught:
            self.client(opener).call('run', {'request_id': 'stable-id'})
        self.assertNotIn('sensitive', str(caught.exception))
        self.assertEqual(opener.open.call_count, 1)

    def test_quota_denial_is_bounded_and_not_retried(self):
        opener = mock.Mock()
        opener.open.side_effect = HTTPError('https://research-test.run.app', 429, 'secret', {}, io.BytesIO(b'secret'))
        with self.assertRaises(HubError) as caught:
            self.client(opener).call('run', {'request_id': 'x'})
        self.assertEqual(caught.exception.status, 429)
        self.assertNotIn('secret', str(caught.exception))
        self.assertEqual(opener.open.call_count, 1)

    def test_bounded_response_and_credential_destination(self):
        opener = mock.MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = b'{"status":"reserved"}'
        self.assertEqual(self.client(opener).call('get', {'request_id': 'x'}), {'status': 'reserved'})
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, 'https://research-test.run.app/v1/research/get')
        self.assertEqual(request.get_header('Authorization'), 'Bearer stub-id')
        opener.open.return_value.__enter__.return_value.read.return_value = b'x' * (MAX_RESPONSE_BYTES + 1)
        with self.assertRaises(HubError):
            self.client(opener).call('get', {'request_id': 'x'})

    def test_non_origin_destinations_and_redirects_rejected(self):
        for url in ('http://localhost', 'https://evil.invalid', 'https://test.run.app/path',
                    'https://user@test.run.app', 'https://test.run.app?target=secret'):
            with self.assertRaises(ValueError):
                FrontierClient(url)
        self.assertIsNone(NoRedirect().redirect_request(None, None, None, None, None, None))
