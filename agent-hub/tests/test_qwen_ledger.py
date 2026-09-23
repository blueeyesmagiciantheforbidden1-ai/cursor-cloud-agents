"""Synthetic Firestore transactions and provider receipts; never connects online."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import json
import threading
import unittest
from unittest.mock import patch

from agent_hub import qwen
from agent_hub.qwen_ledger import FirestoreQwenLedger, QwenLedgerError


class Conflict(Exception):
    pass


class Snapshot:
    def __init__(self, data):
        self.exists = data is not None
        self.data = deepcopy(data)

    def to_dict(self):
        return deepcopy(self.data)


class Reference:
    def __init__(self, client, path):
        self.client, self.path = client, path

    def get(self, transaction=None):
        if transaction:
            return transaction.read(self)
        with self.client.lock:
            return Snapshot(self.client.documents.get(self.path))


class Collection:
    def __init__(self, client, name):
        self.client, self.name = client, name

    def document(self, name):
        return Reference(self.client, self.name + '/' + name)


class Transaction:
    def __init__(self, client):
        self.client, self.reads, self.writes = client, {}, []

    def reset(self):
        self.reads, self.writes = {}, []

    def read(self, reference):
        if self.writes:
            raise AssertionError('Firestore cannot read after writing')
        with self.client.lock:
            self.reads[reference.path] = self.client.versions.get(reference.path, 0)
            result = Snapshot(self.client.documents.get(reference.path))
            barrier = None
            if reference.path.endswith('_budget/global') and self.client.gated_reads:
                self.client.gated_reads -= 1
                barrier = self.client.read_barrier
        if barrier:
            barrier.wait(timeout=10)
        return result

    def create(self, reference, data):
        self.writes.append(('create', reference.path, deepcopy(data)))

    def set(self, reference, data):
        self.writes.append(('set', reference.path, deepcopy(data)))

    def commit(self):
        with self.client.lock:
            if self.client.fail_before_commit:
                self.client.fail_before_commit = False
                raise OSError('synthetic unavailable before commit')
            for path, version in self.reads.items():
                if self.client.versions.get(path, 0) != version:
                    self.client.conflicts += 1
                    raise Conflict()
            for operation, path, _ in self.writes:
                if operation == 'create' and path in self.client.documents:
                    raise Conflict()
            for _, path, data in self.writes:
                self.client.documents[path] = data
                self.client.versions[path] = self.client.versions.get(path, 0) + 1
            if self.client.fail_after_commit:
                self.client.fail_after_commit = False
                raise OSError('synthetic unavailable after durable commit')


def transactional(function):
    def execute(transaction):
        for _ in range(100):
            transaction.reset()
            result = function(transaction)
            try:
                transaction.commit()
                return result
            except Conflict:
                continue
        raise AssertionError('synthetic retry limit')
    return execute


class Firestore:
    def __init__(self):
        self.documents, self.versions = {}, {}
        self.lock = threading.RLock()
        self.conflicts, self.gated_reads = 0, 0
        self.read_barrier = None
        self.fail_before_commit = self.fail_after_commit = False

    def collection(self, name):
        return Collection(self, name)

    def transaction(self):
        return Transaction(self)


class QwenLedgerTests(unittest.TestCase):
    def setUp(self):
        network = patch('socket.socket.connect', side_effect=AssertionError('Tests must remain offline'))
        network.start()
        self.addCleanup(network.stop)
        self.client = Firestore()
        self.now = datetime(2026, 9, 20, 23, 59, tzinfo=timezone.utc).timestamp()
        self.ledger = self.make_ledger()
        self.profile = qwen.QwenProfile(max_completion_tokens=128)

    def make_ledger(self, **limits):
        return FirestoreQwenLedger(self.client, clock=lambda: self.now, transactional=transactional, **limits)

    def reservation(self, *, profile=None):
        profile = profile or self.profile
        return {'profile': profile.public(), 'mode': 'hybrid', 'request_sha256': 'a' * 64, 'input_bytes': 16,
                'input_token_reserve': 32768, 'output_token_reserve': profile.max_completion_tokens + 10,
                'reserved_microusd': profile.cost_microusd(32768, profile.max_completion_tokens + 10),
                'per_call_limit_microusd': profile.per_call_limit_microusd,
                'daily_limit_microusd': profile.daily_limit_microusd}

    def settlement(self, *, prompt=10, completion=20, unknown=False, failed=False, profile=None):
        profile = profile or self.profile
        amount = self.reservation(profile=profile)['reserved_microusd']
        actual = None if unknown else profile.cost_microusd(prompt, completion)
        return {'profile': profile.public(), 'outcome': 'uncertain' if unknown else 'failed' if failed else 'completed',
                'error_code': 'provider_transport_error' if unknown else 'output_token_limit_exceeded' if failed else None,
                'usage': None if unknown else {'prompt_tokens': prompt, 'completion_tokens': completion,
                                                'total_tokens': prompt + completion, 'reasoning_tokens': None},
                'actual_cost_microusd': actual, 'cost_basis': 'unknown' if unknown else 'pinned_list_price_upper_bound',
                'reserved_microusd': amount, 'over_reservation': False if unknown else actual > amount}

    def test_readonly_snapshot_and_schema_store_no_prompt_key_or_response(self):
        initial = self.ledger.snapshot()
        self.assertEqual(self.client.documents, {})
        self.assertEqual((initial['available_microusd'], initial['concurrency']), (100000, 1))
        self.assertTrue(initial['can_reserve'])
        self.assertTrue(self.ledger.reserve('synthetic-one', self.reservation()))
        self.assertTrue(self.ledger.reconcile('synthetic-one', self.settlement()))
        public = json.dumps(self.ledger.snapshot())
        self.assertNotIn('synthetic-one', public)
        self.assertNotIn('a' * 64, public)
        self.assertEqual(self.ledger.snapshot()['spent_microusd'], 3)
        for field in ('prompt', 'api_key', 'response_text', 'day'):
            value = self.reservation()
            value[field] = 'SYNTHETIC-SECRET'
            with self.subTest(field=field), self.assertRaises(QwenLedgerError):
                self.ledger.reserve('secret-' + field, value)
        self.assertNotIn('SYNTHETIC-SECRET', json.dumps(self.client.documents))

    def test_duplicate_ids_never_redispatch_terminal_receipts_are_idempotent(self):
        reserve, settle = self.reservation(), self.settlement()
        self.assertTrue(self.ledger.reserve('one', reserve))
        self.assertFalse(self.ledger.reserve('one', reserve))
        self.assertTrue(self.ledger.reconcile('one', settle))
        before = deepcopy(self.client.documents)
        self.now += 5
        self.assertTrue(self.make_ledger().reconcile('one', settle))
        self.assertEqual(self.client.documents, before)
        self.assertFalse(self.make_ledger().reserve('one', reserve))
        with self.assertRaisesRegex(QwenLedgerError, 'conflicting_terminal_settlement'):
            self.ledger.reconcile('one', self.settlement(completion=21))
        self.assertEqual(self.client.documents, before)

    def test_real_transaction_conflicts_allow_only_one_concurrent_admission(self):
        count = 8
        self.client.read_barrier = threading.Barrier(count)
        self.client.gated_reads = count
        with ThreadPoolExecutor(max_workers=count) as pool:
            admitted = list(pool.map(lambda i: self.make_ledger().reserve('race-' + str(i), self.reservation()), range(count)))
        self.assertEqual(sum(admitted), 1)
        self.assertGreater(self.client.conflicts, 0)
        status = self.ledger.snapshot()
        self.assertEqual((status['held_microusd'], status['spent_microusd']), (1001, 0))
        self.assertFalse(status['can_reserve'])
        winner = 'race-' + str(admitted.index(True))
        self.ledger.reconcile(winner, self.settlement())
        for i in range(count):
            self.assertFalse(self.ledger.reserve('race-' + str(i), self.reservation()))

    def test_daily_budget_atomic_and_denied_ids_remain_tombstoned_next_day(self):
        profile = qwen.QwenProfile(max_completion_tokens=128, daily_limit_microusd=2002, per_call_limit_microusd=1001)
        ledger = self.make_ledger(daily_limit_microusd=2002, per_call_limit_microusd=1001)
        for i in range(2):
            self.assertTrue(ledger.reserve(str(i), self.reservation(profile=profile)))
            ledger.reconcile(str(i), self.settlement(prompt=32768, completion=138, profile=profile))
        self.assertEqual(ledger.snapshot()['spent_microusd'], 2002)
        self.assertFalse(ledger.reserve('denied', self.reservation(profile=profile)))
        self.now += 86400
        self.assertEqual(ledger.snapshot()['available_microusd'], 2002)
        self.assertFalse(ledger.reserve('denied', self.reservation(profile=profile)))
        self.assertTrue(ledger.reserve('next-day', self.reservation(profile=profile)))

    def test_uncertain_holds_survive_restart_expiration_and_days_then_charge_settlement_day(self):
        self.ledger.reserve('one', self.reservation())
        unknown = self.settlement(unknown=True)
        self.assertTrue(self.ledger.reconcile('one', unknown))
        before = deepcopy(self.client.documents)
        self.assertTrue(self.ledger.reconcile('one', unknown))
        self.assertEqual(self.client.documents, before)
        self.now += 86400 * 3
        restarted = self.make_ledger()
        status = restarted.snapshot()
        self.assertEqual((status['active_state'], status['held_microusd'], status['available_microusd']), ('uncertain', 1001, 98999))
        self.assertEqual(self.client.documents, before)  # snapshot cannot clear a hold
        self.assertFalse(restarted.reserve('still-blocked', self.reservation()))
        self.assertTrue(restarted.reconcile('one', self.settlement()))
        status = restarted.snapshot()
        self.assertEqual((status['day'], status['spent_microusd'], status['total_spent_microusd']), ('2026-09-23', 3, 3))
        self.assertEqual(status['held_microusd'], 0)
        self.assertTrue(restarted.reserve('resolved', self.reservation()))

    def test_abandoned_pending_turns_uncertain_without_releasing_slot(self):
        self.ledger.reserve('crash', self.reservation())
        self.now += 121
        restarted = self.make_ledger()
        self.assertEqual(restarted.snapshot()['active_state'], 'uncertain')
        self.assertEqual(restarted.snapshot()['held_microusd'], 1001)
        self.assertFalse(restarted.reserve('later', self.reservation()))
        self.assertFalse(restarted.reserve('crash', self.reservation()))

    def test_rollback_and_ambiguous_commit_never_authorize_a_second_dispatch(self):
        self.client.fail_before_commit = True
        with self.assertRaises(OSError):
            self.ledger.reserve('rollback', self.reservation())
        self.assertEqual(self.client.documents, {})
        self.assertTrue(self.ledger.reserve('rollback', self.reservation()))
        self.ledger.reconcile('rollback', self.settlement())
        self.client.fail_after_commit = True
        with self.assertRaises(OSError):
            self.ledger.reserve('ambiguous', self.reservation())
        self.assertFalse(self.make_ledger().reserve('ambiguous', self.reservation()))
        self.assertEqual(self.ledger.snapshot()['held_microusd'], 1001)

    def test_ambiguous_settlement_commit_is_idempotently_recoverable(self):
        self.ledger.reserve('one', self.reservation())
        self.client.fail_after_commit = True
        with self.assertRaises(OSError):
            self.ledger.reconcile('one', self.settlement())
        self.assertTrue(self.make_ledger().reconcile('one', self.settlement()))
        self.assertEqual(self.ledger.snapshot()['total_spent_microusd'], 3)

    def test_known_overrun_records_full_charge_and_blocks_future_calls_across_days(self):
        self.ledger.reserve('overrun', self.reservation())
        settlement = self.settlement(completion=100000, failed=True)
        self.assertTrue(settlement['over_reservation'])
        self.assertTrue(self.ledger.reconcile('overrun', settlement))
        status = self.ledger.snapshot()
        self.assertEqual(status['spent_microusd'], 13001)
        self.assertEqual(status['blocked_reason'], 'reservation_overrun')
        self.assertFalse(self.ledger.reserve('blocked', self.reservation()))
        self.now += 86400
        self.assertFalse(self.ledger.reserve('still-blocked', self.reservation()))
        self.assertEqual(self.ledger.snapshot()['total_spent_microusd'], 13001)

    def test_mutable_request_cannot_raise_budget_change_profile_or_lie_about_cost(self):
        changes = ({'daily_limit_microusd': 100001}, {'per_call_limit_microusd': 10001},
                   {'reserved_microusd': 1}, {'input_token_reserve': 20}, {'output_token_reserve': 128},
                   {'request_sha256': 'not-a-hash'}, {'input_bytes': 8193}, {'input_bytes': True})
        for i, change in enumerate(changes):
            value = self.reservation()
            value.update(change)
            with self.subTest(change=change), self.assertRaises(QwenLedgerError):
                self.ledger.reserve(str(i), value)
        profile = self.reservation()
        profile['profile']['model'] = 'unapproved-model'
        with self.assertRaises(QwenLedgerError):
            self.ledger.reserve('bad-model', profile)
        with self.assertRaises(QwenLedgerError):
            self.ledger.reserve('oversized', {'garbage': 'x' * 9000})
        self.assertEqual(self.client.documents, {})
        self.ledger.reserve('real', self.reservation())
        bad = self.settlement()
        bad['actual_cost_microusd'] = 0
        with self.assertRaisesRegex(QwenLedgerError, 'settlement_cost_mismatch'):
            self.ledger.reconcile('real', bad)
        self.assertEqual(self.ledger.snapshot()['held_microusd'], 1001)

    def test_fixed_constructor_policy_cannot_be_changed_by_second_controller(self):
        for change in ({'daily_limit_microusd': 100001}, {'per_call_limit_microusd': 10001},
                       {'daily_limit_microusd': True}, {'per_call_limit_microusd': 0}):
            with self.subTest(change=change), self.assertRaises(QwenLedgerError):
                self.make_ledger(**change)
        self.ledger.reserve('one', self.reservation())
        changed = self.make_ledger(daily_limit_microusd=2000, per_call_limit_microusd=1001)
        with self.assertRaisesRegex(QwenLedgerError, 'ledger_policy_mismatch'):
            changed.snapshot()
        self.now -= 86400
        with self.assertRaisesRegex(QwenLedgerError, 'server_day_regression'):
            self.ledger.snapshot()

    def test_unknown_or_rejected_invocation_cannot_settle_and_unknown_cost_cannot_clear_hold(self):
        with self.assertRaisesRegex(QwenLedgerError, 'unknown_invocation'):
            self.ledger.reconcile('missing', self.settlement())
        self.ledger.reserve('one', self.reservation())
        self.assertFalse(self.ledger.reserve('denied', self.reservation()))
        with self.assertRaisesRegex(QwenLedgerError, 'invocation_was_not_reserved'):
            self.ledger.reconcile('denied', self.settlement())
        invalid = self.settlement(unknown=True)
        invalid['outcome'] = 'completed'
        with self.assertRaises(QwenLedgerError):
            self.ledger.reconcile('one', invalid)
        self.assertEqual(self.ledger.snapshot()['held_microusd'], 1001)

    def test_low_budget_snapshot_does_not_claim_possible_admission(self):
        tiny = self.make_ledger(daily_limit_microusd=100, per_call_limit_microusd=100)
        self.assertFalse(tiny.snapshot()['can_reserve'])

    def test_actual_adapter_callbacks_match_and_duplicate_never_calls_provider_again(self):
        adapter = qwen.QwenAdapter('sk-SYNTHETIC-NOT-A-REAL-KEY', reserve=self.ledger.reserve,
                                   reconcile=self.ledger.reconcile, profile=self.profile)
        response = {'model': qwen.MODEL, 'choices': [{'message': {'role': 'assistant', 'content': 'SYNTHETIC-ANSWER'},
                                                      'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 10, 'completion_tokens': 20, 'total_tokens': 30}}
        with patch.object(qwen, '_http_post', return_value=json.dumps(response).encode()) as post:
            result = adapter.complete([{'role': 'user', 'content': 'SYNTHETIC-PROMPT'}], invocation_id='adapter', mode='hybrid')
            self.assertEqual(result['status'], 'completed')
            with self.assertRaises(qwen.QwenError):
                adapter.complete([{'role': 'user', 'content': 'SYNTHETIC-PROMPT'}], invocation_id='adapter', mode='hybrid')
        post.assert_called_once()
        persisted = json.dumps(self.client.documents)
        for secret in ('SYNTHETIC-PROMPT', 'SYNTHETIC-ANSWER', 'sk-SYNTHETIC'):
            self.assertNotIn(secret, persisted)
        self.assertEqual(self.ledger.snapshot()['spent_microusd'], 3)


if __name__ == '__main__':
    unittest.main()
