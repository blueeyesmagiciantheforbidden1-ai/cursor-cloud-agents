"""Offline real HTTP/signature checks; Google wire state is fault injected."""
import base64
import copy
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import hashlib
import http.client
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
            for error in (Exception('raw secret'),backend.BrokerError('raw secret'),backend.MutationUncertain('raw secret')):
                self.assertNotIn('raw secret', json.dumps(service.error_reply(error)))
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
