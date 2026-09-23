"""Small, no-dispatch app-server preflight contract for a single native process.

The owning transport performs initialize/initialized, serializes all traffic,
enforces deadlines/byte limits, and denies unsolicited requests. This module
never sends thread/start or turn/start, so it cannot perform model inference.
It is not an OS isolation implementation or a catalog ranking service.
"""
import hashlib
import json
import re
import time

EFFORTS = ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra')
WORKSPACE = '/workspace/default'
SANDBOX = {'type': 'workspaceWrite', 'writableRoots': [WORKSPACE],
           'networkAccess': False, 'excludeSlashTmp': True, 'excludeTmpdirEnvVar': True}


class GateError(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    allow_nan=False).encode()).hexdigest()


def account_ref(email):
    if not isinstance(email, str) or not email.strip():
        raise GateError('account_identity_unavailable')
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


class PrePromptGate:
    def __init__(self, rpc, selection, *, config_sha256, provider_account_ref,
                 clock=time.monotonic):
        # Selection and expected digests must be immutable controller-owned data,
        # never supplied by a task. Validate the full model_policy plan upstream.
        for value in (selection.get('account_ref'), config_sha256, provider_account_ref):
            if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{64}', value):
                raise GateError('trusted_binding_required')
        if (selection.get('billing') not in ('subscription_included', 'existing_credits')
                or not isinstance(selection.get('cli_model_id'), str) or not selection['cli_model_id']
                or selection.get('effort') not in EFFORTS):
            raise GateError('invalid_model_selection')
        self.rpc, self.selection = rpc, dict(selection)
        self.config_sha256, self.provider_account_ref = config_sha256, provider_account_ref
        self.clock, self.checked_at, self.thread_id = clock, None, None
        self.state = 'new'

    def _read(self, method, params):
        try:
            value = self.rpc(method, params)
            if not isinstance(value, dict):
                raise ValueError()
            # Transport must bound the raw bytes before parsing; this is a second
            # bound on the decoded object, not protection from a malicious parser.
            if len(json.dumps(value, allow_nan=False).encode()) > 2 * 1024 * 1024:
                raise ValueError()
            return value
        except Exception:
            self.state = 'denied'
            raise GateError('native_metadata_unavailable') from None

    def collect(self):
        if self.state != 'new':
            raise GateError('single_use_gate_required')
        self.state = 'checking'
        started = self.clock()
        try:
            account = self._read('account/read', {'refreshToken': False}).get('account')
            if (not isinstance(account, dict) or account.get('type') != 'chatgpt'
                    or account_ref(account.get('email')) != self.selection['account_ref']
                    or account.get('planType') in (None, 'unknown')):
                raise GateError('account_mismatch_or_unknown')
            models, cursor, seen = [], None, set()
            for _ in range(16):
                params = {'includeHidden': True, 'limit': 100}
                if cursor is not None:
                    params['cursor'] = cursor
                page = self._read('model/list', params)
                if not isinstance(page.get('data'), list):
                    raise GateError('model_catalog_unavailable')
                models.extend(page['data'])
                if len(models) > 256:
                    raise GateError('model_catalog_limit')
                cursor = page.get('nextCursor')
                if cursor is None:
                    break
                if not isinstance(cursor, str) or not cursor or cursor in seen:
                    raise GateError('model_catalog_cursor_invalid')
                seen.add(cursor)
            else:
                raise GateError('model_catalog_limit')
            candidates = [m for m in models if isinstance(m, dict) and m.get('model') == self.selection['cli_model_id']]
            if len(candidates) != 1 or candidates[0].get('hidden') is not False:
                raise GateError('exact_visible_model_required')
            options = candidates[0].get('supportedReasoningEfforts')
            if not isinstance(options, list) or not options:
                raise GateError('model_efforts_unknown')
            levels = [x.get('reasoningEffort') if isinstance(x, dict) else None for x in options]
            if any(x not in EFFORTS for x in levels) or self.selection['effort'] != max(levels, key=EFFORTS.index):
                raise GateError('highest_supported_effort_required')
            rates = self._read('account/rateLimits/read', {})
            backend_id = rates.get('accountId')
            if (not isinstance(backend_id, str) or not backend_id
                    or hashlib.sha256(('openai-chatgpt:' + backend_id).encode()).hexdigest() != self.provider_account_ref):
                raise GateError('quota_account_mismatch')
            if rates.get('ordinaryUsageAllowed') is not True:
                # Existing credits require verified provider-enforced cap and
                # no-autoreload evidence. This package does not manufacture it
                # from the units-unknown native `credits.balance` string.
                raise GateError('included_usage_unavailable_credit_integration_required')
            if self.selection['billing'] != 'subscription_included':
                raise GateError('included_usage_must_be_used_first')
            config = self._read('config/read', {'includeLayers': True, 'cwd': WORKSPACE}).get('config')
            if not isinstance(config, dict) or digest(config) != self.config_sha256:
                raise GateError('effective_config_mismatch')
            if config.get('forced_login_method') != 'chatgpt' or config.get('cli_auth_credentials_store') != 'file':
                raise GateError('subscription_route_required')
            requirements = self._read('configRequirements/read', {})
            if self.clock() - started > 30:
                raise GateError('metadata_collection_expired')
            self.checked_at, self.state = self.clock(), 'checked'
            return {'account_ref': self.selection['account_ref'], 'model': self.selection['cli_model_id'],
                    'effort': self.selection['effort'], 'config_sha256': self.config_sha256,
                    'model_catalog_sha256': digest(models), 'requirements_sha256': digest(requirements),
                    'ordinaryUsageAllowed': True, 'dispatch_enabled': False}
        except (GateError, TypeError, ValueError, KeyError):
            self.state = 'denied'
            raise

    def _fresh(self):
        if self.checked_at is None or not 0 <= self.clock() - self.checked_at <= 30:
            self.state = 'denied'
            raise GateError('preflight_expired')

    def thread_request(self):
        """Return request DATA, not authorization to execute it or evidence of isolation."""
        if self.state != 'checked':
            raise GateError('preflight_required')
        self._fresh()
        return {'model': self.selection['cli_model_id'], 'modelProvider': 'openai', 'cwd': WORKSPACE,
                'approvalPolicy': 'never', 'sandbox': 'workspace-write', 'ephemeral': True,
                'config': {'model_reasoning_effort': self.selection['effort'],
                           'sandbox_workspace_write': {'writable_roots': [WORKSPACE], 'network_access': False,
                                                       'exclude_slash_tmp': True, 'exclude_tmpdir_env_var': True}}}

    def accept_thread(self, response):
        if self.state != 'checked':
            raise GateError('preflight_required')
        self._fresh()
        expected = {'model': self.selection['cli_model_id'], 'modelProvider': 'openai',
                    'reasoningEffort': self.selection['effort'], 'cwd': WORKSPACE,
                    'approvalPolicy': 'never', 'sandbox': SANDBOX}
        if not isinstance(response, dict) or any(response.get(k) != v for k, v in expected.items()):
            self.state = 'denied'
            raise GateError('applied_native_settings_mismatch')
        thread = response.get('thread')
        if (not isinstance(thread, dict) or thread.get('ephemeral') is not True
                or not isinstance(thread.get('id'), str) or not thread['id'] or thread.get('turns') != []):
            self.state = 'denied'
            raise GateError('empty_ephemeral_thread_required')
        self.thread_id, self.state = thread['id'], 'thread_checked'

    def turn_request(self, prompt):
        if self.state != 'thread_checked':
            raise GateError('applied_settings_check_required')
        self._fresh()
        if not isinstance(prompt, str) or not prompt or len(prompt.encode()) > 65536:
            raise GateError('bounded_prompt_required')
        self.state = 'prepared'
        return {'threadId': self.thread_id, 'input': [{'type': 'text', 'text': prompt}],
                'model': self.selection['cli_model_id'], 'effort': self.selection['effort'],
                'sandboxPolicy': dict(SANDBOX)}
