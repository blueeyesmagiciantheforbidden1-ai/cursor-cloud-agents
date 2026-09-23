"""Pinned native Cursor ACP commissioning: one bounded, read-only review.

No network or native process at import. ACP ask mode is not a tools-empty
transport: read/write/shell denies and permission rejection are defense in depth;
the caller must explicitly authorize that documented limitation. Any observed
tool event aborts; no provider retry or second prompt is allowed.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time

from metadata import (MetadataError, NativeProcess, account_metadata,
                      initialize_params, metadata_environment, parse_models)

MODEL = 'composer-2.5'
DENY = ['Read(**)', 'Write(**)', 'Shell(*)', 'Bash(*)', 'Mcp(*)']
MAX_WORDS, MAX_BYTES = 400, 8000


class ReviewError(ValueError):
    """Only fixed error enums, never raw provider text."""


def require(ok, code):
    if not ok:
        raise ReviewError(code)


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'duplicate_json_key')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ReviewError('nonfinite_json')))


def config_options(value, *, selected=False):
    options = value.get('configOptions') if isinstance(value, dict) else None
    require(isinstance(options, list) and 1 <= len(options) <= 64, 'session_config_missing')
    found = {}
    for option in options:
        require(isinstance(option, dict) and option.get('type') == 'select', 'session_config_schema')
        key = option.get('id')
        require(isinstance(key, str) and key not in found and len(key) <= 128, 'session_config_duplicate')
        choices = option.get('options')
        require(isinstance(choices, list) and 1 <= len(choices) <= 1000, 'session_config_choices')
        values = [x.get('value') if isinstance(x, dict) else None for x in choices]
        require(all(isinstance(x, str) and 1 <= len(x) <= 128 for x in values)
                and len(values) == len(set(values)) and option.get('currentValue') in values,
                'session_config_current_invalid')
        found[key] = option
    require('mode' in found and 'model' in found, 'session_mode_model_missing')
    if selected:
        require(set(found) == {'mode', 'model', 'fast'}, 'unreviewed_model_parameter')
        require(found['mode']['category'] == 'mode' and found['mode']['currentValue'] == 'ask'
                and found['model']['category'] == 'model' and found['model']['currentValue'] == MODEL
                and found['fast']['category'] == 'model_config'
                and found['fast']['currentValue'] == 'false', 'effective_model_or_mode_mismatch')
    return found


def check_catalog(value):
    matches = [m for m in parse_models(value) if m['model_id'] == MODEL]
    require(len(matches) == 1, 'selected_native_model_unavailable')
    params = matches[0]['parameters']
    require(len(params) == 1 and params[0]['id'] == 'fast'
            and params[0]['category'] == 'model_config'
            and set(params[0]['available_values']) == {'false', 'true'}, 'native_model_parameters_changed')
    return {'model': MODEL, 'fast': False, 'effort': 'not_exposed_in_native_catalog'}


def lock_settings(path):
    require(path.is_file() and not path.is_symlink() and path.stat().st_size <= 1048576,
            'fresh_generated_settings_missing')
    value = strict_json(path.read_bytes())
    require(isinstance(value, dict), 'fresh_generated_settings_invalid')
    value.update(permissions={'allow': [], 'deny': DENY}, approvalMode='allowlist',
                 autoAcceptWebSearch=False, webFetchDomainAllowlist=[],
                  exploreSubagentModel='default', subagentModels={'explore': 'disabled'})
    if 'bedrock' in value:
        require(isinstance(value['bedrock'], dict) and value['bedrock'].get('enabled') is False,
                'provider_override_detected')
    path.write_text(json.dumps(value), encoding='utf-8')
    path.chmod(0o600)


def check_settings(path):
    require(path.is_file() and not path.is_symlink() and path.stat().st_size <= 1048576,
            'review_settings_missing')
    value = strict_json(path.read_bytes())
    require(value.get('permissions') == {'allow': [], 'deny': DENY}
            and value.get('approvalMode') == 'allowlist'
            and value.get('autoAcceptWebSearch') is False
            and value.get('webFetchDomainAllowlist') == []
            and value.get('exploreSubagentModel') == 'default'
            and value.get('subagentModels') == {'explore': 'disabled'}, 'review_permissions_changed')
    if 'bedrock' in value:
        require(value['bedrock'].get('enabled') is False, 'provider_override_detected')


class ReviewProcess(NativeProcess):
    """Same ACP process from initialization through selected settings and answer."""
    def __init__(self, environment, workspace, deadline, heartbeat):
        super().__init__(('acp',), environment, workspace, deadline)
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
            require(self.heartbeat(), 'review_lease_lost')
            self.next_heartbeat = time.monotonic() + 8
        super().pump()

    def request(self, method, params):
        sequence = ['initialize', 'cursor/list_available_models', 'session/new',
                    'session/set_config_option', 'session/set_config_option',
                    'session/set_config_option', 'session/prompt']
        require(self.stage < len(sequence) and method == sequence[self.stage], 'review_rpc_sequence')
        if self.stage == 0:
            require(params == initialize_params(), 'review_initialize_mismatch')
        elif self.stage == 1:
            require(params == {}, 'review_catalog_parameters')
        elif self.stage == 2:
            require(set(params) == {'cwd', 'mcpServers'} and params['mcpServers'] == [],
                    'review_session_parameters')
        elif self.stage in (3, 4, 5):
            key, value = [('model', MODEL), ('fast', 'false'), ('mode', 'ask')][self.stage - 3]
            require(params == {'sessionId': self.session_id, 'configId': key, 'value': value},
                    'review_selection_parameters')
        else:
            require(self.effective_selected and not self.prompt_sent and params.get('sessionId') == self.session_id
                    and set(params) == {'sessionId', 'prompt'} and isinstance(params['prompt'], list)
                    and len(params['prompt']) == 1 and set(params['prompt'][0]) == {'type', 'text'}
                    and params['prompt'][0]['type'] == 'text'
                    and isinstance(params['prompt'][0]['text'], str)
                    and len(params['prompt'][0]['text'].encode()) <= 12000,
                    'review_prompt_not_authorized')
            require(self.heartbeat(), 'review_lease_lost_before_prompt')
            self.prompt_sent = True  # a write error is still an uncertain attempt
        self.stage += 1
        request_id = self.stage
        self.process.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': request_id,
            'method': method, 'params': params}, separators=(',', ':')).encode() + b'\n')
        self.process.stdin.flush()
        while True:
            while b'\n' not in self.buffer:
                require(bool(self.selector.get_map()), 'review_rpc_closed')
                self.pump()
            line, _, rest = self.buffer.partition(b'\n')
            self.buffer = bytearray(rest)
            value = strict_json(line)
            require(isinstance(value, dict) and value.get('jsonrpc') == '2.0', 'review_rpc_envelope')
            if 'method' in value:
                self.notification(value)
                continue
            require(value.get('id') == request_id and 'error' not in value
                    and set(value) == {'jsonrpc', 'id', 'result'}, 'review_rpc_response')
            result = value['result']
            require(isinstance(result, dict), 'review_rpc_result')
            if method == 'session/new':
                sid = result.get('sessionId')
                require(isinstance(sid, str) and re.fullmatch(r'[a-f0-9-]{36}', sid), 'review_session_identity')
                self.session_id = sid
                config_options(result)
                for pending in getattr(self, 'pending_session_updates', []):
                    self.notification(pending)
                self.pending_session_updates = []
            if self.stage == 6:
                config_options(result, selected=True)
                self.effective_selected = True
            return result

    def notification(self, value):
        if value.get('method') == 'session/request_permission' and 'id' in value:
            self.process.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': value['id'],
                'result': {'outcome': {'outcome': 'selected', 'optionId': 'reject-once'}}}).encode() + b'\n')
            self.process.stdin.flush()
            raise ReviewError('tool_permission_rejected')
        require(value.get('method') == 'session/update' and 'id' not in value,
                'unsupported_native_request_or_notification')
        params = value.get('params')
        require(isinstance(params, dict), 'native_session_update_mismatch')
        if self.session_id is None:
            # Native schedules available-command metadata during session/new.
            # Buffer only passive known metadata, then bind every frame to the
            # authoritative sessionId returned by that exact outstanding call.
            update = params.get('update')
            require(self.stage == 3 and isinstance(params.get('sessionId'), str)
                    and re.fullmatch(r'[a-f0-9-]{36}', params['sessionId'])
                    and isinstance(update, dict) and update.get('sessionUpdate') in
                    ('available_commands_update', 'current_mode_update', 'session_info_update', 'config_option_update'),
                    'unsafe_update_before_session_binding')
            pending = getattr(self, 'pending_session_updates', [])
            require(len(pending) < 32, 'session_metadata_buffer_excessive')
            pending.append(value);self.pending_session_updates = pending
            return
        require(params.get('sessionId') == self.session_id, 'native_session_update_mismatch')
        update = params.get('update')
        require(isinstance(update, dict), 'native_update_schema')
        kind = update.get('sessionUpdate')
        known = ('agent_message_chunk', 'agent_thought_chunk', 'available_commands_update',
                 'current_mode_update', 'session_info_update', 'config_option_update', 'usage_update')
        if kind not in known:
            self.counts['unknown_or_tool'] = self.counts.get('unknown_or_tool', 0) + 1
            raise ReviewError('tool_or_unknown_native_update')
        self.counts[kind] = self.counts.get(kind, 0) + 1
        if kind in ('agent_message_chunk', 'agent_thought_chunk'):
            require(self.prompt_sent, 'native_content_before_prompt')
            content = update.get('content')
            require(isinstance(content, dict) and content.get('type') == 'text'
                    and isinstance(content.get('text'), str), 'native_content_schema')
            if kind == 'agent_message_chunk':
                self.answer += content['text']
                require(len(self.answer.encode()) <= MAX_BYTES, 'native_answer_excessive')
        elif kind == 'current_mode_update':
            permitted = ('ask',) if self.effective_selected or self.prompt_sent else ('ask', 'agent', 'plan')
            require(update.get('currentModeId') in permitted, 'native_mode_changed')
        elif kind == 'config_option_update':
            config_options(update, selected=self.effective_selected)

    def drain_buffered(self):
        """Validate received trailing frames without awaiting persistent ACP EOF."""
        while b'\n' in self.buffer:
            line, _, rest = self.buffer.partition(b'\n');self.buffer = bytearray(rest)
            value = strict_json(line)
            require(isinstance(value, dict) and value.get('jsonrpc') == '2.0'
                    and 'method' in value, 'unexpected_trailing_rpc_response')
            update = value.get('params', {}).get('update', {})
            require(update.get('sessionUpdate') not in ('agent_message_chunk', 'agent_thought_chunk'),
                    'unexpected_content_after_turn_result')
            self.notification(value)
        require(not self.buffer, 'incomplete_trailing_native_frame')


def run_native_review(api_key, expected_account_ref, prompt, *, heartbeat,
                      timeout_seconds, before_prompt=lambda: None, parent=Path('/home/worker'),
                      metadata_factory=None, review_factory=ReviewProcess):
    require(isinstance(prompt, str) and bool(prompt.strip()) and not prompt.lstrip().startswith('/')
            and len(prompt.encode()) <= 12000, 'review_prompt_invalid')
    require(type(timeout_seconds) in (int, float) and 10 <= timeout_seconds <= 180,
            'review_deadline_invalid')
    deadline = time.monotonic() + timeout_seconds
    started = time.monotonic()
    report = {'provider': 'cursor', 'prompt_sent': False, 'outcome': 'failed',
              'automatic_retry_count': 0, 'model': MODEL, 'fast': False,
              'effort': 'not_exposed_in_native_catalog', 'transport': 'native_acp',
              'tool_policy': 'ask_deny_local_reject_permissions_abort_observed_tools',
              'tools_empty_guaranteed': False, 'native_session_autonaming_possible': True,
              'stage': 'preflight', 'timing_ms': {}}
    def mark(stage):
        report['stage'] = stage
        report['timing_ms'][stage] = max(0, round((time.monotonic() - started) * 1000))
    native = None
    class HeartbeatMetadata(NativeProcess):
        def pump(self):
            if time.monotonic() >= getattr(self, 'next_heartbeat', 0):
                require(heartbeat(), 'review_lease_lost_during_metadata')
                self.next_heartbeat = time.monotonic() + 8
            return super().pump()
    metadata_factory = metadata_factory or HeartbeatMetadata
    try:
        with tempfile.TemporaryDirectory(prefix='cursor-review-', dir=parent) as temp:
            home = Path(temp)
            home.chmod(0o700)
            for rel in ('.cursor', 'workspace', 'tmp'):
                (home / rel).mkdir(mode=0o700)
            environment = metadata_environment(api_key, home)
            workspace = home / 'workspace'
            require(heartbeat(), 'review_lease_lost_before_metadata')
            mark('native_auth_exchange')
            with metadata_factory(('models',), environment, workspace, deadline) as proc:
                proc.completed_output()
            require(heartbeat(), 'review_lease_lost_after_catalog_auth')
            mark('account_check')
            with metadata_factory(('status', '--format', 'json'), environment, workspace, deadline) as proc:
                account_metadata(strict_json(proc.completed_output()), expected_account_ref)
            report['owner_verified'] = True
            settings = home / '.cursor/cli-config.json'
            lock_settings(settings)
            mark('native_acp_start')
            with review_factory(environment, workspace, deadline, heartbeat) as native:
                result = native.request('initialize', initialize_params())
                require(result.get('protocolVersion') == 1, 'native_protocol_unsupported')
                check_catalog(native.request('cursor/list_available_models', {}))
                mark('session_configuration')
                native.request('session/new', {'cwd': str(workspace), 'mcpServers': []})
                for key, value in [('model', MODEL), ('fast', 'false'), ('mode', 'ask')]:
                    native.request('session/set_config_option', {'sessionId': native.session_id,
                                   'configId': key, 'value': value})
                check_settings(settings)
                before_prompt()
                mark('prompt_dispatch')
                result = native.request('session/prompt', {'sessionId': native.session_id,
                    'prompt': [{'type': 'text', 'text': prompt}]})
                require(result.get('stopReason') == 'end_turn', 'native_turn_not_completed')
                mark('result_received')
                native.drain_buffered()
                answer = native.answer.strip()
                require(bool(answer) and len(answer.split()) <= MAX_WORDS
                        and len(answer.encode()) <= MAX_BYTES, 'native_answer_bound_or_empty')
                require(api_key not in answer, 'credential_in_answer')
                check_settings(settings)
                report.update(outcome='verified_review', output_sha256=hashlib.sha256(answer.encode()).hexdigest(),
                              output_bytes=len(answer.encode()), output_words=len(answer.split()))
                mark('verified_result')
                return report, answer
    except (ReviewError, MetadataError) as error:
        report['error_code'] = str(error)
    except Exception:
        report['error_code'] = 'native_failure_outcome_uncertain'
    finally:
        report['elapsed_ms'] = max(0, round((time.monotonic() - started) * 1000))
        if native is not None:
            report['prompt_sent'] = native.prompt_sent
            report['event_counts'] = dict(native.counts)
    return report, None
