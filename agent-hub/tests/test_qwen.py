"""Hosted adapter tests use synthetic responses; all network connects are denied."""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from email.message import Message
import io
import json
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from agent_hub import qwen


KEY = 'sk-SYNTHETIC-NOT-A-REAL-KEY'
MESSAGES = [{'role': 'user', 'content': 'Synthetic prompt'}]


def response(*, prompt=10, completion=20, reasoning=None, text='Synthetic answer', finish='stop', model=qwen.MODEL):
    return {'model': model, 'choices': [{'message': {'role': 'assistant', 'content': text}, 'finish_reason': finish}],
            'usage': {'prompt_tokens': prompt, 'completion_tokens': completion, 'total_tokens': prompt + completion,
                      'completion_tokens_details': {'reasoning_tokens': reasoning}}}


def encoded(value):
    return json.dumps(value, ensure_ascii=False).encode('utf-8')


class SyntheticLedger:
    """Test fixture only; production accounting must be atomic and durable."""
    def __init__(self):
        self.reservations, self.settlements, self.events = {}, {}, []

    def reserve(self, invocation_id, reservation):
        self.events.append('reserve')
        if invocation_id in self.reservations:
            return False
        self.reservations[invocation_id] = deepcopy(reservation)
        return True

    def reconcile(self, invocation_id, settlement):
        self.events.append('reconcile')
        self.settlements[invocation_id] = deepcopy(settlement)
        return True


class QwenTests(unittest.TestCase):
    def setUp(self):
        self.network = patch('socket.socket.connect', side_effect=AssertionError('Tests must remain offline'))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.ledger = SyntheticLedger()
        self.adapter = qwen.QwenAdapter(KEY, reserve=self.ledger.reserve, reconcile=self.ledger.reconcile)

    def call(self, value=None, *, invocation_id='synthetic-call', **kwargs):
        with patch.object(qwen, '_http_post', return_value=encoded(value or response())) as post:
            result = self.adapter.complete(MESSAGES, invocation_id=invocation_id, mode='hybrid', **kwargs)
        return result, post

    def test_success_reserves_before_single_request_and_reconciles_real_usage(self):
        def post(payload, key, timeout):
            self.assertEqual(self.ledger.events, ['reserve'])
            self.assertEqual(key, KEY)
            self.assertEqual(timeout, 30)
            wire = json.loads(payload)
            self.assertEqual(wire['model'], qwen.MODEL)
            self.assertFalse(wire['enable_thinking'])
            self.assertFalse(wire['enable_search'])
            self.assertFalse(wire['stream'])
            self.assertEqual(wire['max_completion_tokens'], 512)
            self.assertNotIn('max_tokens', wire)
            self.assertNotIn('extra_body', wire)
            self.assertNotIn('tools', wire)
            self.ledger.events.append('dispatch')
            return encoded(response())
        with patch.object(qwen, '_http_post', side_effect=post) as posted:
            result = self.adapter.complete(MESSAGES, invocation_id='one', mode='hybrid')
        posted.assert_called_once()
        self.assertEqual(self.ledger.events, ['reserve', 'dispatch', 'reconcile'])
        reserve = self.ledger.reservations['one']
        self.assertEqual((reserve['input_token_reserve'], reserve['output_token_reserve']), (32768, 522))
        self.assertEqual(reserve['reserved_microusd'], 1051)
        self.assertEqual(result['cost_microusd'], 3)  # ceil(10*.030 + 20*.130)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['text'], 'Synthetic answer')
        self.assertNotIn(KEY, json.dumps(result))
        self.assertNotIn('Synthetic prompt', json.dumps(reserve))

    def test_output_margin_charged_and_truncation_not_claimed_complete(self):
        result, post = self.call(response(completion=522, finish='length'))
        self.assertEqual(result['status'], 'truncated')
        self.assertEqual(result['usage']['completion_tokens'], 522)
        self.assertEqual(result['cost_microusd'], 69)
        post.assert_called_once()

    def test_local_only_and_implicit_mode_cannot_reserve_or_dispatch(self):
        for mode in ('local_only', 'automatic', None):
            with self.subTest(mode=mode), patch.object(qwen, '_http_post') as post:
                with self.assertRaises(qwen.QwenError):
                    self.adapter.complete(MESSAGES, invocation_id='one', mode=mode)
                post.assert_not_called()
        self.assertEqual(self.ledger.reservations, {})

    def test_coding_plan_and_invalid_keys_rejected_without_leak(self):
        for key in ('', 'sk-sp-SYNTHETIC', 'sk-key\nInjected: header', 'not-a-model-studio-key', 'sk-☃xxxxxxxx'):
            with self.subTest(length=len(key)), self.assertRaises(qwen.QwenError) as failure:
                qwen.QwenAdapter(key, reserve=self.ledger.reserve, reconcile=self.ledger.reconcile)
            self.assertEqual(str(failure.exception), 'pay_as_you_go_key_required')
        self.assertNotIn(KEY, repr(self.adapter))

    def test_profile_prices_region_model_revision_and_limits_are_pinned(self):
        changes = ({'model': 'qwen3.7-flash'}, {'region': 'beijing'}, {'price_revision': 'latest'},
                   {'input_usd_per_million': Decimal(0)}, {'output_usd_per_million': Decimal('-1')},
                   {'input_usd_per_million': Decimal('NaN')}, {'input_usd_per_million': 0.03},
                   {'output_usd_per_million': Decimal('0.001')}, {'max_completion_tokens': 513},
                   {'max_completion_tokens': True}, {'timeout_seconds': 61},
                   {'daily_limit_microusd': 100001}, {'per_call_limit_microusd': 10001})
        for change in changes:
            with self.subTest(change=change), self.assertRaises(qwen.QwenError):
                replace(qwen.QwenProfile(), **change)
        with self.assertRaises(TypeError):
            qwen.QwenProfile(api_base='https://unapproved.example')

    def test_messages_are_text_only_and_bounded_in_utf8_bytes(self):
        cases = ([], MESSAGES * 17, [{'role': 'tool', 'content': 'x'}], [{'role': 'user', 'content': []}],
                 [{'role': 'user', 'content': 'x', 'tool_calls': []}], [{'role': 'system', 'content': 'x'}],
                 [{'role': 'user', 'content': '\ud800'}], [{'role': 'user', 'content': '☃' * 2731}])
        for messages in cases:
            with self.subTest(case=len(messages)), patch.object(qwen, '_http_post') as post:
                with self.assertRaises(qwen.QwenError):
                    self.adapter.complete(messages, invocation_id='one', mode='hybrid')
                post.assert_not_called()
        self.assertEqual(self.ledger.reservations, {})
        with patch.object(qwen, '_http_post', return_value=encoded(response())):
            self.adapter.complete([{'role': 'user', 'content': '☃' * 2730}], invocation_id='unicode', mode='hybrid')
        reserve = self.ledger.reservations['unicode']
        self.assertEqual(reserve['input_bytes'], 8190)
        self.assertEqual(reserve['input_token_reserve'], 32768)

    def test_reservation_failure_truthy_nonboolean_or_duplicate_never_dispatches(self):
        for callback in (Mock(return_value=False), Mock(return_value=1), Mock(side_effect=RuntimeError(KEY))):
            adapter = qwen.QwenAdapter(KEY, reserve=callback, reconcile=self.ledger.reconcile)
            with patch.object(qwen, '_http_post') as post, self.assertRaises(qwen.QwenError) as failure:
                adapter.complete(MESSAGES, invocation_id='one', mode='hybrid')
            post.assert_not_called()
            self.assertNotIn(KEY, str(failure.exception))
        self.call(invocation_id='one')
        restarted = qwen.QwenAdapter(KEY, reserve=self.ledger.reserve, reconcile=self.ledger.reconcile)
        with patch.object(qwen, '_http_post') as post, self.assertRaises(qwen.QwenError):
            restarted.complete(MESSAGES, invocation_id='one', mode='hybrid')
        post.assert_not_called()

    def test_insufficient_per_call_budget_rejected_before_reservation(self):
        profile = qwen.QwenProfile(per_call_limit_microusd=1000)
        adapter = qwen.QwenAdapter(KEY, reserve=self.ledger.reserve, reconcile=self.ledger.reconcile, profile=profile)
        with patch.object(qwen, '_http_post') as post, self.assertRaises(qwen.QwenError):
            adapter.complete(MESSAGES, invocation_id='one', mode='hybrid')
        post.assert_not_called()
        self.assertEqual(self.ledger.reservations, {})

    def test_timeout_and_transport_failure_retain_unknown_reservation_without_retry(self):
        for index, problem in enumerate((TimeoutError(KEY), OSError(KEY), RuntimeError(KEY))):
            with patch.object(qwen, '_http_post', side_effect=problem) as post, self.assertRaises(qwen.QwenError) as failure:
                self.adapter.complete(MESSAGES, invocation_id=str(index), mode='hybrid')
            post.assert_called_once()
            self.assertEqual(failure.exception.charge_status, 'uncertain')
            self.assertNotIn(KEY, str(failure.exception))
            settlement = self.ledger.settlements[str(index)]
            self.assertIsNone(settlement['actual_cost_microusd'])
            self.assertEqual(settlement['outcome'], 'uncertain')
            self.assertEqual(settlement['reserved_microusd'], 1051)

    def test_bad_or_missing_usage_or_wrong_model_never_releases_reserve(self):
        cases = []
        for usage in (None, {}, {'prompt_tokens': True, 'completion_tokens': 1, 'total_tokens': 2},
                      {'prompt_tokens': 1, 'completion_tokens': 2, 'total_tokens': 999},
                      {'prompt_tokens': 32769, 'completion_tokens': 1, 'total_tokens': 32770},
                      {'prompt_tokens': 1, 'completion_tokens': 2, 'total_tokens': 3,
                       'completion_tokens_details': {'reasoning_tokens': 3}}):
            value = response()
            value['usage'] = usage
            cases.append(value)
        cases.append(response(model='different-model'))
        for index, value in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(qwen.QwenError) as failure:
                self.call(value, invocation_id=str(index))
            self.assertEqual(failure.exception.charge_status, 'uncertain')
            self.assertIsNone(self.ledger.settlements[str(index)]['actual_cost_microusd'])

    def test_malformed_or_oversized_response_retains_reserve(self):
        raws = (b'not-json', b'[]', b'{"usage":NaN}', b'{"model":"a","model":"b"}',
                b'x' * (qwen.MAX_RESPONSE_BYTES + 1), b'[' * 2000 + b']' * 2000)
        for index, raw in enumerate(raws):
            with self.subTest(case=index), patch.object(qwen, '_http_post', return_value=raw) as post:
                with self.assertRaises(qwen.QwenError) as failure:
                    self.adapter.complete(MESSAGES, invocation_id=str(index), mode='hybrid')
                post.assert_called_once()
            self.assertEqual(failure.exception.charge_status, 'uncertain')

    def test_failed_answer_settles_known_usage_and_reasoning_is_not_double_charged(self):
        value = response(prompt=10, completion=20, reasoning=5)
        with self.assertRaises(qwen.QwenError) as failure:
            self.call(value)
        self.assertEqual(failure.exception.code, 'unexpected_thinking')
        self.assertEqual(failure.exception.charge_status, 'settled')
        self.assertEqual(failure.exception.settlement['actual_cost_microusd'], 3)
        self.assertEqual(failure.exception.settlement['outcome'], 'failed')
        self.assertEqual(failure.exception.settlement['usage']['reasoning_tokens'], 5)

    def test_output_overrun_is_charged_including_over_reservation(self):
        with self.assertRaises(qwen.QwenError) as failure:
            self.call(response(completion=20000))
        self.assertEqual(failure.exception.code, 'output_token_limit_exceeded')
        self.assertEqual(failure.exception.charge_status, 'settled')
        self.assertTrue(failure.exception.settlement['over_reservation'])
        self.assertEqual(failure.exception.settlement['actual_cost_microusd'], 2601)

    def test_tools_multiple_choices_invalid_finish_and_huge_text_are_rejected_but_charged(self):
        cases = [response(text='x' * (qwen.MAX_TEXT_BYTES + 1)), response(finish='tool_calls')]
        tools = response()
        tools['choices'][0]['message']['tool_calls'] = [{'function': {'name': 'must_not_execute'}}]
        cases.append(tools)
        multiple = response()
        multiple['choices'] *= 2
        cases.append(multiple)
        for index, value in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(qwen.QwenError) as failure:
                self.call(value, invocation_id=str(index))
            self.assertEqual(failure.exception.charge_status, 'settled')
            self.assertEqual(failure.exception.settlement['actual_cost_microusd'], 3)

    def test_reconciliation_failure_never_returns_success_or_retries(self):
        for callback in (Mock(return_value=False), Mock(return_value=1), Mock(side_effect=RuntimeError(KEY))):
            ledger = SyntheticLedger()
            adapter = qwen.QwenAdapter(KEY, reserve=ledger.reserve, reconcile=callback)
            with patch.object(qwen, '_http_post', return_value=encoded(response())) as post, self.assertRaises(qwen.QwenError) as failure:
                adapter.complete(MESSAGES, invocation_id='one', mode='hybrid')
            self.assertEqual(failure.exception.code, 'reconciliation_failed')
            self.assertEqual(failure.exception.charge_status, 'uncertain')
            self.assertEqual(failure.exception.settlement['actual_cost_microusd'], 3)
            post.assert_called_once()
            callback.assert_called_once()

    def test_http_transport_uses_fixed_endpoint_blocks_redirects_and_bounds_read(self):
        wire = Mock()
        wire.status = 200
        wire.headers = Message()
        wire.headers['Content-Type'] = 'application/json'
        wire.read.return_value = encoded(response())
        opener = Mock()
        opener.open.return_value.__enter__ = Mock(return_value=wire)
        opener.open.return_value.__exit__ = Mock(return_value=False)
        with patch.object(qwen, 'build_opener', return_value=opener) as factory:
            raw = qwen._http_post(b'{}', KEY, 30)
        self.assertEqual(raw, encoded(response()))
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, qwen.API_BASE + '/chat/completions')
        self.assertEqual(request.get_header('Authorization'), 'Bearer ' + KEY)
        self.assertEqual(request.method, 'POST')
        wire.read.assert_called_once_with(qwen.MAX_RESPONSE_BYTES + 1)
        proxy, redirects = factory.call_args.args
        self.assertEqual(proxy.proxies, {})
        self.assertIsNone(redirects.redirect_request(request, None, 302, '', {}, 'https://other.example'))

    def test_http_failure_closes_body_and_never_exposes_or_reads_error_text(self):
        error_body = io.BytesIO(KEY.encode())
        error = HTTPError(qwen.API_BASE, 401, KEY, {}, error_body)
        opener = Mock()
        opener.open.side_effect = error
        with patch.object(qwen, 'build_opener', return_value=opener), self.assertRaises(qwen.QwenError) as failure:
            self.adapter.complete(MESSAGES, invocation_id='http', mode='hybrid')
        self.assertTrue(error_body.closed)
        self.assertEqual(failure.exception.code, 'provider_http_error')
        self.assertEqual(failure.exception.charge_status, 'uncertain')
        self.assertNotIn(KEY, str(failure.exception))
        opener.open.assert_called_once()


if __name__ == '__main__':
    unittest.main()
