"""Atomic, fail-closed accounting for the one approved hosted Qwen profile.

Use the controller's existing Firestore client (including its named database).
Only trusted controller code may call these methods. No prompts, response text,
provider keys, automatic retries, time-based release or spending reset exists.
Day rollover resets settled daily spend only; every unresolved hold survives.
Late settlements are charged to the server UTC day on which they are accepted.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import time

from .qwen import INPUT_TIER_TOKENS, MAX_INPUT_BYTES, OUTPUT_VARIANCE, QwenError, QwenProfile


ERROR_CODES = frozenset(('invalid_http_response', 'response_too_large', 'provider_http_error',
    'invalid_response', 'missing_usage', 'invalid_usage', 'usage_outside_price_profile',
    'output_token_limit_exceeded', 'invalid_choices', 'invalid_answer', 'answer_too_large',
    'unexpected_thinking', 'invalid_finish_reason', 'returned_model_mismatch', 'provider_transport_error'))
MAX_METADATA_BYTES = 8192


class QwenLedgerError(ValueError):
    """Static error codes only; never include submitted metadata in messages."""


def _require(condition, code='invalid_ledger_metadata'):
    if not condition:
        raise QwenLedgerError(code)


def _integer(value, maximum=10**12, minimum=0):
    _require(type(value) is int and minimum <= value <= maximum)
    return value


def _identifier(value):
    _require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value) is not None)
    return value


def _json(value):
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise QwenLedgerError('invalid_ledger_metadata') from None
    _require(len(encoded) <= MAX_METADATA_BYTES, 'ledger_metadata_too_large')
    return encoded


def _fields(value, fields):
    _require(isinstance(value, dict) and set(value) == set(fields))


def _profile(value):
    _require(isinstance(value, dict))
    try:
        profile = QwenProfile(max_completion_tokens=value.get('max_completion_tokens'))
    except QwenError:
        raise QwenLedgerError('unapproved_ledger_profile') from None
    _require(_json(value) == _json(profile.public()), 'unapproved_ledger_profile')
    return profile


class FirestoreQwenLedger:
    def __init__(self, client, clock=time.time, *, daily_limit_microusd=100000,
                 per_call_limit_microusd=10000, collection_prefix='agent_hub_qwen', transactional=None):
        _integer(daily_limit_microusd, 100000, 1)
        _integer(per_call_limit_microusd, min(10000, daily_limit_microusd), 1)
        _require(isinstance(collection_prefix, str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', collection_prefix) is not None)
        if transactional is None:
            from google.cloud import firestore
            transactional = firestore.transactional
        self.client, self.clock, self.transactional = client, clock, transactional
        self.invocations = client.collection(collection_prefix + '_invocations')
        self.budget = client.collection(collection_prefix + '_budget').document('global')
        self.policy = {'daily_limit_microusd': daily_limit_microusd,
                       'per_call_limit_microusd': per_call_limit_microusd,
                       'concurrency': 1, 'pending_uncertain_seconds': 120}

    def _now(self):
        now = self.clock()
        _require(type(now) in (int, float) and math.isfinite(now) and 0 <= now <= 253402300799, 'invalid_server_clock')
        return now, datetime.fromtimestamp(now, timezone.utc).date().isoformat()

    def _state(self, snapshot, day):
        if not snapshot.exists:
            return {'schema_version': 1, 'policy': deepcopy(self.policy), 'day': day,
                    'spent_microusd': 0, 'held_microusd': 0, 'total_spent_microusd': 0,
                    'active_id': None, 'active_since': None, 'active_state': None, 'blocked_reason': None}
        state = snapshot.to_dict()
        _fields(state, ('schema_version', 'policy', 'day', 'spent_microusd', 'held_microusd',
                       'total_spent_microusd', 'active_id', 'active_since', 'active_state', 'blocked_reason'))
        _require(state['schema_version'] == 1 and state['policy'] == self.policy, 'ledger_policy_mismatch')
        _require(isinstance(state['day'], str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', state['day']) is not None,
                 'ledger_state_invalid')
        _require(day >= state['day'], 'server_day_regression')
        for key in ('spent_microusd', 'held_microusd', 'total_spent_microusd'):
            _integer(state[key], 10**18)
        _require(state['blocked_reason'] in (None, 'reservation_overrun'), 'ledger_state_invalid')
        if state['active_id'] is None:
            _require(state['held_microusd'] == 0 and state['active_since'] is None and state['active_state'] is None,
                     'ledger_state_invalid')
        else:
            _identifier(state['active_id'])
            _require(state['held_microusd'] > 0 and state['active_state'] in ('pending', 'uncertain'), 'ledger_state_invalid')
            _require(type(state['active_since']) in (int, float) and math.isfinite(state['active_since'])
                     and 0 <= state['active_since'] <= 253402300799, 'ledger_state_invalid')
        if day > state['day']:
            state.update(day=day, spent_microusd=0)
        return state

    def _reservation(self, value):
        _json(value)
        _fields(value, ('profile', 'mode', 'request_sha256', 'input_bytes', 'input_token_reserve',
                       'output_token_reserve', 'reserved_microusd', 'per_call_limit_microusd', 'daily_limit_microusd'))
        profile = _profile(value['profile'])
        _require(isinstance(value['mode'], str) and value['mode'] in ('hybrid', 'frontier'))
        _require(isinstance(value['request_sha256'], str) and re.fullmatch(r'[a-f0-9]{64}', value['request_sha256']) is not None)
        _integer(value['input_bytes'], MAX_INPUT_BYTES)
        _integer(value['input_token_reserve'], INPUT_TIER_TOKENS, INPUT_TIER_TOKENS)
        _integer(value['output_token_reserve'], profile.max_completion_tokens + OUTPUT_VARIANCE,
                 profile.max_completion_tokens + OUTPUT_VARIANCE)
        expected = profile.cost_microusd(INPUT_TIER_TOKENS, profile.max_completion_tokens + OUTPUT_VARIANCE)
        _integer(value['reserved_microusd'], expected, expected)
        _require(expected <= self.policy['per_call_limit_microusd'], 'per_call_budget_exceeded')
        for field in ('per_call_limit_microusd', 'daily_limit_microusd'):
            _integer(value[field], self.policy[field], self.policy[field])
        return deepcopy(value)

    def reserve(self, invocation_id, reservation_dict):
        """True only for a new ID with an atomically acquired budget and slot.

        Rejected valid IDs are tombstoned too; callers must never turn a repeated
        ID into another dispatch. Holds do not expire or disappear on restart.
        """
        invocation_id = _identifier(invocation_id)
        reservation = self._reservation(reservation_dict)
        reference = self.invocations.document(invocation_id)
        def apply(transaction):
            now, day = self._now()
            existing = reference.get(transaction=transaction)
            budget_snapshot = self.budget.get(transaction=transaction)
            if existing.exists:
                return False
            state = self._state(budget_snapshot, day)
            amount = reservation['reserved_microusd']
            reason = ('ledger_blocked' if state['blocked_reason'] else 'concurrency_limit' if state['active_id'] else
                      'daily_budget_exceeded' if state['spent_microusd'] + state['held_microusd'] + amount > self.policy['daily_limit_microusd'] else None)
            entry = {'schema_version': 1, 'created_at': now, 'day': day, 'reservation': reservation,
                     'state': 'rejected' if reason else 'pending', 'rejection': reason,
                     'settlement': None, 'settlement_sha256': None, 'settled_at': None}
            if reason is None:
                state.update(held_microusd=state['held_microusd'] + amount, active_id=invocation_id,
                             active_since=now, active_state='pending')
            transaction.create(reference, entry)
            transaction.set(self.budget, state)
            return reason is None
        return self.transactional(apply)(self.client.transaction())

    def _settlement(self, value, reservation):
        raw = _json(value)
        _fields(value, ('profile', 'outcome', 'error_code', 'usage', 'actual_cost_microusd',
                       'cost_basis', 'reserved_microusd', 'over_reservation'))
        profile = _profile(value['profile'])
        _require(value['profile'] == reservation['profile'], 'settlement_profile_mismatch')
        _integer(value['reserved_microusd'], reservation['reserved_microusd'], reservation['reserved_microusd'])
        outcome, error, usage, actual = (value[key] for key in ('outcome', 'error_code', 'usage', 'actual_cost_microusd'))
        _require(isinstance(outcome, str) and outcome in ('completed', 'truncated', 'failed', 'uncertain'))
        _require(error is None or isinstance(error, str) and error in ERROR_CODES)
        _require(type(value['over_reservation']) is bool)
        if actual is None:
            _require(usage is None and outcome == 'uncertain' and error is not None
                     and value['cost_basis'] == 'unknown' and value['over_reservation'] is False)
        else:
            _integer(actual)
            _fields(usage, ('prompt_tokens', 'completion_tokens', 'total_tokens', 'reasoning_tokens'))
            _integer(usage['prompt_tokens'], INPUT_TIER_TOKENS, 1)
            _integer(usage['completion_tokens'], 2_000_000)
            _integer(usage['total_tokens'], 2_000_000 + INPUT_TIER_TOKENS)
            if usage['reasoning_tokens'] is not None:
                _integer(usage['reasoning_tokens'], usage['completion_tokens'])
            _require(usage['total_tokens'] == usage['prompt_tokens'] + usage['completion_tokens'])
            _require(actual == profile.cost_microusd(usage['prompt_tokens'], usage['completion_tokens']), 'settlement_cost_mismatch')
            _require(value['cost_basis'] == 'pinned_list_price_upper_bound')
            _require(value['over_reservation'] is (actual > reservation['reserved_microusd']))
            _require((outcome in ('completed', 'truncated') and error is None) or (outcome == 'failed' and error is not None))
            if outcome in ('completed', 'truncated'):
                _require(usage['completion_tokens'] <= profile.max_completion_tokens + OUTPUT_VARIANCE
                         and usage['reasoning_tokens'] in (None, 0))
        return deepcopy(value), hashlib.sha256(raw).hexdigest()

    def reconcile(self, invocation_id, settlement_dict):
        """Persist a settlement; unknown cost retains both reservation and slot.

        Known actual cost is charged even when output failed. If it exceeds the
        reservation, record the whole charge and block new calls for review.
        """
        reference = self.invocations.document(_identifier(invocation_id))
        _json(settlement_dict)
        def apply(transaction):
            now, day = self._now()
            existing = reference.get(transaction=transaction)
            budget_snapshot = self.budget.get(transaction=transaction)
            _require(existing.exists, 'unknown_invocation')
            entry = existing.to_dict()
            _require(entry['state'] != 'rejected', 'invocation_was_not_reserved')
            settlement, receipt_hash = self._settlement(settlement_dict, entry['reservation'])
            if entry['state'] == 'settled':
                _require(entry['settlement_sha256'] == receipt_hash, 'conflicting_terminal_settlement')
                return True
            if entry['settlement_sha256'] == receipt_hash:
                return True
            state = self._state(budget_snapshot, day)
            amount = entry['reservation']['reserved_microusd']
            _require(state['active_id'] == invocation_id and state['held_microusd'] == amount, 'reservation_state_mismatch')
            actual = settlement['actual_cost_microusd']
            if actual is None:
                entry['state'] = 'uncertain'
                state['active_state'] = 'uncertain'
            else:
                entry['state'] = 'settled'
                entry['settled_at'] = now
                state.update(held_microusd=0, active_id=None, active_since=None, active_state=None,
                             spent_microusd=state['spent_microusd'] + actual,
                             total_spent_microusd=state['total_spent_microusd'] + actual)
                if actual > amount:
                    state['blocked_reason'] = 'reservation_overrun'
            entry.update(settlement=settlement, settlement_sha256=receipt_hash)
            transaction.set(reference, entry)
            transaction.set(self.budget, state)
            return True
        return self.transactional(apply)(self.client.transaction())

    def snapshot(self):
        """Read-only totals; old pending work is uncertain, never available money."""
        now, day = self._now()
        state = self._state(self.budget.get(), day)
        active = state['active_state']
        if active == 'pending' and now - state['active_since'] > self.policy['pending_uncertain_seconds']:
            active = 'uncertain'
        available = max(0, self.policy['daily_limit_microusd'] - state['spent_microusd'] - state['held_microusd'])
        minimum = QwenProfile(max_completion_tokens=1).cost_microusd(INPUT_TIER_TOKENS, 1 + OUTPUT_VARIANCE)
        return {'schema_version': 1, 'day': day, **self.policy, 'spent_microusd': state['spent_microusd'],
                'held_microusd': state['held_microusd'], 'available_microusd': available,
                'total_spent_microusd': state['total_spent_microusd'], 'active_state': active,
                'blocked_reason': state['blocked_reason'],
                'minimum_reservation_microusd': minimum,
                'can_reserve': active is None and state['blocked_reason'] is None
                and min(available, self.policy['per_call_limit_microusd']) >= minimum}
