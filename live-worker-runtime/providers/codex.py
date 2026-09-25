"""Prepared Codex subscription process; one explicit project prompt per grant.

Package beside the reviewed codex-cloud-transport modules. Their pinned native
binary, parser and final-item correlation remain authoritative. This adapter
adds an idle/prepared phase, not a second credential acquisition or turn.
Official protocol: https://learn.chatgpt.com/docs/app-server
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import signal
import threading
import time

import metadata
import protocol_gate
import broker_renew
import provider_errors
import transport

MODEL, EFFORT = 'gpt-6-astra', 'ultra'
CLI_NAME = 'runcrew_codex_live'
CLI_VERSION = '1'
MAX_PROMPT, MAX_ANSWER = 200000, 15000
WARM_SECONDS, NATIVE_SECONDS, FINALIZE_RESERVE = 3600, 600, 45
# execute() refuses a task deadline shorter than this. The live loop's claim
# gate also requires this much time left on warm_deadline. Cloud Run's job
# timeout (5400 s) already reserves a full task past that window, so execute()
# does not cap the native deadline on warm_deadline.
EXECUTE_WARM_FLOOR = FINALIZE_RESERVE + 30
RENEW_SECONDS = 20
# Hub release 3 marks usage stale 900 s after observed_at; refresh sooner.
QUOTA_REFRESH_SECONDS = 600
# Swallowed validation failures back off so maintain() does not re-request every poll.
QUOTA_RETRY_SECONDS = 120
# Recorded by the successful pinned Linux build in two independent empty homes.
CONFIG_SHA = 'c584ec84021d23203d0c444cf474c3f184a0b759faf51bed0ba1fc16361af9cf'
REQUIREMENTS_SHA = '25b86fa3671a4ee1ea904a1f5777c164347763d01dda591fcac3022b64235e10'
SAFE_CODE = re.compile(r'[a-z][a-z0-9_]{0,99}')
# Mid-turn account exhaustion only: tokens are matched and dropped. A plain
# transient 429 / rate_limit without one of these must stay a normal failure.
_QUOTA_TOKENS = frozenset({
    'usage_limit', 'insufficient_quota', 'quota_exceeded', 'billing', 'credit'})
# Failed-turn notifications only. Content methods (item/completed, item deltas,
# reasoning, agentMessage text) must never be scanned for quota tokens.
_TURN_FAILURE_METHODS = frozenset({'turn/completed'})


class LiveCodexError(provider_errors.ProviderCodeError, RuntimeError):
    """Only fixed codes and conservative outcome flags cross the supervisor API."""


def need(ok, code):
    if not ok:
        raise LiveCodexError(code)


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
                    'detail', 'details', 'status', 'data', 'body', 'turn', 'params'):
                if _quota_exhausted_signal(item, depth=depth + 1):
                    return True
        return False
    if type(value) is list:
        return any(_quota_exhausted_signal(item, depth=depth + 1) for item in value[:32])
    return False


def _midturn_quota_exhausted(event):
    """Classify only JSON-RPC errors or failed turn/completed frames, never content."""
    if type(event) is not dict:
        return False
    # JSON-RPC response error object (no notification method).
    if 'error' in event and event.get('method') is None:
        return _quota_exhausted_signal(event.get('error'))
    if event.get('method') not in _TURN_FAILURE_METHODS:
        return False
    params = event.get('params')
    if type(params) is not dict:
        return False
    turn = params.get('turn')
    if type(turn) is not dict or turn.get('status') != 'failed':
        return False
    # Scan the failed turn / its error object only — not sibling content events.
    return _quota_exhausted_signal(turn)


def _deadline(value):
    need(type(value) in (int, float) and math.isfinite(value), 'deadline_invalid')
    return float(value)


class WarmRPC(transport.TurnRPC):
    """Original strict transport with serialized metadata on an idle thread."""
    # FixtureRPC skips __init__, so the retry slot has to exist on the class.
    renew_retry_at = None
    # Set by poll_idle when it swallowed an idle hub loss; maintain() then skips
    # this tick's quota refresh (the due renew would fail again inside it).
    idle_hub_loss = False

    def __init__(self, *args, **kwargs):
        try:
            super().__init__(*args, **kwargs)
        except BaseException:
            if getattr(self, 'process', None) is not None:
                self.stop_group()
            raise

    def request(self, method, params):
        idle_metadata = method in transport.META_METHODS and self.protocol_state == 'thread_ready'
        self._idle_metadata = idle_metadata
        if idle_metadata:
            self.protocol_state = 'metadata'
        try:
            result = super().request(method, params)
        except BaseException:
            self.protocol_state = 'denied'
            raise
        if idle_metadata:
            self.protocol_state = 'thread_ready'
        self._idle_metadata = False
        return result

    def tick(self):
        super().tick()  # base-image NativeRPC.tick sets next_renew = now+25 once renew() returns
        retry, self.renew_retry_at = self.renew_retry_at, None
        if retry is not None:
            self.next_renew = min(self.next_renew, retry)

    def _notification(self, value):
        if getattr(self, '_idle_metadata', False):
            previous = self.protocol_state
            self.protocol_state = 'thread_ready'
            try:
                super()._notification(value)
            finally:
                self.protocol_state = previous
        else:
            super()._notification(value)

    def _send(self, value=None, *, close=False):
        raw = b'' if value is None else (json.dumps(value, separators=(',', ':'), ensure_ascii=False, allow_nan=False) + '\n').encode()
        need(len(raw) <= MAX_PROMPT + 8192, 'native_request_limit')
        done, failed = threading.Event(), threading.Event()

        def write():
            try:
                remaining = memoryview(raw)
                while remaining:
                    need(time.monotonic() < self.deadline, 'native_write_deadline')
                    try:
                        count = self.process.stdin.write(remaining)
                    except InterruptedError:
                        continue
                    need(type(count) is int and 0 < count <= len(remaining), 'native_short_write_invalid')
                    remaining = remaining[count:]
                if raw:
                    self.process.stdin.flush()
                if close:
                    self.process.stdin.close()
            except Exception:
                failed.set()
            finally:
                done.set()

        writer = threading.Thread(target=write, daemon=True)
        self.writers.append(writer)
        writer.start()
        while not done.wait(.02):
            self.tick()
        self.tick()
        need(not failed.is_set(), 'native_input_failed')

    def poll_idle(self):
        need(self.protocol_state == 'thread_ready' and not self.turn_submitted, 'native_idle_phase_required')
        try:
            self.tick()
        except Exception as error:
            # tick() calls renew() and only then advances next_renew. Let a lost
            # hub heartbeat leave that renew due, and do not fail the warm process.
            if not _idle_hub_loss(getattr(self, '_idle_handle', None), error):
                raise
            self.idle_hub_loss = True
        need(self.process.poll() is None, 'native_not_running')
        # Drain only already queued frames; no request or model call is issued.
        while True:
            try:
                raw = self.frames.get_nowait()
            except queue.Empty:
                break
            need(raw is not metadata.EOF, 'native_exited_while_idle')
            value = metadata.json_value(raw)
            need('method' in value, 'unsolicited_native_response')
            self._notification(value)
        for event in self.pending_events:
            need(event['method'] in ('thread/started', 'thread/status/changed'), 'native_work_before_prompt')
            params = event['params']
            observed = params.get('thread', {}).get('id') if event['method'] == 'thread/started' else params.get('threadId')
            need(observed == getattr(self, 'expected_thread_id', None), 'native_idle_thread_mismatch')

    def stop_group(self):
        # Kill the group even if its leader already exited: descendants must not
        # refresh credentials after the supervisor commits the file.
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.process.wait(timeout=5)
        for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
            try:
                pipe.close()
            except (OSError, ValueError):
                pass
        for worker in self.readers + self.writers:
            if worker.ident is not None:
                worker.join(timeout=.5)
            need(not worker.is_alive(), 'native_io_stop_uncertain')


@dataclass
class Handle:
    session: object = field(repr=False)
    heartbeat: object = field(repr=False)
    warm_deadline: float
    lease_clock: object = field(default=None, repr=False)
    native: object = field(default=None, repr=False)
    gate: object = field(default=None, repr=False)
    thread_response: dict | None = field(default=None, repr=False)
    state: str = 'preparing'
    model: str = MODEL
    effort: str = EFFORT
    usage: dict | None = None
    preflight: dict = field(default_factory=dict)
    owner_verified: bool = False
    prompt_attempted: bool = False
    consumed: bool = False
    native_stopped: bool = False
    credential_writeback: str = 'pending'
    credential_version_ref: str | None = None
    next_renew: float = 0
    next_quota_refresh: float = 0
    lock: object = field(default_factory=threading.Lock, repr=False)

    @property
    def readiness(self):
        ready = self.state == 'ready' and not self.consumed and time.monotonic() < self.warm_deadline
        return {'provider': 'codex', 'authenticated': self.owner_verified,
                'ready_for_project_prompt': ready, 'model': self.model, 'effort': self.effort,
                'preflight': self.preflight, 'tools_enabled': False, 'full_coding_ready': False,
                'automatic_improvement_ready': False, 'credential_writeback': self.credential_writeback}


def _enter(handle):
    need(isinstance(handle, Handle) and handle.lock.acquire(blocking=False), 'concurrent_provider_operation')


def _idle_hub_loss(handle, error):
    """A missed hub heartbeat is retried only while the warm process is still idle."""
    return (isinstance(handle, Handle) and handle.state == 'ready' and not handle.consumed
            and provider_errors.error_code(error) == 'hub_lease_lost')


def _renew(handle, *, strict=False):
    renewed = handle.lease_clock.renew(handle.session.broker, handle.session.lease, strict=strict)
    need(handle.heartbeat() is True, 'hub_lease_lost')
    handle.next_renew = broker_renew.next_due(renewed, RENEW_SECONDS)
    if renewed is False and handle.native is not None:
        handle.native.renew_retry_at = handle.next_renew
    return renewed


def _quota(value, canonical):
    need(isinstance(value, dict) and metadata.canonical_account_ref(value.get('accountId')) == canonical,
         'native_quota_account_mismatch')
    need(value.get('ordinaryUsageAllowed') is True, 'included_usage_unavailable')
    buckets = value.get('rateLimitsByLimitId')
    if buckets is None:
        legacy = value.get('rateLimits')
        buckets = {legacy.get('limitId') or 'codex': legacy} if isinstance(legacy, dict) else None
    need(type(buckets) is dict and 'codex' in buckets and 1 <= len(buckets) <= 64, 'native_quota_unavailable')
    windows = []
    for key, bucket in buckets.items():
        need(isinstance(key, str) and isinstance(bucket, dict) and bucket.get('limitId') in (None, key),
             'native_quota_schema')
        credits = bucket.get('credits')
        need(type(credits) is dict and credits.get('hasCredits') is False and credits.get('unlimited') is False,
             'native_extra_credit_route_not_disabled')
        balance = credits.get('balance')
        try:
            zero = type(balance) is str and Decimal(balance).is_finite() and Decimal(balance) == 0
        except InvalidOperation:
            zero = False
        need(zero and bucket.get('spendControlReached') is False, 'native_zero_extra_spending_unverified')
        for name in ('primary', 'secondary'):
            window = bucket.get(name)
            if window is None:
                continue
            need(type(window) is dict, 'native_quota_window_invalid')
            duration = window.get('windowDurationMins')
            if not (type(duration) is int and not isinstance(duration, bool)
                    and 1 <= duration <= 525600):
                duration = None
            used = window.get('usedPercent')
            need(used is None or type(used) in (int, float) and math.isfinite(used) and 0 <= used <= 10000,
                 'native_quota_percentage_invalid')
            need(used is None or used < 100, 'included_quota_exhausted')
            reset = window.get('resetsAt')
            need(reset is None or type(reset) is int and reset > time.time(), 'native_quota_reset_stale')
            windows.append({'limit_id': key, 'window': name, 'used_percent': used, 'resets_at': reset,
                            'window_duration_mins': duration})
    known = [row['used_percent'] for row in windows if row['used_percent'] is not None]
    return {'ordinary_usage_allowed': True, 'included_used_percent': max(known) if known else None,
            'windows': windows, 'source': 'same_process_native', 'extra_spending_enabled': False,
            'api_fallback_enabled': False, 'automatic_improvement_ready': False,
            'observed_at': datetime.now(timezone.utc).isoformat()}


def _collect(handle):
    session, native = handle.session, handle.native
    captured = {}
    handle.owner_verified = False

    def rpc(method, params):
        result = native.request(method, params)
        if method in ('account/read', 'account/rateLimits/read'):
            captured[method] = result
        if method == 'account/read':
            identity = metadata.account_metadata(result, session.lease.profile, session.lease.account_ref)
            need(identity['plan_type'] != 'unknown', 'native_plan_unknown')
        if method == 'account/rateLimits/read':
            need(metadata.canonical_account_ref(result.get('accountId')) == session.lease.canonical_account_ref,
                 'native_quota_account_mismatch')
            # Set only after this recheck verified both owner and canonical pool.
            handle.owner_verified = True
        return result

    selection = {'account_ref': session.lease.account_ref, 'cli_model_id': MODEL,
                 'effort': EFFORT, 'billing': 'subscription_included'}
    gate = protocol_gate.PrePromptGate(rpc, selection, config_sha256=CONFIG_SHA,
        provider_account_ref=session.lease.canonical_account_ref, execution_mode='read_only_review')
    preflight = gate.collect()
    need(preflight['requirements_sha256'] == REQUIREMENTS_SHA, 'managed_requirements_changed')
    quota = _quota(captured['account/rateLimits/read'], session.lease.canonical_account_ref)
    handle.usage = quota
    handle.preflight = {**preflight, 'quota': quota, 'native_sha256': metadata.NATIVE_SHA256,
                        'canonical_account_ref': session.lease.canonical_account_ref,
                        'selection_basis': 'exact_reviewed_diverse_model_and_native_maximum_effort',
                        'universal_best_claimed': False}
    handle.next_quota_refresh = time.monotonic() + QUOTA_REFRESH_SECONDS
    handle.gate = gate


def _prepare_home(session):
    for name in ('work', 'tmp'):
        (session.home / name).mkdir(mode=0o700)
    with (session.home / 'config.toml').open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(metadata.CONFIG)
        stream.flush()
        os.fsync(stream.fileno())


def _close(handle):
    if handle.state == 'closed':
        return
    if handle.state == 'quarantined':
        raise LiveCodexError('credential_reconciliation_required')
    handle.state = 'closing'
    try:
        if handle.native is not None:
            handle.native.stop_group()
            handle.native_stopped = True
        need(handle.native_stopped and handle.owner_verified, 'verified_native_owner_and_stop_required')
        version = handle.session.finish(native_stopped=True)
        handle.credential_version_ref = hashlib.sha256(version.encode()).hexdigest()
        handle.credential_writeback = 'committed'
        handle.state = 'closed'
    except Exception:
        handle.state = 'quarantined'
        handle.credential_writeback = 'uncertain'
        try:
            handle.session.broker.quarantine(handle.session.lease, 'provider_refresh_uncertain')
        except Exception:
            pass
        raise LiveCodexError('credential_reconciliation_required') from None


def _fail(handle, error):
    try:
        _close(handle)
    except Exception:
        pass
    # Prefer an already-vetted quota code; otherwise scan any attached payload
    # for fixed exhaustion tokens (never copy native text into the code).
    if (provider_errors.error_code(error) == 'codex_quota_exhausted'
            or _quota_exhausted_signal(getattr(error, 'payload', None))
            or _quota_exhausted_signal(getattr(error, 'native_error', None))):
        code = 'codex_quota_exhausted'
    else:
        code = str(error) if isinstance(error, (LiveCodexError, transport.TransportError,
            metadata.MetadataError, protocol_gate.GateError)) else ''
        code = code if SAFE_CODE.fullmatch(code) else 'codex_live_operation_failed'
    failure = LiveCodexError(code)
    failure.model_call_attempted = handle.prompt_attempted
    failure.credential_writeback = handle.credential_writeback
    raise failure from None


def prepare(session, heartbeat, deadline):
    deadline = _deadline(deadline)
    need(session.state == 'active' and callable(heartbeat) and time.monotonic() < deadline, 'active_session_required')
    lease, broker = session.lease, session.broker
    need(lease.profile in metadata.OWNER_REFS and lease.account_ref == metadata.OWNER_REFS[lease.profile]
         and isinstance(lease.canonical_account_ref, str) and re.fullmatch('[a-f0-9]{64}', lease.canonical_account_ref)
         and lease.execution == broker.execution and lease.execution_uid == broker.execution_uid,
         'exact_broker_profile_execution_required')
    need(not getattr(session, '_codex_live_prepared', False), 'native_session_already_prepared')
    session._codex_live_prepared = True
    handle = Handle(session, heartbeat, time.monotonic() + WARM_SECONDS,
                    lease_clock=broker_renew.LeaseClock.for_lease(session.lease, LiveCodexError, 'codex'))
    try:
        broker.assert_current(lease)
        _prepare_home(session)
        startup_end = min(deadline, time.monotonic() + 120)
        handle.native = WarmRPC(session.home, lambda: _renew(handle),
            timeout_seconds=max(30, math.ceil(startup_end - time.monotonic())), execution_mode='read_only_review')
        handle.native._idle_handle = handle
        handle.native.deadline = startup_end
        handle.native.request('initialize', {'clientInfo': {'name': 'runcrew_codex_live', 'version': '1'}})
        _collect(handle)
        response = handle.native.request('thread/start', handle.gate.thread_request())
        handle.gate.accept_thread(response)
        handle.thread_response = response
        handle.native.expected_thread_id = handle.gate.thread_id
        handle.native.poll_idle()
        _renew(handle)
        handle.state = 'ready'
        return handle
    except Exception as error:
        _fail(handle, error)


def _refresh_quota(handle):
    """Re-measure quota for hub reports. Validation failures keep the last real row.

    WarmRPC.request sets protocol_state='denied' on ANY exception, so a failed
    refresh cannot continue on this handle. A coded LiveCodexError other than a
    hub loss (broker renew rejected/failed, quota) is re-raised unchanged and
    keeps its meaning. Everything else becomes codex_quota_refresh_transport_lost
    (drain): the private transport's TransportError codes (native_rpc_rejected
    for an error frame or a desync, deadline and tick codes), metadata/protocol
    errors, uncoded exceptions, and a hub loss, which cannot be retried on a
    'denied' transport. After a successful request: swallow ordinary
    validation misses; raise codex_quota_exhausted for exhaustion; clear
    owner_verified and raise on native_quota_account_mismatch. Do not restore
    protocol_state. Skip when the warm window is too short or the transport is
    not idle-ready; leave next_quota_refresh due so the next tick retries.
    """
    if time.monotonic() < handle.next_quota_refresh:
        return
    native = handle.native
    if handle.warm_deadline - time.monotonic() < 20:
        return
    # Gate before any request: only real RPC outcomes remain (denied already).
    if getattr(native, 'protocol_state', None) != 'thread_ready':
        return
    # Re-bound after possible _renew spend earlier in this maintain() tick.
    native.deadline = min(handle.warm_deadline, time.monotonic() + 15)
    try:
        rates = native.request('account/rateLimits/read', {})
    except Exception as error:
        # Vetted codex codes (renew rejected/failed, quota) keep their meaning.
        if (isinstance(error, LiveCodexError) and provider_errors.error_code(error) is not None
                and not _idle_hub_loss(handle, error)):
            raise
        # Protocol already 'denied'; drain idle without a controller strike.
        raise LiveCodexError('codex_quota_refresh_transport_lost') from None
    try:
        quota = _quota(rates, handle.session.lease.canonical_account_ref)
        handle.preflight['quota'] = quota
        handle.next_quota_refresh = time.monotonic() + QUOTA_REFRESH_SECONDS
    except Exception as error:
        code = provider_errors.error_code(error)
        if code == 'native_quota_account_mismatch':
            # Idle owner-change policy: never commit after a failed owner proof.
            handle.owner_verified = False
            raise
        # included_quota_exhausted / included_usage_unavailable → quota park.
        if code in ('included_quota_exhausted', 'included_usage_unavailable'):
            raise LiveCodexError('codex_quota_exhausted') from None
        if code is not None and code.endswith('_quota_exhausted'):
            raise
        # Keep last real observed_at; back off so a persistent validation miss
        # does not re-request on every idle poll.
        handle.next_quota_refresh = time.monotonic() + QUOTA_RETRY_SECONDS


def maintain(handle):
    _enter(handle)
    try:
        need(handle.state == 'ready' and not handle.consumed, 'prepared_handle_required')
        need(time.monotonic() < handle.warm_deadline, 'warm_session_expired')
        # A failed renew attempt can take about 30s, and NativeRPC.tick checks
        # the deadline after renew (native_metadata_timeout).
        handle.native.deadline = min(handle.warm_deadline, time.monotonic() + 45)
        skipped_refresh_for_hub_loss = False
        handle.native.idle_hub_loss = False
        try:
            handle.native.poll_idle()
            if time.monotonic() >= handle.next_renew:
                _renew(handle)
        except Exception as error:
            # Idle renew only reaches the hub. A dropped heartbeat is retried
            # on the next maintain; closing the native process fails the warm run.
            # _renew sets next_renew only after the heartbeat returns, so a
            # miss stays due. A non-vetted broker failure still fails below.
            if not _idle_hub_loss(handle, error):
                raise
            # Skip refresh: a due renew inside rateLimits would fail again and
            # turn a retried hub blip into a transport-lost drain.
            skipped_refresh_for_hub_loss = True
        if not skipped_refresh_for_hub_loss and not handle.native.idle_hub_loss:
            _refresh_quota(handle)
        return handle.readiness
    except Exception as error:
        _fail(handle, error)
    finally:
        handle.lock.release()


def execute(handle, prompt, task_deadline, *, task_kind='project'):
    _enter(handle)
    try:
        need(task_kind == 'project', 'automatic_improvement_not_enabled')
        need(handle.state == 'ready' and not handle.consumed, 'single_project_prompt_required')
        task_deadline = _deadline(task_deadline)
        remaining = task_deadline - time.monotonic()
        need(EXECUTE_WARM_FLOOR <= remaining <= 900, 'task_deadline_out_of_bounds')
        need(time.monotonic() < handle.warm_deadline, 'warm_session_expired')
        need(isinstance(prompt, str) and 0 < len(prompt.encode()) <= MAX_PROMPT, 'bounded_prompt_required')
        handle.consumed = True
        handle.state = 'executing'
        handle.native.deadline = min(task_deadline - FINALIZE_RESERVE, time.monotonic() + NATIVE_SECONDS)
        handle.session.broker.assert_current(handle.session.lease)
        _renew(handle, strict=True)
        _collect(handle)
        handle.gate.accept_thread(handle.thread_response)
        handle.gate._fresh()
        handle.native.poll_idle()
        payload = {'threadId': handle.gate.thread_id, 'input': [{'type': 'text', 'text': prompt}],
                   'model': MODEL, 'effort': EFFORT, 'sandboxPolicy': dict(handle.gate.sandbox)}
        # The controller's durable task intent precedes this API. An exclusive
        # local marker additionally forbids a duplicate attempt in this execution.
        prompt_sha = hashlib.sha256(prompt.encode()).hexdigest()
        with (handle.session.home / 'live-prompt-intent.json').open('x', encoding='utf-8') as stream:
            json.dump({'prompt_sha256': prompt_sha, 'model': MODEL, 'effort': EFFORT}, stream)
            stream.flush(); os.fsync(stream.fileno())
        handle.prompt_attempted = True
        initial = handle.native.request('turn/start', payload).get('turn')
        need(isinstance(initial, dict) and initial.get('status') == 'inProgress', 'native_turn_ack_invalid')
        outcome = transport.TurnResult(handle.gate.thread_id, initial.get('id'), MODEL,
                                       execution_mode='read_only_review')
        while not outcome.complete:
            event = handle.native.next_event()
            # Classify only failure/error frames before transport flattens them.
            need(not _midturn_quota_exhausted(event), 'codex_quota_exhausted')
            outcome.consume(event)
        for event in handle.native.finish_turn():
            need(not _midturn_quota_exhausted(event), 'codex_quota_exhausted')
            outcome.consume(event)
        need(handle.native.clean_shutdown, 'native_clean_exit_unconfirmed')
        result = outcome.result()
        need(0 < len(result['output'].encode()) <= MAX_ANSWER, 'answer_exceeds_hub_limit')
        _close(handle)
        return {'provider': 'codex', 'text': result['output'], 'model': MODEL, 'effort': EFFORT,
                'usage': result['usage'], 'preflight': handle.preflight,
                'review_sha256': hashlib.sha256(result['output'].encode()).hexdigest(),
                'prompt_sha256': prompt_sha, 'prompt_sent_once_by_wrapper': True,
                'prompt_correlation': 'matching_thread_turn_and_completed_final_item',
                'native_stopped': True, 'credential_writeback': 'committed',
                'credential_version_ref': handle.credential_version_ref, 'automatic_retry': False,
                'model_execution_attested': False, 'tools_enabled': False, 'full_coding_ready': False}
    except Exception as error:
        _fail(handle, error)
    finally:
        handle.lock.release()


def close(handle):
    _enter(handle)
    try:
        _close(handle)
    finally:
        handle.lock.release()
