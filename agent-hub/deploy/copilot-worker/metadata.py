"""Copilot SDK-compatible read-only RPCs under a durable credential lease.

No session APIs, inference, account mutation, or credential-export RPC exists in
this client's allowlist. Native refresh is written back before reporting success.
"""
from __future__ import annotations
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import time
import uuid
from datetime import datetime, timezone

AGENT = 'copilot'
ACCOUNT_REF = '9ddbfe0cce4b6653b86b2057f45c398360541f21a100c1589a67a01cbc80aadc'
EXPECTED_LOGIN = 'blueeyesmagiciantheforbidden1-ai'
NATIVE = '/opt/runcrew/copilot/copilot'
ARGS = ('--no-auto-update', '--no-custom-instructions', '--disable-builtin-mcps',
        '--headless', '--log-level', 'none', '--stdio')
METHODS = ('connect', 'auth.getStatus', 'models.list', 'account.getQuota', 'runtime.shutdown')
BROKER_CONFIG = Path('/run/config/credential-broker.json')
ATTEMPT_ROOT = Path('/home/worker')
MAX_FRAME, MAX_TOTAL = 2 * 1024 * 1024, 8 * 1024 * 1024


class MetadataError(ValueError):
    pass


def ident(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:+/\[\]-]{0,127}', value):
        raise MetadataError('metadata_identifier_invalid')
    return value


def number(value, *, minimum=0):
    if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= 10**15:
        raise MetadataError('metadata_number_invalid')
    return value


def auth_metadata(value):
    if (not isinstance(value, dict) or value.get('isAuthenticated') is not True
            or value.get('authType') != 'user' or value.get('login') != EXPECTED_LOGIN
            or value.get('host') not in ('github.com', 'https://github.com')):
        raise MetadataError('native_owner_or_subscription_route_mismatch')
    return {'verified': True, 'account_ref': ACCOUNT_REF, 'auth_type': 'user',
            'github_login_ref': hashlib.sha256(EXPECTED_LOGIN.encode()).hexdigest(),
            'host': 'github.com', 'email_binding_source': 'independent_verified_enrollment',
            'native_email_or_immutable_subject_returned': False}


def model_metadata(value):
    models = value.get('models') if isinstance(value, dict) else None
    if not isinstance(models, list) or not 1 <= len(models) <= 1000:
        raise MetadataError('model_catalog_unavailable')
    output, seen = [], set()
    for item in models:
        if not isinstance(item, dict):
            raise MetadataError('model_catalog_invalid')
        model = ident(item.get('id'))
        if model in seen:
            raise MetadataError('duplicate_model')
        seen.add(model)
        row = {'id': model, 'maximum_model_verified': False}
        capabilities = item.get('capabilities', {})
        supports = capabilities.get('supports', {}) if isinstance(capabilities, dict) else {}
        effort = supports.get('reasoningEffort') if isinstance(supports, dict) else None
        if effort is not None and type(effort) is not bool:
            raise MetadataError('effort_capability_invalid')
        row['supports_reasoning_effort'] = effort
        levels = item.get('supportedReasoningEfforts')
        if levels is not None:
            if not isinstance(levels, list) or not 1 <= len(levels) <= 32:
                raise MetadataError('effort_choices_invalid')
            row['supported_reasoning_efforts'] = [ident(v) for v in levels]
            if len(set(levels)) != len(levels):
                raise MetadataError('effort_choices_invalid')
        else:
            row['supported_reasoning_efforts'] = None
        default = item.get('defaultReasoningEffort')
        row['default_reasoning_effort'] = ident(default) if default is not None else None
        row['maximum_effort'] = None  # Catalog order is not a ranking contract.
        policy = item.get('policy', {})
        state = policy.get('state') if isinstance(policy, dict) else None
        if state is not None and state not in ('enabled', 'disabled', 'unconfigured'):
            raise MetadataError('model_policy_invalid')
        row['policy_state'] = state
        billing = item.get('billing', {})
        row['billing'] = {}
        if isinstance(billing, dict):
            if 'multiplier' in billing:
                row['billing']['multiplier'] = number(billing['multiplier'])
            prices = billing.get('tokenPrices')
            if isinstance(prices, dict):
                keys = ('inputPrice', 'outputPrice', 'cachePrice', 'cacheReadPrice',
                        'cacheWritePrice', 'cacheWrite1hPrice', 'batchSize', 'contextMax', 'maxPromptTokens')
                row['billing']['tokenPrices'] = {k: number(prices[k]) for k in keys if k in prices}
                if isinstance(prices.get('longContext'), dict):
                    row['billing']['tokenPrices']['longContext'] = {k: number(prices['longContext'][k])
                                                                   for k in keys if k in prices['longContext']}
        output.append(row)
    return output


def quota_metadata(value, canonical_account_ref):
    if not isinstance(canonical_account_ref, str) or not re.fullmatch('[a-f0-9]{64}', canonical_account_ref):
        raise MetadataError('canonical_quota_identity_required')
    snapshots = value.get('quotaSnapshots') if isinstance(value, dict) else None
    if not isinstance(snapshots, dict) or not 1 <= len(snapshots) <= 32:
        return {'status': 'unavailable', 'reason': 'native_quota_snapshot_missing', 'pools': []}
    pools = []
    for key, item in snapshots.items():
        kind = ident(key)
        if not isinstance(item, dict):
            continue
        pool = {'quota_type': kind,
                'pool_ref': hashlib.sha256(('github-copilot:' + canonical_account_ref + ':' + kind).encode()).hexdigest(),
                'pool_identity_source': 'native_quota_type_and_durable_canonical_account'}
        for field in ('isUnlimitedEntitlement', 'usageAllowedWithExhaustedQuota', 'overageAllowedWithExhaustedQuota'):
            value = item.get(field)
            if type(value) is not bool:
                raise MetadataError('quota_boolean_missing_or_invalid')
            pool[field] = value
        for field in ('entitlementRequests', 'usedRequests', 'remainingPercentage', 'overage'):
            pool[field] = number(item.get(field), minimum=-1 if field == 'entitlementRequests' else 0)
        if pool['remainingPercentage'] > 100:
            raise MetadataError('quota_percentage_invalid')
        reset = item.get('resetDate')
        if reset is not None:
            if not isinstance(reset, str) or len(reset) > 40:
                raise MetadataError('quota_reset_invalid')
            try:
                parsed = datetime.fromisoformat(reset.replace('Z', '+00:00'))
            except ValueError:
                raise MetadataError('quota_reset_invalid') from None
            if parsed.tzinfo is None:
                raise MetadataError('quota_reset_timezone_missing')
            pool['resetDate'] = parsed.isoformat()
        pool['remaining_fraction'] = None if pool['isUnlimitedEntitlement'] else pool['remainingPercentage'] / 100
        pool['billing_authorized'] = False
        pools.append(pool)
    return {'status': 'observed' if pools else 'unavailable', 'pools': pools,
            'unlimited_is_free': False, 'provider_spend_cap': 'unavailable'}


def environment(home):
    return {'HOME': str(home), 'COPILOT_HOME': str(home / '.copilot'),
            'COPILOT_CACHE_HOME': str(home / '.cache'), 'COPILOT_DISABLE_KEYTAR': '1',
            'COPILOT_AUTO_UPDATE': 'false', 'PATH': '/usr/local/bin:/usr/bin:/bin',
            'LANG': 'C.UTF-8', 'TMPDIR': str(home / 'tmp'), 'CI': 'true', 'NO_COLOR': '1'}


class NativeRPC:
    def __init__(self, home, renew):
        self.process = subprocess.Popen([NATIVE, *ARGS], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=home / 'work',
            env=environment(home), shell=False, start_new_session=True, bufsize=0)
        self.selector = selectors.DefaultSelector()
        for pipe, label in ((self.process.stdout, 'out'), (self.process.stderr, 'err')):
            self.selector.register(pipe, selectors.EVENT_READ, label)
        self.buffer, self.total, self.index = bytearray(), 0, 0
        self.deadline, self.next_renew = time.monotonic() + 90, time.monotonic() + 25
        self.renew, self.clean_shutdown = renew, False

    def __enter__(self): return self

    def __exit__(self, *_):
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.process.wait(timeout=5)
        self.selector.close()
        for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
            pipe.close()

    def pump(self):
        now = time.monotonic()
        if now >= self.deadline:
            raise MetadataError('native_metadata_timeout')
        if now >= self.next_renew:
            self.renew()
            self.next_renew = time.monotonic() + 25
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise MetadataError('native_metadata_timeout')
        for key, _ in self.selector.select(min(0.25, remaining)):
            data = os.read(key.fd, 65536)
            if not data:
                self.selector.unregister(key.fileobj)
                continue
            self.total += len(data)
            if self.total > MAX_TOTAL:
                raise MetadataError('native_output_limit')
            if key.data == 'out':
                self.buffer.extend(data)
                if len(self.buffer) > MAX_FRAME + 4096:
                    raise MetadataError('native_frame_limit')

    def frame(self):
        while b'\r\n\r\n' not in self.buffer:
            if len(self.buffer) > 4096:
                raise MetadataError('rpc_header_limit')
            if not self.selector.get_map():
                raise MetadataError('rpc_closed')
            self.pump()
        header, _, _ = self.buffer.partition(b'\r\n\r\n')
        if len(header) > 4096:
            raise MetadataError('rpc_header_limit')
        matches = re.findall(rb'(?im)^Content-Length: ([0-9]+)\r?$', header)
        if len(matches) != 1 or not 1 <= int(matches[0]) <= MAX_FRAME:
            raise MetadataError('rpc_length_invalid')
        start, length = len(header) + 4, int(matches[0])
        while len(self.buffer) < start + length:
            if not self.selector.get_map():
                raise MetadataError('rpc_closed')
            self.pump()
        payload = bytes(self.buffer[start:start+length])
        del self.buffer[:start+length]
        return json.loads(payload)

    def request(self, method):
        if self.index >= len(METHODS) or method != METHODS[self.index]:
            raise MetadataError('rpc_method_sequence_rejected')
        self.index += 1
        frame = json.dumps({'jsonrpc': '2.0', 'id': self.index, 'method': method, 'params': {}}, separators=(',', ':')).encode()
        self.process.stdin.write(f'Content-Length: {len(frame)}\r\n\r\n'.encode() + frame)
        self.process.stdin.flush()
        result = self.frame()
        if (not isinstance(result, dict) or result.get('jsonrpc') != '2.0' or type(result.get('id')) is not int
                or result['id'] != self.index or 'method' in result or 'error' in result or 'result' not in result):
            raise MetadataError('rpc_response_rejected')
        if method == 'runtime.shutdown':
            self.clean_shutdown = True  # SDK contract: response follows cleanup.
        return result['result']


def collect(session, *, rpc_factory=NativeRPC):
    if session.state != 'active' or session.lease.account_ref != ACCOUNT_REF:
        raise MetadataError('active_durable_owner_lease_required')
    session.broker.assert_current(session.lease)
    home = session.home
    for folder in ('work', 'tmp', '.cache'):
        (home / folder).mkdir(mode=0o700)
    settings = {'storeTokenPlaintext': True, 'autoUpdate': False,
                'disableAllHooks': True, 'ide': {'autoConnect': False}}
    with (home / '.copilot/settings.json').open('x', encoding='utf-8') as stream:
        json.dump(settings, stream)
    observed = datetime.now(timezone.utc).isoformat()
    try:
        with rpc_factory(home, lambda: session.broker.renew(session.lease)) as native:
            protocol = native.request('connect')
            if not isinstance(protocol, dict) or protocol.get('protocolVersion') != 3:
                raise MetadataError('unsupported_copilot_rpc_protocol')
            auth = auth_metadata(native.request('auth.getStatus'))
            models = model_metadata(native.request('models.list'))
            quota = quota_metadata(native.request('account.getQuota'), session.lease.canonical_account_ref)
            native.request('runtime.shutdown')
            if not native.clean_shutdown:
                raise MetadataError('native_shutdown_unconfirmed')
    except Exception:
        session.broker.quarantine(session.lease, 'provider_refresh_uncertain')
        raise
    version = session.finish(native_stopped=True)
    return {'schema_version': 1, 'provider': AGENT, 'observed_at': observed, 'auth': auth,
            'models': models, 'quota': quota, 'credential_writeback': 'committed',
            'credential_version_ref': hashlib.sha256(version.encode()).hexdigest(),
            'same_process_account_model_quota': True, 'task_execution_enabled': False,
            'model_calls': 0, 'sessions_created': 0, 'claims_tasks': False}


def cloud_main():
    """Only protected config plus Cloud Run identity can acquire credentials."""
    from agent_hub.credential_broker_service import load_client_config
    from credential_state import RefreshSession
    broker = load_client_config(BROKER_CONFIG)
    config = broker.config
    if config.provider != AGENT or config.account_ref != ACCOUNT_REF or config.profile != 'blueeyes':
        raise MetadataError('broker_owner_binding_mismatch')
    execution_id = os.environ.get('CLOUD_RUN_EXECUTION', '')
    if not execution_id or broker.execution != config.job_name + '/executions/' + execution_id:
        raise MetadataError('broker_execution_binding_mismatch')
    request_id = uuid.uuid4().hex
    attempt = ATTEMPT_ROOT / ('metadata-' + request_id)
    attempt.mkdir(mode=0o700)
    journal = attempt / 'acquisition.json'
    with journal.open('x', encoding='utf-8') as stream:
        json.dump({'request_id': request_id, 'execution': execution_id, 'state': 'acquiring'}, stream)
        stream.flush(); os.fsync(stream.fileno())
    lease = broker.acquire(broker.execution, request_id)
    session = RefreshSession(broker, lease, attempt / 'home')
    try:
        session.restore()
        result = collect(session)
    except Exception:
        broker.quarantine(lease, 'provider_refresh_uncertain')
        raise
    # Never delete fresh provider state in this helper. On uncertain writeback it
    # stays present and quarantined; on success the broker owns the exact version.
    return result
