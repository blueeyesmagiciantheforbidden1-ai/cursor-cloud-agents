"""Offline hub-contract tests for measured usage reporting."""
from __future__ import annotations

import copy
import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / 'agent-hub'))

import usage_report  # noqa: E402
from agent_hub.telemetry import usage_entry  # noqa: E402
from live_loop import Worker, Settings  # noqa: E402


OBSERVED = '2026-09-24T12:00:00Z'
# Copilot stamps preflight quota with datetime.now(timezone.utc).isoformat() (+00:00).
QUOTA_OBSERVED = '2026-09-24T11:55:00+00:00'
QUOTA_OBSERVED_Z = '2026-09-24T11:55:00Z'
NOW = datetime.fromisoformat(OBSERVED.replace('Z', '+00:00')).timestamp()
ACTOR = 'codex'

# Real shapes from provider fixtures (tests/test_codex.py:137-139, etc.).
CODEX_USAGE = {
    'inputTokens': 12, 'outputTokens': 5, 'cachedInputTokens': 0,
    'reasoningOutputTokens': 2, 'totalTokens': 17,
}
CLAUDE_USAGE = {
    'usage': {'input_tokens': 10, 'output_tokens': 5,
              'cache_creation_input_tokens': 1, 'cache_read_input_tokens': 2},
    'api_equivalent_cost_usd': 0.01,
    'actual_account_charge_verified': False,
}
COPILOT_USAGE = {
    'reported_reasoning_effort': 'high', 'reported_is_byok': False, 'reported_is_auto': False,
    'inputTokens': 2095, 'outputTokens': 293,
}
GROK_USAGE = {
    'inputTokens': 123, 'outputTokens': 12, 'reasoningTokens': 3, 'cachedReadTokens': 20,
}
CURSOR_USAGE = {
    'native_event_counts': {'usage_update': 1, 'agent_message_chunk': 2},
    'actual_account_charge_verified': False,
}


def premium_preflight(*, used=180, entitlement=20000, unlimited=False, malformed=False,
                      observed_at=QUOTA_OBSERVED):
    pool = {
        'quota_type': 'premium_interactions',
        'pool_ref': 'a' * 64,
        'isUnlimitedEntitlement': unlimited,
        'entitlementRequests': entitlement,
        'usedRequests': used,
        'remainingPercentage': 99.1,
        'native_included_used_percent': 0.9,
        'usageAllowedWithExhaustedQuota': False,
        'overageAllowedWithExhaustedQuota': False,
        'overage': 0,
        'resetDate': None,
    }
    if malformed:
        pool = {'quota_type': 'premium_interactions', 'usedRequests': 'not-a-number'}
    quota = {
        'source': 'native_authenticated_jsonrpc',
        'native_usage_status': 'available',
        'native_included_used_percent': 0.9,
        'pools': [pool],
    }
    if observed_at is not None:
        quota['observed_at'] = observed_at
    return {
        'quota': quota,
        'same_process_account_model_quota': True,
    }


def all_fixture_entries():
    """Every entry produced by the standard fixtures (for contract checks)."""
    rows = []
    for provider, usage in (
        ('codex', CODEX_USAGE),
        ('claude', CLAUDE_USAGE),
        ('copilot', COPILOT_USAGE),
        ('grok', GROK_USAGE),
        ('cursor', CURSOR_USAGE),
    ):
        preflight = premium_preflight() if provider == 'copilot' else None
        rows.extend(usage_report.entries(provider, usage, preflight, OBSERVED))
    rows.extend(usage_report.entries('copilot', COPILOT_USAGE, premium_preflight(unlimited=True), OBSERVED))
    rows.extend(usage_report.entries('copilot', COPILOT_USAGE, premium_preflight(malformed=True), OBSERVED))
    rows.extend(usage_report.entries('copilot', None, premium_preflight(), OBSERVED))
    rows.extend(usage_report.entries('copilot', None, premium_preflight(), None))
    rows.extend(usage_report.entries('copilot', COPILOT_USAGE, premium_preflight(observed_at=None), OBSERVED))
    rows.extend(usage_report.entries('copilot', COPILOT_USAGE, premium_preflight(observed_at='bad'), OBSERVED))
    rows.extend(usage_report.entries('codex', {'inputTokens': 1}, None, OBSERVED))
    rows.extend(usage_report.entries('codex', None, None, None))
    return rows


class EntriesTests(unittest.TestCase):
    def test_codex_session_tokens_exact(self):
        got = usage_report.entries('codex', CODEX_USAGE, None, OBSERVED)
        self.assertEqual(got, [{
            'scope': 'session', 'metric': 'tokens', 'status': 'available',
            'source': 'provider_cli', 'observed_at': OBSERVED, 'used': 17,
        }])

    def test_claude_session_tokens_nested_exact(self):
        got = usage_report.entries('claude', CLAUDE_USAGE, None, OBSERVED)
        self.assertEqual(got, [{
            'scope': 'session', 'metric': 'tokens', 'status': 'available',
            'source': 'provider_cli', 'observed_at': OBSERVED, 'used': 15,
        }])

    def test_copilot_session_tokens_exact(self):
        got = usage_report.entries('copilot', COPILOT_USAGE, None, OBSERVED)
        self.assertEqual(got, [{
            'scope': 'session', 'metric': 'tokens', 'status': 'available',
            'source': 'provider_cli', 'observed_at': OBSERVED, 'used': 2388,
        }])

    def test_grok_session_tokens_exact(self):
        got = usage_report.entries('grok', GROK_USAGE, None, OBSERVED)
        self.assertEqual(got, [{
            'scope': 'session', 'metric': 'tokens', 'status': 'available',
            'source': 'provider_cli', 'observed_at': OBSERVED, 'used': 135,
        }])

    def test_cursor_yields_no_session_tokens(self):
        self.assertEqual(usage_report.entries('cursor', CURSOR_USAGE, None, OBSERVED), [])

    def test_ignores_cache_and_reasoning_in_used(self):
        # Codex: used is input+output only (12+5), not totalTokens or reasoning.
        got = usage_report.entries('codex', CODEX_USAGE, None, OBSERVED)
        self.assertEqual(got[0]['used'], 17)
        self.assertNotEqual(got[0]['used'], CODEX_USAGE['totalTokens'] + CODEX_USAGE['reasoningOutputTokens'])

    def test_copilot_premium_limited(self):
        got = usage_report.entries('copilot', COPILOT_USAGE, premium_preflight(), OBSERVED)
        self.assertEqual(len(got), 2)
        self.assertEqual(got[0]['observed_at'], OBSERVED)  # session uses turn time
        self.assertEqual(got[1], {
            'scope': 'account', 'metric': 'requests', 'status': 'available',
            'source': 'provider_cli', 'observed_at': QUOTA_OBSERVED_Z,
            'used': 180, 'limit': 20000, 'remaining': 19820,
        })

    def test_copilot_account_uses_quota_observed_at_not_turn(self):
        got = usage_report.entries('copilot', COPILOT_USAGE, premium_preflight(), OBSERVED)
        self.assertEqual(got[1]['observed_at'], QUOTA_OBSERVED_Z)
        self.assertNotEqual(got[1]['observed_at'], OBSERVED)

    def test_copilot_account_without_turn_observed_at(self):
        got = usage_report.entries('copilot', None, premium_preflight(), None)
        self.assertEqual(got, [{
            'scope': 'account', 'metric': 'requests', 'status': 'available',
            'source': 'provider_cli', 'observed_at': QUOTA_OBSERVED_Z,
            'used': 180, 'limit': 20000, 'remaining': 19820,
        }])

    def test_copilot_account_skipped_missing_quota_observed_at(self):
        got = usage_report.entries(
            'copilot', COPILOT_USAGE, premium_preflight(observed_at=None), OBSERVED)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]['metric'], 'tokens')

    def test_copilot_account_skipped_invalid_quota_observed_at(self):
        for bad in ('not-a-time', '2026-09-24T11:55:00', 12345, ''):
            got = usage_report.entries(
                'copilot', COPILOT_USAGE, premium_preflight(observed_at=bad), OBSERVED)
            self.assertEqual(len(got), 1, bad)
            self.assertEqual(got[0]['metric'], 'tokens', bad)

    def test_copilot_premium_unlimited_skipped(self):
        got = usage_report.entries(
            'copilot', COPILOT_USAGE, premium_preflight(unlimited=True), OBSERVED)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]['metric'], 'tokens')

    def test_copilot_premium_malformed_skipped(self):
        got = usage_report.entries(
            'copilot', COPILOT_USAGE, premium_preflight(malformed=True), OBSERVED)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]['metric'], 'tokens')

    def test_no_percent_credits_or_cost_entries(self):
        for provider, usage in (
            ('codex', CODEX_USAGE),
            ('claude', CLAUDE_USAGE),
            ('grok', GROK_USAGE),
        ):
            for entry in usage_report.entries(provider, usage, {'quota': {'included_used_percent': 40}}, OBSERVED):
                self.assertNotEqual(entry.get('metric'), 'credits')
                self.assertNotEqual(entry.get('metric'), 'budget_usd')
                self.assertNotIn('percent', str(entry).lower())

    def test_never_raises_on_bad_shapes(self):
        cases = [
            ('codex', None, None, None),
            ('codex', 'x', {'quota': object()}, OBSERVED),
            (None, CODEX_USAGE, None, OBSERVED),
            ('claude', {'usage': None}, None, OBSERVED),
            ('copilot', {'inputTokens': -1, 'outputTokens': 1}, premium_preflight(), OBSERVED),
            ('copilot', {'inputTokens': 1.5, 'outputTokens': 1}, None, OBSERVED),
        ]
        for args in cases:
            self.assertIsInstance(usage_report.entries(*args), list)

    def test_at_most_eight_entries(self):
        rows = usage_report.entries('copilot', COPILOT_USAGE, premium_preflight(), OBSERVED)
        self.assertLessEqual(len(rows), 8)

    def test_no_identity_leak_keys_or_values(self):
        forbidden = ('@', 'accountId', 'login')
        for entry in all_fixture_entries():
            blob = repr(entry)
            for token in forbidden:
                self.assertNotIn(token, blob)
            for key, value in entry.items():
                self.assertNotIn('@', str(key))
                for token in forbidden:
                    self.assertNotIn(token, str(key))
                    self.assertNotIn(token, str(value))

    def test_hub_contract_usage_entry_accepts_every_fixture_row(self):
        for entry in all_fixture_entries():
            validated = usage_entry(ACTOR if entry.get('metric') != 'requests' else 'copilot',
                                    copy.deepcopy(entry), NOW)
            # usage_entry returns enriched row; input fields must survive unchanged in meaning.
            self.assertEqual(validated['scope'], entry['scope'])
            self.assertEqual(validated['metric'], entry['metric'])
            self.assertEqual(validated['status'], entry['status'])
            self.assertEqual(validated['source'], entry['source'])
            self.assertEqual(validated['used'], entry['used'])
            if 'limit' in entry:
                self.assertEqual(validated['limit'], entry['limit'])
            if 'remaining' in entry:
                self.assertEqual(validated['remaining'], entry['remaining'])


class ReportClient:
    def __init__(self):
        self.calls = []

    def post(self, path, value):
        self.calls.append((path, copy.deepcopy(value)))
        return {'accepted': True}


class ReportPayloadTests(unittest.TestCase):
    def _worker(self, agent='codex'):
        client = ReportClient()
        worker = Worker(Settings(agent, agent + '-live', warm_seconds=60),
                        client, SimpleNamespace(), object(), clock=lambda: 1000.0)
        return worker, client

    def test_report_usage_empty_before_any_turn(self):
        worker, client = self._worker()
        worker.ready = True
        worker.report(force=True)
        self.assertEqual(client.calls[-1][1]['usage'], [])

    def test_report_carries_session_tokens_after_turn(self):
        worker, client = self._worker('codex')
        worker.ready = True
        worker._last_turn_usage = CODEX_USAGE
        worker._last_turn_observed_at = OBSERVED
        worker.report(force=True)
        self.assertEqual(client.calls[-1][1]['usage'], [{
            'scope': 'session', 'metric': 'tokens', 'status': 'available',
            'source': 'provider_cli', 'observed_at': OBSERVED, 'used': 17,
        }])

    def test_report_usage_empty_when_entries_raises(self):
        worker, client = self._worker()
        worker.ready = True
        worker._last_turn_usage = CODEX_USAGE
        worker._last_turn_observed_at = OBSERVED
        with patch.object(usage_report, 'entries', side_effect=RuntimeError('boom')):
            worker.report(force=True)
        self.assertEqual(client.calls[-1][1]['usage'], [])

    def test_report_reads_handle_preflight_for_copilot(self):
        worker, client = self._worker('copilot')
        worker.ready = True
        worker.handle = SimpleNamespace(preflight=premium_preflight())
        worker._last_turn_usage = COPILOT_USAGE
        worker._last_turn_observed_at = OBSERVED
        worker.report(force=True)
        usage = client.calls[-1][1]['usage']
        self.assertEqual(len(usage), 2)
        self.assertEqual(usage[1]['metric'], 'requests')
        self.assertEqual(usage[1]['used'], 180)
        self.assertEqual(usage[1]['observed_at'], QUOTA_OBSERVED_Z)

    def test_report_copilot_account_before_any_turn(self):
        worker, client = self._worker('copilot')
        worker.ready = True
        worker.handle = SimpleNamespace(preflight=premium_preflight())
        worker.report(force=True)
        usage = client.calls[-1][1]['usage']
        self.assertEqual(len(usage), 1)
        self.assertEqual(usage[0]['metric'], 'requests')
        self.assertEqual(usage[0]['observed_at'], QUOTA_OBSERVED_Z)

    def test_report_success_sends_usage_once(self):
        worker, client = self._worker('codex')
        worker.ready = True
        worker._last_turn_usage = CODEX_USAGE
        worker._last_turn_observed_at = OBSERVED
        worker.report(force=True)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0][0], '/v1/workers/report')
        self.assertEqual(client.calls[0][1]['usage'], [{
            'scope': 'session', 'metric': 'tokens', 'status': 'available',
            'source': 'provider_cli', 'observed_at': OBSERVED, 'used': 17,
        }])
        self.assertEqual(worker.next_report, 1025.0)

    def test_report_failure_with_usage_retries_once_without_usage(self):
        worker, client = self._worker('codex')
        worker.ready = True
        worker._last_turn_usage = CODEX_USAGE
        worker._last_turn_observed_at = OBSERVED
        outcomes = [RuntimeError('rejected'), {'accepted': True}]

        def post(path, value):
            client.calls.append((path, copy.deepcopy(value)))
            result = outcomes.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result

        client.post = post
        worker.report(force=True)
        self.assertEqual(len(client.calls), 2)
        self.assertTrue(client.calls[0][1]['usage'])
        self.assertEqual(client.calls[1][1]['usage'], [])
        self.assertEqual(worker.usage_rejected, 1)
        self.assertTrue(all('span' in item for item in worker.spans))
        self.assertEqual(worker.next_report, 1025.0)

    def test_report_not_accepted_with_usage_retries(self):
        worker, client = self._worker('codex')
        worker.ready = True
        worker._last_turn_usage = CODEX_USAGE
        worker._last_turn_observed_at = OBSERVED
        outcomes = [{'accepted': False}, {'accepted': True}]

        def post(path, value):
            client.calls.append((path, copy.deepcopy(value)))
            return outcomes.pop(0)

        client.post = post
        worker.report(force=True)
        self.assertEqual(len(client.calls), 2)
        self.assertTrue(client.calls[0][1]['usage'])
        self.assertEqual(client.calls[1][1]['usage'], [])
        self.assertEqual(worker.usage_rejected, 1)
        self.assertTrue(all('span' in item for item in worker.spans))
        self.assertEqual(worker.next_report, 1025.0)

    def test_report_failure_without_usage_no_retry(self):
        worker, client = self._worker()
        worker.ready = True

        def post(path, value):
            client.calls.append((path, copy.deepcopy(value)))
            raise RuntimeError('rejected')

        client.post = post
        with self.assertRaises(RuntimeError) as caught:
            worker.report(force=True)
        self.assertEqual(str(caught.exception), 'rejected')
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0][1]['usage'], [])
        self.assertEqual(worker.spans, [])
        self.assertEqual(worker.usage_rejected, 0)
        self.assertEqual(worker.next_report, 0.0)

    def test_report_both_attempts_fail_preserve_retry_exception(self):
        worker, client = self._worker('codex')
        worker.ready = True
        worker._last_turn_usage = CODEX_USAGE
        worker._last_turn_observed_at = OBSERVED
        outcomes = [ValueError('first'), KeyError('second')]

        def post(path, value):
            client.calls.append((path, copy.deepcopy(value)))
            raise outcomes.pop(0)

        client.post = post
        with self.assertRaises(KeyError) as caught:
            worker.report(force=True)
        self.assertEqual(caught.exception.args, ('second',))
        self.assertEqual(len(client.calls), 2)
        self.assertTrue(client.calls[0][1]['usage'])
        self.assertEqual(client.calls[1][1]['usage'], [])
        self.assertEqual(worker.usage_rejected, 1)
        self.assertTrue(all('span' in item for item in worker.spans))
        self.assertEqual(worker.next_report, 0.0)

    def test_heartbeat_true_when_usage_rejected_then_empty_ok(self):
        worker, client = self._worker('codex')
        worker.ready = True
        worker._last_turn_usage = CODEX_USAGE
        worker._last_turn_observed_at = OBSERVED
        outcomes = [RuntimeError('rejected'), {'accepted': True}]

        def post(path, value):
            client.calls.append((path, copy.deepcopy(value)))
            result = outcomes.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result

        client.post = post
        self.assertTrue(worker.heartbeat())
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.calls[1][1]['usage'], [])
        self.assertEqual(worker.usage_rejected, 1)
        self.assertTrue(all('span' in item for item in worker.spans))


if __name__ == '__main__':
    unittest.main()
