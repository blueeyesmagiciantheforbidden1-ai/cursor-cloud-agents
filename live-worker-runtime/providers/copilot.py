"""One warm Copilot subscription session for one explicit project request.

The common worker owns the exact execution grant, room purpose and claim journal.
This adapter never acquires another lease, retries a prompt or enables tools.
Readiness describes an authenticated native process, not a completed coding task.
All deadlines passed by callers are absolute time.monotonic() values.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
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
from uuid import uuid4

import provider_errors

MODEL, EFFORT = 'kimi-k3', 'max'
TOOLS_POLICY = 'deny_all_and_abort_on_observed_tool'
# The prepare() argument is only the startup budget. The live loop then waits
# up to an hour. This cap does not slide; maintain() must not push it forward.
WARM_SECONDS = 3600
# Idle maintain() failures that mean the warm native session is gone. A dead
# stdio pipe is the third case and arrives as OSError, which has no code.
# Every other vetted CopilotError keeps its own code. copilot_hub_heartbeat_lost
# is not in this set: the handle stays open so the next maintain() can renew.
IDLE_SESSION_LOSS = frozenset((
    'copilot_warm_process_ended', 'copilot_idle_deadline_expired'))
ACCOUNT_REF = '9ddbfe0cce4b6653b86b2057f45c398360541f21a100c1589a67a01cbc80aadc'
EXPECTED_LOGIN = 'blueeyesmagiciantheforbidden1-ai'
NATIVE = '/opt/runcrew/copilot/copilot'
NATIVE_SHA256 = 'be0152ea29b06d54dd23e1fc5512e978b4e84097f6da5314df96b3731a2dd6aa'
ARGS = ('--no-auto-update', '--no-custom-instructions', '--disable-builtin-mcps',
        '--headless', '--log-level', 'none', '--stdio')
EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')
MAX_PROMPT_BYTES, MAX_ANSWER_BYTES = 200000, 15000
MAX_FRAME, MAX_TOTAL, MAX_EVENTS = 2 * 1024 * 1024, 8 * 1024 * 1024, 2048
METHODS = frozenset(('connect', 'auth.getStatus', 'models.list', 'account.getQuota',
    'session.create', 'session.model.setAllowedModels', 'session.model.getCurrent',
    'session.send', 'runtime.shutdown'))
PASSIVE_LIFECYCLE = frozenset(('session.created', 'session.updated', 'session.foreground', 'session.background'))
PASSIVE_EVENTS = frozenset(('session.start', 'session.resume', 'session.idle', 'session.info',
    'session.warning', 'session.title_changed', 'session.shutdown', 'session.truncation'))
FORBIDDEN_PREFIXES = ('tool.', 'tools.', 'subagent.', 'permission.', 'userinput.',
    'user_input.', 'elicitation.', 'exitplanmode.', 'automodeswitch.', 'hooks.',
    'llminference.', 'githubtoken.', 'sessionfs.', 'mcp.')
FORBIDDEN_EVENTS = frozenset(('assistant.tool_call_delta', 'assistant.server_tool_progress',
    'skill.invoked', 'hook.start'))
# Mid-turn account exhaustion only: tokens are matched and dropped. A plain
# transient 429 / rate_limit without one of these must stay a normal failure.
_QUOTA_TOKENS = frozenset({
    'usage_limit', 'insufficient_quota', 'quota_exceeded', 'billing', 'credit'})
_TURN_FAILURE_EVENTS = frozenset((
    'session.error', 'session.model_change', 'session.auto_tier_switch_failed',
    'assistant.turn_retry', 'model.call_failure'))


class CopilotError(provider_errors.ProviderCodeError, ValueError):
    """Only fixed adapter codes, never raw native errors or credential bytes."""


class NativeStartupStopped(CopilotError):
    """Constructor failed after positively stopping its native process group."""


def need(condition, code):
    if not condition:
        raise CopilotError(code)


def _quota_token(text):
    """True when allowlisted exhaustion tokens appear; never returns native text."""
    if type(text) is not str or not text:
        return False
    blob = re.sub(r'[^a-z0-9]+', '_', text[:4096].lower()).strip('_')
    if not blob:
        return False
    padded = f'_{blob}_'
    return any(f'_{token}_' in padded for token in _QUOTA_TOKENS)


def _quota_exhausted_signal(value, *, depth=0):
    """Fixed mid-turn exhaustion signals only; 429/rate_limit alone are not enough."""
    if depth > 8 or value is None:
        return False
    if type(value) is str:
        return _quota_token(value)
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                continue
            if key.lower() in (
                    'code', 'type', 'name', 'error', 'message', 'msg', 'reason',
                    'detail', 'details', 'status', 'data', 'body', 'params'):
                if _quota_exhausted_signal(item, depth=depth + 1):
                    return True
        return False
    if type(value) is list:
        return any(_quota_exhausted_signal(item, depth=depth + 1) for item in value[:32])
    return False


def _deadline(value, *, maximum=None):
    now = time.monotonic()
    need(type(value) in (int, float) and math.isfinite(value) and value > now,
         'copilot_deadline_invalid')
    need(maximum is None or value - now <= maximum, 'copilot_deadline_too_long')
    return value


def _number(value, *, minimum=0, maximum=10**15):
    need(type(value) in (int, float) and math.isfinite(value) and minimum <= value <= maximum,
         'copilot_quota_number_invalid')
    return value


def _owner(value):
    need(type(value) is dict and value.get('isAuthenticated') is True
         and value.get('authType') == 'user' and value.get('login') == EXPECTED_LOGIN
         and value.get('host') in ('github.com', 'https://github.com'), 'copilot_owner_mismatch')
    return {'native_owner_verified': True, 'intended_account_ref': ACCOUNT_REF,
            'auth_type': 'user', 'api_fallback': False}


def _catalog(value):
    rows = value.get('models') if type(value) is dict else None
    need(type(rows) is list and 0 < len(rows) <= 1000 and all(type(row) is dict for row in rows),
         'copilot_catalog_invalid')
    selected = [row for row in rows if row.get('id') == MODEL]
    need(len(selected) == 1, 'copilot_reviewed_model_unavailable')
    row = selected[0]
    capabilities, policy = row.get('capabilities'), row.get('policy')
    support = capabilities.get('supports') if type(capabilities) is dict else None
    levels = row.get('supportedReasoningEfforts')
    need(type(policy) is dict and policy.get('state') == 'enabled', 'copilot_model_policy_disabled')
    need(type(support) is dict and support.get('reasoningEffort') is True
         and type(levels) is list and 0 < len(levels) <= 16
         and all(type(level) is str and level in EFFORTS for level in levels)
         and len(set(levels)) == len(levels) and max(levels, key=EFFORTS.index) == EFFORT,
         'copilot_maximum_effort_unverified')
    return {'source': 'native_authenticated_jsonrpc', 'model': MODEL,
            'canonical_model_id': 'moonshot/kimi-k3', 'reviewed_maximum_effort': EFFORT,
            'reasoning_efforts': list(levels), 'account_eligible_catalog': True,
            'selection_basis': 'reviewed_diverse_family_plan', 'universal_best_claimed': False}


def _billing(value, canonical_account_ref):
    need(type(canonical_account_ref) is str and re.fullmatch('[a-f0-9]{64}', canonical_account_ref),
         'copilot_canonical_quota_identity_required')
    snapshots = value.get('quotaSnapshots') if type(value) is dict else None
    need(type(snapshots) is dict and 0 < len(snapshots) <= 32, 'copilot_native_quota_required')
    pools = []
    for kind, item in snapshots.items():
        need(type(kind) is str and re.fullmatch('[A-Za-z0-9][A-Za-z0-9._-]{0,127}', kind)
             and type(item) is dict, 'copilot_quota_shape')
        unlimited = item.get('isUnlimitedEntitlement')
        need(type(unlimited) is bool, 'copilot_quota_boolean_required')
        need(item.get('usageAllowedWithExhaustedQuota') is False
             and item.get('overageAllowedWithExhaustedQuota') is False,
             'copilot_native_zero_extra_route_required')
        need(_number(item.get('overage')) == 0, 'copilot_native_overage_nonzero')
        entitlement = _number(item.get('entitlementRequests'), minimum=-1)
        used = _number(item.get('usedRequests'))
        percentage = item.get('remainingPercentage')
        if percentage is not None:
            percentage = _number(percentage, maximum=100)
        # Chat/completion pools can be unlimited without quantifying premium
        # model allowance. Only the reviewed premium pool admits this plan.
        if kind == 'premium_interactions':
            need(not unlimited and entitlement > 0 and used < entitlement,
                 'copilot_included_allowance_unverified_or_exhausted')
            need(percentage is None or percentage > 0, 'copilot_included_allowance_exhausted')
        reset = item.get('resetDate')
        if reset is not None:
            need(type(reset) is str and len(reset) <= 40, 'copilot_quota_reset_invalid')
            try:
                parsed = datetime.fromisoformat(reset.replace('Z', '+00:00'))
            except ValueError:
                raise CopilotError('copilot_quota_reset_invalid') from None
            need(parsed.tzinfo is not None, 'copilot_quota_reset_timezone_missing')
        pools.append({'quota_type': kind,
            'pool_ref': hashlib.sha256(('github-copilot:' + canonical_account_ref + ':' + kind).encode()).hexdigest(),
            'isUnlimitedEntitlement': unlimited, 'entitlementRequests': entitlement, 'usedRequests': used,
            'remainingPercentage': percentage,
            'native_included_used_percent': None if unlimited or percentage is None else 100-percentage,
            'usageAllowedWithExhaustedQuota': False, 'overageAllowedWithExhaustedQuota': False,
            'overage': 0, 'resetDate': reset})
    premium = [pool for pool in pools if pool['quota_type'] == 'premium_interactions']
    need(len(premium) == 1, 'copilot_premium_allowance_required')
    percentage = premium[0]['native_included_used_percent']
    return {'source': 'native_authenticated_jsonrpc', 'observed_at': datetime.now(timezone.utc).isoformat(),
            'native_usage_status': 'unavailable' if percentage is None else 'available',
            'native_included_used_percent': percentage, 'pools': pools,
            'zero_extra_route': 'native_exhausted_quota_and_overage_denied',
            'provider_spend_cap': 'unavailable', 'browser_evidence_used': False,
            'unlimited_is_free': False, 'automatic_improvement_ready': False}


def _applied(value):
    need(type(value) is dict and value.get('modelId') == MODEL and value.get('reasoningEffort') == EFFORT,
         'copilot_applied_selection_mismatch')


def verify_native():
    path = Path(NATIVE)
    need(not path.is_symlink() and path.is_file() and path.stat().st_size <= 512*1024*1024,
         'copilot_native_file_invalid')
    with path.open('rb') as stream:
        need(stream.read(4) == b'\x7fELF', 'copilot_native_format_invalid')
        stream.seek(0)
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    need(digest == NATIVE_SHA256, 'copilot_native_digest_mismatch')


def _environment(home):
    return {'HOME': str(home), 'COPILOT_HOME': str(home / '.copilot'),
            'COPILOT_CACHE_HOME': str(home / '.cache'), 'COPILOT_DISABLE_KEYTAR': '1',
            'COPILOT_AUTO_UPDATE': 'false', 'PATH': '/usr/local/bin:/usr/bin:/bin',
            'LANG': 'C.UTF-8', 'TMPDIR': str(home / 'tmp'), 'CI': 'true', 'NO_COLOR': '1'}


class Native:
    """Bounded native stdio transport; no server request/tool authorization."""
    def __init__(self, home, renew, deadline):
        self.deadline, self.renew = _deadline(deadline), renew
        self.next_renew = time.monotonic()+20
        self.process, self.selector = None, None
        self.buffer, self.events = bytearray(), []
        self.total = self.index = self.event_count = 0
        self.session_id, self.send_started, self.native_stopped = None, False, False
        self.clean_shutdown, self.ignored_notification_count = False, 0
        try:
            self.process = subprocess.Popen([NATIVE, *ARGS], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, cwd=home/'work', env=_environment(home), shell=False,
                start_new_session=True, bufsize=0)
            self.selector = selectors.DefaultSelector()
            for pipe, label in ((self.process.stdout, 'out'), (self.process.stderr, 'err')):
                self.selector.register(pipe, selectors.EVENT_READ, label)
            os.set_blocking(self.process.stdin.fileno(), False)
        except Exception:
            try:
                self.close()
            except Exception:
                raise CopilotError('copilot_native_startup_stop_uncertain') from None
            raise NativeStartupStopped('copilot_native_startup_stopped') from None

    def close(self):
        try:
            if self.process is not None and not self.native_stopped:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.process.wait(timeout=5)
                need(type(self.process.poll()) is int, 'copilot_native_stop_unconfirmed')
            self.native_stopped = True
        finally:
            if self.selector is not None:
                self.selector.close()
            if self.process is not None:
                for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
                    if pipe is not None:
                        pipe.close()

    def drain_after_shutdown(self):
        """Reap then inspect all emitted frames before admitting final output."""
        need(self.clean_shutdown, 'copilot_native_shutdown_required')
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.process.wait(timeout=5)
        self.native_stopped = type(self.process.poll()) is int
        need(self.native_stopped, 'copilot_native_stop_unconfirmed')
        drain_deadline = time.monotonic()+5
        while self.selector.get_map():
            need(time.monotonic() < drain_deadline, 'copilot_terminal_drain_timeout')
            for key, _ in self.selector.select(0.05):
                data = os.read(key.fd, 65536)
                if not data:
                    self.selector.unregister(key.fileobj)
                    continue
                self.total += len(data)
                need(self.total <= MAX_TOTAL, 'copilot_native_output_limit')
                if key.data == 'out':
                    self.buffer.extend(data)
                    need(len(self.buffer) <= MAX_FRAME+4096, 'copilot_native_frame_limit')
            while True:
                value = self._take_frame()
                if value is None:
                    break
                self.notification(value)
        need(not self.buffer, 'copilot_terminal_frame_incomplete')

    def _tick(self):
        need(time.monotonic() < self.deadline, 'copilot_native_deadline_expired')
        if time.monotonic() >= self.next_renew:
            self.renew()
            self.next_renew = time.monotonic()+20
        need(time.monotonic() < self.deadline, 'copilot_native_deadline_expired')

    def pump(self, wait=0.25):
        self._tick()
        for key, _ in self.selector.select(min(wait, max(0, self.deadline-time.monotonic()))):
            data = os.read(key.fd, 65536)
            if not data:
                self.selector.unregister(key.fileobj)
                continue
            self.total += len(data)
            need(self.total <= MAX_TOTAL, 'copilot_native_output_limit')
            if key.data == 'out':
                self.buffer.extend(data)
                need(len(self.buffer) <= MAX_FRAME+4096, 'copilot_native_frame_limit')

    def _take_frame(self):
        at = self.buffer.find(b'\r\n\r\n')
        if at < 0:
            need(len(self.buffer) <= 4096, 'copilot_rpc_header_limit')
            return None
        need(at <= 4096, 'copilot_rpc_header_limit')
        header = bytes(self.buffer[:at])
        lengths = re.findall(rb'(?im)^Content-Length: ([0-9]{1,8})\r?$', header)
        need(len(lengths) == 1 and 0 < int(lengths[0]) <= MAX_FRAME, 'copilot_rpc_length_invalid')
        end = at+4+int(lengths[0])
        if len(self.buffer) < end:
            return None
        payload = bytes(self.buffer[at+4:end])
        del self.buffer[:end]
        try:
            value = json.loads(payload)
        except (ValueError, UnicodeError):
            raise CopilotError('copilot_native_json_invalid') from None
        need(type(value) is dict, 'copilot_native_frame_invalid')
        return value

    def frame(self):
        while True:
            self._tick()
            value = self._take_frame()
            if value is not None:
                return value
            need(self.selector.get_map(), 'copilot_native_rpc_closed')
            self.pump()

    def notification(self, value):
        method = value.get('method') if type(value) is dict else None
        need(type(value) is dict and value.get('jsonrpc') == '2.0' and 'id' not in value
             and type(method) is str and re.fullmatch(r'[A-Za-z_$][A-Za-z0-9_.$/-]{0,127}', method),
             'copilot_unexpected_native_frame')
        self.event_count += 1
        need(self.event_count <= MAX_EVENTS, 'copilot_native_event_limit')
        need(not method.lower().startswith(FORBIDDEN_PREFIXES), 'copilot_tools_forbidden')
        if method not in ('session.event', 'session.lifecycle'):
            self.ignored_notification_count += 1
            return
        params = value.get('params')
        need(type(params) is dict and self.session_id is not None and params.get('sessionId') == self.session_id,
             'copilot_native_session_mismatch')
        if method == 'session.lifecycle':
            need(params.get('type') in PASSIVE_LIFECYCLE
                 and (params.get('metadata') is None or type(params['metadata']) is dict),
                 'copilot_native_lifecycle_invalid')
            return
        event = params.get('event')
        need(type(event) is dict and type(event.get('type')) is str, 'copilot_native_event_invalid')
        kind = event['type']
        need(not kind.lower().startswith(FORBIDDEN_PREFIXES) and kind not in FORBIDDEN_EVENTS,
             'copilot_tools_forbidden')
        if not self.send_started:
            need(kind in PASSIVE_EVENTS, 'copilot_unexpected_pre_prompt_activity')
            return  # Passive setup has no answer provenance and is never queued.
        self.events.append(event)

    def maintain(self):
        self.pump(0)
        while True:
            value = self._take_frame()
            if value is None:
                break
            self.notification(value)
        need(self.process.poll() is None, 'copilot_warm_process_ended')

    def write_frame(self, frame):
        pending = memoryview(frame)
        while pending:
            self.pump(0)
            try:
                count = os.write(self.process.stdin.fileno(), pending)
            except BlockingIOError:
                self.pump()
                continue
            need(type(count) is int and 0 < count <= len(pending), 'copilot_native_input_closed')
            pending = pending[count:]

    def request(self, method, params=None):
        need(method in METHODS and self.index < 24, 'copilot_native_method_forbidden')
        need(not self.clean_shutdown, 'copilot_native_already_shutdown')
        params = {} if params is None else params
        need(type(params) is dict, 'copilot_native_params_invalid')
        if method == 'session.create':
            need(self.session_id is None and type(params.get('sessionId')) is str, 'copilot_duplicate_session_forbidden')
            self.session_id = params['sessionId']
        if method.startswith('session.') and method != 'session.create':
            need(self.session_id is not None and params.get('sessionId') == self.session_id,
                 'copilot_native_session_mismatch')
        if method == 'session.send':
            need(not self.send_started, 'copilot_duplicate_prompt_forbidden')
            self.maintain()  # Parse already queued setup before opening the turn.
            need(not self.events and not self.buffer, 'copilot_pre_prompt_frame_incomplete')
            self.send_started = True  # Any subsequent failure can have consumed usage.
        self.index += 1
        body = json.dumps({'jsonrpc': '2.0', 'id': self.index, 'method': method, 'params': params},
                          ensure_ascii=False, separators=(',', ':')).encode()
        need(len(body) <= MAX_FRAME, 'copilot_outbound_frame_limit')
        self.write_frame(f'Content-Length: {len(body)}\r\n\r\n'.encode()+body)
        while True:
            value = self.frame()
            if 'method' in value:
                self.notification(value)
                continue
            need(value.get('jsonrpc') == '2.0' and type(value.get('id')) is int and value['id'] == self.index,
                 'copilot_native_request_failed')
            if 'error' in value:
                need(not _quota_exhausted_signal(value.get('error')), 'copilot_quota_exhausted')
                raise CopilotError('copilot_native_request_failed')
            need('result' in value, 'copilot_native_request_failed')
            if method == 'runtime.shutdown':
                self.clean_shutdown = True
            return value['result']

    def next_event(self):
        while not self.events:
            self.notification(self.frame())
        return self.events.pop(0)


NativeProcess = Native


def _response(native):
    text, usage, turns = None, None, 0
    for _ in range(MAX_EVENTS):
        event = native.next_event()
        kind, data = event.get('type'), event.get('data', {})
        need(type(kind) is str and type(data) is dict, 'copilot_native_event_invalid')
        need(not kind.lower().startswith(FORBIDDEN_PREFIXES) and kind not in FORBIDDEN_EVENTS,
             'copilot_tools_forbidden')
        if kind in _TURN_FAILURE_EVENTS:
            need(not _quota_exhausted_signal(event) and not _quota_exhausted_signal(data),
                 'copilot_quota_exhausted')
            raise CopilotError('copilot_native_turn_failed_or_changed')
        if kind == 'assistant.turn_start':
            turns += 1
            need(turns == 1, 'copilot_multiple_turns_forbidden')
        elif kind == 'assistant.message':
            need(turns == 1 and text is None and not data.get('toolRequests') and not data.get('serverTools'),
                 'copilot_final_text_or_tools_invalid')
            text = data.get('content')
            need(type(text) is str and text.strip() and len(text.encode()) <= MAX_ANSWER_BYTES,
                 'copilot_final_text_invalid')
            need(data.get('model') in (None, MODEL), 'copilot_served_model_mismatch')
        elif kind == 'assistant.usage':
            need(turns == 1 and usage is None and data.get('model') == MODEL
                 and data.get('reasoningEffort') in (None, EFFORT)
                 and (data.get('isByok') is None or data['isByok'] is False)
                 and (data.get('isAuto') is None or data['isAuto'] is False), 'copilot_served_selection_unverified')
            usage = {'reported_reasoning_effort': data.get('reasoningEffort'),
                     'reported_is_byok': data.get('isByok'), 'reported_is_auto': data.get('isAuto')}
            for key in ('inputTokens', 'outputTokens', 'cacheReadTokens', 'cacheWriteTokens', 'reasoningTokens'):
                if key in data:
                    need(type(data[key]) is int and 0 <= data[key] <= 10**9, 'copilot_usage_counter_invalid')
                    usage[key] = data[key]
        elif kind == 'session.idle':
            need(turns == 1 and text is not None and usage is not None, 'copilot_completion_unverified')
            return text, usage
    raise CopilotError('copilot_native_event_limit')


@dataclass
class Handle:
    session: object = field(repr=False)
    heartbeat: object = field(repr=False)
    idle_deadline: float
    native: object = field(default=None, repr=False)
    sid: str = field(default='', repr=False)
    preflight: dict = field(default_factory=dict)
    owner_verified: bool = False
    attempted: bool = False
    stopped_proven: bool = True
    finished: bool = False
    close_failed: bool = False
    credential_version: str = field(default='', repr=False)

    @property
    def readiness(self):
        live = bool(self.sid) and self.owner_verified and self.native is not None and not self.finished and not self.close_failed
        return {'provider': 'copilot', 'authenticated': live,
            'ready_for_project_prompt': live and not self.attempted, 'model': MODEL, 'effort': EFFORT,
            'preflight': self.preflight, 'tools_enabled': False, 'workspace_access': False,
            'full_coding_ready': False, 'automatic_improvement_ready': False}


def _renew(handle):
    handle.session.broker.renew(handle.session.lease)
    need(handle.heartbeat() is True, 'copilot_hub_heartbeat_lost')


def _fresh_metadata(handle):
    native = handle.native
    handle.owner_verified = False
    account = _owner(native.request('auth.getStatus'))
    handle.owner_verified = True
    catalog = _catalog(native.request('models.list'))
    quota = _billing(native.request('account.getQuota'), handle.session.lease.canonical_account_ref)
    return {'account': account, 'catalog': catalog, 'quota': quota,
            'same_process_account_model_billing': True,
            'same_process_account_model_quota': quota['native_usage_status'] == 'available'}


def close(handle):
    """Stop native before credential commit/release; never repeat uncertain commit."""
    if handle.finished:
        return handle.credential_version
    need(not handle.close_failed, 'copilot_close_requires_reconciliation')
    try:
        if handle.native is not None:
            handle.native.close()
            need(handle.native.native_stopped is True, 'copilot_native_stop_unconfirmed')
            handle.stopped_proven = True
            handle.native = None
        need(handle.stopped_proven, 'copilot_native_stop_unconfirmed')
        # A changed credential may only be published after native identity proof.
        need(handle.owner_verified, 'copilot_owner_unverified_at_close')
        handle.credential_version = handle.session.finish(native_stopped=True)
        need(type(handle.credential_version) is str and bool(handle.credential_version)
             and handle.session.state == 'committed', 'copilot_credential_commit_unconfirmed')
        handle.finished = True
        return handle.credential_version
    except Exception:
        handle.close_failed = True
        try:
            handle.session.broker.quarantine(handle.session.lease, 'provider_refresh_uncertain')
        except Exception:
            pass
        raise CopilotError('copilot_close_requires_reconciliation') from None


def _home(session):
    need(session.home.is_dir() and not session.home.is_symlink(), 'copilot_private_home_required')
    for name in ('work', 'tmp', '.cache'):
        (session.home/name).mkdir(mode=0o700)
    path = session.home/'.copilot'/'settings.json'
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        json.dump({'storeTokenPlaintext': True, 'autoUpdate': False, 'disableAllHooks': True,
                   'ide': {'autoConnect': False}}, stream)
        stream.flush()
        os.fsync(stream.fileno())


def prepare(session, heartbeat, deadline):
    """Warm the exact reviewed subscription model without sending a task."""
    deadline = _deadline(deadline, maximum=3600)
    need(session.state == 'active' and session.lease.account_ref == ACCOUNT_REF
         and callable(heartbeat), 'copilot_active_owner_lease_required')
    handle = Handle(session, heartbeat, deadline)
    try:
        session.broker.assert_current(session.lease)
        verify_native()
        _home(session)
        handle.stopped_proven = False
        try:
            handle.native = NativeProcess(session.home, lambda: _renew(handle), deadline)
        except NativeStartupStopped:
            handle.stopped_proven = True
            raise
        protocol = handle.native.request('connect')
        need(type(protocol) is dict and protocol.get('protocolVersion') == 3, 'copilot_protocol_unsupported')
        handle.preflight = _fresh_metadata(handle)
        sid = str(uuid4())
        created = handle.native.request('session.create', {'sessionId': sid, 'model': MODEL, 'reasoningEffort': EFFORT,
            'workingDirectory': str(session.home/'work'), 'availableTools': [], 'excludedTools': [],
            'toolFilterPrecedence': 'excluded', 'tools': [], 'mcpServers': {}, 'customAgents': [],
            'requestPermission': False, 'requestElicitation': False, 'requestExitPlanMode': False,
            'requestAutoModeSwitch': False, 'streaming': False})
        need(type(created) is dict and created.get('sessionId') == sid, 'copilot_session_identity_mismatch')
        allowed = handle.native.request('session.model.setAllowedModels', {'sessionId': sid, 'allowedModels': [MODEL]})
        need(type(allowed) is dict and allowed.get('allowedModels') == [MODEL]
             and allowed.get('effectiveAllowedModels') == [MODEL]
             and allowed.get('fallbackModel') in (None, MODEL) and allowed.get('modelId') in (None, MODEL),
             'copilot_exact_model_allowlist_unverified')
        _applied(handle.native.request('session.model.getCurrent', {'sessionId': sid}))
        handle.native.maintain()
        need(not handle.native.events, 'copilot_unexpected_pre_prompt_activity')
        handle.sid = sid
        need(heartbeat() is True, 'copilot_hub_heartbeat_lost')
        # Startup budget ends here. An idle session is allowed to live until
        # the fixed warm cap; the CLI, not this deadline, is what drops it.
        handle.idle_deadline = time.monotonic() + WARM_SECONDS
        handle.native.deadline = handle.idle_deadline
        return handle
    except Exception as error:
        try:
            close(handle)
        except CopilotError:
            pass
        if isinstance(error, CopilotError):
            raise
        raise CopilotError('copilot_prepare_requires_reconciliation') from None


def maintain(handle):
    """Renew an idle session.

    The live loop sleeps between calls. The Copilot CLI can destroy a native
    session that has never received a prompt during that gap (its stale-session
    cleanup fires after about 35 minutes of idle). The next maintain() sees the
    process gone, the idle deadline passed, or a dead pipe. Only those become
    copilot_warm_session_lost.

    copilot_hub_heartbeat_lost is re-raised with the native session still
    alive. Native._tick advances next_renew only after renew() returns, so the
    renew stays due and the next maintain() retries it.

    Every other vetted CopilotError closes the handle and is re-raised with
    its own code. BrokerError and MutationUncertain from renew(), and any
    other non-vetted exception, close the handle and become
    copilot_idle_renew_failed.
    """
    need(not handle.finished and not handle.attempted and not handle.close_failed and handle.native is not None,
         'copilot_handle_not_idle')
    try:
        need(time.monotonic() < handle.idle_deadline, 'copilot_idle_deadline_expired')
        handle.native.maintain()
        return handle.readiness
    except Exception as error:
        code = provider_errors.error_code(error)
        # Leave the native process and the due renew in place. Closing here
        # would make the retry target a finished handle.
        if code == 'copilot_hub_heartbeat_lost':
            raise
        close(handle)
        if isinstance(error, OSError) or code in IDLE_SESSION_LOSS:
            raise CopilotError('copilot_warm_session_lost') from None
        if isinstance(error, CopilotError):
            raise
        raise CopilotError('copilot_idle_renew_failed') from None


def execute(handle, prompt, task_deadline, *, task_kind='project'):
    need(not handle.finished and not handle.attempted and not handle.close_failed and handle.native is not None,
         'copilot_task_replay_forbidden')
    try:
        need(task_kind == 'project', 'copilot_automatic_improvement_not_enabled')
        need(type(prompt) is str and prompt.strip() and 0 < len(prompt.encode()) <= MAX_PROMPT_BYTES,
             'copilot_prompt_limit')
        need(time.monotonic() < handle.idle_deadline, 'copilot_idle_deadline_expired')
        handle.native.deadline = _deadline(task_deadline, maximum=900)
        handle.preflight = _fresh_metadata(handle)  # Billing never reuses the idle-time snapshot.
        _applied(handle.native.request('session.model.getCurrent', {'sessionId': handle.sid}))
        handle.session.broker.assert_current(handle.session.lease)
        need(handle.heartbeat() is True, 'copilot_hub_heartbeat_lost')
        handle.attempted = True
        sent = handle.native.request('session.send', {'sessionId': handle.sid, 'prompt': prompt})
        need(type(sent) is dict and type(sent.get('messageId')) is str and bool(sent['messageId']),
             'copilot_send_ack_unverified')
        text, usage = _response(handle.native)
        _applied(handle.native.request('session.model.getCurrent', {'sessionId': handle.sid}))
        handle.native.request('runtime.shutdown')
        need(handle.native.clean_shutdown is True, 'copilot_shutdown_unconfirmed')
        handle.native.drain_after_shutdown()
        for event in handle.native.events:
            need(event.get('type') in PASSIVE_EVENTS, 'copilot_activity_after_completion')
        version = close(handle)
        return {'provider': 'copilot', 'text': text, 'model': MODEL, 'effort': EFFORT, 'usage': usage,
            'preflight': handle.preflight, 'same_process_account_model_quota': handle.preflight['same_process_account_model_quota'],
            'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
            'review_sha256': hashlib.sha256(text.encode()).hexdigest(),
            'native_session_ref_sha256': hashlib.sha256(handle.sid.encode()).hexdigest(),
            'prompt_sent_once_by_wrapper': True, 'automatic_retry': False, 'server_usage_verified': True,
            'effort_verified_by': 'native_applied_settings_before_and_after',
            'automatic_improvement_ready': False, 'full_coding_ready': False,
            'tools_policy': 'deny_all_and_abort_on_observed_tool', 'actual_charge': 'unverified',
            'native_stopped': True, 'credential_writeback': 'committed',
            'credential_version_ref': hashlib.sha256(version.encode()).hexdigest()}
    except Exception as error:
        if not handle.finished and not handle.close_failed:
            close(handle)
        if isinstance(error, CopilotError):
            raise
        raise CopilotError('copilot_task_outcome_requires_reconciliation') from None
