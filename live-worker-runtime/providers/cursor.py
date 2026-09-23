"""Warm, single-task Cursor subscription adapter for explicit project prompts.

Same pinned native ACP profile as the verified cloud review (composer-2.5, fast
off, ask mode, deny-all local permissions, abort on any observed tool event).
prepare() exchanges the enrolled user key, verifies the account owner, creates
one configured session and waits. execute() sends exactly one prompt.
close() kills the process group and commits/releases the credential lease
before a result is delivered. No retries. Deadlines are time.monotonic().

Runtime imports: `metadata` and `cursor_review_runtime` are the same pinned
modules the review image used (/opt/runcrew and /opt/runcrew/app).
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import sys
import time

from agent_hub.cloud_credential_broker import BrokerError
import provider_errors

NATIVE_DIR = Path(__file__).resolve().parents[1] / 'cursor_native'


def _load_native():
    """Load the pinned Cursor modules by path so a same-named module elsewhere cannot shadow them."""
    spec = importlib.util.spec_from_file_location('cursor_native_metadata', NATIVE_DIR / 'metadata.py')
    native = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(native)
    previous = sys.modules.get('metadata')
    sys.modules['metadata'] = native  # cursor_review_runtime does `from metadata import ...`
    try:
        spec = importlib.util.spec_from_file_location('cursor_native_review', NATIVE_DIR / 'cursor_review_runtime.py')
        runtime = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runtime)
    finally:
        if previous is None:
            sys.modules.pop('metadata', None)
        else:
            sys.modules['metadata'] = previous
    return native, runtime


metadata, review = _load_native()

MODEL = review.MODEL
FAST, MODE = 'false', 'ask'
TOOLS_POLICY = 'ask_mode_deny_all_abort_on_observed_tool'
# Keep this binding identical to the enrolled Blueeyes profile (never derived from a prompt).
ACCOUNT_REF = '9ddbfe0cce4b6653b86b2057f45c398360541f21a100c1589a67a01cbc80aadc'
MAX_PROMPT_BYTES = 200000
MAX_ANSWER_BYTES = 15000
HEARTBEAT_SECONDS = 8
# The prepare() argument is only the startup budget. The live loop then waits
# up to an hour. This cap does not slide; maintain() must not push it forward.
WARM_SECONDS = 3600
# MetadataError codes idle_pump can raise while reading a frame. A deadline
# miss is not one of these: the warm cap above is what keeps the pump inside
# its window. Renew failures are broker errors, not protocol frames.
_IDLE_PROTOCOL = frozenset(('native_metadata_output_bound', 'native_metadata_frame_bound'))
SEQUENCE = ('initialize', 'cursor/list_available_models', 'session/new',
            'session/set_config_option', 'session/set_config_option',
            'session/set_config_option', 'session/prompt')
PASSIVE = ('available_commands_update', 'current_mode_update', 'session_info_update', 'config_option_update')
KNOWN = PASSIVE + ('agent_message_chunk', 'agent_thought_chunk', 'usage_update')

if not (hasattr(metadata, 'metadata_environment') and hasattr(metadata, 'account_metadata')
        and getattr(metadata, 'EXECUTABLE', '') == '/opt/runcrew/cursor/cursor-agent'):
    raise ImportError('cursor_native_metadata_module_required')


class NativeError(provider_errors.ProviderCodeError, RuntimeError):
    """Only fixed codes; never provider output, identifiers or the key."""


class NativeStartupStopped(NativeError):
    """The native process never started, so no stop needs proving."""


def need(ok, code):
    if not ok:
        raise NativeError(code)


def _deadline(value):
    need(type(value) in (int, float) and math.isfinite(value) and value > time.monotonic(),
         'cursor_deadline_invalid')
    return value


class LiveProcess(metadata.NativeProcess):
    """Same ACP process from initialization through selection, idle wait and one answer."""

    def __init__(self, environment, workspace, deadline, heartbeat):
        super().__init__(('acp',), environment, workspace, deadline)
        self._init_state(heartbeat)

    def _init_state(self, heartbeat):
        self.heartbeat = heartbeat
        self.next_heartbeat = time.monotonic()
        self.session_id = None
        self.stage = 0
        self.prompt_sent = False
        self.answer = ''
        self.counts = {}
        self.effective_selected = False
        self.pending_session_updates = []

    def pump(self):
        if time.monotonic() >= self.next_heartbeat:
            need(self.heartbeat() is True, 'cursor_lease_lost')
            self.next_heartbeat = time.monotonic() + HEARTBEAT_SECONDS
        super().pump()

    def alive(self):
        return self.process.poll() is None

    def idle_pump(self):
        """Consume passive notifications while idle; content or tools abort."""
        if self.selector.get_map():
            self.pump()
        while b'\n' in self.buffer:
            line, _, rest = self.buffer.partition(b'\n')
            self.buffer = bytearray(rest)
            value = review.strict_json(line)
            need(isinstance(value, dict) and value.get('jsonrpc') == '2.0' and 'method' in value,
                 'cursor_unexpected_idle_response')
            self.notification(value)

    def request(self, method, params):
        need(self.stage < len(SEQUENCE) and method == SEQUENCE[self.stage], 'cursor_rpc_sequence')
        if self.stage == 0:
            need(params == metadata.initialize_params(), 'cursor_initialize_mismatch')
        elif self.stage == 1:
            need(params == {}, 'cursor_catalog_parameters')
        elif self.stage == 2:
            need(set(params) == {'cwd', 'mcpServers'} and params['mcpServers'] == [], 'cursor_session_parameters')
        elif self.stage in (3, 4, 5):
            key, value = [('model', MODEL), ('fast', FAST), ('mode', MODE)][self.stage - 3]
            need(params == {'sessionId': self.session_id, 'configId': key, 'value': value},
                 'cursor_selection_parameters')
        else:
            need(self.effective_selected and not self.prompt_sent and params.get('sessionId') == self.session_id
                 and set(params) == {'sessionId', 'prompt'} and isinstance(params['prompt'], list)
                 and len(params['prompt']) == 1 and set(params['prompt'][0]) == {'type', 'text'}
                 and params['prompt'][0]['type'] == 'text' and isinstance(params['prompt'][0]['text'], str)
                 and 0 < len(params['prompt'][0]['text'].encode()) <= MAX_PROMPT_BYTES,
                 'cursor_prompt_not_authorized')
            need(self.heartbeat() is True, 'cursor_lease_lost_before_prompt')
            self.prompt_sent = True  # a write error is still an uncertain attempt
        self.stage += 1
        request_id = self.stage
        self.process.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': request_id, 'method': method,
                                             'params': params}, separators=(',', ':')).encode() + b'\n')
        self.process.stdin.flush()
        while True:
            while b'\n' not in self.buffer:
                need(bool(self.selector.get_map()), 'cursor_rpc_closed')
                self.pump()
            line, _, rest = self.buffer.partition(b'\n')
            self.buffer = bytearray(rest)
            value = review.strict_json(line)
            need(isinstance(value, dict) and value.get('jsonrpc') == '2.0', 'cursor_rpc_envelope')
            if 'method' in value:
                self.notification(value)
                continue
            need(value.get('id') == request_id and 'error' not in value
                 and set(value) == {'jsonrpc', 'id', 'result'}, 'cursor_rpc_response')
            result = value['result']
            need(isinstance(result, dict), 'cursor_rpc_result')
            if method == 'session/new':
                sid = result.get('sessionId')
                need(isinstance(sid, str) and re.fullmatch(r'[a-f0-9-]{36}', sid), 'cursor_session_identity')
                self.session_id = sid
                review.config_options(result)
                for pending in self.pending_session_updates:
                    self.notification(pending)
                self.pending_session_updates = []
            if self.stage == 6:
                review.config_options(result, selected=True)
                self.effective_selected = True
            return result

    def notification(self, value):
        if value.get('method') == 'session/request_permission' and 'id' in value:
            self.process.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': value['id'],
                'result': {'outcome': {'outcome': 'selected', 'optionId': 'reject-once'}}}).encode() + b'\n')
            self.process.stdin.flush()
            raise NativeError('cursor_tool_permission_rejected')
        need(value.get('method') == 'session/update' and 'id' not in value,
             'cursor_unsupported_native_request')
        params = value.get('params')
        need(isinstance(params, dict), 'cursor_session_update_mismatch')
        if self.session_id is None:
            update = params.get('update')
            need(self.stage == 3 and isinstance(params.get('sessionId'), str)
                 and re.fullmatch(r'[a-f0-9-]{36}', params['sessionId'])
                 and isinstance(update, dict) and update.get('sessionUpdate') in PASSIVE,
                 'cursor_unsafe_update_before_session_binding')
            need(len(self.pending_session_updates) < 32, 'cursor_session_metadata_buffer_excessive')
            self.pending_session_updates.append(value)
            return
        need(params.get('sessionId') == self.session_id, 'cursor_session_update_mismatch')
        update = params.get('update')
        need(isinstance(update, dict), 'cursor_update_schema')
        kind = update.get('sessionUpdate')
        if kind not in KNOWN:
            self.counts['unknown_or_tool'] = self.counts.get('unknown_or_tool', 0) + 1
            raise NativeError('cursor_tool_or_unknown_native_update')
        self.counts[kind] = self.counts.get(kind, 0) + 1
        if kind in ('agent_message_chunk', 'agent_thought_chunk'):
            need(self.prompt_sent, 'cursor_content_before_prompt')
            content = update.get('content')
            need(isinstance(content, dict) and content.get('type') == 'text'
                 and isinstance(content.get('text'), str), 'cursor_content_schema')
            if kind == 'agent_message_chunk':
                self.answer += content['text']
                need(len(self.answer.encode()) <= MAX_ANSWER_BYTES, 'cursor_answer_excessive')
        elif kind == 'current_mode_update':
            permitted = (MODE,) if self.effective_selected or self.prompt_sent else (MODE, 'agent', 'plan')
            need(update.get('currentModeId') in permitted, 'cursor_mode_changed')
        elif kind == 'config_option_update':
            review.config_options(update, selected=self.effective_selected)

    def drain_buffered(self):
        """Validate received trailing frames without awaiting persistent ACP EOF."""
        while b'\n' in self.buffer:
            line, _, rest = self.buffer.partition(b'\n')
            self.buffer = bytearray(rest)
            value = review.strict_json(line)
            need(isinstance(value, dict) and value.get('jsonrpc') == '2.0' and 'method' in value,
                 'cursor_unexpected_trailing_response')
            update = value.get('params', {}).get('update', {}) if isinstance(value.get('params'), dict) else {}
            need(update.get('sessionUpdate') not in ('agent_message_chunk', 'agent_thought_chunk'),
                 'cursor_content_after_turn_result')
            self.notification(value)
        need(not self.buffer, 'cursor_incomplete_trailing_frame')

    def close(self):
        self.__exit__(None, None, None)


def _metadata_process(command, environment, workspace, deadline, heartbeat):
    class HeartbeatMetadata(metadata.NativeProcess):
        def pump(self):
            if time.monotonic() >= getattr(self, 'next_heartbeat', 0):
                need(heartbeat() is True, 'cursor_lease_lost_during_metadata')
                self.next_heartbeat = time.monotonic() + HEARTBEAT_SECONDS
            return super().pump()
    return HeartbeatMetadata(command, environment, workspace, deadline)


def _acp_process(environment, workspace, deadline, heartbeat):
    try:
        return LiveProcess(environment, workspace, deadline, heartbeat)
    except OSError:
        raise NativeStartupStopped('cursor_native_start_failed') from None


@dataclass
class Handle:
    session: object = field(repr=False)
    heartbeat: object = field(repr=False)
    native: object = field(default=None, repr=False)
    secret: str = field(default='', repr=False)
    settings: object = field(default=None, repr=False)
    sid: str = field(default='', repr=False)
    preflight: dict = field(default_factory=dict)
    attempted: bool = False
    stopped_proven: bool = True
    finished: bool = False
    close_failed: bool = False
    credential_version: str = field(default='', repr=False)

    @property
    def readiness(self):
        live = bool(self.sid) and self.native is not None and not self.finished and not self.close_failed
        return {'provider': 'cursor', 'authenticated': live,
                'ready_for_project_prompt': live and not self.attempted,
                'model': MODEL, 'effort': 'not_exposed_in_native_catalog', 'preflight': self.preflight,
                'tools_enabled': False, 'workspace_access': False, 'full_coding_ready': False,
                'automatic_improvement_ready': False}


def _renew(handle):
    handle.session.broker.renew(handle.session.lease)
    need(handle.heartbeat() is True, 'cursor_hub_heartbeat_lost')
    return True


def _key(session):
    path = session.auth_path
    need(not path.is_symlink() and path.is_file() and path.stat().st_size <= 16_384, 'cursor_credential_file_invalid')
    value = path.read_bytes().decode('utf-8', 'strict').strip()
    need(bool(value) and not any(c.isspace() or ord(c) < 32 for c in value), 'cursor_credential_malformed')
    return value


def _home(session):
    for name in ('work', 'tmp'):
        (session.home / name).mkdir(mode=0o700)
    need((session.home / '.cursor').is_dir(), 'cursor_private_config_home_required')


def close(handle):
    """Stop all native descendants before commit/release; idempotent after success."""
    if handle.finished:
        return handle.credential_version
    need(not handle.close_failed, 'cursor_close_requires_reconciliation')
    try:
        if handle.native is not None:
            handle.native.close()
            handle.stopped_proven = True
            handle.native = None
        need(handle.stopped_proven, 'cursor_native_stop_unconfirmed')
        handle.credential_version = handle.session.finish(native_stopped=True)
        handle.finished = True
        handle.secret = ''
        return handle.credential_version
    except Exception:
        handle.close_failed = True
        try:
            handle.session.broker.quarantine(handle.session.lease, 'provider_refresh_uncertain')
        except Exception:
            pass  # Existing non-idle owner state still prohibits takeover.
        raise


def prepare(session, heartbeat, deadline):
    """Authenticate, verify the owner and model, create one configured warm session."""
    _deadline(deadline)
    # Warm cap origin. The startup deadline passed in (from prepare start, typically
    # 180s) is not reused after success.
    started = time.monotonic()
    need(session.state == 'active' and session.lease.account_ref == ACCOUNT_REF, 'cursor_owner_lease_required')
    handle = Handle(session=session, heartbeat=heartbeat)
    try:
        session.broker.assert_current(session.lease)
        handle.secret = _key(session)
        _home(session)
        environment = metadata.metadata_environment(handle.secret, session.home)
        workspace = session.home / 'work'
        renew = lambda: _renew(handle)
        need(heartbeat() is True, 'cursor_hub_heartbeat_lost')
        # Each metadata helper is a context manager that kills its own process
        # group on exit, so the stop is proven again as soon as the block ends.
        handle.stopped_proven = False
        try:
            with _metadata_process(('models',), environment, workspace, deadline, renew) as native:
                native.completed_output()  # human model list is deliberately discarded
        finally:
            handle.stopped_proven = True
        handle.stopped_proven = False
        try:
            with _metadata_process(('status', '--format', 'json'), environment, workspace, deadline, renew) as native:
                account = metadata.account_metadata(review.strict_json(native.completed_output()), ACCOUNT_REF)
        finally:
            handle.stopped_proven = True
        handle.settings = session.home / '.cursor' / 'cli-config.json'
        review.lock_settings(handle.settings)
        handle.stopped_proven = False
        try:
            handle.native = _acp_process(environment, workspace, deadline, renew)
        except NativeStartupStopped:
            handle.stopped_proven = True
            raise
        init = handle.native.request('initialize', metadata.initialize_params())
        need(isinstance(init, dict) and init.get('protocolVersion') == 1, 'cursor_protocol_unsupported')
        catalog = review.check_catalog(handle.native.request('cursor/list_available_models', {}))
        handle.native.request('session/new', {'cwd': str(workspace), 'mcpServers': []})
        for key, value in (('model', MODEL), ('fast', FAST), ('mode', MODE)):
            handle.native.request('session/set_config_option',
                                  {'sessionId': handle.native.session_id, 'configId': key, 'value': value})
        review.check_settings(handle.settings)
        handle.sid = handle.native.session_id
        handle.preflight = {
            'account': {'intended_account_ref': ACCOUNT_REF, 'native_owner_verified': True,
                        'source': account.get('source')},
            'catalog': {**catalog, 'mode': MODE, 'source': 'native_acp_cursor/list_available_models'},
            'quota': {'native_usage_status': 'unavailable', 'native_included_used_percent': None,
                      'note': 'Cursor CLI exposes no usage counter; the browser spending receipt '
                              'remains the operator control'},
            'same_process_account_model_billing': False,
            'same_process_account_model_quota': False}
        need(heartbeat() is True, 'cursor_hub_heartbeat_lost')
        # Startup budget ends here. The warm cap is WARM_SECONDS from prepare
        # start and does not slide. It is also a full warm window after startup
        # so idle pumping covers the live loop's wait. execute() replaces it.
        handle.native.deadline = max(started + WARM_SECONDS, time.monotonic() + WARM_SECONDS)
        return handle
    except Exception:
        if not handle.finished and not handle.close_failed:
            close(handle)
        raise


def _idle_code(error):
    """Vetted code for an idle_pump fault, or None when the error already is one."""
    if isinstance(error, NativeError):
        return None
    if isinstance(error, BrokerError):
        return 'cursor_idle_renew_failed'
    if isinstance(error, metadata.MetadataError) and str(error) in _IDLE_PROTOCOL:
        return 'cursor_idle_protocol_invalid'
    if isinstance(error, (review.ReviewError, json.JSONDecodeError)):
        return 'cursor_idle_protocol_invalid'
    return None


def _fail_idle(handle, code):
    if not handle.finished and not handle.close_failed:
        close(handle)
    raise NativeError(code) from None


def maintain(handle):
    """Call during idle polling at least every 20s; passive notifications only."""
    need(not handle.finished and not handle.attempted and not handle.close_failed and handle.native is not None,
         'cursor_handle_not_idle')
    need(handle.native.alive(), 'cursor_warm_process_ended')
    try:
        handle.native.idle_pump()
    except Exception as error:
        code = _idle_code(error)
        if code is None:
            raise
        _fail_idle(handle, code)
    need(not handle.native.answer and not handle.native.prompt_sent, 'cursor_content_before_prompt')
    return handle.readiness


def execute(handle, prompt, task_deadline, *, task_kind='project'):
    """One explicit project prompt, followed by confirmed stop and writeback."""
    need(not handle.finished and not handle.attempted and handle.native is not None, 'cursor_task_replay_forbidden')
    try:
        need(task_kind == 'project', 'cursor_automatic_improvement_not_enabled')
        need(type(prompt) is str and 0 < len(prompt.encode()) <= MAX_PROMPT_BYTES, 'cursor_prompt_limit')
        handle.native.deadline = _deadline(task_deadline)
        need(handle.heartbeat() is True, 'cursor_hub_heartbeat_lost')
        handle.attempted = True
        result = handle.native.request('session/prompt', {'sessionId': handle.sid,
                                                          'prompt': [{'type': 'text', 'text': prompt}]})
        need(result.get('stopReason') == 'end_turn', 'cursor_turn_not_completed')
        handle.native.drain_buffered()
        text = handle.native.answer.strip()
        need(0 < len(text.encode()) <= MAX_ANSWER_BYTES, 'cursor_answer_missing_or_large')
        need(handle.secret not in text, 'cursor_credential_in_answer')
        review.check_settings(handle.settings)
        counts = dict(handle.native.counts)
        version = close(handle)
        return {'text': text, 'provider': 'cursor', 'model': MODEL, 'effort': 'not_exposed_in_native_catalog',
                'fast': False, 'mode': MODE, 'usage': {'native_event_counts': counts,
                                                       'actual_account_charge_verified': False},
                'preflight': handle.preflight, 'same_process_account_model_quota': False,
                'review_sha256': hashlib.sha256(text.encode()).hexdigest(),
                'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
                'native_session_ref_sha256': hashlib.sha256(handle.sid.encode()).hexdigest(),
                'prompt_sent_once_by_wrapper': True, 'automatic_retry': False,
                'tools_policy': 'ask_mode_deny_all_abort_on_observed_tool', 'full_coding_ready': False,
                'actual_charge': 'unverified', 'native_stopped': True, 'credential_writeback': 'committed',
                'credential_version_ref': hashlib.sha256(version.encode()).hexdigest()}
    except Exception:
        if not handle.finished and not handle.close_failed:
            close(handle)
        raise
