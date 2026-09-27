"""The 409 reason is bounded advisory metadata, including on old hubs.

Run this module with either hub tree on PYTHONPATH to cover both client copies.
"""
import io
import json
from pathlib import Path
import unittest
from unittest.mock import Mock
from urllib.error import HTTPError

from agent_hub.worker import Config, HubClient, LeaseLost


class LeaseReasonClientTests(unittest.TestCase):
    def assert_refusal(self, body, expected_reason=None, *, read_error=None, close_error=None,
                       operation='heartbeat'):
        stream = io.BytesIO(body)
        response = HTTPError('http://127.0.0.1:8080', 409, 'Conflict', {}, stream)
        self.addCleanup(response.close)
        response.read = Mock(wraps=response.read, side_effect=read_error)
        response.close = Mock(wraps=response.close, side_effect=close_error)
        self.addCleanup(stream.close)
        config = Config('http://127.0.0.1:8080', 'codex', 'test-token',
                        'TEST_TOKEN', {'project': Path.cwd()})
        client = HubClient(config)
        client.opener = Mock()
        client.opener.open.side_effect = response
        with self.assertRaises(LeaseLost) as raised:
            client.post('/v1/rooms/test-room/' + operation, {'lease_token': 'test-lease'})
        self.assertEqual(raised.exception.reason, expected_reason)
        self.assertEqual(str(raised.exception), 'The hub revoked or expired this task lease')
        self.assertIs(raised.exception.__cause__, response)
        response.read.assert_called_once_with(4097)
        response.close.assert_called_once_with()
        client.opener.open.assert_called_once()
        if close_error is None:
            self.assertTrue(stream.closed)

    def test_one_argument_exception_remains_compatible(self):
        error = LeaseLost('legacy refusal')
        self.assertIsNone(error.reason)
        self.assertEqual(error.args, ('legacy refusal',))
        self.assertEqual(str(error), 'legacy refusal')

    def test_known_reason_strings_are_preserved(self):
        for reason in ('lease_expired_completable', 'lease_inactive'):
            for operation in ('heartbeat', 'complete'):
                with self.subTest(reason=reason, operation=operation):
                    self.assert_refusal(json.dumps({'error': 'Lease is not active',
                                                    'reason': reason}).encode(), reason,
                                        operation=operation)

    def test_absent_reason_keeps_old_hub_behavior(self):
        self.assert_refusal(b'{"error":"Lease is not active"}')

    def test_unknown_or_non_string_reason_is_ignored(self):
        for reason in ('future_reason', '', None, True, 409,
                       ['lease_expired_completable'], {'value': 'lease_inactive'}):
            with self.subTest(reason=reason):
                self.assert_refusal(json.dumps({'reason': reason}).encode())

    def test_non_object_json_is_ignored(self):
        for body in (b'null', b'[]', b'"lease_expired_completable"', b'409', b'true'):
            with self.subTest(body=body):
                self.assert_refusal(body)

    def test_malformed_json_or_encoding_is_ignored(self):
        for body in (b'', b'{', b'not-json', b'{"reason":"lease_inactive"} trailing',
                     b'{"reason":"lease_inactive", "error":"\xff"}'):
            with self.subTest(body=body):
                self.assert_refusal(body)

    def test_exact_limit_body_is_accepted(self):
        body = b'{"reason":"lease_expired_completable"}'
        self.assert_refusal(body + b' ' * (4096 - len(body)), 'lease_expired_completable')

    def test_oversize_body_is_ignored_even_with_known_reason(self):
        body = b'{"reason":"lease_expired_completable"}'
        for size in (4097, 10000):
            with self.subTest(size=size):
                self.assert_refusal(body + b' ' * (size - len(body)))

    def test_unreadable_body_still_raises_lease_lost_and_closes(self):
        for error in (OSError('read failed'), ValueError('closed'), RuntimeError('read failed')):
            with self.subTest(error=type(error).__name__):
                self.assert_refusal(b'{"reason":"lease_expired_completable"}', read_error=error)

    def test_close_failure_cannot_mask_lease_lost(self):
        self.assert_refusal(b'{"reason":"lease_inactive"}', 'lease_inactive',
                            close_error=OSError('close failed'))


if __name__ == '__main__':
    unittest.main()
