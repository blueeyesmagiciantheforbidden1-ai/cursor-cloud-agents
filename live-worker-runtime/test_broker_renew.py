"""Unit tests for the shared broker-renew tolerance helper."""
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import broker_renew
import dynamic_broker
import provider_errors
from agent_hub.cloud_credential_broker import BrokerError, Conflict, Lease, MutationUncertain, ProfileConfig
from agent_hub.credential_broker_service import BoundaryError, BrokerHTTPClient
import agent_hub.credential_broker_service as broker_service


class CodeError(provider_errors.ProviderCodeError, RuntimeError):
    pass


class Clock:
    def __init__(self, now=0):
        self.now = now

    def __call__(self):
        return self.now


def broker(effect):
    def renew(lease):
        effect()
    return type('Broker', (), {'renew': staticmethod(renew)})()


class BrokerRenewTests(unittest.TestCase):
    def clock_for(self, provider='grok', anchor=0, now=0):
        clock = Clock(now)
        return broker_renew.LeaseClock(CodeError, provider, anchor, clock=clock), clock

    def test_classification(self):
        for code in ('broker_http_outcome_uncertain', 'broker_acknowledgement_invalid'):
            self.assertTrue(broker_renew.transient(MutationUncertain(code)))
        for code in broker_renew.TRANSIENT_BOUNDARY_CODES:
            self.assertTrue(broker_renew.transient(BoundaryError(code)))
        self.assertFalse(broker_renew.transient(Conflict('broker_operation_rejected')))
        self.assertFalse(broker_renew.transient(BrokerError('broker_request_denied')))
        self.assertFalse(broker_renew.transient(BrokerError('exact_secret_version_required')))
        for code in ('credential_fence_lost', 'request_invalid', 'message_too_large'):
            self.assertFalse(broker_renew.transient(BoundaryError(code)))
        self.assertFalse(broker_renew.transient(RuntimeError('transport_failed')))

    def test_window_edge_tolerates_180_and_fails_above(self):
        lease_clock, clock = self.clock_for(anchor=0, now=0)

        def fail():
            raise MutationUncertain('broker_http_outcome_uncertain')

        clock.now = broker_renew.TOLERANCE_SECONDS
        self.assertIs(lease_clock.renew(broker(fail), None), False)
        self.assertEqual(lease_clock.renewed_at, 0)
        self.assertTrue(lease_clock.degraded)
        clock.now = broker_renew.TOLERANCE_SECONDS + 1
        with self.assertRaises(CodeError) as caught:
            lease_clock.renew(broker(fail), None)
        self.assertEqual(str(caught.exception), 'grok_broker_renew_failed')
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertNotIn('broker_http', str(caught.exception))

    def test_attempt_duration_counts_toward_the_window(self):
        lease_clock, clock = self.clock_for(anchor=0, now=0)

        def slow():
            clock.now = broker_renew.TOLERANCE_SECONDS + 1
            raise BoundaryError('transport_failed')

        with self.assertRaisesRegex(CodeError, '^grok_broker_renew_failed$'):
            lease_clock.renew(broker(slow), None)
        lease_clock, clock = self.clock_for(anchor=0, now=0)

        def inside():
            clock.now = broker_renew.TOLERANCE_SECONDS
            raise BoundaryError('transport_limit')

        self.assertIs(lease_clock.renew(broker(inside), None), False)

    def test_success_anchors_at_attempt_start_and_clears_failures(self):
        lease_clock, clock = self.clock_for(anchor=0, now=10)
        lease_clock.failures = 2

        def renew():
            clock.now = 40

        self.assertIs(lease_clock.renew(broker(renew), None), True)
        self.assertEqual(lease_clock.renewed_at, 10)
        self.assertEqual(lease_clock.failures, 0)
        self.assertFalse(lease_clock.degraded)

    def test_rejections_are_immediate_and_do_not_move_the_anchor(self):
        for error in (Conflict('broker_operation_rejected'), BrokerError('broker_request_denied'),
                      BoundaryError('credential_fence_lost'), BoundaryError('request_invalid'),
                      BoundaryError('message_too_large')):
            with self.subTest(error=type(error).__name__ + ':' + str(error)):
                lease_clock, clock = self.clock_for(provider='copilot', anchor=5, now=5)

                def fail(error=error):
                    raise error

                with self.assertRaises(CodeError) as caught:
                    lease_clock.renew(broker(fail), None)
                self.assertEqual(str(caught.exception), 'copilot_broker_renew_rejected')
                self.assertIsNone(caught.exception.__cause__)
                self.assertTrue(caught.exception.__suppress_context__)
                self.assertNotIn(str(error), str(caught.exception))
                self.assertEqual(lease_clock.renewed_at, 5)
                self.assertEqual(lease_clock.failures, 0)

    def test_strict_fails_a_transient_inside_the_window(self):
        lease_clock, clock = self.clock_for(provider='claude', anchor=0, now=1)

        def fail():
            raise MutationUncertain('broker_acknowledgement_invalid')

        with self.assertRaisesRegex(CodeError, '^claude_broker_renew_failed$'):
            lease_clock.renew(broker(fail), None, strict=True)
        self.assertEqual(lease_clock.renewed_at, 0)

    def test_non_broker_and_base_exception_propagate(self):
        lease_clock, clock = self.clock_for()

        def bug():
            raise RuntimeError('adapter bug')

        with self.assertRaises(RuntimeError) as caught:
            lease_clock.renew(broker(bug), None)
        self.assertIn('adapter bug', str(caught.exception))
        self.assertEqual(lease_clock.failures, 0)

        def stop():
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            lease_clock.renew(broker(stop), None)

    def test_check_backstop_and_invalid_construction(self):
        lease_clock, clock = self.clock_for(provider='cursor', anchor=0, now=broker_renew.TOLERANCE_SECONDS)
        lease_clock.check()
        clock.now = broker_renew.TOLERANCE_SECONDS + 1
        with self.assertRaisesRegex(CodeError, '^cursor_broker_renew_failed$'):
            lease_clock.check()
        with self.assertRaises(TypeError):
            broker_renew.LeaseClock(CodeError, 'other', 0)
        with self.assertRaises(TypeError):
            broker_renew.LeaseClock(RuntimeError, 'grok', 0)
        with self.assertRaises(TypeError):
            broker_renew.LeaseClock(CodeError, 'grok', True)

    def test_acquire_anchor_registry_and_prepare_fallback(self):
        lease_id = 'ab' * 16
        started = 12.5
        with self.subTest('recorded'):
            broker_renew._acquire_started.pop(lease_id, None)
            broker_renew.record_acquire_start(lease_id, started)
            clock = Clock(99)
            lease = type('Lease', (), {'lease_id': lease_id})()
            lease_clock = broker_renew.LeaseClock.for_lease(lease, CodeError, 'codex', clock=clock)
            self.assertEqual(lease_clock.renewed_at, started)
            self.assertIs(lease_clock.clock, clock)
        with self.subTest('fallback'):
            clock = Clock(40)
            lease_clock = broker_renew.LeaseClock.for_lease(object(), CodeError, 'codex', clock=clock)
            self.assertEqual(lease_clock.renewed_at, 40)
        broker_renew._acquire_started.pop(lease_id, None)

    def test_next_due(self):
        clock = Clock(100)
        self.assertEqual(broker_renew.next_due(False, 20, clock), 100 + broker_renew.RETRY_SECONDS)
        self.assertEqual(broker_renew.next_due(True, 20, clock), 120)
        self.assertEqual(broker_renew.next_due(None, 8, clock), 108)
        self.assertEqual(broker_renew.next_due(False, 25, clock), 110)

    def test_lease_seconds_matches_policy_default(self):
        default = dynamic_broker.Policy.__dataclass_fields__['lease_seconds'].default
        self.assertEqual(broker_renew.LEASE_SECONDS, default)
        self.assertEqual(broker_renew.TOLERANCE_SECONDS, broker_renew.LEASE_SECONDS - broker_renew.CLOSE_RESERVE_SECONDS)
        self.assertEqual(broker_renew.TOLERANCE_SECONDS, 180)
        self.assertEqual(broker_renew.RETRY_SECONDS, 10)

    def test_renew_tolerates_http_503_and_does_not_treat_it_as_conflict(self):
        profile = ProfileConfig('grok', 'blueeyes', 'a' * 64, 'b' * 64,
                                'runcrew-credential-grok-blueeyes', 'runcrew-worker-grok')
        execution = profile.job_name + '/executions/' + profile.job_id + '-abcde'
        uid = 'e93aecda-13b2-46d3-af37-f834d5bff100'
        client = BrokerHTTPClient(profile, endpoint='https://broker.run.app', execution=execution,
                                   execution_uid=uid, grant='g' * 43)
        lease = Lease(profile.profile, profile.account_ref, profile.canonical_account_ref, 1,
                      profile.secret_name + '/versions/1', 'ab' * 16, execution, uid, b'')

        def exchange(*_args, **_kwargs):
            return 503, {'Retry-After': '2'}, b'{"error":"broker_upstream_unavailable","token":"raw-secret"}'

        with patch.object(client, '_id_token', return_value='synthetic.jwt.signature'), \
                patch.object(broker_service, 'exchange', side_effect=exchange) as transport:
            with self.assertRaises(MutationUncertain) as caught:
                client.renew(lease)
            self.assertNotIsInstance(caught.exception, Conflict)
            self.assertNotIn('raw-secret', str(caught.exception))
            self.assertEqual(transport.call_count, 1)
        self.assertTrue(broker_renew.transient(caught.exception))
        lease_clock, _clock = self.clock_for(anchor=0, now=1)

        def fail():
            raise caught.exception

        self.assertIs(lease_clock.renew(broker(fail), lease), False)
        self.assertEqual(lease_clock.renewed_at, 0)
        self.assertEqual(lease_clock.failures, 1)
        self.assertTrue(lease_clock.degraded)

    def test_entrypoint_stamps_acquire_start(self):
        source = (Path(__file__).resolve().parent / 'entrypoint.py').read_text(encoding='utf-8')
        acquire = source[source.index('def acquire_lease'):source.index('class Client')]
        self.assertIn('except MutationUncertain:', acquire)
        self.assertNotIn('except Conflict', acquire)
        self.assertNotIn('uuid', acquire)
        main = source[source.index('def main'):]
        self.assertEqual(main.count('uuid.uuid4()'), 1)
        start = main.index('acquire_started = time.monotonic()')
        record = main.index('broker_renew.record_acquire_start(lease.lease_id, acquire_started)')
        between = main[start:record]
        self.assertEqual(between.count('time.monotonic()'), 1)
        self.assertIn('lease = acquire_lease(broker, request_id, started=acquire_started)', between)
        from entrypoint import ACQUIRE_RETRY_AFTER_SECONDS
        self.assertEqual(ACQUIRE_RETRY_AFTER_SECONDS, 2)
        self.assertIn('sleeper(ACQUIRE_RETRY_AFTER_SECONDS)', acquire)

    def test_entrypoint_retries_acquire_once_with_the_same_request_id(self):
        from entrypoint import ACQUIRE_RETRY_SECONDS, acquire_lease
        self.assertEqual(ACQUIRE_RETRY_SECONDS, 20)
        profile = ProfileConfig('grok', 'blueeyes', 'a' * 64, 'b' * 64,
                                'runcrew-credential-grok-blueeyes', 'runcrew-worker-grok')
        execution = profile.job_name + '/executions/' + profile.job_id + '-abcde'
        uid = 'e93aecda-13b2-46d3-af37-f834d5bff100'
        request_id = '1' * 32
        version = profile.secret_name + '/versions/1'
        lease = Lease(profile.profile, profile.account_ref, profile.canonical_account_ref, 1,
                      version, request_id, execution, uid, b'opaque')
        calls = []

        class Broker:
            def acquire(self, observed, request):
                calls.append(request)
                if len(calls) == 1:
                    raise MutationUncertain('broker_http_outcome_uncertain')
                return lease

        Broker.execution = execution

        slept = []
        self.assertIs(acquire_lease(Broker(), request_id, started=0, clock=lambda: 1,
                                    sleeper=slept.append), lease)
        self.assertEqual(slept, [2])
        self.assertEqual(calls, [request_id, request_id])
        calls.clear()
        with self.assertRaises(MutationUncertain):
            acquire_lease(Broker(), request_id, started=0, clock=lambda: 20, sleeper=slept.append)
        self.assertEqual(calls, [request_id])
        self.assertEqual(slept, [2])
        calls.clear()
        with self.assertRaises(MutationUncertain):
            acquire_lease(Broker(), request_id, started=0, clock=lambda: 18, sleeper=slept.append)
        self.assertEqual(calls, [request_id])
        self.assertEqual(slept, [2])
        calls.clear()

        class Rejected(Broker):
            def acquire(self, observed, request):
                calls.append(request)
                raise BrokerError('execution_already_consumed')

        with self.assertRaisesRegex(BrokerError, '^execution_already_consumed$'):
            acquire_lease(Rejected(), request_id, started=0, clock=lambda: 1)
        self.assertEqual(calls, [request_id])
        calls.clear()

        class OnceThenConsumed(Broker):
            def acquire(self, observed, request):
                calls.append((observed, request))
                if len(calls) == 1:
                    raise MutationUncertain('broker_http_outcome_uncertain')
                raise BrokerError('execution_already_consumed')

        with self.assertRaisesRegex(BrokerError, '^execution_already_consumed$'):
            acquire_lease(OnceThenConsumed(), request_id, started=0, clock=lambda: 17,
                          sleeper=lambda _seconds: None)
        self.assertEqual(calls, [(execution, request_id), (execution, request_id)])

        client = BrokerHTTPClient(profile, endpoint='https://broker.run.app', execution=execution,
                                   execution_uid=uid, grant='g' * 43)
        bodies = []
        ready = broker_service.encode({
            'lease': {'fence': 1, 'version': version, 'lease_id': request_id},
            'credential_b64': 'b3BhcXVl',
        })
        replies = [
            (503, {'Retry-After': '2'}, b'{"error":"broker_upstream_unavailable"}'),
            (200, {}, ready),
        ]

        def exchange(*_args, **kwargs):
            bodies.append(kwargs.get('body'))
            return replies.pop(0)

        with patch.object(client, '_id_token', return_value='synthetic.jwt.signature'), \
                patch.object(broker_service, 'exchange', side_effect=exchange):
            acquired = acquire_lease(client, request_id, started=0, clock=lambda: 1,
                                     sleeper=lambda _seconds: None)
        self.assertEqual(acquired.lease_id, request_id)
        self.assertEqual(acquired.auth_bytes, b'opaque')
        self.assertEqual(bodies, [b'{"request_id":"' + request_id.encode() + b'"}'] * 2)
        broker_renew.record_acquire_start(acquired.lease_id, 0)
        anchored = broker_renew.LeaseClock.for_lease(acquired, CodeError, 'grok', clock=lambda: 50)
        self.assertEqual(anchored.renewed_at, 0)
        denied = [
            (503, {}, b'{"error":"broker_upstream_unavailable"}'),
            (409, {}, b'{"error":"broker_operation_rejected"}'),
        ]

        def exchange_denied(*_args, **_kwargs):
            return denied.pop(0)

        with patch.object(client, '_id_token', return_value='synthetic.jwt.signature'), \
                patch.object(broker_service, 'exchange', side_effect=exchange_denied):
            with self.assertRaises(Conflict) as caught:
                acquire_lease(client, request_id, started=0, clock=lambda: 1,
                              sleeper=lambda _seconds: None)
        self.assertNotIsInstance(caught.exception, MutationUncertain)


if __name__ == '__main__':
    unittest.main()
