"""Battle tests for fleet_status: mutant killers, properties, fuzz, adversarial."""
from __future__ import annotations

import os
import random
import threading
import unittest

from fleet_status import (
    STATUS_KEYS,
    STATUS_SCHEMA,
    PROVISIONING_PHASES,
    FakeStatusPublisher,
    FirestoreStatusPublisher,
    StatusPublisher,
    status_document,
    status_reason,
)


BATTLE_ITERATIONS = int(os.environ.get('BATTLE_ITERATIONS', '3000'))

PROVISIONING = (
    'binding_intent', 'binding_ready', 'launch_intent', 'launch_submitted', 'grant_intent',
)
KNOWN_PHASES = ('active', 'blocked', 'idle') + PROVISIONING
REASON_SET = frozenset({
    'unknown', 'disabled', 'ready_or_running', 'provisioning', 'blocked',
    'parked', 'backoff', 'idle_launching',
})


def _ref_status_reason(state, now):
    """Independent reference model for status_reason (must stay in sync with spec)."""
    if not isinstance(state, dict):
        return 'unknown'
    if state.get('slot_enabled') is False:
        return 'disabled'
    phase = state.get('phase')
    if phase == 'active':
        return 'ready_or_running'
    if phase in frozenset(PROVISIONING):
        return 'provisioning'
    if phase == 'blocked':
        return 'blocked'
    if phase == 'idle':
        next_launch_at = state.get('next_launch_at')
        waiting = type(next_launch_at) in (int, float) and now < next_launch_at
        if waiting:
            if state.get('error') == 'provider_quota_exhausted':
                return 'parked'
            failures = state.get('consecutive_failures', 0)
            if type(failures) is int and failures > 0:
                return 'backoff'
            return 'idle_launching'
        return 'idle_launching'
    return 'unknown'


def _ref_status_document(state, now, provider):
    """Independent reference model for status_document."""
    state = state if isinstance(state, dict) else {}
    failures = state.get('consecutive_failures', 0)
    if type(failures) is not int:
        failures = 0
    return {
        'schema': 1,
        'provider': provider,
        'phase': state.get('phase'),
        'reason': _ref_status_reason(state, now),
        'next_launch_at': state.get('next_launch_at'),
        'consecutive_failures': failures,
        'published_at': int(now),
    }


def _rand_state(rng):
    """Build a random (possibly adversarial) controller-slot-like state."""
    kind = rng.randrange(20)
    if kind == 0:
        return None
    if kind == 1:
        return 'not-a-dict'
    if kind == 2:
        return []
    if kind == 3:
        return 42
    if kind == 4:
        return {}
    state = {}
    if rng.random() < 0.3:
        state['slot_enabled'] = rng.choice([True, False, None, 0, 1, 'no'])
    phase_roll = rng.randrange(12)
    if phase_roll < 8:
        state['phase'] = rng.choice(KNOWN_PHASES + ('weird', '', None, 'ACTIVE', ' Idle '))
    elif phase_roll == 8:
        state['phase'] = rng.choice(['\u2603', 'x' * 5000, 'phase\x00null', '   '])
    elif phase_roll == 9:
        state['phase'] = rng.randint(-10, 10)  # hashable non-string
    # consecutive_failures variants
    cf_roll = rng.randrange(10)
    if cf_roll == 0:
        pass  # absent
    elif cf_roll == 1:
        state['consecutive_failures'] = rng.randint(-5, 20)
    elif cf_roll == 2:
        state['consecutive_failures'] = rng.choice([0.0, 1.5, True, False, '3', None, [], {}])
    elif cf_roll == 3:
        state['consecutive_failures'] = float('nan')
    elif cf_roll == 4:
        state['consecutive_failures'] = float('inf')
    elif cf_roll == 5:
        state['consecutive_failures'] = -10 ** 12
    else:
        state['consecutive_failures'] = rng.randint(0, 5)
    # next_launch_at
    nla_roll = rng.randrange(8)
    if nla_roll == 0:
        pass
    elif nla_roll == 1:
        state['next_launch_at'] = rng.uniform(-1e6, 1e6)
    elif nla_roll == 2:
        state['next_launch_at'] = rng.randint(-1000, 10_000)
    elif nla_roll == 3:
        state['next_launch_at'] = rng.choice([None, 'soon', True, [], float('nan'), float('inf')])
    else:
        state['next_launch_at'] = 1000 + rng.randint(-200, 500)
    if rng.random() < 0.2:
        state['error'] = rng.choice([
            'provider_quota_exhausted', 'other', '', None, 1, '\u0000boom',
        ])
    # secret / deep / huge junk that must never leak into the document
    if rng.random() < 0.25:
        state['grant'] = 'secret-' + ('\u2603' * rng.randint(0, 20))
        state['token'] = 'x' * rng.randint(0, 200)
        state['nested'] = {'a': {'b': {'c': list(range(rng.randint(0, 50)))}}}
        state['huge'] = 'Z' * rng.randint(0, 10_000)
    return state


def _rand_now(rng):
    choice = rng.randrange(8)
    if choice == 0:
        return float('nan')
    if choice == 1:
        return float('inf')
    if choice == 2:
        return float('-inf')
    if choice == 3:
        return -10 ** 15
    if choice == 4:
        return 10 ** 15
    if choice == 5:
        return rng.uniform(-1e4, 1e4)
    return rng.randint(0, 20_000)


def _rand_provider(rng):
    return rng.choice([
        'grok', 'claude', 'codex', '', None, 0, True,
        '\u2603provider', 'p' * 200, {'x': 1},
    ])


class MutantKillerTests(unittest.TestCase):
    """Pin behaviours the baseline mutation run left untested."""

    def test_missing_consecutive_failures_while_waiting_is_idle_launching_not_backoff(self):
        # Kills line 49 int 0->1 (default becomes positive => false backoff).
        # Line 49 int 0->-1 is equivalent: -1 > 0 is False, same branch as 0.
        now = 1000
        state = {'phase': 'idle', 'next_launch_at': now + 60}
        self.assertNotIn('consecutive_failures', state)
        self.assertEqual(status_reason(state, now), 'idle_launching')
        doc = status_document(state, now, 'grok')
        self.assertEqual(doc['consecutive_failures'], 0)
        self.assertEqual(doc['reason'], 'idle_launching')

    def test_status_document_default_failures_is_zero_when_key_absent(self):
        # Kills line 60 int 0->+/-1.
        doc = status_document({'phase': 'active'}, 1000, 'grok')
        self.assertEqual(doc['consecutive_failures'], 0)

    def test_non_int_consecutive_failures_coerced_to_zero_in_document(self):
        # Kills line 61 if-cond->False and line 62 int 0->+/-1.
        for bad in ('3', 1.5, True, False, None, [], {}, 2.0, object()):
            with self.subTest(bad=bad):
                doc = status_document(
                    {'phase': 'idle', 'consecutive_failures': bad}, 1000, 'grok',
                )
                self.assertEqual(doc['consecutive_failures'], 0)
                self.assertIs(type(doc['consecutive_failures']), int)

    def test_base_status_publisher_raise_not_implemented(self):
        # Kills line 81 remove raise.
        with self.assertRaises(NotImplementedError):
            StatusPublisher().publish('grok', {'schema': 1})

    def test_fake_publisher_failures_counter_starts_at_zero_and_increments_by_one(self):
        # Kills line 90 int 0->+/-1 and line 95 int 1->0 / 1->2.
        pub = FakeStatusPublisher()
        self.assertEqual(pub.failures, 0)
        pub.fail_next = True
        with self.assertRaises(RuntimeError):
            pub.publish('grok', {'schema': 1})
        self.assertEqual(pub.failures, 1)
        self.assertEqual(pub.published, [])
        pub.publish('claude', {'schema': 1, 'provider': 'claude'})
        self.assertEqual(pub.failures, 1)
        self.assertEqual(len(pub.published), 1)


class InvariantAndFuzzTests(unittest.TestCase):
    """Seeded property / fuzz suite against the independent reference model."""

    def test_property_matches_reference_model(self):
        seed = int.from_bytes(os.urandom(8), 'big')
        rng = random.Random(seed)
        n = BATTLE_ITERATIONS
        for i in range(n):
            state = _rand_state(rng)
            now = _rand_now(rng)
            provider = _rand_provider(rng)
            try:
                got_reason = status_reason(state, now)
                exp_reason = _ref_status_reason(state, now)
                self.assertEqual(
                    got_reason, exp_reason,
                    f'seed={seed} i={i} reason state={state!r} now={now!r}',
                )
                self.assertIn(got_reason, REASON_SET, f'seed={seed} i={i}')

                # status_document: int(now) may raise for NaN/inf — mirror that.
                try:
                    exp_doc = _ref_status_document(state, now, provider)
                except (ValueError, OverflowError) as exc:
                    with self.assertRaises(type(exc), msg=f'seed={seed} i={i}'):
                        status_document(state, now, provider)
                    continue
                got_doc = status_document(state, now, provider)
                self.assertEqual(
                    got_doc, exp_doc,
                    f'seed={seed} i={i} doc state={state!r} now={now!r} provider={provider!r}',
                )
                self.assertEqual(set(got_doc), STATUS_KEYS, f'seed={seed} i={i}')
                self.assertEqual(got_doc['schema'], STATUS_SCHEMA, f'seed={seed} i={i}')
                for forbidden in ('grant', 'grant_sha256', 'intent', 'token', 'receipt',
                                  'error', 'execution', 'nested', 'huge'):
                    self.assertNotIn(forbidden, got_doc, f'seed={seed} i={i}')
            except AssertionError:
                raise
            except Exception as exc:
                self.fail(f'seed={seed} i={i} unexpected {type(exc).__name__}: {exc}')

    def test_adversarial_inputs(self):
        seed = 0xBA771E01 ^ BATTLE_ITERATIONS
        rng = random.Random(seed)
        cases = [
            (None, 0, 'x'),
            ('str', 0, 'x'),
            (object(), 0, 'x'),
            ({'phase': 'idle', 'consecutive_failures': True}, 0, 'g'),  # bool is int subclass but type() is bool
            ({'phase': 'idle', 'consecutive_failures': False}, 0, 'g'),
            ({'phase': 'idle', 'next_launch_at': float('nan')}, 0, 'g'),
            ({'phase': 'idle', 'next_launch_at': float('inf'), 'consecutive_failures': 3}, 0, 'g'),
            ({'phase': 'idle', 'next_launch_at': 10, 'consecutive_failures': -1}, 0, 'g'),
            ({'phase': 'idle', 'next_launch_at': 10, 'consecutive_failures': 0}, 0, 'g'),
            ({'phase': 'idle', 'next_launch_at': 10}, 0, 'g'),
            ({'slot_enabled': False, 'phase': 'active'}, 0, 'g'),
            ({'phase': 'active', 'grant': 'leak', 'token': 't'}, 0, 'g'),
            ({'phase': '\u2603'}, 0, '\u2603'),
            ({'phase': 'idle', 'error': 'provider_quota_exhausted',
              'next_launch_at': 9999, 'consecutive_failures': 9}, 0, 'g'),
            ({}, float('nan'), 'g'),
            ({'phase': 'idle'}, float('inf'), 'g'),
            ({'phase': 'idle', 'consecutive_failures': []}, 1, 'g'),
            ({'phase': 'idle', 'consecutive_failures': {'a': 1}}, 1, 'g'),
            ({'phase': 'idle', 'next_launch_at': True}, 0, 'g'),
            ({'phase': 'idle', 'next_launch_at': '100'}, 0, 'g'),
        ]
        for state, now, provider in cases:
            with self.subTest(state=state, now=now, provider=provider):
                try:
                    exp = _ref_status_reason(state, now)
                    self.assertEqual(
                        status_reason(state, now), exp,
                        f'seed={seed} state={state!r} now={now!r}',
                    )
                    try:
                        exp_doc = _ref_status_document(state, now, provider)
                    except (ValueError, OverflowError):
                        with self.assertRaises((ValueError, OverflowError)):
                            status_document(state, now, provider)
                        continue
                    got = status_document(state, now, provider)
                    self.assertEqual(got, exp_doc, f'seed={seed}')
                    self.assertEqual(set(got), STATUS_KEYS)
                except AssertionError:
                    raise
                except Exception as exc:
                    self.fail(f'seed={seed} unexpected {exc}')

        # Extra random adversarial batch scaled by iterations (capped for speed).
        extra = min(max(BATTLE_ITERATIONS // 10, 50), 5000)
        for i in range(extra):
            state = _rand_state(rng)
            now = _rand_now(rng)
            provider = _rand_provider(rng)
            try:
                self.assertEqual(
                    status_reason(state, now), _ref_status_reason(state, now),
                    f'seed={seed} adv_i={i}',
                )
            except AssertionError:
                raise
            except Exception as exc:
                self.fail(f'seed={seed} adv_i={i} {exc}')

    def test_document_key_whitelist_invariant(self):
        seed = 0xC0FFEE01
        rng = random.Random(seed)
        n = min(BATTLE_ITERATIONS, 20_000)
        for i in range(n):
            state = {
                'phase': rng.choice(KNOWN_PHASES),
                'consecutive_failures': rng.randint(0, 3),
                'next_launch_at': rng.randint(0, 5000),
                'grant': 'secret',
                'token': 'tok',
                'error': 'e',
                'extra_' + str(i): i,
            }
            doc = status_document(state, 1000, 'grok')
            self.assertEqual(set(doc), STATUS_KEYS, f'seed={seed} i={i}')
            self.assertEqual(doc['schema'], 1, f'seed={seed} i={i}')

    def test_bool_is_not_int_for_failures_gate(self):
        # type(True) is bool, not int — must not count as backoff / must coerce to 0 in doc.
        now = 1000
        waiting = {'phase': 'idle', 'next_launch_at': now + 10, 'consecutive_failures': True}
        self.assertEqual(status_reason(waiting, now), 'idle_launching')
        self.assertEqual(status_document(waiting, now, 'g')['consecutive_failures'], 0)

    def test_unhashable_phase_should_be_unknown_not_typeerror(self):
        # Battle-test finding, fixed: an unhashable phase (dict/list/set) used to
        # raise TypeError from `phase in PROVISIONING_PHASES`; any non-string
        # phase now reads as 'unknown'.
        for phase in ({'nested': True}, ['launch_intent'], set(), 7, None, b'active'):
            with self.subTest(phase=phase):
                self.assertEqual(status_reason({'phase': phase}, 0), 'unknown')

    def test_provisioning_phases_frozenset_matches(self):
        self.assertEqual(PROVISIONING_PHASES, frozenset(PROVISIONING))
        for phase in PROVISIONING:
            self.assertEqual(status_reason({'phase': phase}, 0), 'provisioning')


class PublisherConcurrencyTests(unittest.TestCase):
    def test_fake_publisher_random_operation_sequences(self):
        seed = 0xFAB5E9 ^ BATTLE_ITERATIONS
        rng = random.Random(seed)
        n = min(max(BATTLE_ITERATIONS // 5, 100), 10_000)
        for i in range(n):
            pub = FakeStatusPublisher()
            expect_failures = 0
            expect_published = 0
            ops = rng.randint(1, 30)
            for _ in range(ops):
                op = rng.randrange(4)
                if op == 0:
                    pub.fail_next = True
                elif op == 1:
                    pub.fail_next = False
                elif op == 2:
                    if pub.fail_next:
                        with self.assertRaises(RuntimeError, msg=f'seed={seed} i={i}'):
                            pub.publish('p', {'schema': 1})
                        expect_failures += 1
                        self.assertFalse(pub.fail_next)
                    else:
                        pub.publish('p', {'schema': 1})
                        expect_published += 1
                else:
                    self.assertEqual(pub.failures, expect_failures, f'seed={seed} i={i}')
                    self.assertEqual(len(pub.published), expect_published, f'seed={seed} i={i}')
            self.assertEqual(pub.failures, expect_failures, f'seed={seed} i={i}')
            self.assertEqual(len(pub.published), expect_published, f'seed={seed} i={i}')

    def test_fake_publisher_threaded_publishes(self):
        seed = 0x7DEAD5
        rng = random.Random(seed)
        pub = FakeStatusPublisher()
        lock = threading.Lock()
        errors = []
        n_threads = 8
        per_thread = min(max(BATTLE_ITERATIONS // 20, 20), 2000)

        def worker(tid):
            for j in range(per_thread):
                doc = {'schema': 1, 'provider': f't{tid}', 'i': j}
                try:
                    with lock:
                        pub.publish(f't{tid}', doc)
                except Exception as exc:
                    errors.append((tid, j, exc))

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [], f'seed={seed} errors={errors!r}')
        self.assertEqual(len(pub.published), n_threads * per_thread, f'seed={seed}')
        self.assertEqual(pub.failures, 0, f'seed={seed}')

    def test_firestore_stub_message(self):
        with self.assertRaises(NotImplementedError) as caught:
            FirestoreStatusPublisher().publish('grok', status_document({'phase': 'idle'}, 1, 'grok'))
        self.assertIn('runcrew_fleet_status', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
