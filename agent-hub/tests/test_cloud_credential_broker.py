"""Fault-injected Google wire-schema tests; no cloud, login, tokens or inference."""
import base64
import copy
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
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
            with self.assertRaisesRegex(cb.BrokerError, '^google_read_failed$'):
                self.rest._exchange('firestore.googleapis.com', '/v1/known', 'GET')
            with self.assertRaisesRegex(cb.MutationUncertain, '^google_mutation_uncertain$'):
                self.rest._exchange('secretmanager.googleapis.com', '/v1/known:addVersion', 'POST')
        self.assertEqual(len(Connection.calls), 2)

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
            with self.assertRaisesRegex(cb.BrokerError, '^google_response_limit$'):
                self.rest._exchange('firestore.googleapis.com', '/v1/known', 'GET')
        self.assertEqual(response.limit, cb.MAX_RESPONSE_BYTES + 1)


if __name__ == '__main__':
    unittest.main()
