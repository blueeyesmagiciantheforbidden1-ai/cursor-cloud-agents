"""Offline hub-contract tests for measured usage reporting."""
from __future__ import annotations

import copy
import inspect
import math
import socket
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
# Append agent-hub (never insert at 0): agent-hub/tests must not shadow
# live-worker-runtime/tests when unittest discover loads this module.
_AGENT_HUB = str(ROOT.parent / 'agent-hub')
if _AGENT_HUB not in sys.path:
    sys.path.append(_AGENT_HUB)

import usage_report  # noqa: E402
from agent_hub.core import HubError  # noqa: E402
from agent_hub.telemetry import ERRORS, iso, timestamp, usage_entry  # noqa: E402
from agent_hub.worker import LeaseLost, WorkerError  # noqa: E402
from live_loop import Worker, Settings  # noqa: E402


OBSERVED = '2026-09-24T12:00:00Z'
# Copilot stamps preflight quota with datetime.now(timezone.utc).isoformat() (+00:00).
QUOTA_OBSERVED = '2026-09-24T11:55:00+00:00'
QUOTA_OBSERVED_Z = '2026-09-24T11:55:00Z'
CODEX_QUOTA_OBSERVED = '2026-09-24T11:50:00Z'
GROK_QUOTA_OBSERVED = '2026-09-24T11:40:00+00:00'
GROK_QUOTA_OBSERVED_Z = '2026-09-24T11:40:00Z'
GROK_PERIOD_END = '2026-10-01T00:00:00Z'
COPILOT_RESET = '2026-10-01T00:00:00+00:00'
COPILOT_RESET_Z = '2026-10-01T00:00:00Z'
# Epochs later than CODEX_QUOTA_OBSERVED (aligned with agent-hub/runtime/codex-usage.json scale).
CODEX_RESET_PRIMARY = int(datetime.fromisoformat('2026-09-25T12:00:00+00:00').timestamp())
CODEX_RESET_SECONDARY = int(datetime.fromisoformat('2026-10-01T00:00:00+00:00').timestamp())
CODEX_RESET_PRIMARY_Z = datetime.fromtimestamp(
    CODEX_RESET_PRIMARY, timezone.utc).isoformat().replace('+00:00', 'Z')
CODEX_RESET_SECONDARY_Z = datetime.fromtimestamp(
    CODEX_RESET_SECONDARY, timezone.utc).isoformat().replace('+00:00', 'Z')
NOW = datetime.fromisoformat(OBSERVED.replace('Z', '+00:00')).timestamp()
ACTOR = 'codex'

# Hub release-3 (a6b060d) constants — mirrored locally; private runcrew not imported.
_USAGE_METRICS = ('credits', 'tokens', 'requests', 'budget_usd', 'quota_percent')
_USAGE_UNITS = {'budget_usd': 'USD', 'quota_percent': '%'}
_RESET_HORIZON_SECONDS = 400 * 86400


def _mirror_timestamp(value, field='observed_at'):
    """agent_hub.telemetry.timestamp plus optional field name; HubError → ValueError."""
    try:
        return timestamp(value)
    except HubError as exc:
        message = str(exc)
        if field != 'observed_at' and 'observed_at' in message:
            message = message.replace('observed_at', field)
        raise ValueError(message) from None


def _release3_number(value, field):
    """Hub release-3 number(): range check BEFORE isfinite (avoids OverflowError on huge ints)."""
    if value is None:
        return None
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1e15
            or not math.isfinite(value)):
        raise ValueError(f'{field} must be a finite nonnegative number or null')
    return value


def _quota_label(window):
    if window is None:
        return 'Allowance (window not stated)'
    if window == 10080:
        return 'Weekly allowance'
    return f'{window}-minute allowance'


def _release3_usage_entry(actor, value, now):
    """Exact mirror of private hub telemetry.usage_entry at a6b060d; HubError → ValueError."""
    if not isinstance(value, dict):
        raise ValueError('Each usage entry must be an object')
    scope, metric, state = (value.get(key) for key in ('scope', 'metric', 'status'))
    source, error = value.get('source', 'provider_cli'), value.get('error', 'none')
    if scope not in ('account', 'session') or metric not in _USAGE_METRICS:
        raise ValueError('Usage scope or metric is invalid')
    if metric == 'quota_percent' and scope != 'account':
        raise ValueError('quota_percent is an account measurement')
    if state not in ('available', 'unavailable', 'auth_failed') or source not in (
            'provider_cli', 'provider_api', 'manual'):
        raise ValueError('Usage status or source is invalid')
    if not isinstance(error, str) or error not in ERRORS:
        raise ValueError('Unknown usage error code')
    observed = _mirror_timestamp(value['observed_at']) if value.get('observed_at') else None
    if observed is not None and (observed > now + 60 or observed < 0):
        raise ValueError('Usage observation time is invalid')
    if state == 'available' and observed is None:
        raise ValueError('Available usage requires observed_at')
    window = value.get('window_minutes')
    if window is not None and (metric != 'quota_percent' or type(window) is not int
                               or not 1 <= window <= 525600):
        raise ValueError('window_minutes is invalid')
    reset_at = None
    if value.get('resets_at') is not None:
        reset_at = _mirror_timestamp(value['resets_at'], 'resets_at')
        if (scope != 'account' or reset_at > now + _RESET_HORIZON_SECONDS
                or reset_at <= (observed if observed is not None else now)):
            raise ValueError('resets_at is invalid')
    used, limit, remaining = [_release3_number(value.get(key), key)
                              for key in ('used', 'limit', 'remaining')]
    if state != 'available':
        used = limit = remaining = None
    elif metric == 'quota_percent':
        if (used is None or remaining is None or used > 100 or remaining > 100
                or abs(used + remaining - 100) >= 1e-6 or limit not in (None, 100)):
            raise ValueError('quota_percent needs measured used and remaining that sum to 100')
        limit = 100
    elif all(item is None for item in (used, limit, remaining)):
        raise ValueError('Available usage requires a measured value')
    if limit is not None and remaining is not None and remaining > limit:
        raise ValueError('Remaining usage cannot exceed its limit')
    row = {
        'provider': actor, 'scope': scope, 'metric': metric, 'used': used, 'limit': limit,
        'remaining': remaining, 'unit': _USAGE_UNITS.get(metric, metric), 'source': source,
        'observed_at': iso(observed) if observed is not None else None,
        'status': state, 'error': ERRORS[error],
    }
    if window is not None:
        row['window_minutes'] = window
    if reset_at is not None:
        row['resets_at'] = iso(reset_at)
    if metric == 'quota_percent':
        row['label'] = _quota_label(window)
    return row


def _usage_entry_accepts_quota_percent():
    """True once Alpha release-3 lands quota_percent in telemetry.usage_entry."""
    try:
        usage_entry('codex', {
            'scope': 'account', 'metric': 'quota_percent', 'status': 'available',
            'source': 'provider_cli', 'observed_at': OBSERVED,
            'used': 10, 'remaining': 90, 'limit': 100, 'window_minutes': 10080,
            'resets_at': '2026-10-01T00:00:00Z',
        }, NOW)
        return True
    except Exception:
        source = inspect.getsource(usage_entry)
        return 'quota_percent' in source


def codex_preflight(windows, *, observed_at=CODEX_QUOTA_OBSERVED):
    quota = {
        'ordinary_usage_allowed': True,
        'included_used_percent': max(
            (w['used_percent'] for w in windows if w.get('used_percent') is not None),
            default=None),
        'windows': windows,
        'source': 'same_process_native',
        'extra_spending_enabled': False,
        'api_fallback_enabled': False,
        'automatic_improvement_ready': False,
    }
    if observed_at is not None:
        quota['observed_at'] = observed_at
    return {'quota': quota}


def grok_preflight(*, percent=25, observed_at=GROK_QUOTA_OBSERVED, period_end=GROK_PERIOD_END,
                   usage_source='native_percentage'):
    quota = {
        'source': 'native_authenticated_acp',
        'native_included_used_percent': percent,
        'native_usage_status': 'unavailable' if percent is None else 'available',
        'usage_source': usage_source,
        'period': {'start': '2026-09-01T00:00:00Z', 'end': period_end},
        'automatic_improvement_ready': False,
    }
    if observed_at is not None:
        quota['observed_at'] = observed_at
    return {'quota': quota, 'same_process_account_model_quota': percent is not None}

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
                      observed_at=QUOTA_OBSERVED, reset_date=None):
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
        'resetDate': reset_date,
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
    """Every entry produced by the standard fixtures (for contract checks).

    Includes session tokens, Copilot requests (with and without resets_at),
    Codex quota_percent windows, and Grok quota_percent.
    """
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
    rows.extend(usage_report.entries(
        'copilot', COPILOT_USAGE, premium_preflight(reset_date=COPILOT_RESET), OBSERVED))
    rows.extend(usage_report.entries('codex', {'inputTokens': 1}, None, OBSERVED))
    rows.extend(usage_report.entries('codex', None, None, None))
    rows.extend(usage_report.entries('codex', CODEX_USAGE, codex_preflight([
        {'limit_id': 'codex', 'window': 'primary', 'used_percent': 12,
         'resets_at': CODEX_RESET_PRIMARY, 'window_duration_mins': 300},
        {'limit_id': 'codex', 'window': 'secondary', 'used_percent': 40,
         'resets_at': CODEX_RESET_SECONDARY, 'window_duration_mins': 10080},
    ]), OBSERVED))
    rows.extend(usage_report.entries('grok', GROK_USAGE, grok_preflight(percent=25), OBSERVED))
    return rows


def _actor_for_entry(entry):
    if entry.get('metric') == 'requests':
        return 'copilot'
    if entry.get('metric') == 'quota_percent' and 'window_minutes' not in entry:
        return 'grok'
    return ACTOR


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

    def test_release3_usage_entry_accepts_every_fixture_row(self):
        """Every entries() row must pass the release-3 usage_entry mirror (a6b060d)."""
        rows = all_fixture_entries()
        self.assertTrue(any(r.get('metric') == 'tokens' for r in rows))
        self.assertTrue(any(r.get('metric') == 'requests' and 'resets_at' in r for r in rows))
        self.assertTrue(any(r.get('metric') == 'quota_percent' and 'window_minutes' in r for r in rows))
        self.assertTrue(any(r.get('metric') == 'quota_percent' and 'window_minutes' not in r for r in rows))
        for entry in rows:
            validated = _release3_usage_entry(_actor_for_entry(entry), copy.deepcopy(entry), NOW)
            self.assertEqual(validated['scope'], entry['scope'])
            self.assertEqual(validated['metric'], entry['metric'])
            self.assertEqual(validated['status'], entry['status'])
            self.assertEqual(validated['source'], entry['source'])
            self.assertEqual(validated['used'], entry['used'])
            if 'limit' in entry:
                self.assertEqual(validated['limit'], entry['limit'])
            if 'remaining' in entry:
                self.assertEqual(validated['remaining'], entry['remaining'])
            if 'window_minutes' in entry:
                self.assertEqual(validated['window_minutes'], entry['window_minutes'])
            if 'resets_at' in entry:
                self.assertEqual(validated['resets_at'], entry['resets_at'])


class QuotaPercentTests(unittest.TestCase):
    def test_codex_two_windows_with_durations(self):
        # Quota shaped exactly like providers/codex.py::_quota output.
        preflight = codex_preflight([
            {'limit_id': 'codex', 'window': 'primary', 'used_percent': 12,
             'resets_at': CODEX_RESET_PRIMARY, 'window_duration_mins': 300},
            {'limit_id': 'codex', 'window': 'secondary', 'used_percent': 40,
             'resets_at': CODEX_RESET_SECONDARY, 'window_duration_mins': 10080},
        ])
        got = usage_report.entries('codex', CODEX_USAGE, preflight, OBSERVED)
        self.assertEqual(got[0]['metric'], 'tokens')
        rows = [row for row in got if row['metric'] == 'quota_percent']
        self.assertEqual(len(rows), 2)
        by_minutes = {row['window_minutes']: row for row in rows}
        self.assertEqual(by_minutes[300], {
            'scope': 'account', 'metric': 'quota_percent', 'status': 'available',
            'source': 'provider_cli', 'observed_at': CODEX_QUOTA_OBSERVED,
            'used': 12, 'remaining': 88, 'limit': 100, 'window_minutes': 300,
            'resets_at': CODEX_RESET_PRIMARY_Z,
        })
        self.assertEqual(by_minutes[10080], {
            'scope': 'account', 'metric': 'quota_percent', 'status': 'available',
            'source': 'provider_cli', 'observed_at': CODEX_QUOTA_OBSERVED,
            'used': 40, 'remaining': 60, 'limit': 100, 'window_minutes': 10080,
            'resets_at': CODEX_RESET_SECONDARY_Z,
        })

    def test_codex_window_without_duration_emits_no_row(self):
        # No valid duration → no row (no collapse, no null-window that could shadow).
        preflight = codex_preflight([
            {'limit_id': 'codex', 'window': 'primary', 'used_percent': 12,
             'resets_at': CODEX_RESET_PRIMARY},
            {'limit_id': 'codex', 'window': 'secondary', 'used_percent': 40,
             'resets_at': CODEX_RESET_SECONDARY},
        ])
        rows = [r for r in usage_report.entries('codex', None, preflight, None)
                if r['metric'] == 'quota_percent']
        self.assertEqual(rows, [])

    def test_codex_invalid_duration_emits_no_quota_percent_row(self):
        # Invalid durations (None after tolerant codex._quota read) must not emit a row.
        for bad in (None, 0, 600000, '300', 12.5, True):
            with self.subTest(duration=bad):
                preflight = codex_preflight([
                    {'limit_id': 'codex', 'window': 'primary', 'used_percent': 12,
                     'resets_at': CODEX_RESET_PRIMARY, 'window_duration_mins': bad},
                ])
                rows = [r for r in usage_report.entries('codex', None, preflight, None)
                        if r['metric'] == 'quota_percent']
                self.assertEqual(rows, [])

    def test_codex_one_valid_one_missing_duration_emits_one_row(self):
        preflight = codex_preflight([
            {'limit_id': 'codex', 'window': 'primary', 'used_percent': 12,
             'resets_at': CODEX_RESET_PRIMARY, 'window_duration_mins': 300},
            {'limit_id': 'codex', 'window': 'secondary', 'used_percent': 40,
             'resets_at': CODEX_RESET_SECONDARY},
        ])
        rows = [r for r in usage_report.entries('codex', None, preflight, None)
                if r['metric'] == 'quota_percent']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0], {
            'scope': 'account', 'metric': 'quota_percent', 'status': 'available',
            'source': 'provider_cli', 'observed_at': CODEX_QUOTA_OBSERVED,
            'used': 12, 'remaining': 88, 'limit': 100, 'window_minutes': 300,
            'resets_at': CODEX_RESET_PRIMARY_Z,
        })

    def test_codex_used_percent_none_skipped(self):
        preflight = codex_preflight([
            {'limit_id': 'codex', 'window': 'primary', 'used_percent': None,
             'resets_at': CODEX_RESET_PRIMARY, 'window_duration_mins': 300},
            {'limit_id': 'codex', 'window': 'secondary', 'used_percent': 7,
             'resets_at': CODEX_RESET_SECONDARY, 'window_duration_mins': 10080},
        ])
        rows = [r for r in usage_report.entries('codex', None, preflight, None)
                if r['metric'] == 'quota_percent']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['used'], 7)
        self.assertEqual(rows[0]['window_minutes'], 10080)

    def test_codex_non_codex_limit_id_ignored(self):
        preflight = codex_preflight([
            {'limit_id': 'other', 'window': 'primary', 'used_percent': 99,
             'resets_at': CODEX_RESET_PRIMARY, 'window_duration_mins': 60},
            {'limit_id': 'codex', 'window': 'primary', 'used_percent': 5,
             'resets_at': CODEX_RESET_PRIMARY, 'window_duration_mins': 300},
        ])
        rows = [r for r in usage_report.entries('codex', None, preflight, None)
                if r['metric'] == 'quota_percent']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['used'], 5)
        self.assertEqual(rows[0]['remaining'], 95)

    def test_codex_remaining_is_100_minus_used(self):
        preflight = codex_preflight([
            {'limit_id': 'codex', 'window': 'primary', 'used_percent': 33.5,
             'resets_at': CODEX_RESET_PRIMARY, 'window_duration_mins': 300},
        ])
        row = [r for r in usage_report.entries('codex', None, preflight, None)
               if r['metric'] == 'quota_percent'][0]
        self.assertEqual(row['remaining'], 66.5)
        self.assertLess(abs(row['used'] + row['remaining'] - 100), 1e-6)

    def test_codex_skips_without_measurement_time(self):
        preflight = codex_preflight([
            {'limit_id': 'codex', 'window': 'primary', 'used_percent': 10,
             'resets_at': CODEX_RESET_PRIMARY, 'window_duration_mins': 300},
        ], observed_at=None)
        rows = usage_report.entries('codex', CODEX_USAGE, preflight, OBSERVED)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['metric'], 'tokens')

    def test_codex_never_uses_turn_observed_at(self):
        preflight = codex_preflight([
            {'limit_id': 'codex', 'window': 'primary', 'used_percent': 10,
             'resets_at': CODEX_RESET_PRIMARY, 'window_duration_mins': 300},
        ])
        row = [r for r in usage_report.entries('codex', CODEX_USAGE, preflight, OBSERVED)
               if r['metric'] == 'quota_percent'][0]
        self.assertEqual(row['observed_at'], CODEX_QUOTA_OBSERVED)
        self.assertNotEqual(row['observed_at'], OBSERVED)

    def test_grok_percent_row(self):
        got = usage_report.entries('grok', GROK_USAGE, grok_preflight(percent=25), OBSERVED)
        self.assertEqual(got[0]['metric'], 'tokens')
        self.assertEqual(got[1], {
            'scope': 'account', 'metric': 'quota_percent', 'status': 'available',
            'source': 'provider_cli', 'observed_at': GROK_QUOTA_OBSERVED_Z,
            'used': 25, 'remaining': 75, 'limit': 100,
            'resets_at': GROK_PERIOD_END,
        })
        self.assertNotIn('window_minutes', got[1])

    def test_grok_legacy_cents_budget_usd_absent(self):
        # _billing collapses monthlyLimit/used into native_included_used_percent; no raw cents on quota.
        preflight = grok_preflight(percent=20, usage_source='native_legacy_cents')
        metrics = {row['metric'] for row in usage_report.entries('grok', GROK_USAGE, preflight, OBSERVED)}
        self.assertIn('quota_percent', metrics)
        self.assertNotIn('budget_usd', metrics)

    def test_copilot_requests_with_resets_at(self):
        got = usage_report.entries(
            'copilot', COPILOT_USAGE, premium_preflight(reset_date=COPILOT_RESET), OBSERVED)
        self.assertEqual(got[1], {
            'scope': 'account', 'metric': 'requests', 'status': 'available',
            'source': 'provider_cli', 'observed_at': QUOTA_OBSERVED_Z,
            'used': 180, 'limit': 20000, 'remaining': 19820,
            'resets_at': COPILOT_RESET_Z,
        })
        self.assertNotIn('window_minutes', got[1])

    def test_copilot_stale_or_invalid_reset_date_omitted(self):
        for bad in (None, 'not-a-time', '2026-09-24T11:55:00', QUOTA_OBSERVED,
                    '2020-01-01T00:00:00Z', '2028-01-01T00:00:00Z'):
            got = usage_report.entries(
                'copilot', None, premium_preflight(reset_date=bad), None)
            self.assertEqual(len(got), 1, bad)
            self.assertNotIn('resets_at', got[0], bad)

    def test_release3_usage_entry_rejects_mirror_negatives(self):
        """Negative cases matching hub release-3 usage_entry rejects (ValueError, no OverflowError)."""
        base_quota = {
            'scope': 'account', 'metric': 'quota_percent', 'status': 'available',
            'source': 'provider_cli', 'observed_at': OBSERVED,
            'used': 10, 'remaining': 90, 'limit': 100, 'window_minutes': 300,
            'resets_at': '2026-10-01T00:00:00Z',
        }
        cases = [
            ('quota_percent on session', {
                **base_quota, 'scope': 'session',
            }),
            ('window_minutes on tokens', {
                'scope': 'session', 'metric': 'tokens', 'status': 'available',
                'source': 'provider_cli', 'observed_at': OBSERVED, 'used': 17,
                'window_minutes': 300,
            }),
            ('window_minutes True', {**base_quota, 'window_minutes': True}),
            ('resets_at equal to observed_at', {**base_quota, 'resets_at': OBSERVED}),
            ('resets_at 401 days ahead', {
                **base_quota,
                'resets_at': iso(NOW + 401 * 86400),
            }),
            ('used 60 remaining 30', {**base_quota, 'used': 60, 'remaining': 30}),
            ('limit 99', {**base_quota, 'limit': 99}),
            ('used 10**400', {**base_quota, 'used': 10 ** 400, 'remaining': 90}),
        ]
        for name, row in cases:
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    _release3_usage_entry('codex', copy.deepcopy(row), NOW)

    @unittest.skipUnless(_usage_entry_accepts_quota_percent(),
                         'telemetry.usage_entry does not yet accept quota_percent (Alpha release-3)')
    def test_hub_usage_entry_accepts_quota_percent_when_enabled(self):
        rows = []
        rows.extend(r for r in usage_report.entries('codex', None, codex_preflight([
            {'limit_id': 'codex', 'window': 'primary', 'used_percent': 12,
             'resets_at': CODEX_RESET_PRIMARY, 'window_duration_mins': 300},
        ]), None) if r['metric'] == 'quota_percent')
        rows.extend(r for r in usage_report.entries('grok', None, grok_preflight(percent=25), None)
                    if r['metric'] == 'quota_percent')
        for row in rows:
            validated = usage_entry('codex' if 'window_minutes' in row else 'grok',
                                    copy.deepcopy(row), NOW)
            self.assertEqual(validated['metric'], 'quota_percent')
            self.assertEqual(validated['used'], row['used'])
            self.assertEqual(validated['remaining'], row['remaining'])
            self.assertEqual(validated['limit'], 100)


class ReportClient:
    def __init__(self):
        self.calls = []

    def post(self, path, value):
        self.calls.append((path, copy.deepcopy(value)))
        return {'accepted': True}


def _hub_http_error(code, reason='Bad Request'):
    """WorkerError shaped like HubClient.post on an HTTP error."""
    try:
        raise WorkerError(f'Hub request failed (HTTP {code})') from HTTPError(
            'http://hub/v1/workers/report', code, reason, None, None)
    except WorkerError as exc:
        return exc


def _hub_connection_failed():
    try:
        raise WorkerError('Hub connection failed') from URLError('connection refused')
    except WorkerError as exc:
        return exc


def _lease_lost():
    try:
        raise LeaseLost('The hub revoked or expired this task lease') from HTTPError(
            'http://hub/v1/workers/report', 409, 'Conflict', None, None)
    except LeaseLost as exc:
        return exc


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
        outcomes = [_hub_http_error(400), {'accepted': True}]

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

    def test_report_not_accepted_with_usage_does_not_retry(self):
        # Was test_report_not_accepted_with_usage_retries: not-accepted receipt
        # must not trigger the usage:[] retry (only HTTP 400 does).
        worker, client = self._worker('codex')
        worker.ready = True
        worker._last_turn_usage = CODEX_USAGE
        worker._last_turn_observed_at = OBSERVED

        def post(path, value):
            client.calls.append((path, copy.deepcopy(value)))
            return {'accepted': False}

        client.post = post
        with self.assertRaises(Exception):
            worker.report(force=True)
        self.assertEqual(len(client.calls), 1)
        self.assertTrue(client.calls[0][1]['usage'])
        self.assertEqual(worker.usage_rejected, 0)
        self.assertEqual(worker.next_report, 0.0)

    def test_report_failure_without_usage_no_retry(self):
        worker, client = self._worker()
        worker.ready = True
        first = _hub_http_error(400)

        def post(path, value):
            client.calls.append((path, copy.deepcopy(value)))
            raise first

        client.post = post
        with self.assertRaises(WorkerError) as caught:
            worker.report(force=True)
        self.assertIs(caught.exception, first)
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
        first = _hub_http_error(400)
        second = _hub_http_error(400, 'Still Bad')
        outcomes = [first, second]

        def post(path, value):
            client.calls.append((path, copy.deepcopy(value)))
            raise outcomes.pop(0)

        client.post = post
        with self.assertRaises(WorkerError) as caught:
            worker.report(force=True)
        self.assertIs(caught.exception, second)
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
        outcomes = [_hub_http_error(400), {'accepted': True}]

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

    def _assert_no_retry_on(self, error):
        worker, client = self._worker('codex')
        worker.ready = True
        worker._last_turn_usage = CODEX_USAGE
        worker._last_turn_observed_at = OBSERVED

        def post(path, value):
            client.calls.append((path, copy.deepcopy(value)))
            raise error

        client.post = post
        with self.assertRaises(type(error)) as caught:
            worker.report(force=True)
        self.assertIs(caught.exception, error)
        self.assertEqual(len(client.calls), 1)
        self.assertTrue(client.calls[0][1]['usage'])
        self.assertEqual(worker.usage_rejected, 0)
        self.assertEqual(worker.next_report, 0.0)

    def test_report_http_500_with_usage_no_retry(self):
        self._assert_no_retry_on(_hub_http_error(500, 'Internal Server Error'))

    def test_report_http_503_with_usage_no_retry(self):
        self._assert_no_retry_on(_hub_http_error(503, 'Service Unavailable'))

    def test_report_http_429_with_usage_no_retry(self):
        self._assert_no_retry_on(_hub_http_error(429, 'Too Many Requests'))

    def test_report_http_401_with_usage_no_retry(self):
        self._assert_no_retry_on(_hub_http_error(401, 'Unauthorized'))

    def test_report_lease_lost_409_with_usage_no_retry(self):
        self._assert_no_retry_on(_lease_lost())

    def test_report_connection_failed_with_usage_no_retry(self):
        self._assert_no_retry_on(_hub_connection_failed())

    def test_report_socket_timeout_with_usage_no_retry(self):
        self._assert_no_retry_on(socket.timeout('timed out'))

    def test_report_timeout_error_with_usage_no_retry(self):
        self._assert_no_retry_on(TimeoutError('timed out'))


if __name__ == '__main__':
    unittest.main()
