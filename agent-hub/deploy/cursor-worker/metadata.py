"""Bounded native Cursor metadata collection. This module never creates a session.

The pinned public CLI source establishes this sequence: `models` exchanges an
API key into isolated browser-style credentials, `status` calls getMe, then ACP
lists parameterized models. Only native metadata commands are permitted here.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import tempfile
import time
from datetime import datetime, timezone

EXECUTABLE = '/opt/runcrew/cursor/cursor-agent'
COMMANDS = (('models',), ('status', '--format', 'json'), ('acp',))
METHODS = ('initialize', 'cursor/list_available_models')
OUTPUT_LIMIT = 4 * 1024 * 1024
LINE_LIMIT = 1024 * 1024
IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:+/-]{0,127}')
VALUE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:+/-]{0,63}')


class MetadataError(ValueError):
    """Only the fixed code, never provider output, may be logged."""


def identifier(value, pattern=IDENTIFIER):
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise MetadataError('invalid_catalog_identifier')
    return value


def metadata_environment(api_key, home):
    if (not isinstance(api_key, str) or not 1 <= len(api_key) <= 16384
            or any(c.isspace() or ord(c) < 32 for c in api_key)):
        raise MetadataError('provider_key_missing_or_malformed')
    # No inherited NODE_OPTIONS, provider overrides, proxy, hooks, plugin, or
    # credential-store variables. Native public code supports this file store.
    return {'HOME': str(home), 'CURSOR_CONFIG_DIR': str(home / '.cursor'),
            'CURSOR_DATA_DIR': str(home / '.cursor'),
            'AGENT_CLI_CREDENTIAL_STORE': 'file', 'CURSOR_API_KEY': api_key,
            'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8',
            'TMPDIR': str(home / 'tmp'), 'NO_COLOR': '1', 'TERM': 'dumb'}


def account_metadata(value, expected_account_ref):
    if not isinstance(value, dict) or value.get('status') != 'authenticated' or value.get('isAuthenticated') is not True:
        raise MetadataError('native_account_not_authenticated')
    info = value.get('userInfo')
    email = info.get('email') if isinstance(info, dict) else None
    if not isinstance(email, str) or len(email) > 320 or not re.fullmatch(r'[^\s@]+@[^\s@]+', email):
        # Native status can say authenticated after getMe fails. That is NOT
        # fresh identity evidence and cannot satisfy the owner check.
        raise MetadataError('fresh_native_account_identity_unavailable')
    reference = hashlib.sha256(email.strip().lower().encode()).hexdigest()
    if reference != expected_account_ref:
        raise MetadataError('native_account_owner_mismatch')
    return {'verified': True, 'account_ref': reference, 'source': 'native_status_fresh_getMe'}


def parse_models(value):
    models = value.get('models') if isinstance(value, dict) else None
    if not isinstance(models, list) or not 1 <= len(models) <= 1000:
        # The native extension converts fetch failures into an empty list.
        raise MetadataError('parameterized_catalog_empty_or_unavailable')
    result, model_ids = [], set()
    for model in models:
        if not isinstance(model, dict):
            raise MetadataError('invalid_catalog_model')
        model_id = identifier(model.get('value'))
        if model_id in model_ids:
            raise MetadataError('duplicate_catalog_model')
        model_ids.add(model_id)
        options = model.get('configOptions')
        if not isinstance(options, list) or len(options) > 64:
            raise MetadataError('parameter_metadata_missing')
        parameters, ids, effort_ids = [], set(), []
        for option in options:
            if not isinstance(option, dict) or option.get('type') != 'select':
                raise MetadataError('unsupported_parameter_schema')
            param_id = identifier(option.get('id'))
            category = option.get('category')
            if param_id in ids or category not in ('model_config', 'thought_level'):
                raise MetadataError('invalid_parameter_category_or_duplicate')
            ids.add(param_id)
            choices = option.get('options')
            if not isinstance(choices, list) or not 2 <= len(choices) <= 64:
                raise MetadataError('invalid_parameter_choices')
            values = [identifier(c.get('value'), VALUE) if isinstance(c, dict) else identifier(None) for c in choices]
            current = identifier(option.get('currentValue'), VALUE)
            if len(values) != len(set(values)) or current not in values:
                raise MetadataError('invalid_parameter_default')
            parameters.append({'id': param_id, 'category': category,
                               'default_value': current, 'available_values': values})
            if category == 'thought_level':
                effort_ids.append(param_id)
        result.append({'model_id': model_id, 'parameters': parameters,
                       'effort_status': 'configurable' if effort_ids else 'not_exposed_in_cli_catalog',
                       'effort_parameter_ids': effort_ids, 'maximum_effort': None,
                       'maximum_model_verified': False})
    return result


def settings_metadata(path):
    """Read only generated non-credential config; return a strict small projection."""
    if not path.exists():
        return {'status': 'unavailable'}
    if path.is_symlink() or not path.is_file() or path.stat().st_size > LINE_LIMIT:
        raise MetadataError('invalid_generated_settings')
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise MetadataError('invalid_generated_settings')
    result = {'status': 'observed', 'scope': 'fresh_isolated_cli_config',
              'account_billing_settings': 'unavailable', 'managed_effective_settings': 'unavailable'}
    for key in ('maxMode', 'maxModeAutoEnabled'):
        if key in value:
            if type(value[key]) is not bool:
                raise MetadataError('invalid_generated_settings')
            result[key] = value[key]
    if 'approvalMode' in value:
        if value['approvalMode'] not in ('allowlist', 'unrestricted', 'auto-review'):
            raise MetadataError('invalid_generated_settings')
        result['approvalMode'] = value['approvalMode']
    if isinstance(value.get('bedrock'), dict):
        result['bedrock_enabled'] = value['bedrock'].get('enabled')
        if result['bedrock_enabled'] is not False:
            raise MetadataError('unexpected_provider_override')
    if isinstance(value.get('selectedModel'), dict):
        selected = value['selectedModel']
        params = selected.get('parameters', [])
        if not isinstance(params, list) or len(params) > 64:
            raise MetadataError('invalid_generated_settings')
        result['selected_model'] = {'model_id': identifier(selected.get('modelId')),
                                   'parameters': [{'id': identifier(p.get('id')), 'value': identifier(p.get('value'), VALUE)}
                                                  for p in params if isinstance(p, dict)]}
    return result


def initialize_params():
    return {'protocolVersion': 1,
            'clientInfo': {'name': 'runcrew-metadata', 'version': '1'},
            'clientCapabilities': {'fs': {'readTextFile': False, 'writeTextFile': False},
                                   'terminal': False, '_meta': {'parameterizedModelPicker': True}}}


class NativeProcess:
    """Linux process group, aggregate output/deadline bounds, no shell or retries."""
    def __init__(self, command, environment, workspace, deadline):
        if command not in COMMANDS:
            raise MetadataError('metadata_command_not_allowed')
        self.process = subprocess.Popen([EXECUTABLE, *command], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        cwd=workspace, env=environment, shell=False,
                                        start_new_session=True, bufsize=0)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ, 'stdout')
        self.selector.register(self.process.stderr, selectors.EVENT_READ, 'stderr')
        self.deadline, self.total, self.buffer, self.request_index = deadline, 0, bytearray(), 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        # Kill the entire dedicated group, including metadata helper descendants.
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.process.wait(timeout=5)
        self.selector.close()
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            stream.close()

    def pump(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise MetadataError('native_metadata_deadline')
        for key, _ in self.selector.select(min(remaining, 0.2)):
            chunk = os.read(key.fd, 65536)
            if not chunk:
                self.selector.unregister(key.fileobj)
                continue
            self.total += len(chunk)
            if self.total > OUTPUT_LIMIT:
                raise MetadataError('native_metadata_output_bound')
            if key.data == 'stdout':
                self.buffer.extend(chunk)
                if len(self.buffer) > LINE_LIMIT:
                    raise MetadataError('native_metadata_frame_bound')

    def completed_output(self):
        self.process.stdin.close()
        while self.selector.get_map():
            self.pump()
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise MetadataError('native_metadata_deadline')
        if self.process.wait(timeout=remaining) != 0:
            raise MetadataError('native_metadata_command_failed')
        return bytes(self.buffer)

    def request(self, method, params):
        if self.request_index >= len(METHODS) or method != METHODS[self.request_index]:
            raise MetadataError('metadata_rpc_sequence_rejected')
        expected = initialize_params() if self.request_index == 0 else {}
        if params != expected:
            raise MetadataError('metadata_rpc_parameters_rejected')
        self.request_index += 1
        request_id = self.request_index
        frame = {'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params}
        self.process.stdin.write(json.dumps(frame, separators=(',', ':')).encode() + b'\n')
        self.process.stdin.flush()
        while b'\n' not in self.buffer:
            if not self.selector.get_map():
                raise MetadataError('native_metadata_rpc_closed')
            self.pump()
        line, _, rest = self.buffer.partition(b'\n')
        self.buffer = bytearray(rest)
        response = json.loads(line)
        # A server tool/permission/filesystem request is never answered or run.
        if (not isinstance(response, dict) or response.get('jsonrpc') != '2.0'
                or response.get('id') != request_id or 'method' in response
                or 'error' in response or 'result' not in response):
            raise MetadataError('native_metadata_rpc_rejected')
        return response['result']


def collect_metadata(api_key, expected_account_ref, *, process_factory=NativeProcess, parent=Path('/home/worker')):
    if not isinstance(expected_account_ref, str) or re.fullmatch('[a-f0-9]{64}', expected_account_ref) is None:
        raise MetadataError('expected_account_reference_required')
    observed = datetime.now(timezone.utc).isoformat()
    deadline = time.monotonic() + 90
    with tempfile.TemporaryDirectory(prefix='cursor-metadata-', dir=parent) as temp:
        home = Path(temp)
        home.chmod(0o700)
        for relative in ('.cursor', 'workspace', 'tmp'):
            (home / relative).mkdir(mode=0o700)
        environment = metadata_environment(api_key, home)
        workspace = home / 'workspace'
        with process_factory(COMMANDS[0], environment, workspace, deadline) as native:
            native.completed_output()  # human model list is deliberately discarded
        with process_factory(COMMANDS[1], environment, workspace, deadline) as native:
            account = account_metadata(json.loads(native.completed_output()), expected_account_ref)
        with process_factory(COMMANDS[2], environment, workspace, deadline) as native:
            initialized = native.request(METHODS[0], initialize_params())
            if not isinstance(initialized, dict) or initialized.get('protocolVersion') != 1:
                raise MetadataError('unsupported_acp_protocol')
            models = parse_models(native.request(METHODS[1], {}))
        settings = settings_metadata(home / '.cursor' / 'cli-config.json')
    result = {'schema_version': 1, 'observed_at': observed, 'provider': 'cursor',
              'account': account, 'models': models, 'settings': settings,
              'catalog_source': 'native_acp_cursor/list_available_models',
              'catalog_projection': 'configurable_parameters_only_no_fixed_parameter_proof',
              'parameter_order_is_effort_ranking': False,
              'provider_on_demand_disabled_verified': False,
              'task_execution_enabled': False, 'inference_performed': False,
              'claims_tasks': False, 'sessions_created': 0}
    # Even unexpected vendor fields matching the supplied key cannot escape.
    if api_key in json.dumps(result, separators=(',', ':')):
        raise MetadataError('secret_in_metadata_rejected')
    return result
