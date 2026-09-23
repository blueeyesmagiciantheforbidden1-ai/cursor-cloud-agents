"""Pinned Codex app-server metadata only, with fenced opaque refresh writeback.

Official protocol: https://learn.chatgpt.com/docs/app-server (2026-09-21).
The pinned Linux build verified the required schema field surface. This module
does not create threads/turns, perform login, force refresh, redeem resets or
change provider billing. Raw stdout/stderr/config/token material is not logged.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import signal
import subprocess
import threading
import time
import uuid

NATIVE = Path('/opt/runcrew/codex/bin/codex')
NATIVE_SHA256 = '0753dfe1d8b87a52436deb13eb1c549661ef4c84fee2c5aa688385eebeccb761'
VERSION = '0.155.1'
BROKER_CONFIG = Path('/run/config/credential-broker.json')
ATTEMPT_ROOT = Path('/run/codex-private')
OWNER_REFS = {'ryan': 'fb55abaefbe43b9b1cbd82a01f04c398968b29182c598cda9b77477c9110b29f',
              'blueeyes': '9ddbfe0cce4b6653b86b2057f45c398360541f21a100c1589a67a01cbc80aadc'}
EFFORTS = ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra')
PLANS = frozenset(('free', 'go', 'plus', 'pro', 'prolite', 'team', 'self_serve_business_prolite',
    'self_serve_business_usage_based', 'business', 'ent26', 'enterprise_cbp_automation',
    'enterprise_cbp_usage_based', 'enterprise', 'edu', 'edu_plus', 'edu_pro', 'unknown'))
MAX_FRAME, MAX_TOTAL, MAX_PAGES, MAX_MODELS = 2 * 1024 * 1024, 8 * 1024 * 1024, 16, 512
EOF = object()
CONFIG = ('forced_login_method = "chatgpt"\ncli_auth_credentials_store = "file"\n'
          'model_provider = "openai"\napproval_policy = "never"\nsandbox_mode = "read-only"\n'
          'web_search = "disabled"\n[features]\nhooks = false\nmulti_agent = false\nmulti_agent_v2 = false\n')


class MetadataError(ValueError):
    """Fixed, nonsecret codes only."""


SAFE_DIAGNOSTIC_CODES = frozenset(['unsolicited_native_work_event', 'active_durable_owner_lease_required', 'broker_execution_binding_mismatch', 'broker_owner_binding_mismatch', 'codex_metadata_failed_profile_quarantined', 'duplicate_native_json_key', 'duplicate_native_model', 'immutable_native_required', 'metadata_boolean_invalid', 'metadata_identifier_invalid', 'metadata_method_sequence_rejected', 'metadata_number_invalid', 'metadata_params_invalid', 'metadata_sequence_incomplete', 'native_account_id_required', 'native_auth_route_changed', 'native_canonical_account_mismatch', 'native_catalog_invalid', 'native_catalog_limit', 'native_catalog_page_limit', 'native_clean_exit_unconfirmed', 'native_credit_snapshot_invalid', 'native_cursor_invalid', 'native_decimal_units_invalid', 'native_default_effort_invalid', 'native_digest_mismatch', 'native_effective_config_mismatch', 'native_efforts_invalid', 'native_exited_before_response', 'native_frame_invalid', 'native_frame_limit', 'native_input_failed', 'native_json_invalid', 'native_metadata_failed_profile_quarantined', 'native_metadata_timeout', 'native_notification_limit', 'native_output_rejected', 'native_owner_mismatch', 'native_plan_invalid', 'native_quota_identity_invalid', 'native_quota_invalid', 'native_quota_unavailable', 'native_quota_window_invalid', 'native_request_limit', 'native_reset_snapshot_invalid', 'native_response_rejected', 'native_spend_snapshot_invalid', 'native_subscription_identity_required', 'native_tiers_invalid', 'pinned_native_required', 'protected_owner_profile_mismatch', 'rootless_linux_required', 'unsolicited_native_request_or_event'])
def diagnostic(stage, error, native=None):
    value = {'stage': stage, 'error_type': type(error).__name__, 'retry_safe':False, 'automatic_replay':False}
    value['code'] = str(error) if isinstance(error, MetadataError) and str(error) in SAFE_DIAGNOSTIC_CODES else 'unclassified_runtime_error'
    if native is not None:
        value['rpc_method'] = getattr(native, 'last_method', None)
        value['passive_event_methods'] = sorted({v['method'] for v in getattr(native,'passive_notifications',[])})
        value['native_error_code'] = getattr(native, 'last_response_error_code', None)
        value['exit_code_after_stop'] = native.process.poll() if getattr(native,'process',None) is not None else None
        started = getattr(native,'started_at',None)
        value['elapsed_seconds'] = round(time.monotonic()-started,3) if started is not None else None
    print(json.dumps({'component':'codex-metadata','diagnostic':value}),flush=True)


def require(condition, code):
    if not condition:
        raise MetadataError(code)


def ident(value):
    require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:+/\[\]-]{0,127}', value),
            'metadata_identifier_invalid')
    return value


def integer(value, *, minimum=0, maximum=10**12, nullable=False):
    if value is None and nullable:
        return None
    require(type(value) is int and minimum <= value <= maximum, 'metadata_number_invalid')
    return value


def boolean(value, *, nullable=False):
    require(type(value) is bool or nullable and value is None, 'metadata_boolean_invalid')
    return value


def json_value(raw):
    require(isinstance(raw, bytes) and 0 < len(raw) <= MAX_FRAME, 'native_frame_limit')
    def pairs(items):
        value = {}
        for key, item in items:
            require(key not in value, 'duplicate_native_json_key')
            value[key] = item
        return value
    try:
        value = json.loads(raw, object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise MetadataError('native_json_invalid') from None
    require(isinstance(value, dict), 'native_frame_invalid')
    return value


def canonical_account_ref(account_id):
    # Same explicit namespace used by independent enrollment; no email fallback.
    require(isinstance(account_id, str) and 1 <= len(account_id) <= 1024
            and not any(ord(c) < 32 for c in account_id), 'native_account_id_required')
    value = {'schema': 1, 'provider': 'codex', 'issuer': 'https://chatgpt.com',
             'subject': account_id, 'tenant': ''}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                     separators=(',', ':')).encode('ascii')).hexdigest()


def account_metadata(value, profile, expected_owner):
    require(profile in OWNER_REFS and expected_owner == OWNER_REFS[profile], 'protected_owner_profile_mismatch')
    account = value.get('account') if isinstance(value, dict) else None
    require(isinstance(account, dict) and account.get('type') == 'chatgpt'
            and value.get('requiresOpenaiAuth') is True and isinstance(account.get('email'), str)
            and len(account['email']) <= 320, 'native_subscription_identity_required')
    owner = hashlib.sha256(account['email'].strip().casefold().encode('utf-8')).hexdigest()
    require(owner == expected_owner, 'native_owner_mismatch')
    require(account.get('planType') in PLANS, 'native_plan_invalid')
    return {'account_ref': owner, 'profile': profile, 'auth_type': 'chatgpt', 'plan_type': account['planType'],
            'owner_verified': True, 'api_fallback_used': False}


def model_metadata(models):
    require(isinstance(models, list) and 1 <= len(models) <= MAX_MODELS, 'native_catalog_invalid')
    output, seen = [], set()
    for item in models:
        require(isinstance(item, dict), 'native_catalog_invalid')
        model_id, model = ident(item.get('id')), ident(item.get('model'))
        require(model_id not in seen, 'duplicate_native_model')
        seen.add(model_id)
        options = item.get('supportedReasoningEfforts')
        require(isinstance(options, list) and 1 <= len(options) <= 32
                and all(isinstance(row, dict) for row in options), 'native_efforts_invalid')
        levels = [ident(row.get('reasoningEffort')) for row in options]
        require(len(set(levels)) == len(levels), 'native_efforts_invalid')
        default = ident(item.get('defaultReasoningEffort'))
        require(default in levels, 'native_default_effort_invalid')
        known = all(level in EFFORTS for level in levels)
        tiers = item.get('serviceTiers', [])
        require(isinstance(tiers, list) and len(tiers) <= 32 and all(isinstance(t, dict) for t in tiers),
                'native_tiers_invalid')
        output.append({'id': model_id, 'model': model, 'hidden': boolean(item.get('hidden')),
            'is_default': boolean(item.get('isDefault')), 'default_effort': default, 'supported_efforts': levels,
            'maximum_known_effort': max(levels, key=EFFORTS.index) if known else None,
            'effort_policy_update_required': not known, 'service_tier_ids': [ident(t.get('id')) for t in tiers],
            'strongest_model_verified': False, 'inference_entitlement_verified': False})
    return output


def decimal_units(value):
    if value is None:
        return None
    require(isinstance(value, str) and re.fullmatch(r'[0-9]{1,16}(?:\.[0-9]{1,12})?', value),
            'native_decimal_units_invalid')
    return value


def rate_metadata(value, canonical_ref):
    require(isinstance(value, dict) and canonical_account_ref(value.get('accountId')) == canonical_ref,
            'native_canonical_account_mismatch')
    included = boolean(value.get('ordinaryUsageAllowed'), nullable=True)
    buckets = value.get('rateLimitsByLimitId')
    if buckets is None:
        legacy = value.get('rateLimits')
        require(isinstance(legacy, dict), 'native_quota_unavailable')
        buckets = {legacy.get('limitId') or 'legacy': legacy}
    require(isinstance(buckets, dict) and 1 <= len(buckets) <= 64, 'native_quota_invalid')
    pools = []
    for key, bucket in buckets.items():
        key = ident(key)
        require(isinstance(bucket, dict) and bucket.get('limitId') in (None, key), 'native_quota_identity_invalid')
        row = {'limit_id': key, 'pool_ref': hashlib.sha256(('openai-codex:' + canonical_ref + ':' + key).encode()).hexdigest(),
               'pool_identity_source': 'verified_native_account_and_limit_id',
               'spend_control_reached': boolean(bucket.get('spendControlReached'), nullable=True)}
        for name in ('primary', 'secondary'):
            window = bucket.get(name)
            row[name] = None
            if window is not None:
                require(isinstance(window, dict), 'native_quota_window_invalid')
                used = integer(window.get('usedPercent'), maximum=10000)
                row[name] = {'used_percent': used, 'remaining_fraction': max(0, 100-used)/100,
                    'window_duration_mins': integer(window.get('windowDurationMins'), minimum=1, maximum=5256000, nullable=True),
                    'resets_at': integer(window.get('resetsAt'), maximum=32503680000, nullable=True)}
        credits = bucket.get('credits')
        row['credits'] = None
        if credits is not None:
            require(isinstance(credits, dict), 'native_credit_snapshot_invalid')
            row['credits'] = {'has_credits': boolean(credits.get('hasCredits')),
                'unlimited': boolean(credits.get('unlimited')), 'balance_raw_units': decimal_units(credits.get('balance')),
                'currency': 'unknown', 'purchase_or_credit_spend_authorized_by_snapshot': False}
        individual = bucket.get('individualLimit')
        row['individual_limit'] = None
        if individual is not None:
            require(isinstance(individual, dict), 'native_spend_snapshot_invalid')
            row['individual_limit'] = {'limit_raw_units': decimal_units(individual.get('limit')),
                'used_raw_units': decimal_units(individual.get('used')),
                'remaining_percent': integer(individual.get('remainingPercent'), minimum=-10000, maximum=100),
                'resets_at': integer(individual.get('resetsAt'), maximum=32503680000), 'currency': 'unknown'}
        pools.append(row)
    resets = value.get('rateLimitResetCredits')
    require(resets is None or isinstance(resets, dict), 'native_reset_snapshot_invalid')
    return {'canonical_account_ref': canonical_ref, 'ordinary_usage_allowed': included,
            'pools': pools, 'reset_credits_available': None if resets is None else integer(resets.get('availableCount'), maximum=1000000),
            'credit_spend_requested': False, 'resets_redeemed': 0, 'included_permission_inferred_from_percentages': False}


def config_metadata(value):
    config = value.get('config') if isinstance(value, dict) else None
    require(isinstance(config, dict) and config.get('forced_login_method') == 'chatgpt'
            and config.get('cli_auth_credentials_store') == 'file' and config.get('model_provider') == 'openai'
            and config.get('approval_policy') == 'never' and config.get('sandbox_mode') == 'read-only',
            'native_effective_config_mismatch')
    return {'forced_login_method': 'chatgpt', 'cli_auth_credentials_store': 'file',
            'model_provider': 'openai', 'approval_policy': 'never', 'sandbox_mode': 'read-only',
            'model': ident(config['model']) if config.get('model') is not None else None,
            'model_reasoning_effort': ident(config['model_reasoning_effort']) if config.get('model_reasoning_effort') is not None else None,
            'full_effective_config_sha256': hashlib.sha256(json.dumps(config, sort_keys=True,
                separators=(',', ':'), allow_nan=False).encode()).hexdigest(),
            'project_dispatch_or_plugin_isolation_verified': False}


def environment(home):
    return {'HOME': str(home), 'CODEX_HOME': str(home), 'PATH': '/usr/local/bin:/usr/bin:/bin',
            'LANG': 'C.UTF-8', 'TMPDIR': str(home / 'tmp')}


def verify_native():
    require(os.name == 'posix' and os.geteuid() != 0, 'rootless_linux_required')
    require(not NATIVE.is_symlink() and NATIVE.is_file(), 'pinned_native_required')
    stat = NATIVE.stat()
    require(stat.st_uid == 0 and not stat.st_mode & 0o022 and not os.access(NATIVE, os.W_OK), 'immutable_native_required')
    with NATIVE.open('rb') as stream:
        require(hashlib.file_digest(stream, 'sha256').hexdigest() == NATIVE_SHA256, 'native_digest_mismatch')


class NativeRPC:
    """One process, serial bounded JSON lines, no general-purpose RPC surface."""
    def __init__(self, home, renew):
        verify_native()
        self.home, self.renew = Path(home), renew
        self.started_at = time.monotonic()
        self.deadline, self.next_renew = time.monotonic() + 120, time.monotonic() + 25
        self.process = subprocess.Popen([str(NATIVE), 'app-server'], cwd=self.home / 'work', env=environment(self.home),
            shell=False, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True, bufsize=0)
        self.frames, self.failed = queue.Queue(maxsize=64), threading.Event()
        self.total, self.index, self.stage, self.notifications = 0, 0, 'new', 0
        self.total_lock = threading.Lock()
        self.clean_shutdown = False
        self.last_method, self.last_response_error_code = None, None
        self.writers = []
        self.readers = [threading.Thread(target=self._reader, args=(self.process.stdout, True), daemon=True),
                        threading.Thread(target=self._reader, args=(self.process.stderr, False), daemon=True)]
        for reader in self.readers:
            reader.start()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, AttributeError):
                self.process.kill()
        self.process.wait(timeout=5)
        for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
            try:
                pipe.close()
            except (OSError, ValueError):
                pass
        for thread in self.readers + self.writers:
            thread.join(timeout=0.5)

    def _reader(self, stream, stdout):
        try:
            while True:
                data = stream.readline(MAX_FRAME + 2) if stdout else stream.read(4096)
                if not data:
                    break
                with self.total_lock:
                    self.total += len(data)
                    if self.total > MAX_TOTAL:
                        self.failed.set()
                        return
                if stdout:
                    if len(data) > MAX_FRAME or not data.endswith(b'\n'):
                        self.failed.set()
                        return
                    self.frames.put_nowait(data)
        except (OSError, ValueError, queue.Full):
            self.failed.set()
        finally:
            if stdout:
                try:
                    self.frames.put_nowait(EOF)
                except queue.Full:
                    self.failed.set()

    def tick(self):
        require(not self.failed.is_set(), 'native_output_rejected')
        require(time.monotonic() < self.deadline, 'native_metadata_timeout')
        if time.monotonic() >= self.next_renew:
            self.renew()
            self.next_renew = time.monotonic() + 25
            require(time.monotonic() < self.deadline, 'native_metadata_timeout')

    def _send(self, value=None, *, close=False):
        raw = b'' if value is None else (json.dumps(value, separators=(',', ':'), allow_nan=False)+'\n').encode()
        require(len(raw) <= 4096, 'native_request_limit')
        done, failed = threading.Event(), threading.Event()
        def write():
            try:
                if raw:
                    self.process.stdin.write(raw)
                    self.process.stdin.flush()
                if close:
                    self.process.stdin.close()
            except (OSError, ValueError):
                failed.set()
            finally:
                done.set()
        thread = threading.Thread(target=write, daemon=True)
        self.writers.append(thread)
        thread.start()
        while not done.wait(0.02):
            self.tick()
        self.tick()
        require(not failed.is_set(), 'native_input_failed')

    def _notification(self, value):
        # JSON-RPC notifications are passive: preserve bounded future metadata
        # without interpreting it as auth, model, quota, or task success. Never
        # answer unsolicited requests or accept unrequested work/tool activity.
        method = value.get('method')
        require('id' not in value and isinstance(method, str)
                and re.fullmatch(r'[A-Za-z][A-Za-z0-9_/.-]{0,127}', method),
                'unsolicited_native_request_or_event')
        require(not method.startswith(('thread/', 'turn/', 'item/', 'command/',
                'process/', 'fs/')), 'unsolicited_native_work_event')
        self.notifications += 1
        require(self.notifications <= 64, 'native_notification_limit')
        if not hasattr(self, 'passive_notifications'):
            self.passive_notifications = []
        self.passive_notifications.append(value)
        if method == 'account/updated':
            require(isinstance(value.get('params'), dict)
                    and value['params'].get('authMode') == 'chatgpt', 'native_auth_route_changed')

    def request(self, method, params):
        self.last_method = method
        expected = {'new':'initialize', 'account':'account/read', 'models':'model/list',
                    'rates':'account/rateLimits/read', 'config':'config/read'}.get(self.stage)
        require(method == expected, 'metadata_method_sequence_rejected')
        require(isinstance(params, dict), 'metadata_params_invalid')
        if method == 'initialize':
            require(params == {'clientInfo': {'name':'runcrew_codex_metadata','version':'1.0'}}, 'metadata_params_invalid')
        elif method == 'account/read':
            require(params == {'refreshToken':False}, 'metadata_params_invalid')
        elif method == 'model/list':
            require(set(params) <= {'limit','includeHidden','cursor'} and params.get('limit') == 100
                    and params.get('includeHidden') is True, 'metadata_params_invalid')
            if 'cursor' in params:
                require(isinstance(params['cursor'], str) and 1 <= len(params['cursor']) <= 512
                        and not any(ord(c) < 32 for c in params['cursor']), 'native_cursor_invalid')
        elif method == 'account/rateLimits/read':
            require(params == {}, 'metadata_params_invalid')
        else:
            require(params == {'includeLayers':False,'cwd':str(self.home / 'work')}, 'metadata_params_invalid')
        self.index += 1
        self._send({'id':self.index,'method':method,'params':params})
        while True:
            self.tick()
            try:
                raw = self.frames.get(timeout=0.05)
            except queue.Empty:
                continue
            require(raw is not EOF, 'native_exited_before_response')
            value = json_value(raw)
            if 'method' in value:
                self._notification(value)
                continue
            error = value.get('error')
            if isinstance(error, dict) and type(error.get('code')) is int:
                self.last_response_error_code = error['code']
            require(type(value.get('id')) is int and value['id'] == self.index and 'error' not in value
                    and isinstance(value.get('result'), dict), 'native_response_rejected')
            result = value['result']
            if method == 'initialize':
                self._send({'method':'initialized','params':{}})
                self.stage = 'account'
            elif method == 'account/read':
                self.stage = 'models'
            elif method == 'model/list':
                self.stage = 'models' if result.get('nextCursor') is not None else 'rates'
            elif method == 'account/rateLimits/read':
                self.stage = 'config'
            else:
                self.stage = 'done'
            return result

    def finish(self):
        require(self.stage == 'done', 'metadata_sequence_incomplete')
        self._send(close=True)
        cutoff = min(self.deadline, time.monotonic()+5)
        while self.process.poll() is None and time.monotonic() < cutoff:
            self.tick()
            time.sleep(0.02)
        require(self.process.poll() == 0, 'native_clean_exit_unconfirmed')
        for reader in self.readers:
            reader.join(timeout=0.5)
        self.tick()
        while not self.frames.empty():
            raw = self.frames.get_nowait()
            if raw is not EOF:
                self._notification(json_value(raw))
        self.clean_shutdown = True


def collect(session, *, rpc_factory=NativeRPC):
    lease = session.lease
    require(session.state == 'active' and lease.profile in OWNER_REFS and lease.account_ref == OWNER_REFS[lease.profile]
            and isinstance(getattr(lease, 'canonical_account_ref', None), str)
            and re.fullmatch('[a-f0-9]{64}', lease.canonical_account_ref), 'active_durable_owner_lease_required')
    session.broker.assert_current(lease)
    home = session.home
    for name in ('work','tmp'):
        (home / name).mkdir(mode=0o700)
    with (home / 'config.toml').open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(CONFIG)
    observed = datetime.now(timezone.utc).isoformat()
    stage, native = 'native_start', None
    try:
        with rpc_factory(home, lambda:session.broker.renew(lease)) as native:
            stage = 'initialize'
            native.request('initialize', {'clientInfo': {'name':'runcrew_codex_metadata','version':'1.0'}})
            stage = 'account'
            account = account_metadata(native.request('account/read', {'refreshToken':False}), lease.profile, lease.account_ref)
            stage = 'models'
            models, cursor, seen = [], None, set()
            for _ in range(MAX_PAGES):
                params = {'limit':100,'includeHidden':True}
                if cursor is not None:
                    params['cursor'] = cursor
                result = native.request('model/list', params)
                require(isinstance(result.get('data'), list), 'native_catalog_invalid')
                models.extend(result['data'])
                require(len(models) <= MAX_MODELS, 'native_catalog_limit')
                cursor = result.get('nextCursor')
                if cursor is None:
                    break
                require(isinstance(cursor, str) and 1 <= len(cursor) <= 512 and cursor not in seen,
                        'native_cursor_invalid')
                seen.add(cursor)
            else:
                raise MetadataError('native_catalog_page_limit')
            models = model_metadata(models)
            stage = 'rates'
            rates = rate_metadata(native.request('account/rateLimits/read', {}), lease.canonical_account_ref)
            stage = 'config'
            config = config_metadata(native.request('config/read', {'includeLayers':False,'cwd':str(home / 'work')}))
            stage = 'shutdown'
            native.finish()
            require(native.clean_shutdown, 'native_clean_exit_unconfirmed')
    except Exception as error:
        diagnostic(stage,error,native)
        try:
            session.broker.quarantine(lease, 'provider_refresh_uncertain')
        except Exception:
            pass
        raise MetadataError('native_metadata_failed_profile_quarantined') from None
    version = session.finish(native_stopped=True)
    return {'schema_version':1,'provider':'codex','observed_at':observed,'native_version':VERSION,
        'native_sha256':NATIVE_SHA256,'account':account,'models':models,'quota':rates,'effective_config':config,
        'native_catalog_pages_complete':True,'same_process_account_model_quota':True,
        'credential_writeback':'committed','credential_version_ref':hashlib.sha256(version.encode()).hexdigest(),
        'model_calls':0,'threads_created':0,'turns_started':0,'claims_tasks':False,
        'task_execution_enabled':False,'inference_entitlement_verified':False}


def cloud_main():
    from agent_hub.credential_broker_service import load_client_config
    from credential_state import RefreshSession
    verify_native()  # Reject wrong image before lease/credential acquisition.
    broker = load_client_config(BROKER_CONFIG)
    config = broker.config
    require(config.provider == 'codex' and config.profile in OWNER_REFS
            and config.account_ref == OWNER_REFS[config.profile], 'broker_owner_binding_mismatch')
    execution_id = os.environ.get('CLOUD_RUN_EXECUTION', '')
    require(execution_id and broker.execution == config.job_name + '/executions/' + execution_id,
            'broker_execution_binding_mismatch')
    request_id = uuid.uuid4().hex
    attempt = ATTEMPT_ROOT / ('metadata-' + config.profile + '-' + request_id)
    attempt.mkdir(mode=0o700)
    with (attempt / 'acquisition.json').open('x', encoding='utf-8') as stream:
        json.dump({'request_id':request_id,'execution':execution_id,'state':'acquiring'},stream)
        stream.flush()
        os.fsync(stream.fileno())
    lease = broker.acquire(broker.execution, request_id)
    session = RefreshSession(broker, lease, attempt / 'home')
    try:
        session.restore()
        return collect(session)
    except Exception:
        try:
            broker.quarantine(lease, 'provider_refresh_uncertain')
        except Exception:
            pass
        raise MetadataError('codex_metadata_failed_profile_quarantined') from None


if __name__ == '__main__':
    try:
        print(json.dumps(cloud_main(), allow_nan=False), flush=True)
    except Exception:
        print('{"provider":"codex","status":"metadata_unavailable","model_calls":0,"error":"metadata_failed"}',flush=True)
        raise SystemExit(1)
