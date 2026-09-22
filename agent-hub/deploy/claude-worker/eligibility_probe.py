"""Explicit cloud-only commissioning of one fixed Fable 5.1/max prompt.

Not a worker task adapter and not a fabricated validated model plan. The entrypoint
must load protected operator/enrollment/billing JSON, independently map the owner
and injected credential, and use one uniquely journaled Cloud Run execution with
taskCount=parallelism=1 and maxRetries=0. The wrapper never retries. Native SDK
transport retry behavior is not established by this wrapper.
Raw control/results/stderr are discarded. API-equivalent usage cost is not an
account charge. This module performs inference ONLY when run_probe is called.

Flags/control limits: https://code.claude.com/docs/en/cli-reference (2026-09-21).
Maximum effort and fallback/billing caveats:
https://code.claude.com/docs/en/model-config (2026-09-21).

Installed Windows 2.1.275 help independently checked in a credential-free home:
SHA256 ae85d661e9c086f05637ebcd868f5702b477ff6e55e2e65b8ada7807cd51a4b6.
All flags are public help entries except --max-turns, whose actual installed
option registration includes .argParser(...).hideHelp(). Passing --help alone
does not validate unknown flags; source registration supplies that evidence.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import uuid

from agent_hub.claude_runtime import (
    _Frames, _EOF, _verify_account, _verify_settings, _CommandLifecycle,
    COMMAND_LIFECYCLE_STATES, ClaudeRuntimeError,
)
from agent_hub.subscription_auth import (
    CLAUDE_EXECUTABLE, CLAUDE_PROFILE, CLAUDE_WORKSPACE, _environment_matches,
    _local_configuration_clean, _profile_matches, _strict_json,
)

MODEL = 'claude-fable-5-1'
EFFORT = 'max'
VERSION = '2.1.275'
CLI_SHA256 = '13586f3150a7ca1655f36e1dba759fb404e0f7cf7021d4a3dcd5e6f604e56156'
PROMPT = 'Reply exactly READY.'
MAX_SECONDS = 180
MAX_RECEIPT_BYTES = 16384
MAX_READY_REPLY_BYTES = 128
_SHA = re.compile(r'[a-f0-9]{64}')
_CONSUMED = set()
_CONSUMED_LOCK = threading.Lock()
ARGV = (str(CLAUDE_EXECUTABLE), '-p', '--safe-mode', '--restricted', '--setting-sources', '',
        '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
        '--tools', '', '--disallowedTools', '*', '--permission-mode', 'dontAsk',
        '--permission-prompts', 'none', '--disable-slash-commands', '--no-chrome',
        '--max-turns', '1', '--model', MODEL, '--effort', EFFORT,
        '--output-format', 'stream-json', '--input-format', 'stream-json',
        '--verbose', '--no-session-persistence')

# Official SDK stdout/control schemas and streaming-message lifecycle. These
# references justify transport support, not a new inference or an outcome for
# an earlier interrupted invocation:
# https://github.com/anthropics/claude-agent-sdk-python/blob/main/src/claude_agent_sdk/_internal/query.py
# https://github.com/anthropics/claude-agent-sdk-python/blob/main/src/claude_agent_sdk/_internal/message_parser.py
# https://app.unpkg.com/@anthropic-ai/claude-agent-sdk@0.3.211/files/sdk.d.ts
# https://platform.claude.com/docs/en/build-with-claude/streaming
_FRAME_TYPES = ('system', 'assistant', 'user', 'result', 'rate_limit_event',
                'keep_alive', 'stream_event', 'control_response', 'control_request',
                'control_cancel_request', 'command_lifecycle')
_SYSTEM_TYPES = ('init', 'status', 'informational', 'notification', 'session_state_changed',
                 'turn_starting', 'turn_duration', 'thinking_tokens', 'api_retry', 'api_error',
                 'model_fallback', 'model_consent_fallback', 'model_refusal_fallback')
_STREAM_TYPES = ('message_start', 'content_block_start', 'content_block_delta',
                 'content_block_stop', 'message_delta', 'message_stop')
_DELTA_TYPES = ('text_delta', 'thinking_delta', 'signature_delta', 'input_json_delta')


class ProbeError(ValueError):
    """Bounded internal code only; no provider/configuration values."""


def _uuid(value):
    if not isinstance(value, str) or len(value) != 36:
        return False
    try:
        return str(uuid.UUID(value)) == value.lower()
    except ValueError:
        return False


def _keep_alive(value):
    if set(value) != {'type'} or value.get('type') != 'keep_alive':
        raise ProbeError('invalid_keep_alive_frame')


def _user_echo(value, sent_uuid, prompt=PROMPT):
    """Accept only the exact sent user turn, including its unguessable UUID.

    Claude can normalize text to a one-block content list and assign a session
    ID on its replay acknowledgement. Neither form counts as model acceptance.
    """
    allowed = {'type', 'message', 'parent_tool_use_id', 'session_id', 'uuid',
               'isReplay', 'isSynthetic', 'timestamp', 'origin'}
    message = value.get('message')
    if (set(value) - allowed or value.get('uuid') != sent_uuid
            or value.get('parent_tool_use_id') is not None
            or not isinstance(message, dict) or set(message) != {'role', 'content'}
            or message.get('role') != 'user'
            or message.get('content') not in (prompt, [{'type': 'text', 'text': prompt}])
            or value.get('isSynthetic', False) is not False
            or type(value.get('isReplay', False)) is not bool
            or value.get('session_id', '') != '' and not _uuid(value.get('session_id'))
            or 'origin' in value and value['origin'] != {'kind': 'human'}
            or 'timestamp' in value and (not isinstance(value['timestamp'], str) or len(value['timestamp']) > 64)):
        raise ProbeError('unexpected_user_echo_or_replay')


class _Protocol:
    """No arbitrary provider strings enter the receipt, even on failure."""
    def __init__(self, prompt=PROMPT):
        self.prompt = prompt
        self.diagnostics = {'frame_counts': {}, 'system_subtype_counts': {},
                            'stream_event_counts': {}, 'stream_delta_counts': {},
                            'command_lifecycle_counts': {}, 'unknown_marker': False}
        self.total = 0
        self.stream_started = self.stream_stopped = False
        self.block = None
        self.next_index = 0
        self.lifecycle = None

    def _count(self, group, value, allowed):
        key = value if isinstance(value, str) and value in allowed else 'unknown'
        if key == 'unknown':
            self.diagnostics['unknown_marker'] = True
        counts = self.diagnostics[group]
        counts[key] = min(4096, counts.get(key, 0) + 1)

    def observe(self, frame):
        self.total += 1
        if self.total > 4096:
            raise ProbeError('protocol_frame_count_exceeded')
        kind = frame.get('type')
        self._count('frame_counts', kind, _FRAME_TYPES)
        if kind == 'system':
            self._count('system_subtype_counts', frame.get('subtype'), _SYSTEM_TYPES)
        if kind == 'command_lifecycle':
            self._count('command_lifecycle_counts', frame.get('state'), COMMAND_LIFECYCLE_STATES)
        if kind == 'stream_event':
            event = frame.get('event')
            event = event if isinstance(event, dict) else {}
            self._count('stream_event_counts', event.get('type'), _STREAM_TYPES)
            if event.get('type') == 'content_block_delta':
                delta = event.get('delta')
                self._count('stream_delta_counts', delta.get('type') if isinstance(delta, dict) else None, _DELTA_TYPES)

    def stream(self, frame):
        """Validate optional partial text/thinking lifecycle; reject tool streams."""
        event = frame.get('event')
        if (not _uuid(frame.get('uuid')) or not _uuid(frame.get('session_id'))
                or frame.get('parent_tool_use_id') is not None or not isinstance(event, dict)):
            raise ProbeError('invalid_stream_envelope')
        kind = event.get('type')
        if kind == 'message_start':
            message = event.get('message')
            if (self.stream_started or not isinstance(message, dict)
                    or message.get('role') != 'assistant' or message.get('model') != MODEL
                    or message.get('content') != [] or message.get('stop_reason') is not None):
                raise ProbeError('unexpected_stream_message_start')
            self.stream_started = True
            return
        if not self.stream_started or self.stream_stopped:
            raise ProbeError('unexpected_stream_lifecycle')
        if kind == 'content_block_start':
            block = event.get('content_block')
            index = event.get('index')
            if (self.block is not None or type(index) is not int or index != self.next_index
                    or not 0 <= index < 128 or not isinstance(block, dict)
                    or block.get('type') not in ('text', 'thinking', 'redacted_thinking')):
                raise ProbeError('unexpected_stream_content_block')
            field = {'text': 'text', 'thinking': 'thinking', 'redacted_thinking': 'data'}[block['type']]
            if not isinstance(block.get(field), str):
                raise ProbeError('invalid_stream_content_block')
            self.block = (index, block['type'])
        elif kind == 'content_block_delta':
            delta = event.get('delta')
            if (self.block is None or type(event.get('index')) is not int
                    or event['index'] != self.block[0] or not isinstance(delta, dict)):
                raise ProbeError('unexpected_stream_delta')
            expected = {'text_delta': ('text', 'text'), 'thinking_delta': ('thinking', 'thinking'),
                        'signature_delta': ('thinking', 'signature')}.get(delta.get('type'))
            if expected is None or self.block[1] != expected[0] or not isinstance(delta.get(expected[1]), str):
                raise ProbeError('unexpected_stream_delta_capability')
        elif kind == 'content_block_stop':
            if self.block is None or type(event.get('index')) is not int or event['index'] != self.block[0]:
                raise ProbeError('unexpected_stream_block_stop')
            self.block = None
            self.next_index += 1
        elif kind == 'message_delta':
            delta = event.get('delta')
            if (self.block is not None or not isinstance(delta, dict)
                    or delta.get('stop_reason') not in (None, 'end_turn', 'max_tokens', 'stop_sequence', 'refusal')):
                raise ProbeError('unexpected_stream_message_delta')
        elif kind == 'message_stop':
            if self.block is not None:
                raise ProbeError('unexpected_stream_message_stop')
            self.stream_stopped = True
        else:
            raise ProbeError('unexpected_stream_event')

    def post_prompt(self, event, sent_uuid, *, result_seen=False):
        kind = event.get('type')
        if kind == 'control_request' or event.get('parent_tool_use_id') is not None:
            raise ProbeError('unexpected_tool_or_subagent_request')
        if self.lifecycle is None:
            self.lifecycle = _CommandLifecycle(sent_uuid)
        if kind == 'command_lifecycle':
            try:
                self.lifecycle.accept(event)
            except ClaudeRuntimeError as error:
                raise ProbeError(str(error)) from None
            return
        if kind == 'keep_alive':
            _keep_alive(event)
            return
        if kind == 'user':
            _user_echo(event, sent_uuid, self.prompt)
            return
        if kind == 'control_response':
            # initialize/get_settings were synchronously consumed. There are no
            # outstanding requests after the prompt; duplicates are not accepted.
            raise ProbeError('control_response_without_outstanding_request')
        if kind == 'system':
            subtype = event.get('subtype')
            # The official Python SDK treats new system subtypes as passive
            # SystemMessage metadata. Do the same: never execute their content,
            # authorize tools, change models, or use them as success evidence.
            # Native 2.1.275 emits turn_starting, absent from our original list.
            # Keep unknown labels/contents out of logs, while accepting bounded
            # protocol extensions. Capabilities and final model are checked below.
            if not isinstance(subtype, str) or not 1 <= len(subtype) <= 128:
                raise ProbeError('invalid_system_subtype')
            if subtype in ('model_fallback', 'model_consent_fallback', 'model_refusal_fallback'):
                raise ProbeError('unexpected_native_model_fallback')
            if subtype == 'init' and 'session_id' in event:
                try:
                    self.lifecycle.bind_session(event['session_id'])
                except ClaudeRuntimeError as error:
                    raise ProbeError(str(error)) from None
            if subtype == 'init' and (event.get('model') != MODEL or event.get('tools') != [] or event.get('mcp_servers') != []):
                raise ProbeError('unexpected_native_model_or_tools')
            return
        if kind == 'rate_limit_event':
            return
        if result_seen:
            raise ProbeError('unexpected_frame_after_result')
        if kind == 'stream_event':
            self.stream(event)
            return
        if kind == 'assistant':
            message = event.get('message')
            if not isinstance(message, dict) or message.get('model') != MODEL:
                raise ProbeError('unexpected_served_model')
            content = message.get('content')
            if not isinstance(content, list) or any(not isinstance(x, dict)
                    or x.get('type') not in ('text', 'thinking', 'redacted_thinking') for x in content):
                raise ProbeError('unexpected_assistant_capability')
            return
        if kind == 'result':
            if self.stream_started and not self.stream_stopped:
                raise ProbeError('incomplete_stream_before_result')
            return event
        raise ProbeError('unexpected_post_prompt_frame')


def receipt_digest(value):
    try:
        raw = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    except (ValueError, TypeError, RecursionError):
        raise ProbeError('invalid_receipt_json') from None
    if len(raw) > MAX_RECEIPT_BYTES:
        raise ProbeError('receipt_too_large')
    return hashlib.sha256(raw).hexdigest()


def _digest(value):
    return isinstance(value, str) and _SHA.fullmatch(value) is not None


def validate_inputs(operator, billing, enrollment, environment, *, now):
    """No file/credential reads. Environment is supplied by the operator hook.

    Receipt hashes use canonical JSON, not pretty-printed file bytes. Enrollment
    credential_sha256 binds the existing injected token without printing it.
    This is a deployment check, not entitlement evidence.
    """
    receipt_digest(operator)
    if not isinstance(operator, dict) or set(operator) != {
        'schema_version', 'enabled', 'invocation_id', 'expected_account_ref',
        'expected_cli_sha256', 'enrollment_sha256', 'billing_sha256', 'expires_at',
        'billing_max_age_seconds', 'included_first', 'existing_credits_authorized',
        'external_one_shot_guard',
    }:
        raise ProbeError('operator_contract_invalid')
    if (type(operator['schema_version']) is not int or operator['schema_version'] != 1
            or operator['enabled'] is not True or operator['included_first'] is not True
            or operator['existing_credits_authorized'] is not True
            or not isinstance(operator['invocation_id'], str)
            or re.fullmatch(r'[a-zA-Z0-9_-]{8,80}', operator['invocation_id']) is None
            or not _digest(operator['expected_account_ref']) or operator['expected_cli_sha256'] != CLI_SHA256
            or type(operator['expires_at']) is not int or not now < operator['expires_at'] <= now + 3600
            or type(operator['billing_max_age_seconds']) is not int
            or not 1 <= operator['billing_max_age_seconds'] <= 3600):
        raise ProbeError('operator_authorization_expired_or_invalid')
    guard = operator['external_one_shot_guard']
    if (not isinstance(guard, dict) or set(guard) != {'task_count', 'parallelism', 'max_retries',
            'prior_executions', 'intent_journaled', 'automatic_replay_disabled'}
            or any(type(guard.get(k)) is not int or guard[k] != v for k, v in
                   (('task_count', 1), ('parallelism', 1), ('max_retries', 0), ('prior_executions', 0)))
            or guard.get('intent_journaled') is not True or guard.get('automatic_replay_disabled') is not True):
        raise ProbeError('external_one_shot_execution_guard_required')
    if receipt_digest(enrollment) != operator['enrollment_sha256'] or receipt_digest(billing) != operator['billing_sha256']:
        raise ProbeError('receipt_binding_mismatch')
    if (not isinstance(enrollment, dict) or enrollment.get('schema_version') != 1
            or enrollment.get('account_ref') != operator['expected_account_ref']
            or enrollment.get('owner_verified') is not True
            or enrollment.get('auth_route') != 'first_party_subscription'
            or not _digest(enrollment.get('evidence_sha256'))
            or not _digest(enrollment.get('credential_sha256'))):
        raise ProbeError('independent_enrollment_required')
    if not _environment_matches(environment):
        raise ProbeError('dedicated_environment_required')
    token_digest = hashlib.sha256(environment['CLAUDE_CODE_OAUTH_TOKEN'].encode()).hexdigest()
    if token_digest != enrollment['credential_sha256']:
        raise ProbeError('enrolled_credential_mismatch')
    if (not isinstance(billing, dict) or billing.get('schema_version') != 1
            or billing.get('account_ref') != operator['expected_account_ref']
            or billing.get('kind') != 'provider_browser_billing_controls'
            or billing.get('plan') not in ('pro', 'max', 'max20x')
            or not _digest(billing.get('evidence_sha256'))
            or type(billing.get('observed_at')) is not int
            or not 0 <= now - billing['observed_at'] < operator['billing_max_age_seconds']
            or type(billing.get('expires_at')) is not int or now >= billing['expires_at']
            or billing.get('provider_cap_enforced') is not True
            or billing.get('currency') != 'USD'
            or any(billing.get(k) is not False for k in
                ('auto_reload_enabled', 'automatic_purchase_enabled', 'api_fallback_enabled'))
            or any(type(billing.get(k)) is not int or not 0 < billing[k] <= 1000000000000
                   for k in ('existing_balance_microusd', 'remaining_spend_cap_microusd'))):
        raise ProbeError('fresh_existing_credit_controls_required')


def _local_check():
    if sys.platform != 'linux' or not hasattr(os, 'geteuid') or os.geteuid() == 0:
        raise ProbeError('cloud_rootless_linux_required')
    if not _profile_matches():
        raise ProbeError('verified_native_profile_required')
    profile = _strict_json(CLAUDE_PROFILE.read_bytes())
    if profile != {'schema': 1, 'cli_version': VERSION, 'sha256': CLI_SHA256}:
        raise ProbeError('pinned_native_release_required')
    if not _local_configuration_clean(CLAUDE_WORKSPACE):
        raise ProbeError('clean_native_home_required')
    # Fixed prompt must not accidentally include project instructions or data.
    if any(CLAUDE_WORKSPACE.iterdir()):
        raise ProbeError('empty_probe_workspace_required')


def _terminate(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=1)


def _safe_usage(result):
    """Whitelist bounded numeric counters, never text, IDs, prompts or emails."""
    output = {}
    for source_name, fields in (
        ('usage', ('input_tokens', 'output_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')),
        ('model', ('inputTokens', 'outputTokens', 'cacheCreationInputTokens', 'cacheReadInputTokens')),
    ):
        used = result.get('modelUsage')
        source = result.get('usage', {}) if source_name == 'usage' else used.get(MODEL, {}) if isinstance(used, dict) else {}
        if not isinstance(source, dict):
            continue
        values = {k: source[k] for k in fields if type(source.get(k)) is int and 0 <= source[k] <= 100000000}
        if values:
            output[source_name] = values
    cost = result.get('total_cost_usd')
    if type(cost) in (int, float) and math.isfinite(cost) and 0 <= cost <= 1000000:
        output['api_equivalent_cost_usd'] = cost
    output['actual_account_charge_verified'] = False
    return output


def run_probe(operator, billing, enrollment, *, environment, timeout_seconds=180):
    return _run_native(operator, billing, enrollment, environment=environment,
                       timeout_seconds=timeout_seconds)


def run_review(operator, billing, enrollment, *, environment, prompt, heartbeat, timeout_seconds=180):
    """Trusted room runner only: same fixed native/model/tool profile, real output.

    The room runner validates the protected room/prompt binding before calling.
    This is a bounded commissioning exception, not a fabricated general model
    catalog or a downgrade of the production worker's model-policy requirement.
    """
    if (not isinstance(prompt, str) or not prompt.strip()
            or len(prompt.encode('utf-8')) > 12_000 or not callable(heartbeat)):
        raise ProbeError('review_request_invalid')
    report = _run_native(operator, billing, enrollment, environment=environment,
                         timeout_seconds=timeout_seconds, review_prompt=prompt, heartbeat=heartbeat)
    # The caller delivers only a verified native result to its claimed room;
    # output is not included in the log-safe commissioning receipt.
    output = report.pop('_review_output', None)
    return report, output


def _run_native(operator, billing, enrollment, *, environment, timeout_seconds=180,
                review_prompt=None, heartbeat=None):
    """Execute at most one process and one fixed prompt, only in the cloud image.

    Operator journals a unique job/invocation, verifies it has zero executions,
    sets taskCount=parallelism=1/maxRetries=0 and submits once. Uncertain submits
    are reconciled, never replayed automatically. The module checks that explicit
    contract and blocks repeats in this Python process; it does not claim a
    cross-process/cross-operator atomic exactly-once guarantee.
    """
    if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= MAX_SECONDS:
        raise ProbeError('probe_deadline_invalid')
    started = time.monotonic()
    deadline = started + max(0, timeout_seconds - min(5, timeout_seconds / 4))
    validate_inputs(operator, billing, enrollment, environment, now=time.time())
    _local_check()
    with _CONSUMED_LOCK:
        if operator['invocation_id'] in _CONSUMED:
            raise ProbeError('one_shot_already_consumed_in_process')
        _CONSUMED.add(operator['invocation_id'])
    process, frames, stderr_thread = None, None, None
    writers = []
    prompt = PROMPT if review_prompt is None else review_prompt
    protocol = _Protocol(prompt)
    next_heartbeat = 0.0
    stderr_excessive = threading.Event()
    selection = {'account_ref': operator['expected_account_ref'], 'cli_model_id': MODEL, 'effort': EFFORT}
    report = {'schema_version': 1, 'probe': 'claude-fable-5-1-max-eligibility',
              'invocation_id': operator['invocation_id'], 'cli_version': VERSION,
              'prompt_sent': False, 'server_accepted': None, 'actual_model': None,
              'applied_effort': None, 'first_party_route': False,
              'owner_binding_verified': True, 'native_owner_reported': False,
              'complete_account_catalog': False, 'actual_billing_route': 'unknown',
              'actual_account_charge_verified': False, 'wrapper_retry_count': 0,
              'native_transport_retries_verified_disabled': False,
              'one_shot_guard': 'external_operator_journal_and_cloud_job_no_retries',
              'protocol_diagnostics': protocol.diagnostics,
              'stage': 'local_preflight', 'timing_ms': {}}

    def mark(stage):
        report['stage'] = stage
        report['timing_ms'][stage] = max(0, round((time.monotonic() - started) * 1000))

    def tick():
        nonlocal next_heartbeat
        if heartbeat is not None and time.monotonic() >= next_heartbeat:
            try:
                if heartbeat() is not True:
                    raise ProbeError('review_lease_lost_outcome_may_be_uncertain')
            except Exception:
                raise ProbeError('review_lease_lost_outcome_may_be_uncertain') from None
            next_heartbeat = time.monotonic() + 8
        if time.monotonic() >= deadline:
            raise ProbeError('probe_timeout_outcome_may_be_uncertain')
        if stderr_excessive.is_set() or frames is not None and frames.failed.is_set():
            raise ProbeError('malformed_or_excessive_native_output')

    def send(value, *, close=False):
        raw = (json.dumps(value, separators=(',', ':')) + '\n').encode()
        done, failed = threading.Event(), threading.Event()
        def write():
            try:
                process.stdin.write(raw)
                process.stdin.flush()
                if close:
                    process.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                failed.set()
            finally:
                done.set()
        writer = threading.Thread(target=write, daemon=True)
        writers.append(writer)
        writer.start()
        while not done.wait(0.02):
            tick()
        tick()
        if failed.is_set():
            raise ProbeError('native_input_failed_outcome_may_be_uncertain')

    def receive(*, allow_eof=False):
        while True:
            tick()
            try:
                value = frames.queue.get(timeout=0.05)
            except queue.Empty:
                continue
            if value is _EOF:
                if allow_eof:
                    return _EOF
                raise ProbeError('native_exited_without_complete_result')
            protocol.observe(value)
            return value

    def control(subtype):
        request_id = str(uuid.uuid4())
        send({'type': 'control_request', 'request_id': request_id, 'request': {'subtype': subtype}})
        value = receive()
        while value.get('type') == 'keep_alive':
            _keep_alive(value)
            value = receive()
        response = value.get('response')
        if (value.get('type') != 'control_response' or not isinstance(response, dict)
                or response.get('request_id') != request_id or response.get('subtype') != 'success'
                or not isinstance(response.get('response'), dict)):
            raise ProbeError('unexpected_pre_prompt_control_frame')
        return response['response']

    def discard_stderr(stream):
        total = 0
        try:
            while chunk := stream.read(4096):
                total += len(chunk)
                if total > 2000000:
                    stderr_excessive.set()
                    break
        finally:
            stream.close()

    try:
        tick()
        mark('native_start')
        process = subprocess.Popen(ARGV, cwd=CLAUDE_WORKSPACE, env=dict(environment), shell=False,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        frames = _Frames(process.stdout)
        frames.thread.start()
        stderr_thread = threading.Thread(target=discard_stderr, args=(process.stderr,), daemon=True)
        stderr_thread.start()
        mark('account_check')
        account_body = control('initialize')
        try:
            _verify_account(account_body, selection)
            _verify_settings(control('get_settings'), selection)
        except ValueError:
            raise ProbeError('native_account_or_settings_mismatch') from None
        report['first_party_route'] = True
        report['applied_effort'] = EFFORT
        report['native_owner_reported'] = account_body['account'].get('email') is not None
        # Configuration acceptance alone never becomes entitlement evidence.
        validate_inputs(operator, billing, enrollment, environment, now=time.time())
        next_heartbeat = 0.0
        tick()
        mark('prompt_dispatch')
        report['prompt_sent'] = True  # Set BEFORE the write; an interrupted write is uncertain.
        sent_uuid = str(uuid.uuid4())
        send({'type': 'user', 'message': {'role': 'user', 'content': prompt},
              'parent_tool_use_id': None, 'session_id': '', 'uuid': sent_uuid}, close=True)
        result = None
        while result is None:
            event = receive()
            if event.get('type') in ('assistant', 'stream_event') and 'first_response' not in report['timing_ms']:
                mark('first_response')
            result = protocol.post_prompt(event, sent_uuid)
        mark('result_received')
        report['usage'] = _safe_usage(result)
        model_usage = result.get('modelUsage')
        exact_model = (isinstance(model_usage, dict) and set(model_usage) == {MODEL}
                       and isinstance(model_usage[MODEL], dict))
        if result.get('is_error') is False and result.get('subtype') == 'success' and exact_model:
            # Server acceptance and obeying the probe's exact response contract
            # are separate facts. A non-READY answer can still incur usage.
            report['actual_model'], report['server_accepted'] = MODEL, True
        reply = result.get('result')
        ready = (isinstance(reply, str) and len(reply.encode('utf-8')) <= MAX_READY_REPLY_BYTES
                 and reply.strip() == 'READY') if review_prompt is None else (
                     isinstance(reply, str) and bool(reply.strip())
                     and len(reply.encode('utf-8')) <= 8_000 and len(reply.split()) <= 450)
        if (result.get('is_error') is not False or result.get('subtype') != 'success'
                or type(result.get('num_turns')) is not int or result['num_turns'] != 1
                or not ready):
            raise ProbeError('one_turn_ready_result_required' if review_prompt is None else 'bounded_one_turn_review_required')
        if not exact_model:
            raise ProbeError('exact_provider_model_usage_required')
        # Drain the remaining stream; a result followed by a tool/control/second
        # answer cannot silently become a successful no-tools commissioning run.
        while True:
            trailing = receive(allow_eof=True)
            if trailing is _EOF:
                break
            protocol.post_prompt(trailing, sent_uuid, result_seen=True)
        try:
            protocol.lifecycle.finish()
        except ClaudeRuntimeError as error:
            raise ProbeError(str(error)) from None
        while process.poll() is None:
            tick()
            time.sleep(0.02)
        frames.thread.join(timeout=0.1)
        tick()
        if process.returncode != 0:
            raise ProbeError('native_nonzero_exit')
        mark('verified_result')
        report['outcome'] = 'verified_eligibility' if review_prompt is None else 'verified_review'
        if review_prompt is not None:
            report['_review_output'] = reply
            report['output_sha256'] = hashlib.sha256(reply.encode('utf-8')).hexdigest()
            report['output_words'] = len(reply.split())
    except ProbeError as error:
        report.update({'outcome': 'failed', 'error_code': str(error)})
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        report.update({'outcome': 'failed', 'error_code': 'native_probe_failed'})
    finally:
        if process is not None:
            _terminate(process)
            if process.stdin is not None and not process.stdin.closed:
                try:
                    process.stdin.close()
                except (OSError, ValueError):
                    pass
            if frames is None and process.stdout is not None:
                process.stdout.close()
            if stderr_thread is None and process.stderr is not None:
                process.stderr.close()
        if frames is not None:
            frames.thread.join(timeout=0.5)
        if stderr_thread is not None:
            stderr_thread.join(timeout=0.5)
        for writer in writers:
            writer.join(timeout=0.1)
    report['elapsed_ms'] = max(0, round((time.monotonic() - started) * 1000))
    report['completed_at'] = int(time.time())
    if not report['prompt_sent']:
        report['server_accepted'] = False
    return report
