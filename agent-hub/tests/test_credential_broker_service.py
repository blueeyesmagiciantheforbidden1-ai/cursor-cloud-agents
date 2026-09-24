"""Offline real HTTP/signature checks; Google wire state is fault injected."""
import base64
import copy
from contextlib import redirect_stdout
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import io
import json
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_hub import credential_broker_service as service
from agent_hub import cloud_credential_broker as backend
from tests.test_cloud_credential_broker import GoogleWire

ENDPOINT = 'https://runcrew-credential-broker-test-uc.a.run.app'
GRANT = 'synthetic_execution_grant_for_offline_tests_' + 'x' * 20
CALLER = {'subject': '123456789012345678901',
          'service_account': 'runcrew-worker-copilot@' + backend.PROJECT_ID + '.iam.gserviceaccount.com'}


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.now = 2000000000
        self.profile = backend.ProfileConfig('copilot', 'blueeyes', 'a' * 64, 'b' * 64,
            'runcrew-credential-copilot-blueeyes', 'runcrew-worker-copilot')
        self.wire = GoogleWire(self.profile)
        self.wire.execution['template'] = {'serviceAccount': CALLER['service_account']}
        self.broker = backend.CloudCredentialBroker(self.profile, rest=self.wire, clock=lambda: self.now)
        self.execution, self.execution_uid = self.wire.execution['name'], self.wire.execution['uid']
        self.binding = service.Binding(self.profile, CALLER['subject'], CALLER['service_account'],
            hashlib.sha256(GRANT.encode()).hexdigest(), self.now + 900)
        self.active = service.ActiveBinding(self.binding, self.execution, self.execution_uid)
        self.grant_store = SimpleNamespace(read=lambda: self.active)
        self.auth = lambda authorization: CALLER if authorization == 'Bearer synthetic.jwt.signature' else {}
        self.service = service.BrokerService(ENDPOINT, [self.binding], authenticator=self.auth,
            broker_factory=lambda *args, **kwargs: self.broker, grant_store_factory=lambda _: self.grant_store,
            clock=lambda: self.now)

    def dispatch(self, action, body, **kwargs):
        return self.service.dispatch(action, kwargs.get('authorization', 'Bearer synthetic.jwt.signature'),
                                     kwargs.get('grant', GRANT), body)

    def acquire(self):
        return self.dispatch('acquire', {'request_id': '1' * 32})

    def test_config_binds_account_subject_grant_and_exact_uid(self):
        document = {'schema_version': 1, 'audience': ENDPOINT, 'bindings': [asdict(self.binding)]}
        audience, bindings = service.parse_service_config(document)
        self.assertEqual(audience, ENDPOINT)
        self.assertEqual(bindings, (self.binding,))
        document['bindings'].append(copy.deepcopy(document['bindings'][0]))
        with self.assertRaises(backend.BrokerError):
            service.parse_service_config(document)

    def test_caller_email_body_and_execution_are_not_authority(self):
        for body in ({'request_id': '1'*32, 'email': CALLER['service_account']},
                     {'request_id': '1'*32, 'execution': self.execution},
                     {'request_id': '1'*32, 'provider': 'codex'}):
            with self.assertRaises(backend.BrokerError):
                self.dispatch('acquire', body)
        for change in ({'subject': '999999'}, {'service_account': 'attacker@example.com'}):
            with patch.object(self.service, 'authenticator', return_value={**CALLER, **change}), self.assertRaises(backend.BrokerError):
                self.acquire()
        self.assertEqual(self.wire.secret_reads, 0)

    def test_wrong_expired_grant_and_changed_execution_uid_rejected_before_secret_read(self):
        with self.assertRaises(backend.BrokerError):
            self.dispatch('acquire', {'request_id':'1'*32}, grant='q'*43)
        self.now += 901
        with self.assertRaises(backend.BrokerError):
            self.acquire()
        self.now -= 901
        self.wire.execution['uid'] = 'f'*32
        with self.assertRaises(backend.BrokerError):
            self.acquire()
        self.assertEqual(self.wire.secret_reads, 0)

    def test_wrong_actual_service_account_and_terminal_execution_rejected(self):
        self.wire.execution['template']['serviceAccount'] = 'another'
        with self.assertRaises(backend.BrokerError):
            self.acquire()
        self.wire.execution['template']['serviceAccount'] = CALLER['service_account']
        self.wire.terminal()
        with self.assertRaises(backend.BrokerError):
            self.acquire()
        self.assertEqual(self.wire.secret_reads, 0)

    def test_unchanged_commit_does_not_create_paid_version(self):
        receipt = self.acquire()
        result = self.dispatch('commit', {'lease': receipt['lease'], 'credential_b64': receipt['credential_b64']})
        self.assertEqual(self.wire.add_calls, 0)
        self.dispatch('release', {'lease': receipt['lease'], 'version': result['version']})
        self.assertEqual(self.wire.state['phase'], 'idle')

    def test_refresh_commit_and_release_keep_backend_fencing(self):
        receipt = self.acquire()
        body = {'lease': receipt['lease'], 'credential_b64': base64.b64encode(b'synthetic-refreshed').decode()}
        result = self.dispatch('commit', body)
        self.assertEqual(self.dispatch('commit', body), result)
        self.dispatch('release', {'lease': receipt['lease'], 'version': result['version']})
        reads_before = self.wire.secret_reads
        with self.assertRaisesRegex(backend.BrokerError, '^execution_already_consumed$'):
            self.dispatch('acquire', {'request_id': '2'*32})
        self.assertEqual(self.wire.secret_reads, reads_before)
        # A successor gets a distinct execution and independently published
        # immutable grant; never rewrite/reuse the prior execution grant.
        self.wire.new_execution()
        self.wire.execution['template'] = {'serviceAccount': CALLER['service_account']}
        next_grant = 'second_execution_grant_for_offline_tests_' + 'y'*20
        next_binding = replace(self.binding, grant_sha256=hashlib.sha256(next_grant.encode()).hexdigest())
        next_active = service.ActiveBinding(next_binding, self.wire.execution['name'], self.wire.execution['uid'])
        next_store = SimpleNamespace(read=lambda: next_active)
        self.service = service.BrokerService(ENDPOINT, [next_binding], authenticator=self.auth,
            broker_factory=lambda *args, **kwargs: self.broker, grant_store_factory=lambda _: next_store,
            clock=lambda: self.now)
        successor = self.dispatch('acquire', {'request_id': '2'*32}, grant=next_grant)
        self.assertEqual(base64.b64decode(successor['credential_b64']), b'synthetic-refreshed')
        with self.assertRaises(backend.BrokerError):
            self.dispatch('renew', {'lease':receipt['lease']}, grant=next_grant)
        self.assertEqual(self.wire.add_calls, 1)

    def test_missing_commit_cannot_release_and_no_reconciliation_endpoint(self):
        receipt = self.acquire()
        with self.assertRaises(backend.BrokerError):
            self.dispatch('release', {'lease':receipt['lease'], 'version':receipt['lease']['version']})
        with self.assertRaises(backend.BrokerError):
            self.dispatch('reconcile', {})
        self.assertEqual(self.wire.state['phase'], 'leased')

    def test_uncertain_writeback_quarantines_without_second_add(self):
        receipt = self.acquire()
        self.wire.lose_add_ack = True
        body = {'lease':receipt['lease'], 'credential_b64':base64.b64encode(b'refresh').decode()}
        for _ in range(2):
            with self.assertRaises(backend.BrokerError):
                self.dispatch('commit', body)
        self.assertEqual(self.wire.state['phase'], 'quarantined')
        self.assertEqual(self.wire.add_calls, 1)

    def test_duplicate_json_and_oversized_credential_rejected(self):
        for raw in (b'{"request_id":1,"request_id":2}', b'{"x":NaN}', b'{}' * service.MAX_BODY):
            with self.assertRaises(backend.BrokerError):
                service.decode(raw)
        with self.assertRaises(backend.BrokerError):
            service.opaque_decode(base64.b64encode(b'x'*65537).decode())

    def test_real_loopback_http_roundtrip_with_existing_backend(self):
        server = service.BrokerServer(('127.0.0.1', 0), self.service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def wire(host, path, *, method='GET', body=None, headers=None, metadata=False, limit=service.MAX_BODY, timeout_seconds=15):
            if metadata:
                self.assertEqual(host, 'metadata.google.internal')
                self.assertIn('audience=https%3A%2F%2F', path)
                return 200, {'Metadata-Flavor':'Google'}, b'synthetic.jwt.signature'
            self.assertEqual(host, service.endpoint_host(ENDPOINT))
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            try:
                connection.request(method, path, body=body, headers=headers)
                response = connection.getresponse()
                return response.status, dict(response.getheaders()), response.read(limit+1)
            finally:
                connection.close()
        try:
            client = service.BrokerHTTPClient(self.profile, endpoint=ENDPOINT, execution=self.execution, grant=GRANT)
            with patch.object(service, 'exchange', side_effect=wire):
                client.bootstrap(timeout_seconds=3)
                self.assertEqual(client.execution_uid, self.execution_uid)
                lease = client.acquire(client.execution, '1'*32)
                client.assert_current(lease)
                client.renew(lease)
                version = client.commit(lease, b'synthetic-http-refreshed')
                client.release(lease, version)
            self.assertEqual(self.wire.versions[version], b'synthetic-http-refreshed')
            self.assertEqual(self.wire.state['phase'], 'idle')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)

    def test_http_duplicate_auth_no_credential_leak_and_redaction(self):
        server = service.BrokerServer(('127.0.0.1', 0), self.service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
        try:
            body = b'{"request_id":"11111111111111111111111111111111"}'
            connection.putrequest('POST', service.PREFIX+'acquire')
            for name, value in (('Content-Length',str(len(body))),('Content-Type','application/json'),
                    ('Authorization','Bearer synthetic.jwt.signature'),('Authorization','Bearer untrusted.raw.secret'),
                    ('X-RunCrew-Execution-Grant',GRANT)):
                connection.putheader(name,value)
            connection.endheaders(body)
            response = connection.getresponse()
            raw = response.read()
            self.assertEqual(response.status, 400)
            self.assertEqual(json.loads(raw), {'error':'request_invalid'})
            self.assertEqual(self.wire.secret_reads, 0)
            for error in (Exception('raw secret'),backend.BrokerError('raw secret'),backend.MutationUncertain('raw secret'),
                          backend.UpstreamUnavailable('raw secret'), backend.Conflict('raw secret')):
                self.assertNotIn('raw secret', json.dumps(service.error_reply(error)))
            status, body, headers = service.error_reply(backend.UpstreamUnavailable('raw secret'))
            self.assertEqual((status, body, headers), (503, {'error': 'broker_upstream_unavailable'}, {'Retry-After': '2'}))
            status, body, headers = service.error_reply(backend.BrokerError('credential_lease_not_active'))
            self.assertEqual((status, body), (409, {'error': 'broker_operation_rejected'}))
            self.assertFalse(headers)
            status, body, headers = service.error_reply(backend.Conflict('control_compare_and_swap_conflict'))
            self.assertEqual((status, body), (409, {'error': 'broker_conflict'}))
            self.assertFalse(headers)
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            thread.join(3)

    def test_client_timeout_is_uncertain_and_never_retries(self):
        client = service.BrokerHTTPClient(self.profile, endpoint=ENDPOINT, execution=self.execution,
            execution_uid=self.execution_uid, grant=GRANT)
        with patch.object(client, '_id_token', return_value='synthetic.jwt.signature'), patch.object(
                service, 'exchange', side_effect=TimeoutError('private raw error')) as transport:
            with self.assertRaisesRegex(backend.MutationUncertain, '^broker_http_outcome_uncertain$'):
                client.acquire(client.execution, '1'*32)
        self.assertEqual(transport.call_count, 1)

    def test_client_endpoint_and_lease_scope_reject_before_transport(self):
        for url in ('http://broker.run.app', 'https://broker.run.app@attacker.test', ENDPOINT+'/',
                    ENDPOINT+':443', ENDPOINT+'?x=1', 'https://broker.run.app.attacker.test'):
            with self.assertRaises(backend.BrokerError):
                service.endpoint_host(url)
        client = service.BrokerHTTPClient(self.profile, endpoint=ENDPOINT, execution=self.execution,
            execution_uid=self.execution_uid, grant=GRANT)
        with patch.object(service, 'exchange') as transport, self.assertRaises(backend.BrokerError):
            client.acquire(self.execution+'different', '1'*32)
        transport.assert_not_called()

    def test_bootstrap_pending_does_not_read_credentials_or_acquire_lease(self):
        self.grant_store.read = lambda: None
        self.assertEqual(self.dispatch('bootstrap', {}), {'ready':False})
        self.assertEqual(self.wire.state['phase'], 'idle')
        self.assertEqual(self.wire.secret_reads, 0)
        with self.assertRaises(backend.BrokerError):
            self.acquire()
        self.grant_store.read = lambda: self.active
        self.assertEqual(self.dispatch('bootstrap', {}), {'ready':True, 'execution':self.execution,
                                                        'execution_uid':self.execution_uid})
        self.assertEqual(self.wire.secret_reads, 0)

    def test_bootstrap_polls_only_readiness_then_binds_authoritative_uid(self):
        client = service.BrokerHTTPClient(self.profile, endpoint=ENDPOINT, execution=self.execution, grant=GRANT)
        replies = [(202,{},b'{"ready":false}'), (200,{},service.encode({'ready':True,
            'execution':self.execution, 'execution_uid':self.execution_uid}))]
        with patch.object(client, '_id_token', return_value='synthetic.jwt.signature'), patch.object(
                service, 'exchange', side_effect=replies) as transport, patch.object(service.time, 'sleep'):
            client.bootstrap(timeout_seconds=2)
        self.assertEqual(client.execution_uid, self.execution_uid)
        self.assertEqual([call.args[1] for call in transport.call_args_list], [service.PREFIX+'bootstrap']*2)
        self.assertEqual([call.kwargs['body'] for call in transport.call_args_list], [b'{}']*2)

    def test_bootstrap_deadline_and_wrong_execution_cannot_launch(self):
        client = service.BrokerHTTPClient(self.profile, endpoint=ENDPOINT, execution=self.execution, grant=GRANT)
        with patch.object(client, '_id_token', return_value='synthetic.jwt.signature'), patch.object(
                service, 'exchange', return_value=(202,{},b'{"ready":false}')) as transport:
            with self.assertRaisesRegex(backend.BrokerError, '^execution_bootstrap_deadline_exceeded$'):
                client.bootstrap(timeout_seconds=1)
        self.assertIsNone(client.execution_uid)
        with self.assertRaises(backend.BrokerError):
            client.acquire(self.execution,'1'*32)
        with patch.object(client, '_id_token', return_value='synthetic.jwt.signature'), patch.object(
                service, 'exchange', return_value=(200,{},service.encode({'ready':True,
                    'execution':self.execution+'other','execution_uid':self.execution_uid}))):
            with self.assertRaises(backend.BrokerError):
                client.bootstrap(timeout_seconds=1)

    def test_controller_publication_is_exact_immutable_and_lost_ack_is_read_resolved(self):
        store = service.ExecutionGrantStore(self.binding, rest=self.wire, clock=lambda:self.now)
        document = None
        lost = False
        writes = []
        def request(method, value=None):
            nonlocal document, lost
            if method == 'GET':
                return document
            self.assertEqual(value['writes'][0]['currentDocument'], {'exists':False})
            self.assertEqual(value['writes'][0]['update']['name'], store.document)
            if document is not None:
                raise backend.Conflict('already_published')
            writes.append(value)
            document = value['writes'][0]['update']
            if lost:
                raise backend.MutationUncertain('lost_ack')
            return {'writeResults':[{'updateTime':'2026-09-21T00:00:00Z'}]}
        with patch.object(store, '_request', side_effect=request):
            self.assertIsNone(store.read())
            lost = True
            self.assertEqual(store.publish(self.execution,self.execution_uid), self.active)
            self.assertEqual(store.read(),self.active)
            with self.assertRaises(backend.Conflict):
                store.publish(self.execution,self.execution_uid)
        self.assertEqual(len(writes),1)
        self.assertEqual(self.wire.secret_reads,0)

    def test_controller_rejects_forged_uid_before_publication(self):
        store = service.ExecutionGrantStore(self.binding, rest=self.wire, clock=lambda:self.now)
        with patch.object(store, '_request') as request, self.assertRaises(backend.BrokerError):
            store.publish(self.execution,'f'*32)
        request.assert_not_called()

    def test_grant_read_403_is_not_bootstrap_pending_and_scope_is_fixed(self):
        rest = SimpleNamespace(_token=lambda:'synthetic-access-token')
        store = service.ExecutionGrantStore(self.binding, rest=rest, clock=lambda:self.now)
        with patch.object(service, 'exchange', return_value=(404,{},b'{}')) as transport:
            self.assertIsNone(store.read())
            self.assertEqual(transport.call_args.args, ('firestore.googleapis.com','/v1/'+store.document))
        with patch.object(service, 'exchange', return_value=(403,{},b'{}')):
            with self.assertRaisesRegex(backend.BrokerError,'^execution_grant_access_denied$'):
                store.read()

    def test_loader_uses_platform_execution_not_task_uid_and_waits_before_return(self):
        document = {'schema_version':1,'endpoint':ENDPOINT,'profile':asdict(self.profile),
                    'grant':GRANT,'bootstrap_timeout_seconds':120}
        with patch.object(service,'read_protected',return_value=document), patch.dict(service.os.environ,
                {'CLOUD_RUN_EXECUTION':self.execution.rsplit('/',1)[1]}), patch.object(
                service.BrokerHTTPClient,'bootstrap') as bootstrap:
            client = service.load_client_config('/run/config/client.json')
        self.assertEqual(client.execution,self.execution)
        bootstrap.assert_called_once_with(timeout_seconds=120)

    def _client(self, resolved=False):
        return service.BrokerHTTPClient(self.profile, endpoint=ENDPOINT, execution=self.execution,
            execution_uid=self.execution_uid if resolved else None, grant=GRANT)

    def _lease(self):
        return backend.Lease(self.profile.profile, self.profile.account_ref, self.profile.canonical_account_ref,
            1, self.profile.secret_name + '/versions/1', '1' * 32, self.execution, self.execution_uid, b'')

    def test_worker_treats_503_as_transient_and_never_as_conflict(self):
        self.assertEqual(service.exchange.__kwdefaults__['timeout_seconds'], 15)
        self.assertEqual(backend.UPSTREAM_BUDGET_SECONDS, 8)
        unavailable = (503, {'Retry-After': '2'}, b'{"error":"broker_upstream_unavailable"}')
        polling = service.BrokerHTTPClient(self.profile, endpoint=ENDPOINT, execution=self.execution, grant=GRANT)
        ready = (200, {}, service.encode({'ready': True, 'execution': self.execution, 'execution_uid': self.execution_uid}))
        with patch.object(polling, '_id_token', return_value='synthetic.jwt.signature'), patch.object(
                service, 'exchange', side_effect=[unavailable, ready]) as transport, patch.object(service.time, 'sleep'):
            polling.bootstrap(timeout_seconds=3)
        self.assertEqual(polling.execution_uid, self.execution_uid)
        self.assertEqual(transport.call_count, 2)
        client = self._client(resolved=True)
        lease = self._lease()
        bodies = []
        def transport(*args, **kwargs):
            bodies.append(kwargs.get('body'))
            return unavailable
        with patch.object(client, '_id_token', return_value='synthetic.jwt.signature'), patch.object(
                service, 'exchange', side_effect=transport) as calls:
            with self.assertRaises(backend.MutationUncertain) as caught:
                client.renew(lease)
            self.assertNotIsInstance(caught.exception, backend.Conflict)
            with self.assertRaises(backend.MutationUncertain) as caught:
                client.acquire(client.execution, '1' * 32)
            self.assertNotIsInstance(caught.exception, backend.Conflict)
        self.assertEqual(calls.call_count, 2)
        self.assertEqual(bodies[1], service.encode({'request_id': '1' * 32}))
        self.assertEqual(bodies[1].count(b'request_id'), 1)
        with patch.object(client, '_id_token', return_value='synthetic.jwt.signature'), patch.object(
                service, 'exchange', return_value=(503, {}, b'<html>raw secret</html>')):
            with self.assertRaisesRegex(backend.MutationUncertain, '^broker_http_outcome_uncertain$') as caught:
                client.renew(lease)
            self.assertNotIn('raw secret', str(caught.exception))
            self.assertNotIsInstance(caught.exception, backend.Conflict)
        denied = service.BrokerHTTPClient(self.profile, endpoint=ENDPOINT, execution=self.execution, grant=GRANT)
        with patch.object(denied, '_id_token', return_value='synthetic.jwt.signature'), patch.object(
                service, 'exchange', return_value=(409, {}, b'{"error":"broker_operation_rejected"}')) as transport, \
                patch.object(service.time, 'sleep'):
            with self.assertRaisesRegex(backend.BrokerError, '^bootstrap_not_authorized$'):
                denied.bootstrap(timeout_seconds=3)
        self.assertEqual(transport.call_count, 1)
        self.assertIsNone(denied.execution_uid)

    def test_grant_read_stall_is_upstream_unavailable_within_budget(self):
        rest = SimpleNamespace(_token=lambda: 'synthetic-access-token')
        store = service.ExecutionGrantStore(self.binding, rest=rest, clock=lambda: self.now)
        with patch.object(service, 'exchange', return_value=(503, {}, b'raw secret')) as transport:
            with self.assertRaisesRegex(backend.UpstreamUnavailable, '^execution_grant_read_unavailable$') as caught:
                store.read()
            self.assertNotIn('raw secret', str(caught.exception))
            self.assertNotIsInstance(caught.exception, backend.Conflict)
            self.assertEqual(transport.call_args.kwargs['timeout_seconds'], backend.UPSTREAM_BUDGET_SECONDS)
        with patch.object(service, 'exchange', side_effect=TimeoutError('stalled raw secret')):
            with self.assertRaises(backend.UpstreamUnavailable) as caught:
                store.read()
            self.assertNotIn('stalled', str(caught.exception))
            self.assertNotIn('raw secret', str(caught.exception))
        with patch.object(service, 'exchange', return_value=(409, {}, b'{}')):
            with self.assertRaises(backend.Conflict):
                store.read()
        with patch.object(service, 'exchange', return_value=(200, {}, b'{')):
            with self.assertRaises(backend.UpstreamUnavailable):
                store.read()

    def test_http_stall_is_503_and_log_has_no_secret_material(self):
        email = 'person@example.com'
        canary = 'do-not-log-canary-secret'
        version = self.profile.secret_name + '/versions/1'
        lease_id = '1' * 32
        server = service.BrokerServer(('127.0.0.1', 0), self.service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        buffer = io.StringIO()

        def post(kind):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            try:
                if kind == 'get':
                    connection.request('GET', service.PREFIX + 'bootstrap')
                else:
                    connection.request('POST', service.PREFIX + 'bootstrap', body=b'{}', headers={
                        'Content-Type': 'application/json',
                        'Authorization': 'Bearer synthetic.jwt.signature',
                        'X-RunCrew-Execution-Grant': GRANT,
                        'X-Email': email,
                        'X-Lease-Id': lease_id,
                        'X-Version': version,
                    })
                response = connection.getresponse()
                return response.status, response.getheader('Retry-After'), response.read()
            finally:
                connection.close()

        try:
            with redirect_stdout(buffer):
                self.grant_store.read = lambda: (_ for _ in ()).throw(backend.UpstreamUnavailable(canary))
                stall = post('bootstrap')
                self.grant_store.read = lambda: (_ for _ in ()).throw(backend.Conflict(canary))
                conflict = post('bootstrap')
                self.grant_store.read = lambda: (_ for _ in ()).throw(backend.BrokerError(canary))
                rejected = post('bootstrap')
                self.grant_store.read = lambda: self.active
                ready = post('bootstrap')
                other = post('get')
                deadline = time.monotonic() + 2
                while buffer.getvalue().count('\n') < 5 and time.monotonic() < deadline:
                    time.sleep(0.01)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)
        self.assertEqual(stall, (503, '2', b'{"error":"broker_upstream_unavailable"}'))
        self.assertEqual(conflict[0], 409)
        self.assertIsNone(conflict[1])
        self.assertEqual(json.loads(conflict[2]), {'error': 'broker_conflict'})
        self.assertEqual(rejected[0], 409)
        self.assertIsNone(rejected[1])
        self.assertEqual(json.loads(rejected[2]), {'error': 'broker_operation_rejected'})
        self.assertEqual(ready[0], 200)
        self.assertIn(self.execution_uid.encode(), ready[2])
        self.assertEqual(other[0], 405)
        text = buffer.getvalue()
        for secret in (GRANT, 'synthetic.jwt.signature', email, canary, version, lease_id, self.execution_uid):
            self.assertNotIn(secret, text)
        lines = [json.loads(line) for line in text.splitlines() if line]
        self.assertEqual(len(lines), 5)
        expected = [
            ('bootstrap', 503, 'broker_upstream_unavailable'),
            ('bootstrap', 409, 'broker_conflict'),
            ('bootstrap', 409, 'broker_operation_rejected'),
            ('bootstrap', 200, 'ok'),
            ('unknown', 405, 'method_not_allowed'),
        ]
        for line, (action, status, code) in zip(lines, expected):
            self.assertEqual(list(line), ['kind', 'action', 'status', 'code', 'duration_ms'])
            self.assertEqual(line['kind'], 'runcrew_broker_request')
            self.assertEqual((line['action'], line['status'], line['code']), (action, status, code))
            self.assertIs(type(line['status']), int)
            self.assertIs(type(line['duration_ms']), int)
            self.assertGreaterEqual(line['duration_ms'], 0)

    def _serve(self):
        server = service.BrokerServer(('127.0.0.1', 0), self.service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def _stop(self, server, thread):
        server.shutdown()
        server.server_close()
        thread.join(3)

    def _post(self, server, action, body, timeout=3):
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=timeout)
        try:
            connection.request('POST', service.PREFIX + action, body=json.dumps(body).encode(), headers={
                'Content-Type': 'application/json',
                'Authorization': 'Bearer synthetic.jwt.signature',
                'X-RunCrew-Execution-Grant': GRANT,
            })
            response = connection.getresponse()
            return response.status, response.getheader('Retry-After'), response.read()
        finally:
            connection.close()

    def _stall_rest(self, mode):
        wire = self.wire

        class Rest:
            def __init__(self):
                self.gets = 0

            def request(self, method, host, path, value=None):
                if method == 'GET' and host == 'secretmanager.googleapis.com' and mode == 'access':
                    raise backend.UpstreamUnavailable('google_upstream_unavailable')
                if host == 'firestore.googleapis.com' and method == 'GET':
                    self.gets += 1
                    if mode == 'assert' and self.gets == 2:
                        raise backend.UpstreamUnavailable('google_upstream_unavailable')
                return wire.request(method, host, path, value)

        self.broker.rest = Rest()

    def _assert_clean_release(self):
        version = self.profile.secret_name + '/versions/1'
        state = self.wire.state
        self.assertEqual(state['phase'], 'idle')
        self.assertEqual(state['quarantine_reason'], '')
        self.assertEqual(state['lease_until_ms'], 0)
        self.assertEqual(state['last_release_id'], '1' * 32)
        self.assertEqual(state['last_release_fence'], 1)
        self.assertEqual(state['last_release_version'], version)
        self.assertEqual(state['version'], version)
        self.assertEqual(state['fence'], 1)

    def test_post_lease_stall_answers_503_and_idles_the_lease(self):
        server, thread = self._serve()
        try:
            self._stall_rest('access')
            status, retry, raw = self._post(server, 'acquire', {'request_id': '1' * 32})
            self.assertEqual((status, retry), (503, '2'))
            self.assertEqual(json.loads(raw), {'error': 'broker_upstream_unavailable'})
            self.assertNotIn(b'credential_b64', raw)
            self.assertNotIn(b'opaque', raw)
            self._assert_clean_release()
            self.assertEqual(self.wire.secret_reads, 0)
            self.wire.state = backend.initial_control_state(
                self.profile, self.profile.secret_name + '/versions/1', 'c' * 64)
            self.wire.revision = 1
            self._stall_rest('assert')
            status, retry, raw = self._post(server, 'acquire', {'request_id': '1' * 32})
            self.assertEqual((status, retry), (503, '2'))
            self.assertEqual(json.loads(raw), {'error': 'broker_upstream_unavailable'})
            self.assertNotIn(b'credential_b64', raw)
            self._assert_clean_release()
            self.assertEqual(self.wire.secret_reads, 1)
        finally:
            self._stop(server, thread)

    def test_request_budget_stops_five_second_calls_with_503(self):
        timeouts = []
        completed = []

        class Connection:
            sock = None

            def __init__(self, host, timeout):
                timeouts.append(timeout)
                self.closed = threading.Event()

            def request(self, *args, **kwargs):
                if self.closed.wait(5):
                    raise TimeoutError('stalled')

            def getresponse(self):
                completed.append(timeouts[-1])

                class Response:
                    status = 200

                    def read(self, limit):
                        return b'{}'

                    def getheader(self, name):
                        return None

                return Response()

            def close(self):
                self.closed.set()

        rest = backend.GoogleREST(self.profile)

        def read():
            for _ in range(4):
                rest._exchange('firestore.googleapis.com', '/v1/documents/stacked', 'GET')
            return self.active

        self.grant_store.read = read
        server, thread = self._serve()
        started = time.monotonic()
        try:
            with patch.object(backend.http.client, 'HTTPSConnection', Connection):
                status, retry, raw = self._post(server, 'bootstrap', {}, timeout=20)
            elapsed = time.monotonic() - started
        finally:
            self._stop(server, thread)
        self.assertEqual((status, retry), (503, '2'))
        self.assertEqual(json.loads(raw), {'error': 'broker_upstream_unavailable'})
        self.assertGreaterEqual(elapsed, 10)
        self.assertLess(elapsed, 15)
        self.assertEqual(timeouts[0], 8)
        self.assertTrue(all(item <= 8 for item in timeouts))
        self.assertLess(len(timeouts), 4)
        self.assertLessEqual(len(completed), 2)
        self.assertTrue(any(item < 8 for item in timeouts))

    def test_fast_upstream_call_keeps_the_eight_second_cap(self):
        timeouts = []

        class Connection:
            sock = None

            def __init__(self, host, timeout):
                timeouts.append(timeout)

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

        rest = backend.GoogleREST(self.profile)

        def read():
            rest._exchange('firestore.googleapis.com', '/v1/documents/fast', 'GET')
            return self.active

        self.grant_store.read = read
        server, thread = self._serve()
        started = time.monotonic()
        try:
            with patch.object(backend.http.client, 'HTTPSConnection', Connection):
                status, _, raw = self._post(server, 'bootstrap', {})
            elapsed = time.monotonic() - started
        finally:
            self._stop(server, thread)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)['ready'], True)
        self.assertLess(elapsed, 2)
        self.assertEqual(timeouts, [8])
        self.assertIsNone(backend._request_deadline.get())
        token = backend.begin_upstream_request()
        try:
            backend._request_deadline.set(time.monotonic() + 0.2)
            with patch.object(service.http.client, 'HTTPSConnection') as connection:
                with self.assertRaisesRegex(backend.UpstreamUnavailable, '^upstream_request_budget_exhausted$'):
                    service.exchange('firestore.googleapis.com', '/v1/documents/floor')
                connection.assert_not_called()
        finally:
            backend.end_upstream_request(token)
        self.assertEqual(service.exchange.__kwdefaults__['timeout_seconds'], 15)


class SignedIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        from google.auth.crypt import RSASigner
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = datetime.now(timezone.utc)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'offline-test')])
        certificate = x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(
            x509.random_serial_number()).not_valid_before(now-timedelta(days=1)).not_valid_after(now+timedelta(days=1)).sign(key, hashes.SHA256())
        cls.signer = RSASigner.from_string(key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()), key_id='offline')
        cls.certs = json.dumps({'offline':certificate.public_bytes(serialization.Encoding.PEM).decode()}).encode()

    def token(self, **changes):
        from google.auth import jwt
        now = int(time.time())
        return jwt.encode(self.signer, {'iss':'https://accounts.google.com','aud':ENDPOINT,'sub':CALLER['subject'],
            'email':CALLER['service_account'],'email_verified':True,'iat':now,'exp':now+3600,**changes}).decode()

    def test_actual_google_auth_signature_claim_verification(self):
        auth = service.GoogleIDAuthenticator(ENDPOINT)
        with patch.object(service, 'exchange', return_value=(200,{},self.certs)) as exchange:
            self.assertEqual(auth('Bearer '+self.token()), CALLER)
            for changes in ({'aud':'https://other.run.app'}, {'iss':'https://attacker.test'},
                            {'email_verified':False}, {'exp':int(time.time())-60}, {'sub':'email@example.com'}):
                with self.assertRaisesRegex(backend.BrokerError, '^authentication_required$'):
                    auth('Bearer '+self.token(**changes))
            signed = self.token()
            parts = signed.split('.')
            parts[1] = base64.urlsafe_b64encode(b'{"sub":"attacker"}').decode().rstrip('=')
            with self.assertRaisesRegex(backend.BrokerError, '^authentication_required$'):
                auth('Bearer '+'.'.join(parts))
        self.assertEqual(exchange.call_count, 1)

    def test_unsigned_stripped_tokens_never_accepted(self):
        auth = service.GoogleIDAuthenticator(ENDPOINT)
        for raw in ('Bearer x.y.', 'Bearer x.y.SIGNATURE_REMOVED_BY_GOOGLE', 'Bearer '+self.token()+' secret'):
            with patch.object(service, 'exchange', return_value=(200,{},self.certs)), self.assertRaises(backend.BrokerError):
                auth(raw)


if __name__ == '__main__':
    unittest.main()
