"""Fault-injected Google wire-schema tests; no cloud, login, tokens or inference."""
import base64
import copy
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from agent_hub import cloud_credential_broker as cb


class GoogleWire:
    """Atomic Firestore CAS plus immutable Secret Manager versions and Run GET.

    Faults happen after server side effects, like a lost HTTP acknowledgement.
    Request bodies are actual Google REST shapes, not broker method substitutes.
    """
    def __init__(self, config):
        self.config = config
        self.lock = threading.RLock()
        self.state = cb.initial_control_state(config, config.secret_name + '/versions/1', 'c' * 64)
        self.revision = 1
        self.versions = {config.secret_name + '/versions/1': b'opaque-first-credential'}
        self.calls = []
        self.add_calls = 0
        self.secret_reads = 0
        self.lose_add_ack = False
        self.lose_phase_ack = None
        self.fail_reads = 0
        self.fail_reads_after_ack = 0
        self.corrupt_checksum = False
        self.read_barrier = None
        self.execution = {'name': config.job_name + '/executions/' + config.job_id + '-abcde',
                          'uid': 'e93aecda-13b2-46d3-af37-f834d5bff100', 'taskCount': 1,
                          'runningCount': 1, 'reconciling': False}

    @property
    def stamp(self):
        return f'2026-09-21T00:00:00.{self.revision:06d}Z'

    def terminal(self):
        self.execution.update(completionTime='2026-09-21T00:03:00Z', runningCount=0, succeededCount=1)

    def new_execution(self):
        self.execution = {'name': self.config.job_name + '/executions/' + self.config.job_id + '-nextb',
                          'uid': 'e93aecda-13b2-46d3-af37-f834d5bff101', 'taskCount': 1,
                          'runningCount': 1, 'reconciling': False}

    def request(self, method, host, path, value=None):
        result = None
        barrier = None
        with self.lock:
            self.calls.append((method, host, path))
            if host == 'run.googleapis.com':
                assert method == 'GET' and path == '/v2/' + self.execution['name']
                return copy.deepcopy(self.execution)
            if host == 'firestore.googleapis.com' and method == 'GET':
                assert path == '/v1/' + self.config.document_name
                if self.fail_reads:
                    self.fail_reads -= 1
                    raise cb.BrokerError('injected_read_failure')
                if self.state is None:
                    raise cb.BrokerError('google_resource_denied_or_missing')
                result = {'name': self.config.document_name, 'updateTime': self.stamp,
                          'fields': cb._fields(copy.deepcopy(self.state))}
                barrier = self.read_barrier
            elif host == 'firestore.googleapis.com' and method == 'POST':
                assert path == '/v1/' + cb.DATABASE + '/documents:commit'
                assert set(value) == {'writes'} and len(value['writes']) == 1
                write = value['writes'][0]
                assert set(write) == {'update', 'currentDocument'}
                assert set(write['update']) == {'name', 'fields'}
                assert write['update']['name'] == self.config.document_name
                condition = write['currentDocument']
                expected = {'updateTime': self.stamp} if self.state is not None else {'exists': False}
                if condition != expected:
                    raise cb.Conflict('control_compare_and_swap_conflict')
                self.state = cb._decode_fields(write['update']['fields'])
                self.revision += 1
                if self.lose_phase_ack == self.state['phase']:
                    self.lose_phase_ack = None
                    self.fail_reads = self.fail_reads_after_ack
                    raise cb.MutationUncertain('injected_lost_ack')
                return {'writeResults': [{'updateTime': self.stamp}], 'commitTime': self.stamp}
            elif host == 'secretmanager.googleapis.com' and method == 'GET':
                assert path.startswith('/v1/' + self.config.secret_name + '/versions/') and path.endswith(':access')
                self.secret_reads += 1
                version = path[4:-7]
                body = self.versions[version]
                return {'name': version, 'payload': {'data': base64.b64encode(body).decode(),
                    'dataCrc32c': '0' if self.corrupt_checksum else str(cb.crc32c(body))}}
            elif host == 'secretmanager.googleapis.com' and method == 'POST':
                assert path == '/v1/' + self.config.secret_name + ':addVersion'
                assert set(value) == {'payload'} and set(value['payload']) == {'data', 'dataCrc32c'}
                body = base64.b64decode(value['payload']['data'], validate=True)
                assert value['payload']['dataCrc32c'] == str(cb.crc32c(body))
                self.add_calls += 1
                name = self.config.secret_name + '/versions/' + str(len(self.versions) + 1)
                self.versions[name] = body
                if self.lose_add_ack:
                    self.lose_add_ack = False
                    raise cb.MutationUncertain('injected_lost_add_ack')
                return {'name': name, 'state': 'ENABLED', 'clientSpecifiedPayloadChecksum': True}
            else:
                raise AssertionError('Unexpected endpoint')
        if barrier is not None:
            barrier.wait(timeout=5)
            with self.lock:
                self.read_barrier = None
        return result


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.config = cb.ProfileConfig('copilot', 'blueeyes', 'a' * 64, 'b' * 64,
                                      'runcrew-credential-copilot-blueeyes', 'runcrew-worker-copilot')
        self.wire = GoogleWire(self.config)
        self.now = 1000.0
        self.broker = cb.CloudCredentialBroker(self.config, rest=self.wire, clock=lambda: self.now, lease_seconds=30)

    def acquire(self, request='1' * 32):
        return self.broker.acquire(self.wire.execution['name'], request)

    def assertBlocked(self, call):
        with self.assertRaises(cb.BrokerError):
            call()

    def test_refresh_publishes_verified_exact_version_before_release(self):
        lease = self.acquire()
        self.assertNotIn('opaque-first', repr(lease))
        self.assertBlocked(lambda: self.broker.release(lease, lease.version))
        version = self.broker.commit(lease, b'opaque-refreshed-credential')
        self.assertEqual(version, self.config.secret_name + '/versions/2')
        self.assertEqual(self.wire.state['phase'], 'committed')
        self.assertBlocked(lambda: self.acquire('2' * 32))
        self.broker.release(lease, version)
        self.wire.new_execution()
        successor = self.acquire('2' * 32)
        self.assertEqual(successor.auth_bytes, b'opaque-refreshed-credential')
        self.assertGreater(successor.fence, lease.fence)
        self.assertEqual(self.wire.add_calls, 1)

    def test_unchanged_bytes_do_not_create_version(self):
        lease = self.acquire()
        version = self.broker.commit(lease, lease.auth_bytes)
        self.broker.release(lease, version)
        self.assertEqual(version, lease.version)
        self.assertEqual(self.wire.add_calls, 0)

    def test_acknowledged_commit_and_release_retry_are_idempotent(self):
        lease = self.acquire()
        version = self.broker.commit(lease, b'refreshed')
        self.assertEqual(self.broker.commit(lease, b'refreshed'), version)
        self.broker.release(lease, version)
        self.broker.release(lease, version)
        self.assertEqual(self.broker.commit(lease, b'refreshed'), version)
        self.assertEqual(self.wire.add_calls, 1)
        self.assertBlocked(lambda: self.acquire())

    def test_released_execution_cannot_acquire_with_new_request_id(self):
        lease = self.acquire()
        # Retry of the same still-active acquisition and renewal remain valid.
        self.assertEqual(self.acquire(), lease)
        self.now += 5
        self.broker.renew(lease)
        version = self.broker.commit(lease, lease.auth_bytes)
        self.broker.release(lease, version)
        reads, fence = self.wire.secret_reads, self.wire.state['fence']
        with self.assertRaisesRegex(cb.BrokerError, '^execution_already_consumed$'):
            self.acquire('2' * 32)
        self.assertEqual(self.wire.secret_reads, reads)
        self.assertEqual(self.wire.state['fence'], fence)
        self.assertEqual(self.wire.state['phase'], 'idle')
        self.wire.new_execution()
        successor = self.acquire('2' * 32)
        self.assertEqual(successor.fence, fence + 1)
        self.assertNotEqual(successor.execution_uid, lease.execution_uid)
        self.assertEqual(successor.auth_bytes, lease.auth_bytes)

    def test_expired_lease_is_not_taken_over_or_renewed(self):
        lease = self.acquire()
        self.now += 31
        for call in (lambda: self.acquire('2' * 32), lambda: self.acquire(),
                     lambda: self.broker.renew(lease), lambda: self.broker.commit(lease, b'refreshed')):
            self.assertBlocked(call)
        self.assertEqual(self.wire.state['fence'], 1)
        self.assertEqual(self.wire.add_calls, 0)

    def test_renew_retains_same_fence_and_stale_owner_cannot_write(self):
        lease = self.acquire()
        self.now += 20
        self.broker.renew(lease)
        self.now += 20
        self.broker.assert_current(lease)
        version = self.broker.commit(lease, lease.auth_bytes)
        self.broker.release(lease, version)
        self.wire.new_execution()
        self.acquire('2' * 32)
        for call in (lambda: self.broker.renew(lease), lambda: self.broker.commit(lease, b'stale'),
                     lambda: self.broker.release(lease, version), lambda: self.broker.quarantine(lease, 'writeback_uncertain')):
            self.assertBlocked(call)
        self.assertEqual(self.wire.state['phase'], 'leased')

    def _stall_rest(self, mode):
        wire = self.wire

        class Rest:
            def __init__(self):
                self.posts = 0
                self.gets = 0

            def request(self, method, host, path, value=None):
                # access: stall the secret read. uncertain/fence/version stall that
                # same read, then fail the idle abort (lost ack, fence, or version).
                if (method == 'GET' and host == 'secretmanager.googleapis.com'
                        and mode in ('access', 'uncertain', 'fence', 'version')):
                    raise cb.UpstreamUnavailable('google_upstream_unavailable')
                if host == 'firestore.googleapis.com' and method == 'GET':
                    self.gets += 1
                    if mode == 'assert' and self.gets == 2:
                        raise cb.UpstreamUnavailable('google_upstream_unavailable')
                if host == 'firestore.googleapis.com' and method == 'POST':
                    self.posts += 1
                    if mode == 'uncertain' and self.posts == 2:
                        raise cb.MutationUncertain('injected_lost_ack')
                    result = wire.request(method, host, path, value)
                    if self.posts == 1 and mode == 'fence':
                        wire.state['fence'] += 1
                    if self.posts == 1 and mode == 'version':
                        wire.state['version'] = wire.config.secret_name + '/versions/2'
                    return result
                return wire.request(method, host, path, value)

        self.broker.rest = Rest()

    def _assert_clean_release(self, request_id='1' * 32):
        version = self.config.secret_name + '/versions/1'
        state = self.wire.state
        self.assertEqual(state['phase'], 'idle')
        self.assertEqual(state['quarantine_reason'], '')
        self.assertEqual(state['lease_until_ms'], 0)
        self.assertEqual(state['last_release_id'], request_id)
        self.assertEqual(state['last_release_fence'], 1)
        self.assertEqual(state['last_release_version'], version)
        self.assertEqual(state['version'], version)
        self.assertEqual(state['fence'], 1)

    def test_access_stall_after_lease_write_idles_without_quarantine(self):
        self._stall_rest('access')
        with self.assertRaisesRegex(cb.UpstreamUnavailable, '^credential_acquisition_upstream_unavailable$') as caught:
            self.acquire()
        self.assertNotIsInstance(caught.exception, cb.Conflict)
        self._assert_clean_release()
        with self.assertRaisesRegex(cb.BrokerError, '^execution_already_consumed$'):
            self.acquire('2' * 32)
        self.broker.rest = self.wire
        self.wire.new_execution()
        successor = self.acquire('2' * 32)
        self.assertEqual(successor.fence, 2)
        self.assertEqual(self.wire.state['phase'], 'leased')

    def test_assert_current_stall_after_lease_write_idles_without_quarantine(self):
        self._stall_rest('assert')
        with self.assertRaisesRegex(cb.UpstreamUnavailable, '^credential_acquisition_upstream_unavailable$'):
            self.acquire()
        self._assert_clean_release()
        self.assertEqual(self.wire.secret_reads, 1)

    def test_uncertain_abort_after_access_stall_quarantines(self):
        self._stall_rest('uncertain')
        with self.assertRaisesRegex(cb.BrokerError, '^credential_acquisition_unavailable$') as caught:
            self.acquire()
        self.assertNotIsInstance(caught.exception, cb.UpstreamUnavailable)
        self.assertEqual(self.wire.state['phase'], 'quarantined')
        self.assertEqual(self.wire.state['quarantine_reason'], 'credential_read_failed')

    def test_definitive_fence_or_version_loss_still_quarantines(self):
        self._stall_rest('fence')
        with self.assertRaisesRegex(cb.BrokerError, '^credential_acquisition_unavailable$') as caught:
            self.acquire()
        self.assertNotIsInstance(caught.exception, cb.UpstreamUnavailable)
        self.assertEqual(self.wire.state['phase'], 'leased')
        self.assertEqual(self.wire.state['last_release_id'], '')
        self.assertEqual(self.wire.state['quarantine_reason'], '')
        self.setUp()
        self._stall_rest('version')
        with self.assertRaisesRegex(cb.BrokerError, '^credential_acquisition_unavailable$') as caught:
            self.acquire()
        self.assertNotIsInstance(caught.exception, cb.UpstreamUnavailable)
        self.assertEqual(self.wire.state['phase'], 'quarantined')
        self.assertEqual(self.wire.state['quarantine_reason'], 'credential_read_failed')

    def test_idempotent_delivery_restamp_makes_the_stalled_abort_conflict(self):
        """A same-request_id retry re-stamps before returning bytes.

        The stalled attempt then aborts against the stamp from its own lease
        write, loses with Conflict, and leaves the delivered lease in place.
        """
        started = threading.Event()
        delivered = threading.Event()
        stalled = {'done': False}
        original = self.broker.rest.request
        holder = {}

        def request(method, host, path, value=None):
            if (not stalled['done'] and method == 'GET' and host == 'secretmanager.googleapis.com'
                    and path.endswith(':access')):
                stalled['done'] = True
                started.set()
                delivered.wait(5)
                raise cb.UpstreamUnavailable('google_upstream_unavailable')
            return original(method, host, path, value)

        self.broker.rest.request = request

        def first():
            try:
                self.broker.acquire(self.wire.execution['name'], '1' * 32)
            except Exception as error:
                holder['error'] = error

        worker = threading.Thread(target=first)
        worker.start()
        self.assertTrue(started.wait(5))
        self.assertEqual(self.wire.state['phase'], 'leased')
        after_lease = self.wire.revision
        self.now = 1010.0
        lease = self.broker.acquire(self.wire.execution['name'], '1' * 32)
        self.assertEqual(lease.auth_bytes, b'opaque-first-credential')
        self.assertEqual(self.wire.revision, after_lease + 1)
        renewed_until = self.wire.state['lease_until_ms']
        self.assertEqual(renewed_until, 1010 * 1000 + 30 * 1000)
        delivered.set()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertIsInstance(holder['error'], cb.UpstreamUnavailable)
        self.assertEqual(str(holder['error']), 'credential_acquisition_upstream_unavailable')
        self.assertNotIsInstance(holder['error'], cb.Conflict)
        self.assertEqual(self.wire.state['phase'], 'leased')
        self.assertEqual(self.wire.state['quarantine_reason'], '')
        self.assertEqual(self.wire.state['lease_until_ms'], renewed_until)
        self.assertEqual(self.wire.state['fence'], 1)
        self.assertEqual(self.wire.revision, after_lease + 1)
        self.assertEqual(self.wire.secret_reads, 1)

    def test_abort_before_the_retry_consumes_the_request_id(self):
        original = self.broker.rest.request

        def request(method, host, path, value=None):
            if method == 'GET' and host == 'secretmanager.googleapis.com' and path.endswith(':access'):
                raise cb.UpstreamUnavailable('google_upstream_unavailable')
            return original(method, host, path, value)

        self.broker.rest.request = request
        with self.assertRaisesRegex(cb.UpstreamUnavailable, '^credential_acquisition_upstream_unavailable$'):
            self.acquire()
        self.assertEqual(self.wire.state['phase'], 'idle')
        self.assertEqual(self.wire.state['last_release_id'], '1' * 32)
        self.assertEqual(self.wire.state['quarantine_reason'], '')
        self.broker.rest.request = original
        with self.assertRaisesRegex(cb.BrokerError, '^execution_already_consumed$'):
            self.acquire()
        self.assertEqual(self.wire.state['phase'], 'idle')
        self.assertEqual(self.wire.state['fence'], 1)
        self.assertEqual(self.wire.state['quarantine_reason'], '')
        self.assertEqual(self.wire.secret_reads, 0)

    def test_pre_mutation_reads_stop_when_the_request_budget_is_spent(self):
        original = self.broker.rest.request

        def request(method, host, path, value=None):
            cb.upstream_transport_limits()
            return original(method, host, path, value)

        self.broker.rest.request = request
        token = cb.begin_upstream_request()
        try:
            cb._request_deadline.set(time.monotonic() + 0.2)
            with self.assertRaisesRegex(cb.UpstreamUnavailable, '^upstream_request_budget_exhausted$'):
                self.acquire()
        finally:
            cb.end_upstream_request(token)
        self.assertEqual(self.wire.state['phase'], 'idle')
        self.assertEqual(self.wire.state['fence'], 0)
        self.assertEqual(self.wire.state['quarantine_reason'], '')
        self.assertEqual(self.wire.secret_reads, 0)

    def test_post_write_calls_keep_the_legacy_budget_when_the_deadline_is_spent(self):
        seen = {}
        wire = self.wire

        class Rest:
            def __init__(self):
                self.posts = 0
                self.gets = 0

            def request(self, method, host, path, value=None):
                if host == 'firestore.googleapis.com' and method == 'GET':
                    self.gets += 1
                    if self.gets == 1:
                        seen['before_write'] = cb._post_mutation.get()
                if host == 'firestore.googleapis.com' and method == 'POST':
                    self.posts += 1
                    if self.posts == 1:
                        result = wire.request(method, host, path, value)
                        cb._request_deadline.set(time.monotonic() + 0.05)
                        return result
                    seen['abort'] = cb.upstream_transport_limits()
                    return wire.request(method, host, path, value)
                if host == 'secretmanager.googleapis.com' and method == 'GET':
                    seen['access'] = cb.upstream_transport_limits()
                    seen['access_post'] = cb._post_mutation.get()
                    raise cb.UpstreamUnavailable('google_upstream_unavailable')
                return wire.request(method, host, path, value)

        self.broker.rest = Rest()
        token = cb.begin_upstream_request()
        try:
            with self.assertRaisesRegex(cb.UpstreamUnavailable, '^credential_acquisition_upstream_unavailable$'):
                self.acquire()
        finally:
            cb.end_upstream_request(token)
        self.assertFalse(seen['before_write'])
        self.assertEqual(seen['access'], (10, 15))
        self.assertTrue(seen['access_post'])
        self.assertEqual(seen['abort'], (10, 15))
        self._assert_clean_release()
        self.assertFalse(cb._post_mutation.get())

    def test_commit_after_intent_stays_quarantine_on_the_legacy_budget(self):
        lease = self.acquire()
        seen = {}
        wire = self.wire

        class Rest:
            def __init__(self):
                self.posts = 0

            def request(self, method, host, path, value=None):
                if host == 'firestore.googleapis.com' and method == 'POST':
                    self.posts += 1
                    result = wire.request(method, host, path, value)
                    if self.posts == 1:
                        seen['during_intent'] = cb._post_mutation.get()
                        cb._request_deadline.set(time.monotonic() + 0.05)
                    return result
                if host == 'secretmanager.googleapis.com' and method == 'POST':
                    seen['add'] = cb.upstream_transport_limits()
                    raise cb.UpstreamUnavailable('google_upstream_unavailable')
                return wire.request(method, host, path, value)

        self.broker.rest = Rest()
        token = cb.begin_upstream_request()
        try:
            with self.assertRaisesRegex(cb.MutationUncertain, '^credential_commit_quarantined$') as caught:
                self.broker.commit(lease, b'new-refresh')
            self.assertNotIsInstance(caught.exception, cb.UpstreamUnavailable)
        finally:
            cb.end_upstream_request(token)
        self.assertFalse(seen['during_intent'])
        self.assertEqual(seen['add'], (10, 15))
        self.assertEqual(self.wire.state['phase'], 'quarantined')
        self.assertEqual(self.wire.state['quarantine_reason'], 'writeback_uncertain')
        self.assertNotEqual(self.wire.state['intent_id'], '')
        self.assertFalse(cb._post_mutation.get())

    def test_renew_reads_stay_on_the_pre_mutation_budget(self):
        lease = self.acquire()
        seen = {}
        original = self.broker.rest.request

        def request(method, host, path, value=None):
            if 'limits' not in seen:
                seen['post'] = cb._post_mutation.get()
                seen['limits'] = cb.upstream_transport_limits()
            return original(method, host, path, value)

        self.broker.rest.request = request
        token = cb.begin_upstream_request()
        try:
            self.broker.renew(lease)
        finally:
            cb.end_upstream_request(token)
        self.assertFalse(seen['post'])
        self.assertEqual(seen['limits'], (8, 8))
        self.assertEqual(self.wire.state['phase'], 'leased')

    def test_acquire_lost_ack_resolved_by_strong_read_without_second_fence(self):
        self.wire.lose_phase_ack = 'leased'
        first = self.acquire()
        retry = self.acquire()
        self.assertEqual(first, retry)
        self.assertEqual(self.wire.state['fence'], 1)

    def test_simultaneous_acquisitions_have_one_winner(self):
        self.wire.read_barrier = threading.Barrier(2)
        results = []
        def run(request):
            try:
                results.append(self.acquire(request))
            except cb.BrokerError as error:
                results.append(error)
        threads = [threading.Thread(target=run, args=(char * 32,)) for char in ('1', '2')]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(8)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sum(isinstance(item, cb.Lease) for item in results), 1)
        self.assertEqual(self.wire.secret_reads, 1)
        self.assertEqual(self.wire.state['fence'], 1)

    def test_racing_commit_intents_cannot_double_create_or_quarantine_winner(self):
        lease = self.acquire()
        self.wire.read_barrier = threading.Barrier(2)
        results = []
        def run():
            try:
                results.append(self.broker.commit(lease, b'same-refresh'))
            except cb.BrokerError as error:
                results.append(error)
        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(8)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sum(isinstance(item, str) for item in results), 1)
        self.assertEqual(sum(isinstance(item, cb.Conflict) for item in results), 1)
        self.assertEqual(self.wire.add_calls, 1)
        self.assertEqual(self.wire.state['phase'], 'committed')

    def test_lost_add_version_ack_quarantines_and_never_retries_add(self):
        lease = self.acquire()
        self.wire.lose_add_ack = True
        self.assertBlocked(lambda: self.broker.commit(lease, b'new-refresh'))
        self.assertEqual(len(self.wire.versions), 2)
        self.assertEqual(self.wire.state['phase'], 'quarantined')
        self.assertBlocked(lambda: self.broker.commit(lease, b'new-refresh'))
        self.assertBlocked(lambda: self.acquire('2' * 32))
        self.assertEqual(self.wire.add_calls, 1)

    def test_lost_intent_ack_must_be_confirmed_before_secret_mutation(self):
        lease = self.acquire()
        self.wire.lose_phase_ack = 'committing'
        version = self.broker.commit(lease, b'new-refresh')
        self.assertEqual(self.wire.add_calls, 1)
        self.broker.release(lease, version)
        self.wire.new_execution()
        lease = self.acquire('2' * 32)
        self.wire.lose_phase_ack, self.wire.fail_reads_after_ack = 'committing', 1
        self.assertBlocked(lambda: self.broker.commit(lease, b'next-refresh'))
        self.assertEqual(self.wire.add_calls, 1)
        self.assertEqual(self.wire.state['phase'], 'quarantined')

    def test_lost_publish_ack_is_confirmed_before_release(self):
        lease = self.acquire()
        self.wire.lose_phase_ack = 'committed'
        version = self.broker.commit(lease, b'new-refresh')
        self.assertEqual(self.wire.state['phase'], 'committed')
        self.broker.release(lease, version)
        self.assertEqual(self.wire.state['phase'], 'idle')
        self.assertEqual(self.wire.add_calls, 1)

    def test_unreadable_publish_ack_blocks_successor_and_retains_bytes(self):
        lease = self.acquire()
        self.wire.lose_phase_ack, self.wire.fail_reads_after_ack = 'committed', 1
        self.assertBlocked(lambda: self.broker.commit(lease, b'new-refresh'))
        self.assertEqual(self.wire.state['phase'], 'quarantined')
        self.assertIn(b'new-refresh', self.wire.versions.values())
        self.assertBlocked(lambda: self.acquire('2' * 32))

    def test_lost_release_ack_can_be_resolved_without_new_version(self):
        lease = self.acquire()
        version = self.broker.commit(lease, b'new-refresh')
        self.wire.lose_phase_ack = 'idle'
        self.broker.release(lease, version)
        self.broker.quarantine(lease, 'writeback_uncertain')
        self.assertEqual(self.wire.state['phase'], 'idle')
        self.assertEqual(self.wire.add_calls, 1)

    def test_checksum_corruption_quarantines_before_returning_credential(self):
        self.wire.corrupt_checksum = True
        self.assertBlocked(lambda: self.acquire())
        self.assertEqual(self.wire.state['phase'], 'quarantined')

    def test_backend_identity_mismatch_prevents_secret_access(self):
        self.wire.state['canonical_account_ref'] = 'd' * 64
        self.assertBlocked(lambda: self.acquire())
        self.assertEqual(self.wire.secret_reads, 0)

    def test_no_implicit_enrollment_and_existing_binding_not_overwritten(self):
        version = self.config.secret_name + '/versions/1'
        self.assertBlocked(lambda: self.broker.initialize_binding(version, 'd' * 64))
        self.wire.state = None
        self.assertBlocked(lambda: self.acquire())
        self.broker.initialize_binding(version, 'd' * 64)
        self.assertEqual(self.wire.state['enrollment_receipt_ref'], 'd' * 64)
        self.assertEqual(self.wire.state['phase'], 'idle')

    def test_secret_size_and_known_crc_vector(self):
        self.assertEqual(cb.crc32c(b'123456789'), 0xe3069283)
        self.assertEqual(len(cb._opaque(b'x' * 65536)), 65536)
        lease = self.acquire()
        self.assertBlocked(lambda: self.broker.commit(lease, b'x' * 65537))
        self.assertBlocked(lambda: self.broker.commit(lease, b''))
        self.assertEqual(self.wire.state['phase'], 'leased')

    def test_only_exact_numeric_versions_allowed(self):
        for suffix in ('latest', '0', '1?alt=media', '../2', '1:access', '%31'):
            self.assertBlocked(lambda suffix=suffix: self.broker._access(self.config.secret_name + '/versions/' + suffix))
        self.assertEqual(self.wire.secret_reads, 0)

    def test_reconciliation_requires_terminal_matching_execution_and_exact_intent(self):
        lease = self.acquire()
        self.wire.lose_add_ack = True
        self.assertBlocked(lambda: self.broker.commit(lease, b'refreshed'))
        candidate = self.config.secret_name + '/versions/2'
        reconcile = lambda: self.broker.reconcile_commit(fence=lease.fence, lease_id=lease.lease_id,
                                                        candidate_version=candidate)
        self.assertBlocked(reconcile)  # deadline alone, even far past, cannot fence a native process
        self.now += 10000
        self.assertBlocked(reconcile)
        self.wire.terminal()
        self.wire.execution['uid'] = 'f' * 32
        self.assertBlocked(reconcile)
        self.wire.execution['uid'] = lease.execution_uid
        self.assertBlocked(lambda: self.broker.reconcile_commit(fence=lease.fence, lease_id=lease.lease_id,
                                                                candidate_version=lease.version))
        self.assertEqual(reconcile(), candidate)
        self.assertEqual(self.wire.state['phase'], 'idle')
        self.assertEqual(self.wire.state['version'], candidate)

    def test_reconciliation_does_not_guess_lost_refresh_without_intent(self):
        lease = self.acquire()
        self.broker.quarantine(lease, 'provider_refresh_uncertain')
        self.wire.terminal()
        self.assertBlocked(lambda: self.broker.reconcile_commit(fence=lease.fence, lease_id=lease.lease_id,
                                                                candidate_version=lease.version))

    def test_typed_malformed_response_cannot_claim_terminal(self):
        lease = self.acquire()
        self.wire.lose_add_ack = True
        self.assertBlocked(lambda: self.broker.commit(lease, b'refreshed'))
        self.wire.terminal()
        self.wire.execution['succeededCount'] = '1'
        self.assertBlocked(lambda: self.broker.reconcile_commit(fence=lease.fence, lease_id=lease.lease_id,
                                           candidate_version=self.config.secret_name + '/versions/2'))

    def test_existing_refresh_session_contract_consumes_actual_broker_lease(self):
        root = Path(__file__).resolve().parents[1]
        for provider in ('copilot', 'grok'):
            name = '_test_broker_contract_' + provider
            spec = importlib.util.spec_from_file_location(name, root / 'deploy' / (provider + '-worker') / 'credential_state.py')
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            try:
                spec.loader.exec_module(module)
                config = cb.ProfileConfig(provider, 'blueeyes', module.ACCOUNT_REF, 'b' * 64,
                                          f'runcrew-credential-{provider}-blueeyes', f'runcrew-worker-{provider}')
                wire = GoogleWire(config)
                broker = cb.CloudCredentialBroker(config, rest=wire, clock=lambda: self.now)
                lease = broker.acquire(wire.execution['name'], '1' * 32)
                with tempfile.TemporaryDirectory(prefix='broker-contract-') as directory:
                    session = module.RefreshSession(broker, lease, Path(directory) / 'fresh-home')
                    session.restore()
                    self.assertEqual(session.auth_path.read_bytes(), lease.auth_bytes)
                    session.auth_path.write_bytes(b'synthetic-native-refresh')
                    with self.assertRaises(module.CredentialError):
                        session.finish(native_stopped=False)
                    version = session.finish(native_stopped=True)
                    self.assertEqual(wire.versions[version], b'synthetic-native-refresh')
                    self.assertEqual(wire.state['phase'], 'idle')
                    self.assertEqual(session.state, 'committed')
            finally:
                del sys.modules[name]


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.config = cb.ProfileConfig('grok', 'blueeyes', 'a' * 64, 'b' * 64,
                                      'runcrew-credential-grok-blueeyes', 'runcrew-worker-grok')
        self.rest = cb.GoogleREST(self.config)

    def test_endpoint_allowlist_fails_before_requesting_access_token(self):
        with patch.object(self.rest, '_token', side_effect=AssertionError('must not fetch token')):
            bad = [('GET', 'attacker.example', '/v1/' + self.config.document_name),
                   ('GET', 'firestore.googleapis.com', '/v1/' + self.config.document_name.replace('/databases/runcrew-provider-auth/', '/databases/(default)/')),
                   ('GET', 'firestore.googleapis.com', '/v1/' + self.config.document_name.replace('/databases/runcrew-provider-auth/', '/databases/runcrew-auth/')),
                   ('GET', 'firestore.googleapis.com', '/v1/' + self.config.document_name.replace('/databases/runcrew-provider-auth/', '/databases/runcrew-hub/')),
                   ('GET', 'firestore.googleapis.com', '/v1/' + self.config.document_name + '?mask=x'),
                   ('POST', 'secretmanager.googleapis.com', '/v1/projects/other/secrets/other:addVersion'),
                   ('GET', 'secretmanager.googleapis.com', '/v1/' + self.config.secret_name + '/versions/latest:access'),
                   ('GET', 'run.googleapis.com', '/v2/' + self.config.job_name + '/executions/wrong'),
                   ('GET', 'firestore.googleapis.com', None)]
            for args in bad:
                with self.assertRaisesRegex(cb.BrokerError, '^google_endpoint_not_allowed$'):
                    self.rest.request(*args)

    def test_firestore_commit_cannot_target_other_document(self):
        with patch.object(self.rest, '_token', side_effect=AssertionError('must not fetch token')):
            for writes in ([{'update': {'name': 'other'}, 'currentDocument': {'exists': False}}], [None]):
                with self.assertRaisesRegex(cb.BrokerError, '^control_write_scope_invalid$'):
                    self.rest.request('POST', 'firestore.googleapis.com', '/v1/' + cb.DATABASE + '/documents:commit',
                                      {'writes': writes})

    def test_only_metadata_supplies_bearer_token_and_cache_is_in_memory(self):
        calls = []
        def exchange(host, path, method, body=None, headers=None, **kwargs):
            calls.append((host, path, method, headers, kwargs))
            if host == 'metadata.google.internal':
                self.assertEqual(headers, {'Metadata-Flavor': 'Google'})
                self.assertTrue(kwargs['metadata'])
                return {'access_token': 'synthetic-metadata-token', 'token_type': 'Bearer', 'expires_in': 3600}
            self.assertEqual(headers['Authorization'], 'Bearer synthetic-metadata-token')
            return {}
        with patch.object(self.rest, '_exchange', side_effect=exchange):
            for _ in range(2):
                self.rest.request('GET', 'firestore.googleapis.com', '/v1/' + self.config.document_name)
        self.assertEqual(len(calls), 3)
        self.assertNotIn('synthetic-metadata-token', repr(self.rest))

    def test_http_errors_never_include_response_or_redirect(self):
        class Response:
            status = 302
            def read(self, limit):
                return b'{"secret":"synthetic-do-not-log"}'
            def getheader(self, name):
                return 'https://attacker.example/'
        class Connection:
            sock = None
            calls = []
            def __init__(self, host, timeout):
                self.calls.append((host, timeout))
            def request(self, *args, **kwargs):
                pass
            def getresponse(self):
                return Response()
            def close(self):
                pass
        with patch.object(cb.http.client, 'HTTPSConnection', Connection):
            with self.assertRaisesRegex(cb.BrokerError, '^google_read_failed$') as caught:
                self.rest._exchange('firestore.googleapis.com', '/v1/known', 'GET')
            self.assertNotIsInstance(caught.exception, cb.UpstreamUnavailable)
            with self.assertRaisesRegex(cb.MutationUncertain, '^google_mutation_uncertain$') as caught:
                self.rest._exchange('secretmanager.googleapis.com', '/v1/known:addVersion', 'POST')
            self.assertNotIsInstance(caught.exception, cb.UpstreamUnavailable)
        self.assertEqual(len(Connection.calls), 2)
        self.assertEqual({timeout for _host, timeout in Connection.calls}, {8})

    def test_duplicate_json_keys_and_email_only_binding_rejected(self):
        with self.assertRaisesRegex(cb.BrokerError, '^invalid_google_response$'):
            cb._json(b'{"name":"x","name":"y"}')
        with self.assertRaisesRegex(cb.BrokerError, '^canonical_identity_required$'):
            replace(self.config, canonical_account_ref=self.config.account_ref)
        with self.assertRaisesRegex(cb.BrokerError, '^profile_invalid$'):
            replace(self.config, provider=[])

    def test_canonical_identity_is_namespaced_and_deterministic(self):
        ref = cb.canonical_identity_ref('grok', 'https://auth.x.ai', 'stable-subject', 'tenant-id')
        self.assertEqual(len(ref), 64)
        self.assertEqual(ref, cb.canonical_identity_ref('grok', 'https://auth.x.ai', 'stable-subject', 'tenant-id'))
        self.assertNotEqual(ref, cb.canonical_identity_ref('grok', 'https://auth.x.ai', 'stable-subject', 'other-tenant'))
        self.assertNotEqual(ref, cb.canonical_identity_ref('copilot', 'https://auth.x.ai', 'stable-subject', 'tenant-id'))

    def test_response_limit_is_enforced_before_parsing(self):
        class Response:
            status = 200
            def read(self, limit):
                self.limit = limit
                return b'x' * limit
        response = Response()
        class Connection:
            sock = None
            def __init__(self, host, timeout):
                pass
            def request(self, *args, **kwargs):
                pass
            def getresponse(self):
                return response
            def close(self):
                pass
        with patch.object(cb.http.client, 'HTTPSConnection', Connection):
            with self.assertRaisesRegex(cb.UpstreamUnavailable, '^google_response_limit$') as caught:
                self.rest._exchange('firestore.googleapis.com', '/v1/known', 'GET')
            self.assertNotIsInstance(caught.exception, cb.Conflict)
            self.assertNotIsInstance(caught.exception, cb.MutationUncertain)
        self.assertEqual(response.limit, cb.MAX_RESPONSE_BYTES + 1)

    def test_request_budget_is_per_thread_and_refuses_under_one_second(self):
        self.assertEqual(cb.REQUEST_BUDGET_SECONDS, 12)
        self.assertEqual(cb.REQUEST_BUDGET_FLOOR_SECONDS, 1)
        self.assertLess(cb.REQUEST_BUDGET_SECONDS, 15)
        self.assertGreater(cb.REQUEST_BUDGET_SECONDS, cb.UPSTREAM_BUDGET_SECONDS)
        created = []

        class Connection:
            sock = None

            def __init__(self, host, timeout):
                created.append(timeout)

            def request(self, *args, **kwargs):
                raise AssertionError('refused call must not connect')

            def close(self):
                pass

        token = cb.begin_upstream_request()
        try:
            self.assertEqual(cb.upstream_call_budget(), 8)
            cb._request_deadline.set(time.monotonic() + 0.2)
            with patch.object(cb.http.client, 'HTTPSConnection', Connection):
                with self.assertRaisesRegex(cb.UpstreamUnavailable, '^upstream_request_budget_exhausted$'):
                    self.rest._exchange('firestore.googleapis.com', '/v1/known', 'GET')
            self.assertEqual(created, [])
        finally:
            cb.end_upstream_request(token)
        self.assertEqual(cb.upstream_call_budget(), 8)
        seen = {}
        barrier = threading.Barrier(2)

        def blocked():
            token = cb.begin_upstream_request()
            cb._request_deadline.set(time.monotonic() + 0.05)
            barrier.wait(timeout=2)
            try:
                cb.upstream_call_budget()
                seen['blocked'] = 'allowed'
            except cb.UpstreamUnavailable:
                seen['blocked'] = 'refused'
            finally:
                cb.end_upstream_request(token)

        def open_request():
            token = cb.begin_upstream_request()
            barrier.wait(timeout=2)
            try:
                seen['open'] = cb.upstream_call_budget()
            finally:
                cb.end_upstream_request(token)

        threads = [threading.Thread(target=blocked), threading.Thread(target=open_request)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(seen, {'blocked': 'refused', 'open': 8})

    def test_post_mutation_exchange_uses_ten_second_socket_and_fifteen_second_watchdog(self):
        self.assertEqual(cb.POST_MUTATION_SOCKET_SECONDS, 10)
        self.assertEqual(cb.POST_MUTATION_WATCHDOG_SECONDS, 15)
        seen = {}

        class Connection:
            sock = None

            def __init__(self, host, timeout):
                seen['socket'] = timeout

            def request(self, *args, **kwargs):
                return None

            def getresponse(self):
                class Response:
                    status = 200

                    def read(self, limit):
                        return b'{}'

                    def getheader(self, name):
                        return None

                return Response()

            def close(self):
                return None

        class Timer:
            def __init__(self, interval, function):
                seen['watchdog'] = interval

            def start(self):
                return None

            def cancel(self):
                return None

        token = cb.begin_upstream_request()
        post = cb.begin_post_mutation()
        try:
            cb._request_deadline.set(time.monotonic() + 0.2)
            with patch.object(cb.http.client, 'HTTPSConnection', Connection), patch.object(cb.threading, 'Timer', Timer):
                self.assertEqual(self.rest._exchange('firestore.googleapis.com', '/v1/known', 'GET') , {})
            self.assertEqual(seen, {'socket': 10, 'watchdog': 15})
        finally:
            cb.end_post_mutation(post)
            cb.end_upstream_request(token)

    def _connection(self, status, body, *, fail=None):
        class Response:
            def __init__(self):
                self.status = status

            def read(self, limit):
                return body

            def getheader(self, name):
                return None

        class Connection:
            sock = None

            def __init__(self, host, timeout):
                self.timeout = timeout

            def request(self, *args, **kwargs):
                if fail is not None:
                    raise fail

            def getresponse(self):
                return Response()

            def close(self):
                pass

        return Connection

    def test_read_stall_is_upstream_unavailable_inside_eight_second_budget(self):
        self.assertEqual(cb.UPSTREAM_BUDGET_SECONDS, 8)
        self.assertLess(cb.UPSTREAM_BUDGET_SECONDS, 15)
        seen = {}

        class Connection:
            sock = None

            def __init__(self, host, timeout):
                seen['timeout'] = timeout
                self.ready = threading.Event()

            def request(self, *args, **kwargs):
                if not self.ready.wait(2):
                    raise AssertionError('watchdog did not stop the stalled read')
                raise socket.timeout('private stall detail')

            def getresponse(self):
                raise AssertionError('stalled read was parsed')

            def close(self):
                self.ready.set()

        class Timer:
            def __init__(self, interval, function):
                seen['budget'] = interval
                self.function = function
                self.daemon = False

            def start(self):
                self.function()

            def cancel(self):
                pass

        with patch.object(cb.http.client, 'HTTPSConnection', Connection), patch.object(cb.threading, 'Timer', Timer):
            started = time.monotonic()
            with self.assertRaisesRegex(cb.UpstreamUnavailable, '^google_upstream_unavailable$') as caught:
                self.rest._exchange('firestore.googleapis.com', '/v1/known', 'GET')
            self.assertLess(time.monotonic() - started, 2)
            with self.assertRaisesRegex(cb.MutationUncertain, '^google_mutation_uncertain$'):
                self.rest._exchange('secretmanager.googleapis.com', '/v1/known:addVersion', 'POST')
        self.assertEqual(seen, {'timeout': 8, 'budget': 8})
        self.assertNotIn('private stall', str(caught.exception))
        self.assertNotIsInstance(caught.exception, cb.Conflict)
        self.assertNotIsInstance(caught.exception, cb.MutationUncertain)
        with patch.object(cb.http.client, 'HTTPSConnection', self._connection(200, b'{}', fail=ConnectionError('refused'))):
            with self.assertRaisesRegex(cb.UpstreamUnavailable, '^google_upstream_unavailable$') as caught:
                self.rest._exchange('firestore.googleapis.com', '/v1/known', 'GET')
        self.assertNotIn('refused', str(caught.exception))

    def test_upstream_http_and_unparsable_reads_are_unavailable_not_conflict(self):
        secret = b'{"error":{"status":"UNAVAILABLE"},"secret":"synthetic-do-not-log"}'
        for status in (429, 500, 502, 503, 504):
            with self.subTest(status=status):
                with patch.object(cb.http.client, 'HTTPSConnection', self._connection(status, secret)):
                    with self.assertRaisesRegex(cb.UpstreamUnavailable, '^google_upstream_unavailable$') as caught:
                        self.rest._exchange('firestore.googleapis.com', '/v1/known', 'GET')
                    self.assertNotIn(b'synthetic-do-not-log', str(caught.exception).encode())
                    with self.assertRaisesRegex(cb.MutationUncertain, '^google_mutation_uncertain$'):
                        self.rest._exchange('secretmanager.googleapis.com', '/v1/known:addVersion', 'POST')
        for status in (409, 412):
            with self.subTest(status=status):
                with patch.object(cb.http.client, 'HTTPSConnection', self._connection(status, secret)):
                    with self.assertRaisesRegex(cb.Conflict, '^control_compare_and_swap_conflict$') as caught:
                        self.rest._exchange('firestore.googleapis.com', '/v1/known', 'GET')
                    self.assertNotIn('synthetic-do-not-log', str(caught.exception))
        denied = b'{"error":{"status":"PERMISSION_DENIED"},"secret":"synthetic-do-not-log"}'
        with patch.object(cb.http.client, 'HTTPSConnection', self._connection(403, denied)):
            with self.assertRaisesRegex(cb.BrokerError, '^google_resource_denied_or_missing$') as caught:
                self.rest._exchange('firestore.googleapis.com', '/v1/known', 'GET')
            self.assertNotIsInstance(caught.exception, (cb.UpstreamUnavailable, cb.Conflict))
        cas = b'{"error":{"status":"FAILED_PRECONDITION"}}'
        with patch.object(cb.http.client, 'HTTPSConnection', self._connection(400, cas)):
            with self.assertRaisesRegex(cb.Conflict, '^control_compare_and_swap_conflict$'):
                self.rest._exchange('firestore.googleapis.com', '/v1/known', 'GET')
        with patch.object(cb.http.client, 'HTTPSConnection', self._connection(200, b'{')):
            with self.assertRaisesRegex(cb.UpstreamUnavailable, '^invalid_google_response$'):
                self.rest._exchange('firestore.googleapis.com', '/v1/known', 'GET')
            with self.assertRaisesRegex(cb.MutationUncertain, '^google_mutation_uncertain$'):
                self.rest._exchange('secretmanager.googleapis.com', '/v1/known:addVersion', 'POST')


if __name__ == '__main__':
    unittest.main()
