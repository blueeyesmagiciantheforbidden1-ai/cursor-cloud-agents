"""Private Cloud Run credential broker HTTP boundary and metadata-only client.

Run: python -m agent_hub.credential_broker_service --config /run/config/broker.json
The service owns GoogleREST access. Workers only receive run.invoker and a
controller-issued, execution-specific capability; task data chooses no profile.
No operator enrollment/reconciliation endpoint or credential-operation retry
exists. Only metadata-only execution bootstrap polls readiness before a lease.
"""
from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass, field
import hashlib
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import socket
import threading
import time
from types import SimpleNamespace
from urllib.parse import quote, urlsplit

from .cloud_credential_broker import (
    BrokerError, CloudCredentialBroker, Conflict, Lease, MAX_CREDENTIAL_BYTES,
    MutationUncertain, ProfileConfig, GoogleREST, UpstreamUnavailable, UPSTREAM_BUDGET_SECONDS,
    UPSTREAM_HTTP_STATUSES, _execution, _version, _fields, _decode_fields,
)
from . import cloud_credential_broker as backend

MAX_BODY = 100 * 1024
MAX_CONFIG = 128 * 1024
MAX_TOKEN = 16384
PREFIX = '/v1/credentials/'
ACTIONS = frozenset(('bootstrap', 'acquire', 'assert-current', 'renew', 'commit', 'release', 'quarantine'))
# ID-token certificate fetch only. Transport, timeout and these 5xx statuses
# become UpstreamUnavailable. A 429 or a rejected token stays authentication_required.
CERTIFICATE_UPSTREAM_STATUSES = frozenset((500, 502, 503, 504))
GOOGLE_CERTS_URL = 'https://www.googleapis.com/oauth2/v1/certs'
SHA = re.compile(r'[a-f0-9]{64}')
UID = re.compile(r'(?:[a-fA-F0-9]{32}|[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12})')
ID = re.compile(r'[a-f0-9]{32}')
GRANT = re.compile(r'[A-Za-z0-9_-]{43,128}')
PROFILE_FIELDS = frozenset(('provider', 'profile', 'account_ref', 'canonical_account_ref', 'secret_id', 'job_id'))
LEASE_FIELDS = frozenset(('fence', 'version', 'lease_id'))


class BoundaryError(BrokerError):
    pass


def require(condition, code='request_invalid'):
    if not condition:
        raise BoundaryError(code)


def encode(value):
    try:
        raw = json.dumps(value, separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, RecursionError):
        raise BoundaryError('request_invalid') from None
    require(len(raw) <= MAX_BODY, 'message_too_large')
    return raw


def decode(raw, limit=MAX_BODY):
    require(isinstance(raw, bytes) and 0 < len(raw) <= limit, 'message_too_large')
    def pairs(items):
        value = {}
        for key, item in items:
            require(key not in value)
            value[key] = item
        return value
    try:
        value = json.loads(raw, object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise BoundaryError('request_invalid') from None
    require(isinstance(value, dict))
    return value


def endpoint_host(endpoint):
    require(isinstance(endpoint, str), 'endpoint_invalid')
    parsed = urlsplit(endpoint)
    require(parsed.scheme == 'https' and parsed.hostname and re.fullmatch(r'[a-z0-9-]+(?:\.[a-z0-9-]+)?\.run\.app', parsed.hostname)
            and parsed.netloc == parsed.hostname and not parsed.path and not parsed.query and not parsed.fragment,
            'endpoint_invalid')
    return parsed.hostname


def opaque_decode(value):
    require(isinstance(value, str) and len(value) <= 4 * ((MAX_CREDENTIAL_BYTES + 2) // 3))
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        raise BoundaryError('request_invalid') from None
    require(0 < len(raw) <= MAX_CREDENTIAL_BYTES)
    return raw


def exchange(host, path, *, method='GET', body=None, headers=None, metadata=False, limit=MAX_BODY, timeout_seconds=15):
    """TLS, no proxies/redirects/retries; bounded reads and socket watchdog.

    Only metadata.google.internal may use HTTP. DNS uses the platform resolver,
    so a platform DNS stall is not strictly bounded by the socket watchdog.
    """
    require(not metadata or host == 'metadata.google.internal', 'endpoint_invalid')
    # Pre-mutation broker reads draw down one 12s deadline. After a lease or
    # intent write the historical 10s socket and 15s watchdog apply instead.
    # The worker client never sets either, so its own 15s cap is unchanged.
    if backend._post_mutation.get():
        socket_timeout = backend.POST_MUTATION_SOCKET_SECONDS
        timeout_seconds = backend.POST_MUTATION_WATCHDOG_SECONDS
    elif backend._request_deadline.get() is not None:
        timeout_seconds = backend.upstream_call_budget()
        socket_timeout = min(10, timeout_seconds)
    else:
        socket_timeout = min(10, timeout_seconds)
    require(type(timeout_seconds) in (int, float) and math.isfinite(timeout_seconds) and 0 < timeout_seconds <= 15,
            'transport_limit')
    connection = (http.client.HTTPConnection if metadata else http.client.HTTPSConnection)(host, timeout=socket_timeout)
    expired = threading.Event()
    active_socket = None
    def stop():
        expired.set()
        sock = active_socket or connection.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        connection.close()
    timer = threading.Timer(timeout_seconds, stop)
    timer.daemon = True
    timer.start()
    try:
        connection.request(method, path, body=body, headers=headers or {})
        active_socket = connection.sock
        response = connection.getresponse()
        data = response.read(limit + 1)
        require(not expired.is_set() and len(data) <= limit, 'transport_limit')
        return response.status, dict(response.getheaders()), data
    except (OSError, http.client.HTTPException, ValueError):
        raise BoundaryError('transport_failed') from None
    finally:
        timer.cancel()
        connection.close()


class GoogleIDAuthenticator:
    """Verifies Google signature, issuer, lifetime and exact service audience.

    Authentication never consumes X-Goog-Authenticated-User-Email, a body email,
    forwarded client IP, or an unsigned/stripped JWT. The Cloud Run IAM front
    door must additionally be enabled by deployment configuration.
    """
    def __init__(self, audience, *, clock=time.time):
        endpoint_host(audience)
        self.audience, self.clock = audience, clock
        self._certificates = None
        self._certificates_until = 0
        self._lock = threading.Lock()

    def _certificate_request(self, url, method='GET', body=None, headers=None, **kwargs):
        require(url == GOOGLE_CERTS_URL and method == 'GET' and body is None,
                'authentication_required')
        with self._lock:
            if self._certificates is None or self.clock() >= self._certificates_until:
                # The lock is held across the fetch. A waiter that queued past
                # its own request deadline fails here instead of starting another
                # fetch; the fetch itself draws that same per-request deadline.
                if backend._request_deadline.get() is not None:
                    backend.upstream_call_budget()
                try:
                    status, response_headers, data = exchange('www.googleapis.com', '/oauth2/v1/certs', limit=65536)
                except UpstreamUnavailable:
                    raise
                except BoundaryError as error:
                    if str(error) in ('transport_failed', 'transport_limit'):
                        raise UpstreamUnavailable('authentication_upstream_unavailable') from None
                    raise
                except (TimeoutError, socket.timeout):
                    raise UpstreamUnavailable('authentication_upstream_unavailable') from None
                if status in CERTIFICATE_UPSTREAM_STATUSES:
                    raise UpstreamUnavailable('authentication_upstream_unavailable')
                require(status == 200, 'authentication_required')
                value = decode(data, 65536)
                require(value and len(value) <= 32 and all(isinstance(k, str) and isinstance(v, str)
                        and 'BEGIN CERTIFICATE' in v for k, v in value.items()), 'authentication_required')
                # Short bounded cache; no attacker-provided URL or cache duration.
                self._certificates, self._certificates_until = data, self.clock() + 300
            return SimpleNamespace(status=200, data=self._certificates, headers={})

    def __call__(self, authorization):
        require(isinstance(authorization, str) and authorization.startswith('Bearer '), 'authentication_required')
        token = authorization[7:]
        require(1 <= len(token) <= MAX_TOKEN and re.fullmatch(r'[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', token),
                'authentication_required')
        try:
            from google.oauth2.id_token import verify_oauth2_token
            claims = verify_oauth2_token(token, self._certificate_request, audience=self.audience,
                                         clock_skew_in_seconds=30)
        except UpstreamUnavailable:
            raise
        except Exception:
            raise BoundaryError('authentication_required') from None
        require(isinstance(claims, dict) and claims.get('aud') == self.audience
                and claims.get('iss') in ('accounts.google.com', 'https://accounts.google.com')
                and isinstance(claims.get('sub'), str) and re.fullmatch(r'[0-9]{6,32}', claims['sub'])
                and claims.get('email_verified') is True and isinstance(claims.get('email'), str)
                and type(claims.get('exp')) is int and type(claims.get('iat')) is int
                and 0 < claims['exp'] - claims['iat'] <= 7200
                and claims['iat'] <= self.clock() + 30 and claims['exp'] > self.clock(), 'authentication_required')
        return {'subject': claims['sub'], 'service_account': claims['email']}


@dataclass(frozen=True)
class Binding:
    profile: ProfileConfig
    caller_subject: str
    caller_service_account: str
    grant_sha256: str = field(repr=False)
    expires_at: int = 0
    lease_seconds: int = 180

    def __post_init__(self):
        require(isinstance(self.profile, ProfileConfig), 'protected_binding_invalid')
        require(isinstance(self.caller_subject, str) and re.fullmatch(r'[0-9]{6,32}', self.caller_subject)
            and isinstance(self.caller_service_account, str) and re.fullmatch(
                r'[a-z][a-z0-9-]{4,62}@project-0c6d31fa-509e-4116-a2c\.iam\.gserviceaccount\.com', self.caller_service_account)
            and isinstance(self.grant_sha256, str) and SHA.fullmatch(self.grant_sha256)
            and type(self.expires_at) is int and type(self.lease_seconds) is int
            and 30 <= self.lease_seconds <= 600, 'protected_binding_invalid')


@dataclass(frozen=True)
class ActiveBinding:
    policy: Binding
    execution: str
    execution_uid: str

    @property
    def profile(self):
        return self.policy.profile


class ExecutionGrantStore:
    """Immutable controller publication in the explicitly configured broker DB.

    Server uses read only. publish() is an explicit operator/controller entry
    point, never a worker HTTP endpoint. No implicit creation or overwrite.
    """
    def __init__(self, binding, *, rest=None, clock=time.time):
        require(isinstance(binding, Binding), 'protected_binding_invalid')
        self.binding, self.clock = binding, clock
        self.rest = rest if rest is not None else GoogleREST(binding.profile)
        self.document = backend.DATABASE + '/documents/runcrew_execution_grants/' + binding.grant_sha256

    def _request(self, method, value=None):
        require(method in ('GET', 'POST'))
        path = '/v1/' + self.document if method == 'GET' else '/v1/' + backend.DATABASE + '/documents:commit'
        body = None if value is None else encode(value)
        try:
            status, _, raw = exchange('firestore.googleapis.com', path, method=method, body=body,
                headers={'Authorization': 'Bearer ' + self.rest._token(), 'Content-Type': 'application/json'},
                timeout_seconds=UPSTREAM_BUDGET_SECONDS)
        except Exception:
            if method == 'POST':
                raise MutationUncertain('execution_grant_publish_uncertain') from None
            raise UpstreamUnavailable('execution_grant_read_unavailable') from None
        if method == 'GET' and status == 404:
            return None  # 403 is NOT treated as a not-yet-published grant.
        if status in (409, 412):
            raise Conflict('execution_grant_already_published')
        if status in (401, 403):
            raise BrokerError('execution_grant_access_denied')
        if status in UPSTREAM_HTTP_STATUSES:
            if method == 'POST':
                raise MutationUncertain('execution_grant_publish_uncertain')
            raise UpstreamUnavailable('execution_grant_read_unavailable')
        if not 200 <= status < 300:
            if method == 'POST':
                raise MutationUncertain('execution_grant_publish_uncertain')
            raise BrokerError('execution_grant_read_unavailable')
        try:
            return decode(raw)
        except BrokerError:
            if method == 'POST':
                raise MutationUncertain('execution_grant_publish_uncertain') from None
            raise UpstreamUnavailable('execution_grant_read_unavailable') from None

    def _record(self, execution, execution_uid):
        b = self.binding
        # Cloud Run echoes execution names with the project ID or the project
        # number, not consistently (2026-09-22/23). _execution() scopes the name
        # and returns the canonical number form; accept either and record that.
        try:
            execution = _execution(b.profile, execution)
        except BrokerError:
            raise BoundaryError('execution_not_authorized') from None
        require(isinstance(execution_uid, str) and UID.fullmatch(execution_uid), 'execution_not_authorized')
        return {**b.profile.binding(), 'caller_subject': b.caller_subject,
                'caller_service_account': b.caller_service_account, 'grant_sha256': b.grant_sha256,
                'expires_at': b.expires_at, 'execution': execution, 'execution_uid': execution_uid}

    def read(self):
        raw = self._request('GET')
        if raw is None:
            return None
        require(raw.get('name') == self.document, 'execution_not_authorized')
        value = _decode_fields(raw.get('fields'))
        expected = self._record(value.get('execution'), value.get('execution_uid'))
        require(value == expected, 'execution_not_authorized')
        return ActiveBinding(self.binding, value['execution'], value['execution_uid'])

    def publish(self, execution, execution_uid):
        require(self.clock() < self.binding.expires_at <= self.clock() + 86400, 'execution_grant_expired')
        record = self._record(execution, execution_uid)
        execution = record['execution']
        observed = CloudCredentialBroker(self.binding.profile, rest=self.rest)._execution_status(execution)
        require(observed.get('uid') == execution_uid
                and observed.get('template', {}).get('serviceAccount') == self.binding.caller_service_account
                and not observed.get('completionTime') and not observed.get('deleteTime'), 'execution_not_authorized')
        value = {'writes': [{'update': {'name': self.document, 'fields': _fields(record)},
                             'currentDocument': {'exists': False}}]}
        try:
            result = self._request('POST', value)
            writes = result.get('writeResults')
            if not isinstance(writes, list) or len(writes) != 1 or not isinstance(writes[0], dict) or not writes[0].get('updateTime'):
                raise MutationUncertain('execution_grant_publish_uncertain')
        except MutationUncertain:
            # Strong read can resolve only this exact immutable publication.
            try:
                actual = self.read()
            except BrokerError:
                raise MutationUncertain('execution_grant_publish_uncertain') from None
            if actual != ActiveBinding(self.binding, execution, execution_uid):
                raise MutationUncertain('execution_grant_publish_uncertain') from None
        return ActiveBinding(self.binding, execution, execution_uid)


def parse_service_config(value):
    require(isinstance(value, dict) and set(value) == {'schema_version', 'audience', 'bindings'}
            and type(value['schema_version']) is int and value['schema_version'] == 1, 'protected_config_invalid')
    endpoint_host(value['audience'])
    require(isinstance(value['bindings'], list) and 1 <= len(value['bindings']) <= 32, 'protected_config_invalid')
    bindings = []
    fields = {'profile', 'caller_subject', 'caller_service_account',
              'grant_sha256', 'expires_at', 'lease_seconds'}
    for raw in value['bindings']:
        require(isinstance(raw, dict) and set(raw) == fields and isinstance(raw['profile'], dict)
                and set(raw['profile']) == PROFILE_FIELDS, 'protected_config_invalid')
        bindings.append(Binding(**{**raw, 'profile': ProfileConfig(**raw['profile'])}))
    require(len({(b.caller_subject, b.grant_sha256) for b in bindings}) == len(bindings)
            and len({b.grant_sha256 for b in bindings}) == len(bindings), 'ambiguous_execution_grant')
    return value['audience'], tuple(bindings)


def read_protected(path):
    path = Path(path)
    require(path.is_absolute() and not path.is_symlink() and path.is_file(), 'protected_config_required')
    metadata = path.stat()
    require(os.name == 'posix' and metadata.st_uid == 0 and not metadata.st_mode & 0o022
            and not os.access(path, os.W_OK) and metadata.st_size <= MAX_CONFIG, 'protected_config_required')
    return decode(path.read_bytes(), MAX_CONFIG)


class BrokerService:
    def __init__(self, audience, bindings, *, authenticator=None, broker_factory=CloudCredentialBroker,
                 grant_store_factory=ExecutionGrantStore, clock=time.time):
        endpoint_host(audience)
        require(isinstance(bindings, (list, tuple)) and bindings and all(isinstance(b, Binding) for b in bindings),
                'protected_config_invalid')
        self.clock = clock
        self.authenticator = authenticator if authenticator is not None else GoogleIDAuthenticator(audience, clock=clock)
        self.bindings = tuple(bindings)
        self.brokers = {b: broker_factory(b.profile, lease_seconds=b.lease_seconds) for b in bindings}
        self.grants = {b: grant_store_factory(b) for b in bindings}

    def _authorize(self, authorization, grant):
        caller = self.authenticator(authorization)
        require(isinstance(caller, dict), 'authentication_required')
        require(isinstance(grant, str) and GRANT.fullmatch(grant), 'execution_not_authorized')
        hashed = hashlib.sha256(grant.encode('ascii')).hexdigest()
        matches = [b for b in self.bindings if caller.get('subject') == b.caller_subject
            and caller.get('service_account') == b.caller_service_account
            and hmac.compare_digest(hashed, b.grant_sha256)]
        require(len(matches) == 1, 'execution_not_authorized')
        binding = matches[0]
        require(self.clock() < binding.expires_at <= self.clock() + 86400, 'execution_grant_expired')
        broker = self.brokers[binding]
        return binding, broker

    def _resolve(self, binding, broker):
        active = self.grants[binding].read()
        if active is None:
            return None
        require(isinstance(active, ActiveBinding) and active.policy == binding, 'execution_not_authorized')
        observed = broker._execution_status(active.execution)
        require(observed.get('uid') == active.execution_uid
                and observed.get('template', {}).get('serviceAccount') == binding.caller_service_account
                and not observed.get('completionTime') and not observed.get('deleteTime'), 'execution_not_authorized')
        return active

    @staticmethod
    def _lease(value, binding):
        require(isinstance(value, dict) and set(value) == LEASE_FIELDS)
        require(type(value['fence']) is int and 1 <= value['fence'] < 2**63 - 1
                and isinstance(value['lease_id'], str) and ID.fullmatch(value['lease_id']))
        version = _version(binding.profile, value['version'])
        return Lease(binding.profile.profile, binding.profile.account_ref, binding.profile.canonical_account_ref,
                     value['fence'], version, value['lease_id'], binding.execution, binding.execution_uid, b'')

    def dispatch(self, action, authorization, grant, body):
        require(action in ACTIONS, 'endpoint_not_found')
        binding, broker = self._authorize(authorization, grant)
        require(isinstance(body, dict))
        if action == 'bootstrap':
            require(body == {})
            active = self._resolve(binding, broker)
            return {'ready': False} if active is None else {
                'ready': True, 'execution': active.execution, 'execution_uid': active.execution_uid}
        binding = self._resolve(binding, broker)
        require(binding is not None, 'execution_not_authorized')
        if action == 'acquire':
            require(set(body) == {'request_id'} and isinstance(body['request_id'], str) and ID.fullmatch(body['request_id']))
            lease = broker.acquire(binding.execution, body['request_id'])
            require(lease.execution_uid == binding.execution_uid, 'execution_not_authorized')
            return {'lease': {'fence': lease.fence, 'version': lease.version, 'lease_id': lease.lease_id},
                    'credential_b64': base64.b64encode(lease.auth_bytes).decode('ascii')}
        extra = {'commit': 'credential_b64', 'release': 'version', 'quarantine': 'reason'}.get(action)
        require(set(body) == ({'lease', extra} if extra else {'lease'}))
        lease = self._lease(body['lease'], binding)
        if action == 'commit':
            opaque = opaque_decode(body['credential_b64'])
            # Rehydrate the original bytes server-side for the broker's no-refresh
            # optimization. Never trust a worker-supplied original credential.
            # Existing backend fencing/CAS remains authoritative for all writes.
            state, _ = broker._read()
            broker._owned(state, lease)
            if state['phase'] == 'leased':
                broker.assert_current(lease)
                original = broker._access(lease.version)
                lease = Lease(lease.profile, lease.account_ref, lease.canonical_account_ref, lease.fence,
                              lease.version, lease.lease_id, lease.execution, lease.execution_uid, original)
            return {'version': broker.commit(lease, opaque)}
        if action == 'release':
            broker.release(lease, _version(binding.profile, body['version']))
        elif action == 'quarantine':
            require(body['reason'] in ('credential_read_failed', 'writeback_uncertain', 'provider_refresh_uncertain',
                    'lease_expired', 'manual_reconciliation_required'))
            broker.quarantine(lease, body['reason'])
        elif action == 'renew':
            broker.renew(lease)
        else:
            broker.assert_current(lease)
        return {'ok': True}


LOG_CODES = frozenset((
    'ok', 'authentication_required', 'execution_not_authorized', 'request_invalid',
    'broker_conflict', 'broker_mutation_uncertain', 'broker_operation_rejected',
    'broker_upstream_unavailable', 'broker_unavailable', 'method_not_allowed',
))


def error_reply(error):
    """Status, fixed JSON body, and extra headers. Messages never leave this map."""
    if isinstance(error, BoundaryError):
        code = str(error)
        if code == 'authentication_required':
            return 401, {'error': 'authentication_required'}, None
        if code in ('execution_not_authorized', 'execution_grant_expired'):
            return 403, {'error': 'execution_not_authorized'}, None
        return 400, {'error': 'request_invalid'}, None
    if isinstance(error, Conflict):
        return 409, {'error': 'broker_conflict'}, None
    if isinstance(error, MutationUncertain):
        return 503, {'error': 'broker_mutation_uncertain'}, None
    if isinstance(error, UpstreamUnavailable):
        return 503, {'error': 'broker_upstream_unavailable'}, {'Retry-After': '2'}
    if isinstance(error, BrokerError):
        return 409, {'error': 'broker_operation_rejected'}, None
    return 503, {'error': 'broker_unavailable'}, None


def log_broker_request(action, status, code, started):
    """One fixed JSON line. Callers must not pass tokens, grants, or bodies."""
    if action not in ACTIONS:
        action = 'unknown'
    if code not in LOG_CODES:
        code = 'broker_unavailable'
    if type(status) is not int:
        status = 500
    duration_ms = int((time.monotonic() - started) * 1000)
    if duration_ms < 0:
        duration_ms = 0
    line = json.dumps({'kind': 'runcrew_broker_request', 'action': action, 'status': status,
                       'code': code, 'duration_ms': duration_ms}, separators=(',', ':'))
    try:
        print(line, flush=True)
    except OSError:
        pass


def handler_for(service):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.0'
        server_version = 'RunCrewBroker'
        sys_version = ''

        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, *_):
            pass  # Request paths, headers, credentials and errors are never logged.

        def send_error(self, *_args, **_kwargs):
            started = time.monotonic()
            try:
                self._reply(400, {'error': 'request_invalid'})
            finally:
                log_broker_request('unknown', 400, 'request_invalid', started)

        def _reply(self, status, value, headers=None):
            raw = encode(value)
            self.close_connection = True
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(raw)))
            if headers:
                for key, item in headers.items():
                    self.send_header(key, item)
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            started = time.monotonic()
            budget = backend.begin_upstream_request()
            action, status, code = 'unknown', 500, 'broker_unavailable'
            try:
                require(self.path.startswith(PREFIX) and self.path[len(PREFIX):] in ACTIONS)
                action = self.path[len(PREFIX):]
                # Reject ambiguity, compression, chunking and duplicate auth headers.
                for key in ('Content-Length', 'Content-Type', 'Authorization', 'X-RunCrew-Execution-Grant'):
                    require(len(self.headers.get_all(key, [])) == 1)
                require(not self.headers.get('Transfer-Encoding') and not self.headers.get('Content-Encoding')
                        and not self.headers.get('Expect'))
                require(self.headers.get('Content-Type') == 'application/json')
                length = self.headers.get('Content-Length', '')
                require(re.fullmatch(r'[1-9][0-9]{0,6}', length) and int(length) <= MAX_BODY)
                raw = self.rfile.read(int(length))
                require(len(raw) == int(length))
                value = service.dispatch(action, self.headers['Authorization'],
                                         self.headers['X-RunCrew-Execution-Grant'], decode(raw))
                status, code = (202, 'ok') if value == {'ready': False} else (200, 'ok')
                self._reply(status, value)
            except Exception as error:
                try:
                    reply = error_reply(error)
                    status, body = reply[0], reply[1]
                    code = body.get('error') if isinstance(body, dict) else 'broker_unavailable'
                    self._reply(*reply)
                except (OSError, ValueError):
                    pass
            finally:
                backend.end_upstream_request(budget)
                log_broker_request(action, status, code, started)

        def do_GET(self):
            started = time.monotonic()
            try:
                self._reply(405, {'error': 'method_not_allowed'})
            finally:
                log_broker_request('unknown', 405, 'method_not_allowed', started)

    return Handler


class BrokerServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, address, service):
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(address, handler_for(service))

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, *_):
        pass


class BrokerHTTPClient:
    """Worker-compatible broker interface; no Firestore or Secret Manager calls."""
    def __init__(self, config, *, endpoint, execution, execution_uid=None, grant):
        require(isinstance(config, ProfileConfig), 'protected_client_config_invalid')
        self.host = endpoint_host(endpoint)
        require((execution_uid is None or isinstance(execution_uid, str) and UID.fullmatch(execution_uid))
                and isinstance(grant, str) and GRANT.fullmatch(grant), 'protected_client_config_invalid')
        require(_execution(config, execution) == execution, 'protected_client_config_invalid')
        self.config, self.endpoint = config, endpoint
        self.execution, self.execution_uid = execution, execution_uid
        self._grant = grant

    def _id_token(self, *, timeout_seconds=15):
        status, headers, raw = exchange('metadata.google.internal',
            '/computeMetadata/v1/instance/service-accounts/default/identity?audience=' + quote(self.endpoint, safe=''),
            headers={'Metadata-Flavor': 'Google'}, metadata=True, limit=MAX_TOKEN, timeout_seconds=timeout_seconds)
        require(status == 200 and {k.lower(): v for k, v in headers.items()}.get('metadata-flavor') == 'Google',
                'metadata_identity_unavailable')
        try:
            token = raw.decode('ascii')
        except UnicodeError:
            raise BoundaryError('metadata_identity_unavailable') from None
        require(re.fullmatch(r'[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', token), 'metadata_identity_unavailable')
        return token  # Only server-side verification authorizes this signed token.

    def bootstrap(self, *, timeout_seconds=120):
        """Readiness polling ONLY, before any provider process or lease request."""
        require(type(timeout_seconds) is int and 1 <= timeout_seconds <= 300, 'bootstrap_deadline_invalid')
        require(self.execution_uid is None, 'bootstrap_already_resolved')
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            token = self._id_token(timeout_seconds=min(15, remaining / 2))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                status, _, raw = exchange(self.host, PREFIX + 'bootstrap', method='POST', body=b'{}',
                    headers={'Authorization': 'Bearer ' + token, 'X-Serverless-Authorization': 'Bearer ' + token,
                             'X-RunCrew-Execution-Grant': self._grant,
                             'Content-Type': 'application/json'}, timeout_seconds=min(15, remaining))
                value = decode(raw)
            except BoundaryError:
                # Only this metadata-only endpoint may retry a transient read.
                status, value = 503, {}
            if status == 200:
                require(set(value) == {'ready', 'execution', 'execution_uid'} and value['ready'] is True
                        and value['execution'] == self.execution and isinstance(value['execution_uid'], str)
                        and UID.fullmatch(value['execution_uid']), 'execution_not_authorized')
                self.execution_uid = value['execution_uid']
                return
            require(status == 202 and value == {'ready': False} or status in (500, 502, 503, 504),
                    'bootstrap_not_authorized')
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(1, remaining))
        raise BrokerError('execution_bootstrap_deadline_exceeded')

    def _post(self, action, body):
        require(action in ACTIONS)
        raw = encode(body)
        token = self._id_token()  # Identity failure happens before mutation dispatch.
        try:
            status, headers, data = exchange(self.host, PREFIX + action, method='POST', body=raw,
                headers={'Authorization': 'Bearer ' + token, 'X-Serverless-Authorization': 'Bearer ' + token,
                             'X-RunCrew-Execution-Grant': self._grant,
                         'Content-Type': 'application/json', 'Accept': 'application/json'})
            value = decode(data)
        except Exception:
            raise MutationUncertain('broker_http_outcome_uncertain') from None
        if status == 200:
            return value
        if status in (400, 401, 403):
            raise BrokerError('broker_request_denied')
        if status == 409:
            raise Conflict('broker_operation_rejected')
        # 503 and every other non-definitive result stay uncertain. Acquire may
        # be retried only with the same request_id and only before a native
        # process starts. Renew surfaces MutationUncertain, which the worker
        # tolerates. A 503 is never a compare-and-swap Conflict.
        raise MutationUncertain('broker_http_outcome_uncertain')

    def _lease(self, lease):
        require(isinstance(lease, Lease) and lease.profile == self.config.profile
            and lease.account_ref == self.config.account_ref and lease.canonical_account_ref == self.config.canonical_account_ref
            and lease.execution == self.execution and lease.execution_uid == self.execution_uid, 'credential_fence_lost')
        require(type(lease.fence) is int and 1 <= lease.fence < 2**63 - 1 and ID.fullmatch(lease.lease_id))
        return {'fence': lease.fence, 'version': _version(self.config, lease.version), 'lease_id': lease.lease_id}

    def acquire(self, execution, request_id):
        require(self.execution_uid is not None and execution == self.execution and isinstance(request_id, str) and ID.fullmatch(request_id),
                'execution_not_authorized')
        value = self._post('acquire', {'request_id': request_id})
        try:
            require(set(value) == {'lease', 'credential_b64'} and set(value['lease']) == LEASE_FIELDS)
            data = value['lease']
            require(type(data['fence']) is int and 1 <= data['fence'] < 2**63 - 1 and data['lease_id'] == request_id)
            return Lease(self.config.profile, self.config.account_ref, self.config.canonical_account_ref, data['fence'],
                _version(self.config, data['version']), request_id, execution, self.execution_uid,
                opaque_decode(value['credential_b64']))
        except Exception:
            raise MutationUncertain('broker_acquisition_receipt_invalid') from None

    def _ok(self, action, lease, **extra):
        value = self._post(action, {'lease': self._lease(lease), **extra})
        if set(value) != {'ok'} or value['ok'] is not True:
            raise MutationUncertain('broker_acknowledgement_invalid')

    def assert_current(self, lease):
        self._ok('assert-current', lease)

    def renew(self, lease):
        self._ok('renew', lease)

    def commit(self, lease, opaque_bytes):
        require(isinstance(opaque_bytes, bytes) and 0 < len(opaque_bytes) <= MAX_CREDENTIAL_BYTES)
        value = self._post('commit', {'lease': self._lease(lease),
                                    'credential_b64': base64.b64encode(opaque_bytes).decode('ascii')})
        try:
            require(set(value) == {'version'})
            return _version(self.config, value['version'])
        except Exception:
            raise MutationUncertain('broker_commit_receipt_invalid') from None

    def release(self, lease, version):
        self._ok('release', lease, version=_version(self.config, version))

    def quarantine(self, lease, reason):
        self._ok('quarantine', lease, reason=reason)


def load_client_config(path):
    value = read_protected(path)
    require(set(value) == {'schema_version', 'endpoint', 'profile', 'grant', 'bootstrap_timeout_seconds'}
            and type(value['schema_version']) is int and value['schema_version'] == 1
            and isinstance(value['profile'], dict) and set(value['profile']) == PROFILE_FIELDS,
            'protected_client_config_invalid')
    profile = ProfileConfig(**value['profile'])
    execution_id = os.environ.get('CLOUD_RUN_EXECUTION', '')
    require(isinstance(execution_id, str) and re.fullmatch(re.escape(profile.job_id) + r'-[a-z0-9]{5,20}', execution_id),
            'cloud_execution_environment_required')
    client = BrokerHTTPClient(profile, endpoint=value['endpoint'],
        execution=profile.job_name + '/executions/' + execution_id, grant=value['grant'])
    client.bootstrap(timeout_seconds=value['bootstrap_timeout_seconds'])
    return client


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('serve', 'publish-execution'), nargs='?', default='serve')
    parser.add_argument('--config', required=True)
    parser.add_argument('--execution')
    parser.add_argument('--execution-uid')
    parser.add_argument('--grant-sha256')
    args = parser.parse_args()
    require(os.name == 'posix' and os.geteuid() != 0 and
            (os.environ.get('K_SERVICE') and os.environ.get('K_REVISION')
             or args.mode == 'publish-execution' and os.environ.get('CLOUD_RUN_JOB')),
            'cloud_rootless_service_required')
    audience, bindings = parse_service_config(read_protected(args.config))
    if args.mode == 'publish-execution':
        matches = [b for b in bindings if b.grant_sha256 == args.grant_sha256]
        require(len(matches) == 1, 'protected_binding_invalid')
        active = ExecutionGrantStore(matches[0]).publish(args.execution, args.execution_uid)
        print(json.dumps({'published': True, 'execution': active.execution, 'execution_uid': active.execution_uid}))
        return
    require(args.execution is None and args.execution_uid is None and args.grant_sha256 is None, 'request_invalid')
    port = os.environ.get('PORT', '8080')
    require(re.fullmatch(r'[0-9]{1,5}', port) and 1024 <= int(port) <= 65535, 'listen_port_invalid')
    service = BrokerService(audience, bindings)
    with BrokerServer(('0.0.0.0', int(port)), service) as server:
        server.serve_forever()


if __name__ == '__main__':
    try:
        main()
    except MutationUncertain:
        print('{"status":"uncertain","error":"controller_publication_requires_reconciliation"}', flush=True)
        raise SystemExit(1)
    except Exception:
        print('{"status":"stopped","error":"broker_startup_rejected"}', flush=True)
        raise SystemExit(1)
