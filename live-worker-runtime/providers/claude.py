"""Warm, single-task Claude Code subscription adapter for explicit project prompts.

Same audited native profile as the verified cloud review (claude-fable-5-1 / max
effort, first-party subscription route, read-only tool set, dedicated clean
config home). The native process is started and preflighted with the SDK
control requests (initialize, get_settings) before any task exists, then waits
with stdin open for exactly one user message. close() kills the process group
and commits/releases the credential lease before a result is delivered.
No tool use, no retries, no second prompt. Deadlines are time.monotonic().
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
from uuid import uuid4

from agent_hub import claude_runtime as rt
from agent_hub import subscription_auth as auth
from agent_hub.adapters import Command
from agent_hub.billing_policy import AuthEvidence, BillingPolicy, enforce_billing_policy
import provider_errors

MODEL = 'claude-fable-5-1'
EFFORT = 'max'
# Keep this binding identical to the enrolled Claude Ryan profile (never derived from a prompt).
ACCOUNT_REF = 'fb55abaefbe43b9b1cbd82a01f04c398968b29182c598cda9b77477c9110b29f'
EXECUTABLE = str(auth.CLAUDE_EXECUTABLE)
WORKSPACE = auth.CLAUDE_WORKSPACE
MAX_PROMPT_BYTES = 200000
MAX_ANSWER_BYTES = 15000
IDLE_QUEUE_LIMIT = 8
BILLING_POLICY = BillingPolicy(mode='subscription_only')
_MODEL_PATTERN = None
# A refused model call names its cause in a structured field (assistant.error,
# a rejected rate_limit_event) or only in the error result's text. The text is
# matched here and dropped: only a fixed code leaves the adapter. An account
# limit is not a transient fault; the controller must not spend retries on it.
# Only definitive account-limit signals map to claude_quota_exhausted, because
# the controller parks that slot for an hour or more. A bare rate_limit (429
# after the CLI's own retries) may be short-lived: it keeps its own code and
# stays an ordinary, visible strike.
QUOTA_TEXT = re.compile(r'usage limit|spend limit|limit reached|hit your limit|'
                        r'credit balance|out of extra usage|quota', re.IGNORECASE)
ASSISTANT_ERRORS = {'billing_error': 'claude_quota_exhausted', 'rate_limit': 'claude_rate_limited',
                    'authentication_failed': 'claude_authentication_failed',
                    'server_error': 'claude_provider_server_error'}


class NativeError(provider_errors.ProviderCodeError, RuntimeError):
    """Only fixed codes; never control frames, environment values or stderr."""


class NativeStartupStopped(NativeError):
    pass


def need(ok, code):
    if not ok:
        raise NativeError(code)


def _deadline(value):
    need(type(value) in (int, float) and math.isfinite(value) and value > time.monotonic(),
         'claude_deadline_invalid')
    return value


def command():
    """The exact argv the subscription audit accepts; the prompt travels in-protocol."""
    return Command(argv=(EXECUTABLE, '-p', '--safe-mode', '--restricted', '--strict-mcp-config',
                         '--output-format', 'json', '--setting-sources', '',
                         '--tools', 'Read,Grep,Glob', '--allowedTools', 'Read,Grep,Glob',
                         '--disallowedTools', 'mcp__*', '--permission-mode', 'dontAsk',
                         '--model', MODEL, '--effort', EFFORT), stdin='')


def _configuration_clean(workspace):
    """The audited clean-profile rules, minus the single-home rule.

    The live entrypoint keeps its acquire journal and the private credential
    home under /home/worker by design; the CLI config home itself must still
    be dedicated and free of settings, hooks, plugins and managed policy.
    """
    try:
        if (workspace != auth.CLAUDE_WORKSPACE or workspace.resolve(strict=True) != workspace
                or not workspace.is_dir()):
            return False
        for directory in (auth.CLAUDE_HOME, auth.CLAUDE_CONFIG):
            if (directory.resolve(strict=True) != directory or not directory.is_dir()
                    or directory.stat().st_uid != os.geteuid()
                    or directory.stat().st_mode & 0o077):
                return False
        if not auth._empty_or_missing(auth.CLAUDE_MANAGED):
            return False
        if not auth._empty_or_missing(auth.CLAUDE_HOME / '.config'):
            return False
        for directory in (workspace, *workspace.parents):
            if not auth._empty_or_missing(directory / '.claude'):
                return False
            if (directory / '.claude.json').exists() or (directory / '.claude.json').is_symlink():
                return False
        for entry in auth.CLAUDE_CONFIG.iterdir():
            if entry.is_symlink() or entry.name not in {'.claude.json', 'backups'}:
                return False
            if entry.name == 'backups':
                if not entry.is_dir():
                    return False
                for backup in entry.iterdir():
                    if (backup.is_symlink() or not backup.is_file()
                            or not backup.name.startswith('.claude.json.backup.')):
                        return False
            else:
                if not entry.is_file() or entry.stat().st_size > 16_384:
                    return False
                value = auth._strict_json(entry.read_bytes())
                if not isinstance(value, dict) or set(value) - auth._STARTUP_KEYS:
                    return False
        return True
    except (OSError, ValueError, RuntimeError, UnicodeError, RecursionError):
        return False


def launch_context_error(command_value, workspace, environment):
    """Validate only the trusted local launch context; never execute the CLI."""
    if sys.platform != 'linux' or not hasattr(os, 'geteuid') or os.geteuid() == 0:
        return 'unsupported_runtime'
    if not auth._command_matches(command_value, execution_mode='read_only'):
        return 'unapproved_command'
    if not auth._environment_matches(environment):
        return 'unapproved_environment'
    if not auth._profile_matches():
        return 'unverified_native_binary'
    if not _configuration_clean(workspace):
        return 'unapproved_local_configuration'
    return None


def _safe_usage(result):
    """Whitelist bounded numeric counters; never text, identifiers or prompts."""
    output = {}
    usage = result.get('usage')
    if isinstance(usage, dict):
        values = {k: usage[k] for k in ('input_tokens', 'output_tokens', 'cache_creation_input_tokens',
                                        'cache_read_input_tokens')
                  if type(usage.get(k)) is int and 0 <= usage[k] <= 10**9}
        if values:
            output['usage'] = values
    cost = result.get('total_cost_usd')
    if type(cost) in (int, float) and math.isfinite(cost) and 0 <= cost <= 10**6:
        output['api_equivalent_cost_usd'] = cost
    output['actual_account_charge_verified'] = False
    return output


class Native:
    """One warm stream-json Claude process: control preflight, one prompt, exit."""

    def __init__(self, environment, workspace, deadline, renew):
        self.deadline, self.renew = deadline, renew
        self.next_renew = time.monotonic() + 20
        self.writers = []
        argv = list(command().argv)
        argv[argv.index('--output-format') + 1] = 'stream-json'
        argv += ['--input-format', 'stream-json', '--verbose', '--no-session-persistence']
        try:
            self.process = subprocess.Popen(argv, cwd=str(workspace), env=dict(environment), shell=False,
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, start_new_session=os.name != 'nt')
        except OSError:
            raise NativeStartupStopped('claude_native_start_failed') from None
        self.frames = rt._Frames(self.process.stdout)
        self.frames.thread.start()
        self.stderr_thread = threading.Thread(target=self._discard, args=(self.process.stderr,), daemon=True)
        self.stderr_thread.start()
        self.closed = False
        self.provider_failure = None

    @staticmethod
    def _discard(stream):
        # Raw diagnostics can contain identity/configuration values: drain, never keep.
        try:
            while stream.read(4096):
                pass
        finally:
            stream.close()

    def tick(self):
        """Check the deadline and protocol, then renew both leases on the 20s cadence.

        Used while idle (maintain) and while a prompt is in flight (send/receive).
        next_renew advances only after renew returns, so a failed renewal stays due.
        Anything renew raises that is not already a vetted provider code becomes one.
        """
        need(time.monotonic() < self.deadline, 'claude_deadline')
        need(not self.frames.failed.is_set(), 'claude_protocol_output_invalid')
        if time.monotonic() < self.next_renew:
            return
        try:
            self.renew()
        except Exception as error:
            code = provider_errors.error_code(error)
            if code:
                raise
            text = str(error)
            if not provider_errors.SAFE_CODE.fullmatch(text):
                text = 'claude_lease_renew_failed'
            raise NativeError(text) from None
        self.next_renew = time.monotonic() + 20

    def send(self, value, *, close=False):
        payload = (json.dumps(value, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')
        need(len(payload) <= rt.MAX_FRAME_BYTES, 'claude_input_frame_limit')
        done, failed = threading.Event(), threading.Event()

        def write():
            try:
                self.process.stdin.write(payload)
                self.process.stdin.flush()
                if close:
                    self.process.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                failed.set()
            finally:
                done.set()

        writer = threading.Thread(target=write, daemon=True)
        self.writers.append(writer)
        writer.start()
        while not done.wait(0.02):
            self.tick()
        self.tick()
        need(not failed.is_set(), 'claude_stopped_accepting_input')

    def receive(self, *, allow_eof=False):
        while True:
            self.tick()
            try:
                value = self.frames.queue.get(timeout=0.05)
            except queue.Empty:
                need(self.process.poll() is None or not self.frames.queue.empty(), 'claude_exited_early')
                continue
            if value is rt._EOF:
                need(allow_eof, 'claude_exited_before_result')
                return rt._EOF
            return value

    def control(self, subtype):
        request_id = str(uuid4())
        self.send({'type': 'control_request', 'request_id': request_id, 'request': {'subtype': subtype}})
        value = self.receive()
        need(value.get('type') == 'control_response', 'claude_unexpected_preflight_message')
        response = value.get('response')
        need(isinstance(response, dict) and response.get('request_id') == request_id
             and response.get('subtype') == 'success' and isinstance(response.get('response'), dict),
             'claude_preflight_not_completed')
        return response['response']

    def idle_events(self):
        """Drain frames that arrive while idle; only keep-alives are acceptable."""
        events = []
        while len(events) < IDLE_QUEUE_LIMIT:
            try:
                value = self.frames.queue.get_nowait()
            except queue.Empty:
                break
            events.append(value)
        return events

    def _note_failure(self, code):
        # An account limit outranks any other signal from the same turn.
        if self.provider_failure is None or code == 'claude_quota_exhausted':
            self.provider_failure = code

    def prompt(self, text):
        self.provider_failure = None
        try:
            return self._prompt(text)
        except (NativeError, rt.ClaudeRuntimeError) as error:
            # A refused call can end without a result frame or a completed
            # lifecycle; the refusal is the cause either way.
            if self.provider_failure:
                raise NativeError(self.provider_failure) from None
            if isinstance(error, NativeError):
                raise
            # Lifecycle errors carry fixed codes; keep them instead of the
            # loop's generic native_or_connection_failure.
            code = 'claude_' + str(error)
            raise NativeError(code if provider_errors.SAFE_CODE.fullmatch(code)
                              else 'claude_command_lifecycle_invalid') from None

    def _prompt(self, text):
        sent = str(uuid4())
        lifecycle = rt._CommandLifecycle(sent)
        self.send({'type': 'user', 'message': {'role': 'user', 'content': text},
                   'parent_tool_use_id': None, 'session_id': '', 'uuid': sent}, close=True)
        result = None
        while result is None:
            event = self.receive()
            kind = event.get('type')
            if kind == 'command_lifecycle':
                lifecycle.accept(event)
                continue
            need(kind in ('system', 'assistant', 'user', 'result', 'rate_limit_event', 'keep_alive', 'stream_event'),
                 'claude_unexpected_task_message')
            if kind == 'keep_alive':
                need(set(event) == {'type'}, 'claude_keep_alive_invalid')
            if kind == 'system' and event.get('subtype') == 'init':
                if 'session_id' in event:
                    lifecycle.bind_session(event['session_id'])
                need(event.get('model') == MODEL, 'claude_model_changed')
            if kind == 'rate_limit_event':
                info = event.get('rate_limit_info')
                if isinstance(info, dict) and info.get('status') == 'rejected':
                    self._note_failure('claude_quota_exhausted')
            if kind == 'assistant' and isinstance(event.get('error'), str):
                self._note_failure(ASSISTANT_ERRORS.get(event['error'], 'claude_provider_error'))
            if kind == 'result':
                result = event
                reason = event.get('result')
                if (event.get('is_error') is not False and isinstance(reason, str)
                        and QUOTA_TEXT.search(reason[:4096])):
                    self._note_failure('claude_quota_exhausted')
        while True:
            trailing = self.receive(allow_eof=True)
            if trailing is rt._EOF:
                break
            if trailing.get('type') == 'command_lifecycle':
                lifecycle.accept(trailing)
            else:
                need(trailing == {'type': 'keep_alive'}, 'claude_unexpected_message_after_result')
        lifecycle.finish()
        while self.process.poll() is None:
            self.tick()
            time.sleep(0.02)
        need(self.process.returncode == 0, self.provider_failure or 'claude_exit_unsuccessful')
        return result

    def close(self):
        if self.closed:
            return
        try:
            if self.process.poll() is None:
                if os.name != 'nt':
                    try:
                        os.killpg(self.process.pid, 9)
                    except (ProcessLookupError, PermissionError):
                        pass
                self.process.kill()
            self.process.wait(timeout=10)
        finally:
            for stream in (self.process.stdin,):
                try:
                    if stream is not None and not stream.closed:
                        stream.close()
                except (OSError, ValueError):
                    pass
            self.frames.thread.join(timeout=1)
            self.stderr_thread.join(timeout=1)
            for writer in self.writers:
                writer.join(timeout=0.1)
            self.closed = True


NativeProcess = Native


@dataclass
class Handle:
    session: object = field(repr=False)
    heartbeat: object = field(repr=False)
    native: object = field(default=None, repr=False)
    environment: dict = field(default_factory=dict, repr=False)
    preflight: dict = field(default_factory=dict)
    attempted: bool = False
    stopped_proven: bool = True
    finished: bool = False
    close_failed: bool = False
    credential_version: str = field(default='', repr=False)

    @property
    def readiness(self):
        live = self.native is not None and not self.finished and not self.close_failed
        return {'provider': 'claude', 'authenticated': live,
                'ready_for_project_prompt': live and not self.attempted,
                'model': MODEL, 'effort': EFFORT, 'preflight': self.preflight,
                'tools_enabled': False, 'workspace_access': False, 'full_coding_ready': False,
                'automatic_improvement_ready': False}


def _renew(handle):
    handle.session.broker.renew(handle.session.lease)
    need(handle.heartbeat() is True, 'claude_hub_heartbeat_lost')


def _token(session):
    path = session.auth_path
    need(not path.is_symlink() and path.is_file() and path.stat().st_size <= 16_384, 'claude_credential_file_invalid')
    value = path.read_bytes().decode('utf-8', 'strict').strip()
    need(bool(value) and not any(c.isspace() or ord(c) < 32 for c in value), 'claude_credential_malformed')
    return value


def close(handle):
    """Stop all native descendants before commit/release; idempotent after success."""
    if handle.finished:
        return handle.credential_version
    need(not handle.close_failed, 'claude_close_requires_reconciliation')
    try:
        if handle.native is not None:
            handle.native.close()
            handle.stopped_proven = True
            handle.native = None
        need(handle.stopped_proven, 'claude_native_stop_unconfirmed')
        handle.credential_version = handle.session.finish(native_stopped=True)
        handle.finished = True
        handle.environment = {}
        return handle.credential_version
    except Exception:
        handle.close_failed = True
        try:
            handle.session.broker.quarantine(handle.session.lease, 'provider_refresh_uncertain')
        except Exception:
            pass  # Existing non-idle owner state still prohibits takeover.
        raise


def prepare(session, heartbeat, deadline):
    """Authenticate and create one warm, model/effort-verified headless session."""
    _deadline(deadline)
    need(session.state == 'active' and session.lease.account_ref == ACCOUNT_REF, 'claude_owner_lease_required')
    handle = Handle(session=session, heartbeat=heartbeat)
    try:
        session.broker.assert_current(session.lease)
        environment = auth.claude_environment(_token(session))
        launch = command()
        error = launch_context_error(launch, WORKSPACE, environment)
        need(error is None, 'claude_' + str(error))
        selection = {'account_ref': ACCOUNT_REF, 'cli_model_id': MODEL, 'effort': EFFORT}
        handle.environment = environment
        handle.stopped_proven = False
        try:
            handle.native = NativeProcess(environment, WORKSPACE, deadline, lambda: _renew(handle))
        except NativeStartupStopped:
            handle.stopped_proven = True
            raise
        account = handle.native.control('initialize')
        rt._verify_account(account, selection)
        settings = handle.native.control('get_settings')
        rt._verify_settings(settings, selection)
        evidence = AuthEvidence('claude', 'subscription_login', time.time(), 'vendor_cli_status', True)
        enforce_billing_policy('claude', environment, BILLING_POLICY, evidence)
        info = account.get('account', {})
        handle.preflight = {
            'account': {'intended_account_ref': ACCOUNT_REF, 'route': 'first_party_subscription',
                        'subscription_type': info.get('subscriptionType'),
                        'native_owner_identity_reported': isinstance(info.get('email'), str)},
            'settings': {'model': MODEL, 'effort': EFFORT, 'clean_dedicated_profile': True,
                         'tools': 'Read,Grep,Glob', 'permission_mode': 'dontAsk'},
            'quota': {'native_usage_status': 'unavailable', 'native_included_used_percent': None,
                      'note': 'Claude Code exposes no usage counter before a prompt; the browser billing '
                              'receipt remains the operator control'},
            'same_process_account_model_billing': True,
            'same_process_account_model_quota': False}
        need(heartbeat() is True, 'claude_hub_heartbeat_lost')
        return handle
    except Exception:
        if not handle.finished and not handle.close_failed:
            close(handle)
        raise


def maintain(handle):
    """Call during idle polling at least every 20s, before the lease renewal window closes."""
    need(not handle.finished and not handle.attempted and not handle.close_failed and handle.native is not None,
         'claude_handle_not_idle')
    need(handle.native.process.poll() is None, 'claude_warm_process_ended')
    need(not handle.native.frames.failed.is_set(), 'claude_protocol_output_invalid')
    for event in handle.native.idle_events():
        need(event == {'type': 'keep_alive'}, 'claude_unexpected_idle_message')
    # The startup deadline is not the warm window. tick() renews the hub task
    # lease and the broker credential lease on the same schedule as a prompt.
    handle.native.deadline = max(handle.native.deadline, time.monotonic() + 30)
    handle.native.tick()
    return handle.readiness


def execute(handle, prompt, task_deadline, *, task_kind='project'):
    """One explicit project prompt, followed by confirmed stop and writeback."""
    need(not handle.finished and not handle.attempted and handle.native is not None, 'claude_task_replay_forbidden')
    try:
        need(task_kind == 'project', 'claude_automatic_improvement_not_enabled')
        need(type(prompt) is str and 0 < len(prompt.encode()) <= MAX_PROMPT_BYTES, 'claude_prompt_limit')
        handle.native.deadline = _deadline(task_deadline)
        need(handle.heartbeat() is True, 'claude_hub_heartbeat_lost')
        handle.attempted = True
        result = handle.native.prompt(prompt)
        need(result.get('is_error') is False and result.get('subtype') == 'success',
             getattr(handle.native, 'provider_failure', None) or 'claude_task_not_successful')
        text = result.get('result')
        need(isinstance(text, str) and 0 < len(text.encode()) <= MAX_ANSWER_BYTES, 'claude_answer_missing_or_large')
        used = result.get('modelUsage')
        need(used is None or (isinstance(used, dict)
                              and all(isinstance(k, str) and k.removesuffix('[1m]') == MODEL for k in used)),
             'claude_usage_from_different_model')
        usage = _safe_usage(result)
        version = close(handle)
        return {'text': text, 'provider': 'claude', 'model': MODEL, 'effort': EFFORT, 'usage': usage,
                'preflight': handle.preflight, 'same_process_account_model_quota': False,
                'review_sha256': hashlib.sha256(text.encode()).hexdigest(),
                'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
                'prompt_sent_once_by_wrapper': True, 'automatic_retry': False,
                'tools_policy': 'read_only_tools_dontAsk_restricted', 'full_coding_ready': False,
                'actual_charge': 'unverified', 'native_stopped': True, 'credential_writeback': 'committed',
                'credential_version_ref': hashlib.sha256(version.encode()).hexdigest()}
    except Exception:
        if not handle.finished and not handle.close_failed:
            close(handle)
        raise
