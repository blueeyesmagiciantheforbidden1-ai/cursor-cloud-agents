"""One bounded native Claude session, verified before its task is released.

Control messages contain no task. The same subprocess answers initialize and
get_settings before receiving a user message. This is a point-in-time check of
its actual route/settings/model, not a guarantee against future provider policy
changes. Production enrollment must independently bind the injected secret to
the expected personal Pro/Max account and its existing spending settings. setup-token
often omits email in accountInfo; we do not manufacture identity verification.

The local execution mode selects an exact read-only or project-work profile.
Project commands require the dedicated rootless container and scoped IAM;
permission allow rules do not create an operating-system sandbox.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
import uuid

from .adapters import Command, EXECUTION_MODES, MAX_OUTPUT_BYTES
from .billing_policy import AuthEvidence, BillingPolicy, enforce_billing_policy
from .subscription_auth import claude_launch_context_error, _strict_json


MAX_FRAME_BYTES = 128_000
MAX_QUEUED_FRAMES = 32
MAX_TOTAL_BYTES = 2_000_000
_EOF = object()


class ClaudeRuntimeError(ValueError):
    """Sanitized failure; never include control frames, env values or stderr."""


COMMAND_LIFECYCLE_STATES = ('queued', 'started', 'completed', 'cancelled', 'discarded', 'refused')


class _CommandLifecycle:
    """Validate one SDK command without mistaking delivery state for a result.

    Native 2.1.275's bundled schema and Anthropic SDK changelog 0.3.206/0.3.238:
    queued may precede system/init; completed may precede OR follow the result.
    Terminal states need not have a started predecessor. A failed/absent terminal
    never licenses an automatic resend or proves whether the account was charged.
    https://github.com/anthropics/claude-agent-sdk-typescript/blob/main/CHANGELOG.md
    """
    def __init__(self, command_uuid):
        self.command_uuid = command_uuid
        self.session_id = None
        self.states = []
        self.frame_uuids = set()

    @staticmethod
    def valid_uuid(value):
        if not isinstance(value, str) or len(value) != 36:
            return False
        try:
            return str(uuid.UUID(value)) == value.lower()
        except ValueError:
            return False

    def bind_session(self, session_id):
        if (not self.valid_uuid(session_id)
                or self.session_id is not None and self.session_id != session_id):
            raise ClaudeRuntimeError('invalid_command_lifecycle_session')
        self.session_id = session_id

    def accept(self, frame):
        if (set(frame) != {'type', 'command_uuid', 'state', 'uuid', 'session_id'}
                or frame.get('type') != 'command_lifecycle'
                or frame.get('command_uuid') != self.command_uuid
                or frame.get('state') not in COMMAND_LIFECYCLE_STATES
                or not self.valid_uuid(frame.get('uuid'))
                or frame['uuid'] in self.frame_uuids):
            raise ClaudeRuntimeError('invalid_command_lifecycle_frame')
        self.bind_session(frame['session_id'])
        state = frame['state']
        if (state in self.states or len(self.states) >= 3
                or any(s in self.states for s in COMMAND_LIFECYCLE_STATES[2:])
                or state == 'queued' and self.states):
            raise ClaudeRuntimeError('invalid_command_lifecycle_transition')
        self.states.append(state)
        self.frame_uuids.add(frame['uuid'])
        if state in ('cancelled', 'discarded', 'refused'):
            raise ClaudeRuntimeError('command_lifecycle_unsuccessful_outcome_uncertain')

    def finish(self):
        if not self.states or self.states[-1] != 'completed':
            raise ClaudeRuntimeError('command_lifecycle_incomplete_outcome_uncertain')


class _Deadline(ClaudeRuntimeError):
    pass


class _Frames:
    def __init__(self, stream):
        self.stream = stream
        self.queue = queue.Queue(maxsize=MAX_QUEUED_FRAMES)
        self.failed = threading.Event()
        self.thread = threading.Thread(target=self._read, daemon=True)

    def _read(self):
        total = 0
        try:
            while True:
                raw = self.stream.readline(MAX_FRAME_BYTES + 1)
                if not raw:
                    self.queue.put_nowait(_EOF)
                    return
                total += len(raw)
                if len(raw) > MAX_FRAME_BYTES or total > MAX_TOTAL_BYTES or not raw.endswith(b'\n'):
                    raise ValueError('Protocol size limit')
                value = _strict_json(raw)
                if not isinstance(value, dict):
                    raise ValueError('Protocol object required')
                self.queue.put_nowait(value)
        except (OSError, ValueError, UnicodeError, RecursionError, queue.Full):
            self.failed.set()
        finally:
            self.stream.close()


def _verify_account(body, selection):
    account = body.get('account')
    if (not isinstance(account, dict) or account.get('apiProvider') != 'firstParty'
            or account.get('tokenSource') != 'CLAUDE_CODE_OAUTH_TOKEN'
            or account.get('apiKeySource') not in (None, 'none')):
        raise ClaudeRuntimeError('Claude did not confirm the dedicated subscription authentication route')
    # A present identity must match the independently configured owner. With a
    # setup-token, absence is expected; enrollment supplies that identity binding.
    email = account.get('email')
    if email is not None:
        if not isinstance(email, str) or not email.strip():
            raise ClaudeRuntimeError('Claude returned an invalid account identity')
        observed = hashlib.sha256(email.strip().lower().encode('utf-8')).hexdigest()
        if observed != selection.get('account_ref'):
            raise ClaudeRuntimeError('Claude is signed in to a different owner account')
    kind = account.get('subscriptionType')
    if kind is not None and kind not in ('pro', 'max'):
        raise ClaudeRuntimeError('Claude did not confirm a personal Pro or Max subscription')


def _verify_settings(body, selection):
    # CLI model/effort arguments appear in `applied`, not in effective settings,
    # as verified against native 2.1.275 without a user prompt. Reject every other
    # setting source in this dedicated profile, including managed customizations.
    if (body.get('effective') != {} or body.get('sources') != []
            or body.get('errors') not in (None, []) or body.get('remote_control_policy_lock_reason')):
        raise ClaudeRuntimeError('Claude loaded settings outside the dedicated clean profile')
    applied = body.get('applied')
    if (not isinstance(applied, dict) or applied.get('model') != selection.get('cli_model_id')
            or applied.get('effort') != selection.get('effort')
            or applied.get('advisor') is not None or applied.get('ultracode') is not False):
        raise ClaudeRuntimeError('Claude did not apply the selected model and reasoning effort')


def _stream_command(command):
    arguments = list(command.argv)
    try:
        index = arguments.index('--output-format')
    except ValueError:
        raise ClaudeRuntimeError('Claude command lacks the verified output format') from None
    if arguments[index + 1] != 'json':
        raise ClaudeRuntimeError('Claude command has an unexpected output format')
    arguments[index + 1] = 'stream-json'
    return tuple(arguments) + ('--input-format', 'stream-json', '--verbose', '--no-session-persistence')


def run_verified_claude(command: Command, workspace: Path, timeout: float, heartbeat, *,
                        environment, selection, billing_policy: BillingPolicy,
                        terminate_tree, contain_process, heartbeat_seconds=10,
                        transient_errors=(), lease_lost_errors=(), mode='read_only',
                        plan_expires_at=None):
    """Return a normal adapter result without exposing raw control metadata.

    `selection` is supplied only by model_runtime's validated local plan. There
    is intentionally no task-selectable executable, verifier, or proof callback.
    Cleanup callbacks are the worker's existing platform process containment.
    """
    if mode not in EXECUTION_MODES:
        raise ClaudeRuntimeError('The requested Claude execution mode is unsupported')
    if (not isinstance(selection, dict) or selection.get('agent') != 'claude'
            or selection.get('billing') not in ('subscription_included', 'existing_credits')
            or billing_policy.mode != 'subscription_only'):
        raise ClaudeRuntimeError('Claude requires a validated subscription model selection')
    if (type(plan_expires_at) not in (int, float) or not math.isfinite(plan_expires_at)
            or not time.time() < plan_expires_at):
        raise ClaudeRuntimeError('The selected Claude model plan has expired')
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        return 124, 'Task exceeded the local execution timeout.'
    deadline = time.monotonic() + timeout
    if claude_launch_context_error(command, workspace, environment, execution_mode=mode) is not None:
        raise ClaudeRuntimeError('Claude local authentication/configuration audit did not pass')
    process, containment, frames, stderr_reader = None, None, None, None
    last_heartbeat = time.monotonic()
    next_heartbeat = last_heartbeat + heartbeat_seconds
    writer_threads = []

    def tick():
        nonlocal last_heartbeat, next_heartbeat
        now = time.monotonic()
        if now >= deadline:
            raise _Deadline('Task exceeded the local execution timeout.')
        if now >= next_heartbeat:
            try:
                if not heartbeat():
                    raise _Deadline('Task stopped because its lease was cancelled or expired.')
                last_heartbeat = time.monotonic()
            except lease_lost_errors:
                raise _Deadline('Task stopped because its lease was cancelled or expired.') from None
            except transient_errors:
                if time.monotonic() - last_heartbeat >= 30:
                    raise _Deadline('Task stopped because the hub could not renew its lease.') from None
            next_heartbeat = time.monotonic() + heartbeat_seconds
        if frames is not None and frames.failed.is_set():
            raise ClaudeRuntimeError('Claude returned malformed or excessive protocol output')

    def send(value, *, close=False):
        payload = (json.dumps(value, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')
        if len(payload) > MAX_FRAME_BYTES:
            raise ClaudeRuntimeError('Claude input exceeded the protocol limit')
        done, failed = threading.Event(), threading.Event()
        def write():
            try:
                process.stdin.write(payload)
                process.stdin.flush()
                if close:
                    process.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                failed.set()
            finally:
                done.set()
        writer = threading.Thread(target=write, daemon=True)
        writer_threads.append(writer)
        writer.start()
        while not done.wait(0.02):
            tick()
        tick()
        if failed.is_set():
            raise ClaudeRuntimeError('Claude stopped accepting protocol input')

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
                raise ClaudeRuntimeError('Claude exited before returning a complete result')
            return value

    def control(subtype):
        request_id = str(uuid.uuid4())
        send({'type': 'control_request', 'request_id': request_id, 'request': {'subtype': subtype}})
        while True:
            value = receive()
            if value.get('type') != 'control_response':
                # A model result before prompt release, an unsolicited tool
                # request, or a mismatched response cannot authorize inference.
                raise ClaudeRuntimeError('Claude sent an unexpected preflight protocol message')
            response = value.get('response')
            if (not isinstance(response, dict) or response.get('request_id') != request_id
                    or response.get('subtype') != 'success' or not isinstance(response.get('response'), dict)):
                raise ClaudeRuntimeError('Claude did not complete the requested preflight check')
            return response['response']

    def discard_stderr(stream):
        # Raw diagnostics can contain identity/configuration values. Drain them
        # to avoid deadlock but never persist or return them to the hub.
        try:
            while stream.read(4096):
                pass
        finally:
            stream.close()

    try:
        tick()
        flags = (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW) if os.name == 'nt' else 0
        process = subprocess.Popen(_stream_command(command), cwd=workspace, env=dict(environment),
                                   shell=False, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, creationflags=flags, start_new_session=os.name != 'nt')
        containment = contain_process(process)
        frames = _Frames(process.stdout)
        frames.thread.start()
        stderr_reader = threading.Thread(target=discard_stderr, args=(process.stderr,), daemon=True)
        stderr_reader.start()
        _verify_account(control('initialize'), selection)
        _verify_settings(control('get_settings'), selection)
        evidence = AuthEvidence('claude', 'subscription_login', time.time(), 'vendor_cli_status', True)
        enforce_billing_policy('claude', environment, billing_policy, evidence)
        # Check the lease immediately before exposing the user's task, regardless
        # of the ordinary heartbeat interval used while initialization runs.
        next_heartbeat = 0
        tick()
        if not time.time() < plan_expires_at:
            raise ClaudeRuntimeError('The selected Claude model plan expired before task release')
        sent_uuid = str(uuid.uuid4())
        lifecycle = _CommandLifecycle(sent_uuid)
        send({'type': 'user', 'message': {'role': 'user', 'content': command.stdin},
              'parent_tool_use_id': None, 'session_id': '', 'uuid': sent_uuid}, close=True)
        result = None
        while result is None:
            event = receive()
            kind = event.get('type')
            if kind == 'command_lifecycle':
                lifecycle.accept(event)
                continue
            if kind not in ('system', 'assistant', 'user', 'result', 'rate_limit_event', 'keep_alive', 'stream_event'):
                raise ClaudeRuntimeError('Claude returned an unexpected task protocol message')
            if kind == 'keep_alive' and set(event) != {'type'}:
                raise ClaudeRuntimeError('Claude returned an invalid keep-alive message')
            if event.get('type') == 'system' and event.get('subtype') == 'init':
                if 'session_id' in event:
                    lifecycle.bind_session(event['session_id'])
                if event.get('model') != selection['cli_model_id']:
                    raise ClaudeRuntimeError('Claude changed its selected model before the task')
            if event.get('type') == 'result':
                result = event
        if result.get('is_error') is not False or result.get('subtype') != 'success':
            raise ClaudeRuntimeError('Claude did not complete the task successfully')
        text = result.get('result')
        if not isinstance(text, str) or len(text.encode('utf-8')) > MAX_OUTPUT_BYTES:
            raise ClaudeRuntimeError('Claude returned a missing or oversized final result')
        # When usage reports actual models, reject a fallback or a second model.
        used = result.get('modelUsage')
        allowed_model = selection['cli_model_id'].removesuffix('[1m]')
        if used is not None and (not isinstance(used, dict)
                                or any(key.removesuffix('[1m]') != allowed_model for key in used)):
            raise ClaudeRuntimeError('Claude reported usage from a different model')
        # The native fresh-turn completion is AFTER result. Drain and validate it;
        # a result followed by cancellation/unknown output is not silently success.
        while True:
            trailing = receive(allow_eof=True)
            if trailing is _EOF:
                break
            if trailing.get('type') == 'command_lifecycle':
                lifecycle.accept(trailing)
            elif trailing == {'type': 'keep_alive'}:
                pass
            else:
                raise ClaudeRuntimeError('Claude returned an unexpected message after its result')
        lifecycle.finish()
        while process.poll() is None:
            tick()
            time.sleep(0.02)
        frames.thread.join(timeout=0.2)
        tick()
        if process.returncode != 0:
            raise ClaudeRuntimeError('Claude exited unsuccessfully after its result')
        return 0, json.dumps({'result': text}, ensure_ascii=False)
    except _Deadline as exc:
        return 124, str(exc)
    finally:
        if process is not None:
            terminate_tree(process)
            if containment is not None:
                containment.close()
            if process.stdin is not None and not process.stdin.closed:
                try:
                    process.stdin.close()
                except (OSError, ValueError):
                    pass
            if frames is None and process.stdout is not None:
                process.stdout.close()
            if stderr_reader is None and process.stderr is not None:
                process.stderr.close()
        if frames is not None:
            frames.thread.join(timeout=1)
        if stderr_reader is not None:
            stderr_reader.join(timeout=1)
        for writer in writer_threads:
            writer.join(timeout=0.1)
