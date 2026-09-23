"""Fenced opaque provider credentials over Google REST, standard library only.

Run in a trusted control-plane process, never import into model-controlled tools.
No provider login/refresh code, credential logging, environment export, automatic
takeover, generic URL, ambient ADC, or retry of Secret Manager addVersion exists.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import http.client
import json
import re
import socket
import threading
import time
import uuid

PROJECT_ID = 'project-0c6d31fa-509e-4116-a2c'
PROJECT_NUMBER = '496481413971'
REGION = 'us-central1'
DATABASE_ID = 'runcrew-provider-auth'
DATABASE = f'projects/{PROJECT_ID}/databases/{DATABASE_ID}'
COLLECTION = 'runcrew_provider_credentials'
MAX_CREDENTIAL_BYTES = 64 * 1024  # Secret Manager's actual per-version limit.
MAX_RESPONSE_BYTES = 128 * 1024
MAX_CONTROL_BYTES = 16 * 1024
_DIGEST = re.compile(r'[a-f0-9]{64}')
_ID = re.compile(r'[a-f0-9]{32}')
_STAMP = re.compile(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,9})?Z')
_PROVIDERS = frozenset(('codex', 'claude', 'cursor', 'copilot', 'grok'))
_PHASES = frozenset(('idle', 'leased', 'committing', 'committed', 'quarantined'))
_REASONS = frozenset(('credential_read_failed', 'writeback_uncertain', 'provider_refresh_uncertain',
                      'lease_expired', 'manual_reconciliation_required'))


class BrokerError(RuntimeError):
    """Only fixed, nonsecret error codes may cross the broker boundary."""


class Conflict(BrokerError):
    pass


class MutationUncertain(BrokerError):
    pass


def _require(condition, code):
    if not condition:
        raise BrokerError(code)


def _json(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError('duplicate')
            value[key] = item
        return value
    try:
        value = json.loads(raw, object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite')))
        _require(isinstance(value, dict), 'invalid_google_response')
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise BrokerError('invalid_google_response') from None


def crc32c(body: bytes) -> int:
    """Castagnoli CRC; Secret Manager checks this independently of transport."""
    crc = 0xffffffff
    for byte in body:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0x82f63b78 if crc & 1 else 0)
    return crc ^ 0xffffffff


def _opaque(value):
    _require(isinstance(value, bytes) and 0 < len(value) <= MAX_CREDENTIAL_BYTES,
             'credential_size_invalid')
    return value


def canonical_identity_ref(provider, issuer, subject, tenant=''):
    """Hash verified provider identity fields, never infer them from credentials.

    The caller must obtain immutable subject and tenant from authenticated
    provider evidence during enrollment. This function cannot verify evidence.
    """
    _require(isinstance(provider, str) and provider in _PROVIDERS, 'profile_invalid')
    _require(all(isinstance(value, str) and len(value) <= 1024
                 and not any(ord(char) < 32 for char in value) for value in (issuer, subject, tenant))
             and issuer and subject, 'canonical_identity_invalid')
    encoded = json.dumps({'schema': 1, 'provider': provider, 'issuer': issuer,
                          'subject': subject, 'tenant': tenant}, sort_keys=True,
                         ensure_ascii=True, separators=(',', ':')).encode('ascii')
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ProfileConfig:
    """Protected operator configuration, independently matched to backend state.

    canonical_account_ref must come from the provider's authenticated immutable
    subject/account/tenant identity, not its mutable email or a task assertion.
    account_ref remains the existing worker owner's independent allowlist hash.
    """
    provider: str
    profile: str
    account_ref: str
    canonical_account_ref: str
    secret_id: str
    job_id: str

    def __post_init__(self):
        _require(isinstance(self.provider, str) and self.provider in _PROVIDERS
                 and isinstance(self.profile, str) and self.profile in ('ryan', 'blueeyes'), 'profile_invalid')
        _require(all(isinstance(item, str) and _DIGEST.fullmatch(item)
                     for item in (self.account_ref, self.canonical_account_ref)), 'account_binding_invalid')
        _require(self.account_ref != self.canonical_account_ref, 'canonical_identity_required')
        _require(self.secret_id == f'runcrew-credential-{self.provider}-{self.profile}', 'secret_scope_invalid')
        _require(isinstance(self.job_id, str) and re.fullmatch(
            rf'runcrew-worker-{self.provider}(?:-{self.profile})?', self.job_id), 'job_scope_invalid')

    @property
    def secret_name(self):
        return f'projects/{PROJECT_NUMBER}/secrets/{self.secret_id}'

    @property
    def document_name(self):
        # Profile aliases cannot create independent locks for the same account.
        return f'{DATABASE}/documents/{COLLECTION}/{self.provider}-{self.canonical_account_ref}'

    @property
    def job_name(self):
        return f'projects/{PROJECT_NUMBER}/locations/{REGION}/jobs/{self.job_id}'

    def binding(self):
        return {'schema': 1, 'provider': self.provider, 'profile': self.profile,
                'account_ref': self.account_ref, 'canonical_account_ref': self.canonical_account_ref,
                'secret_name': self.secret_name, 'job_name': self.job_name}


@dataclass(frozen=True)
class Lease:
    profile: str
    account_ref: str
    canonical_account_ref: str
    fence: int
    version: str
    lease_id: str
    execution: str
    execution_uid: str
    auth_bytes: bytes = field(repr=False, compare=False)


def _version(config, name):
    _require(isinstance(name, str), 'exact_secret_version_required')
    prefix = f'projects/{PROJECT_ID}/'
    if name.startswith(prefix):
        name = f'projects/{PROJECT_NUMBER}/' + name[len(prefix):]
    _require(re.fullmatch(re.escape(config.secret_name) + r'/versions/[1-9][0-9]{0,18}', name),
             'exact_secret_version_required')
    return name


def _execution(config, name):
    _require(isinstance(name, str), 'execution_scope_invalid')
    prefix = f'projects/{PROJECT_ID}/'
    if name.startswith(prefix):
        name = f'projects/{PROJECT_NUMBER}/' + name[len(prefix):]
    _require(re.fullmatch(re.escape(config.job_name) + r'/executions/' + re.escape(config.job_id)
                         + r'-[a-z0-9]{5,20}', name), 'execution_scope_invalid')
    return name


def initial_control_state(config, version, enrollment_receipt_ref):
    """Explicit enrollment preparation; never called by acquire or on a 404."""
    _require(isinstance(enrollment_receipt_ref, str) and _DIGEST.fullmatch(enrollment_receipt_ref),
             'independent_enrollment_receipt_required')
    return {**config.binding(), 'enrollment_receipt_ref': enrollment_receipt_ref,
            'version': _version(config, version), 'fence': 0, 'phase': 'idle',
            'lease_id': '', 'execution': '', 'execution_uid': '', 'lease_until_ms': 0,
            'intent_id': '', 'intent_digest': '', 'commit_version': '',
            'quarantine_reason': '', 'last_release_id': '', 'last_release_fence': 0,
            'last_release_version': ''}


def _fields(value):
    return {key: {'integerValue': str(item)} if type(item) is int else {'stringValue': item}
            for key, item in value.items()}


def _decode_fields(value):
    _require(isinstance(value, dict), 'control_schema_invalid')
    result = {}
    for key, item in value.items():
        _require(isinstance(key, str) and isinstance(item, dict) and len(item) == 1,
                 'control_schema_invalid')
        if 'integerValue' in item:
            raw = item['integerValue']
            _require(isinstance(raw, str) and re.fullmatch(r'0|[1-9][0-9]{0,18}', raw),
                     'control_schema_invalid')
            result[key] = int(raw)
        else:
            _require(set(item) == {'stringValue'} and isinstance(item['stringValue'], str),
                     'control_schema_invalid')
            result[key] = item['stringValue']
    return result


class GoogleREST:
    """Actual Google REST transport, without ADC discovery, proxies or redirects.

    Socket timeout is 10 seconds; response bytes are bounded. A 15-second
    watchdog shuts down the socket (including a Connection: close response).
    Hostname resolution still uses the platform resolver. There
    are no automatic HTTP retries, especially on non-idempotent addVersion.
    """
    def __init__(self, config):
        self.config = config
        self._access_token = None
        self._token_deadline = 0.0

    def _exchange(self, host, path, method, body=None, headers=None, *, metadata=False):
        connection = None
        expired = threading.Event()
        timer = None
        active_socket = None
        try:
            connection = (http.client.HTTPConnection if metadata else http.client.HTTPSConnection)(host, timeout=10)
            def stop():
                expired.set()
                sock = active_socket or connection.sock
                if sock is not None:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                connection.close()
            timer = threading.Timer(15, stop)
            timer.daemon = True
            timer.start()
            connection.request(method, path, body=body, headers=headers or {})
            active_socket = connection.sock
            response = connection.getresponse()
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            _require(not expired.is_set() and len(raw) <= MAX_RESPONSE_BYTES, 'google_response_limit')
            if metadata:
                _require(response.status == 200 and response.getheader('Metadata-Flavor') == 'Google',
                         'metadata_unavailable')
            # No Location handling: a redirect is rejected without another call.
            if not 200 <= response.status < 300:
                status = None
                try:
                    status = _json(raw).get('error', {}).get('status')
                except (BrokerError, AttributeError):
                    pass
                if host == 'firestore.googleapis.com' and status in ('FAILED_PRECONDITION', 'ABORTED', 'ALREADY_EXISTS'):
                    raise Conflict('control_compare_and_swap_conflict')
                if response.status in (401, 403, 404):
                    raise BrokerError('google_resource_denied_or_missing')
                if method != 'GET':
                    raise MutationUncertain('google_mutation_uncertain')
                raise BrokerError('google_read_failed')
            return _json(raw)
        except (Conflict, MutationUncertain):
            raise
        except BrokerError:
            if method != 'GET':
                raise MutationUncertain('google_mutation_uncertain') from None
            raise
        except (OSError, http.client.HTTPException, ValueError):
            if method != 'GET':
                raise MutationUncertain('google_mutation_uncertain') from None
            raise BrokerError('google_read_failed') from None
        finally:
            if timer:
                timer.cancel()
            if connection:
                connection.close()

    def _token(self):
        if self._access_token and time.monotonic() < self._token_deadline:
            return self._access_token
        value = self._exchange('metadata.google.internal',
            '/computeMetadata/v1/instance/service-accounts/default/token', 'GET',
            headers={'Metadata-Flavor': 'Google'}, metadata=True)
        token, duration = value.get('access_token'), value.get('expires_in')
        _require(value.get('token_type') == 'Bearer' and isinstance(token, str)
                 and 1 <= len(token) <= 16384 and not any(c.isspace() or ord(c) < 32 for c in token)
                 and type(duration) is int and 0 < duration <= 7200, 'metadata_token_invalid')
        self._access_token = token
        self._token_deadline = time.monotonic() + max(0, duration - 30)
        return token

    def request(self, method, host, path, value=None):
        c = self.config
        _require(all(isinstance(item, str) for item in (method, host, path)), 'google_endpoint_not_allowed')
        allowed = (method == 'GET' and host == 'firestore.googleapis.com' and path == '/v1/' + c.document_name
            or method == 'POST' and host == 'firestore.googleapis.com' and path == '/v1/' + DATABASE + '/documents:commit'
            or method == 'POST' and host == 'secretmanager.googleapis.com' and path == '/v1/' + c.secret_name + ':addVersion'
            or method == 'GET' and host == 'secretmanager.googleapis.com' and re.fullmatch(
                re.escape('/v1/' + c.secret_name) + r'/versions/[1-9][0-9]{0,18}:access', path)
            or method == 'GET' and host == 'run.googleapis.com' and re.fullmatch(
                re.escape('/v2/' + c.job_name + '/executions/' + c.job_id) + r'-[a-z0-9]{5,20}', path))
        _require(allowed and '?' not in path and '#' not in path, 'google_endpoint_not_allowed')
        if host == 'firestore.googleapis.com' and method == 'POST':
            writes = value.get('writes') if isinstance(value, dict) else None
            _require(isinstance(writes, list) and len(writes) == 1
                     and isinstance(writes[0], dict) and isinstance(writes[0].get('update'), dict)
                     and writes[0].get('update', {}).get('name') == c.document_name
                     and set(writes[0]) == {'update', 'currentDocument'}, 'control_write_scope_invalid')
        try:
            body = None if value is None else json.dumps(value, allow_nan=False, separators=(',', ':')).encode()
        except (ValueError, TypeError, RecursionError):
            raise BrokerError('google_request_invalid') from None
        _require(body is None or len(body) <= MAX_RESPONSE_BYTES, 'google_request_limit')
        token = self._token()
        return self._exchange(host, path, method, body, {'Authorization': 'Bearer ' + token,
            'Content-Type': 'application/json', 'Accept': 'application/json'})


class CloudCredentialBroker:
    def __init__(self, config, *, rest=None, clock=time.time, lease_seconds=180):
        _require(isinstance(config, ProfileConfig), 'protected_profile_required')
        _require(type(lease_seconds) is int and 30 <= lease_seconds <= 600, 'lease_duration_invalid')
        self.config = config
        self.rest = rest if rest is not None else GoogleREST(config)
        self.clock, self.lease_seconds = clock, lease_seconds

    def _now(self):
        return int(self.clock() * 1000)

    def _read(self):
        raw = self.rest.request('GET', 'firestore.googleapis.com', '/v1/' + self.config.document_name)
        _require(raw.get('name') == self.config.document_name and isinstance(raw.get('updateTime'), str)
                 and _STAMP.fullmatch(raw['updateTime']), 'control_response_invalid')
        state = _decode_fields(raw.get('fields'))
        _require(len(json.dumps(state)) <= MAX_CONTROL_BYTES, 'control_schema_invalid')
        expected = initial_control_state(self.config, self.config.secret_name + '/versions/1', '0' * 64)
        _require(set(state) == set(expected) and all(type(state[k]) is type(v) for k, v in expected.items()),
                 'control_schema_invalid')
        _require(all(state.get(k) == v for k, v in self.config.binding().items()), 'backend_account_binding_mismatch')
        _require(_DIGEST.fullmatch(state['enrollment_receipt_ref']) and state['phase'] in _PHASES
                 and 0 <= state['fence'] < 2**63 - 1 and 0 <= state['lease_until_ms'] < 2**63 - 1,
                 'control_schema_invalid')
        _version(self.config, state['version'])
        if state['fence']:
            _require(_ID.fullmatch(state['lease_id']) and state['execution_uid'], 'control_schema_invalid')
            _execution(self.config, state['execution'])
        if state['phase'] in ('committing', 'committed'):
            _require(_ID.fullmatch(state['intent_id']) and _DIGEST.fullmatch(state['intent_digest']),
                     'control_schema_invalid')
        if state['commit_version']:
            _version(self.config, state['commit_version'])
        return state, raw['updateTime']

    def _write(self, state, update_time):
        write = {'update': {'name': self.config.document_name, 'fields': _fields(state)},
                 'currentDocument': {'updateTime': update_time} if update_time else {'exists': False}}
        try:
            reply = self.rest.request('POST', 'firestore.googleapis.com',
                '/v1/' + DATABASE + '/documents:commit', {'writes': [write]})
            results = reply.get('writeResults')
            if not (isinstance(results, list) and len(results) == 1 and isinstance(results[0], dict)
                    and isinstance(results[0].get('updateTime'), str) and _STAMP.fullmatch(results[0]['updateTime'])):
                raise MutationUncertain('control_ack_invalid')
        except MutationUncertain:
            # Only a strong read of this exact unique intent can resolve a lost
            # acknowledgement. CAS conflicts never enter this recovery branch.
            try:
                actual, _ = self._read()
                if actual == state:
                    return
            except BrokerError:
                pass
            raise MutationUncertain('control_write_unconfirmed') from None

    def _access(self, version):
        name = _version(self.config, version)
        reply = self.rest.request('GET', 'secretmanager.googleapis.com', '/v1/' + name + ':access')
        _require(_version(self.config, reply.get('name')) == name, 'secret_version_mismatch')
        payload = reply.get('payload')
        _require(isinstance(payload, dict) and isinstance(payload.get('data'), str), 'secret_payload_invalid')
        try:
            body = base64.b64decode(payload['data'], validate=True)
        except (ValueError, TypeError):
            raise BrokerError('secret_payload_invalid') from None
        _opaque(body)
        checksum = payload.get('dataCrc32c')
        _require(isinstance(checksum, str) and checksum == str(crc32c(body)), 'secret_checksum_mismatch')
        return body

    def _execution_status(self, execution):
        name = _execution(self.config, execution)
        reply = self.rest.request('GET', 'run.googleapis.com', '/v2/' + name)
        _require(_execution(self.config, reply.get('name')) == name
                 and isinstance(reply.get('uid'), str) and re.fullmatch(r'[a-fA-F0-9-]{32,36}', reply['uid'])
                 and type(reply.get('taskCount')) is int and reply['taskCount'] == 1,
                 'execution_identity_unverified')
        return reply

    def initialize_binding(self, version, enrollment_receipt_ref):
        """Explicit operator enrollment only; refuses any existing control doc."""
        state = initial_control_state(self.config, version, enrollment_receipt_ref)
        self._access(state['version'])  # Validate exact version integrity, never infer owner from bytes.
        self._write(state, None)

    def acquire(self, execution, request_id):
        """One unique request_id per native launch; persist it before calling.

        Retrying after an uncertain acquisition is safe only before any native
        process was started. It must never launch another process for that lease.
        A released execution cannot immediately reacquire under a new ID. This
        record remembers only the last execution: A->B->A is not a global
        lifetime replay guard. The trusted service/controller/supervisor must
        restrict grants and launch once; this is not process attestation.
        """
        execution = _execution(self.config, execution)
        _require(isinstance(request_id, str) and _ID.fullmatch(request_id), 'acquisition_id_required')
        observed = self._execution_status(execution)
        _require(not observed.get('completionTime') and not observed.get('deleteTime'), 'execution_already_terminal')
        state, stamp = self._read()
        if (state['phase'] == 'leased' and state['lease_id'] == request_id
                and state['execution'] == execution and state['execution_uid'] == observed['uid']):
            _require(state['lease_until_ms'] > self._now(), 'lease_expired_reconciliation_required')
        else:
            # A deadline does not fence a provider refresh in an old container.
            # Therefore no expired, quarantined or committing lease is stolen.
            _require(state['phase'] == 'idle', 'account_busy_reconciliation_required')
            # This check participates in the same control-document CAS as the
            # acquisition. A second request ID cannot turn a completed lease
            # into another native launch in this recorded execution.
            _require(not (state['last_release_id'] and state['execution_uid'] == observed['uid']),
                     'execution_already_consumed')
            _require(state['last_release_id'] != request_id, 'acquisition_id_reused')
            state = {**state, 'fence': state['fence'] + 1, 'phase': 'leased', 'lease_id': request_id,
                     'execution': execution, 'execution_uid': observed['uid'],
                     'lease_until_ms': self._now() + self.lease_seconds * 1000,
                     'intent_id': '', 'intent_digest': '', 'commit_version': '', 'quarantine_reason': ''}
            self._write(state, stamp)
        lease = Lease(self.config.profile, self.config.account_ref, self.config.canonical_account_ref,
                      state['fence'], state['version'], request_id, execution, observed['uid'], b'')
        try:
            body = self._access(lease.version)
            self.assert_current(lease)
        except BrokerError:
            self._best_quarantine(lease, 'credential_read_failed')
            raise BrokerError('credential_acquisition_unavailable') from None
        return Lease(lease.profile, lease.account_ref, lease.canonical_account_ref, lease.fence,
                     lease.version, lease.lease_id, lease.execution, lease.execution_uid, body)

    def _owned(self, state, lease):
        _require(isinstance(lease, Lease) and lease.profile == self.config.profile
                 and lease.account_ref == self.config.account_ref
                 and lease.canonical_account_ref == self.config.canonical_account_ref
                 and state['fence'] == lease.fence and state['lease_id'] == lease.lease_id
                 and state['execution'] == lease.execution and state['execution_uid'] == lease.execution_uid,
                 'credential_fence_lost')

    def assert_current(self, lease):
        state, _ = self._read()
        self._owned(state, lease)
        _require(state['phase'] == 'leased' and state['version'] == lease.version, 'credential_lease_not_active')
        _require(state['lease_until_ms'] > self._now(), 'lease_expired_reconciliation_required')

    def renew(self, lease):
        state, stamp = self._read()
        self._owned(state, lease)
        _require(state['phase'] == 'leased' and state['version'] == lease.version
                 and state['lease_until_ms'] > self._now(), 'credential_lease_not_active')
        self._write({**state, 'lease_until_ms': self._now() + self.lease_seconds * 1000}, stamp)

    def commit(self, lease, opaque_bytes):
        body = _opaque(opaque_bytes)
        digest = hashlib.sha256(body).hexdigest()
        state, stamp = self._read()
        self._owned(state, lease)
        if state['phase'] in ('committed', 'idle') and state['intent_digest'] == digest:
            _require(state['commit_version'] == state['version'], 'credential_commit_inconsistent')
            return state['version']
        _require(state['phase'] == 'leased' and state['version'] == lease.version
                 and state['lease_until_ms'] > self._now(), 'credential_commit_requires_reconciliation')
        # Unique per attempt: a lost acknowledgement cannot be mistaken for a
        # concurrent writer's equal-payload intent, causing two addVersion calls.
        intent = {**state, 'phase': 'committing', 'intent_id': uuid.uuid4().hex,
                  'intent_digest': digest, 'commit_version': ''}
        try:
            self._write(intent, stamp)
        except Conflict:
            # Another contender owns this intent. Do not mutate its state: in
            # particular, do not quarantine a winning commit merely for racing.
            raise
        except BrokerError:
            self._best_quarantine(lease, 'writeback_uncertain')
            raise
        try:
            if hashlib.sha256(lease.auth_bytes).hexdigest() == digest:
                version = lease.version  # No refresh: no new secret/version charge.
            else:
                result = self.rest.request('POST', 'secretmanager.googleapis.com',
                    '/v1/' + self.config.secret_name + ':addVersion',
                    {'payload': {'data': base64.b64encode(body).decode('ascii'),
                                 'dataCrc32c': str(crc32c(body))}})
                version = _version(self.config, result.get('name'))
                _require(result.get('state') == 'ENABLED' and result.get('clientSpecifiedPayloadChecksum') is True,
                         'secret_version_ack_invalid')
            # Re-read exact immutable version before publishing, not `latest`.
            _require(hashlib.sha256(self._access(version)).hexdigest() == digest, 'secret_roundtrip_mismatch')
            actual, update_time = self._read()
            self._owned(actual, lease)
            _require(actual['phase'] == 'committing' and actual['intent_id'] == intent['intent_id']
                     and actual['intent_digest'] == digest, 'credential_commit_fence_changed')
            self._write({**actual, 'phase': 'committed', 'version': version, 'commit_version': version}, update_time)
            return version
        except BrokerError:
            self._best_quarantine(lease, 'writeback_uncertain')
            raise MutationUncertain('credential_commit_quarantined') from None

    def release(self, lease, version):
        version = _version(self.config, version)
        state, stamp = self._read()
        self._owned(state, lease)
        if (state['phase'] == 'idle' and state['last_release_id'] == lease.lease_id
                and state['last_release_fence'] == lease.fence and state['last_release_version'] == version):
            return
        _require(state['phase'] == 'committed' and state['version'] == version
                 and state['commit_version'] == version, 'durable_commit_required_before_release')
        self._write({**state, 'phase': 'idle', 'lease_until_ms': 0,
                     'last_release_id': lease.lease_id, 'last_release_fence': lease.fence,
                     'last_release_version': version}, stamp)

    def quarantine(self, lease, reason):
        reason = reason if isinstance(reason, str) and reason in _REASONS else 'manual_reconciliation_required'
        state, stamp = self._read()
        self._owned(state, lease)
        if state['phase'] == 'idle':
            # A successful release whose acknowledgement was lost is harmless.
            _require(state['last_release_id'] == lease.lease_id, 'credential_lease_not_active')
            return
        if state['phase'] == 'quarantined':
            return
        self._write({**state, 'phase': 'quarantined', 'quarantine_reason': reason}, stamp)

    def _best_quarantine(self, lease, reason):
        try:
            self.quarantine(lease, reason)
        except BrokerError:
            # Existing leased/committing state already prevents takeover. Never
            # release the lease just because the quarantine write also failed.
            pass

    def reconcile_commit(self, *, fence, lease_id, candidate_version):
        """Separate operator action: verify terminal execution + exact intent bytes.

        Does not guess a version, list secrets, use latest, or recover a lost
        refresh lacking a durable commit intent. Such cases need re-enrollment.
        Requires a separately authorized operator; do not expose to task code.
        """
        state, stamp = self._read()
        _require(state['fence'] == fence and state['lease_id'] == lease_id
                 and state['phase'] in ('committing', 'committed', 'quarantined'), 'reconciliation_state_changed')
        _require(_ID.fullmatch(state['intent_id']) and _DIGEST.fullmatch(state['intent_digest']),
                 'refresh_intent_missing_reenrollment_required')
        execution = self._execution_status(state['execution'])
        counts = [execution.get(key, 0) for key in ('succeededCount', 'failedCount', 'cancelledCount')]
        terminal = (execution.get('uid') == state['execution_uid']
            and isinstance(execution.get('completionTime'), str) and _STAMP.fullmatch(execution['completionTime'])
            and execution.get('reconciling', False) is False
            and type(execution.get('runningCount', 0)) is int and execution.get('runningCount', 0) == 0
            and all(type(count) is int and 0 <= count <= 1 for count in counts) and sum(counts) == 1)
        _require(terminal, 'execution_terminal_not_verified')
        version = _version(self.config, candidate_version)
        _require(hashlib.sha256(self._access(version)).hexdigest() == state['intent_digest'],
                 'reconciliation_payload_mismatch')
        # Old native execution is terminal and these exact refreshed bytes are
        # durably stored; CAS simultaneously publishes and releases its fence.
        self._write({**state, 'phase': 'idle', 'version': version, 'commit_version': version,
            'lease_until_ms': 0, 'quarantine_reason': '', 'last_release_id': lease_id,
            'last_release_fence': fence, 'last_release_version': version}, stamp)
        return version
