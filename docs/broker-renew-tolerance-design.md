> **Base update (2026-09-23 ~21:05Z):** implement on `alpha/live-loop-provider-error-codes` at **05abe14**. Since the design base (2cb35c6) the providers gained: 0b6463a/41a1dc8 claude quota and rate-limit mapping (exit 75), 3e9c1d7 claude renew during execute, 7b79190 mid-turn quota mapping in codex.py, copilot.py and _grok_protocol.py, and d29c8ec CLI_NAME, CLI_VERSION and TOOLS_POLICY constants. live_loop.py gained telemetry spans, a worker_stopping check and a skip for revoked leases. The renew call sites are unchanged. Keep every quota and rate-limit mapping and the exit-75 path intact.

# Bounded broker-renew tolerance: design for all five runcrew worker adapters

Assigned by Alpha to Light (item 7). Designed by Light's workflow on 2cb35c6. Implement on top of **0b6463a** (branch alpha/live-loop-provider-error-codes).

> The design's 3-lens review did not run (spend limit). Light reviews the implementation instead. The constraints marked BINDING come from the lead and are not up for debate.

## BINDING rules (from Alpha)

- Tolerance lives INSIDE each adapter. live_loop.maintain_fault retries only vetted _IDLE_RETRY_CODES.
- Tolerate only MutationUncertain, plus BoundaryError with transient codes, from broker.renew. Keep the renew due. Retry on the next tick.
- Once the lease has less than 60 s left (last confirmed renew start + 240 - 60 = 180 s), raise `<provider>_broker_renew_failed`. This is a FAIL code, never a retry or drain code.
- NEVER tolerate Conflict (a 409 can be a real fence loss).
- Supersedes copilot_idle_renew_failed (from 2cb35c6).
- On 0b6463a, keep the new claude_quota_exhausted mapping and exit-75 behaviour intact.

## Tolerated vs not

Tolerated. The call returns False and the anchor is left alone:
(1) agent_hub.cloud_credential_broker.MutationUncertain, any code. On renew the codes are 'broker_http_outcome_uncertain' and 'broker_acknowledgement_invalid'. They cover every exchange/decode failure on the broker POST: DNS, TLS, connect, the 10 s socket timeout, the 15 s watchdog, a dropped 9th connection, front-end 429/5xx or HTML, the broker's 503 broker_mutation_uncertain or broker_unavailable, and a bad ack. The outcome is unknown and renew is safe to repeat. A lost success can only have extended the lease, so the anchor stays where it is.
(2) agent_hub.credential_broker_service.BoundaryError, only when str(error) is 'transport_failed', 'transport_limit' or 'metadata_identity_unavailable'. BrokerHTTPClient._id_token raises these outside _post's try block, before any broker request, so nothing was mutated.

Narrowing BoundaryError is required. The renew path also raises definitive local BoundaryErrors: 'credential_fence_lost' and 'request_invalid' from _lease(), and 'request_invalid' and 'message_too_large' from encode()/require(action). These never heal.

Never tolerated. Any other BrokerError raises <provider>_broker_renew_rejected at once:
- Conflict('broker_operation_rejected') for any 409. This covers fence loss, credential_lease_not_active and the CAS conflict. The server also maps transient upstream read failures to 409, and the lead's rule keeps them fatal.
- Plain BrokerError('broker_request_denied') for 400/401/403, and 'exact_secret_version_required'.
- The non-transient BoundaryError codes above.

Not caught, propagate unchanged: non-broker exceptions (adapter bugs keep their existing fallbacks) and BaseException, including KeyboardInterrupt from entrypoint's SIGTERM handler.

Tolerance is tested with isinstance against the exact class objects BrokerHTTPClient raises (both modules are already imported by dynamic_broker in every image). Conflict, BoundaryError and MutationUncertain are sibling BrokerError subclasses, so a Conflict can never be tolerated.

## Bound

Definition. TOLERANCE_SECONDS = LEASE_SECONDS - CLOSE_RESERVE_SECONDS = 240 - 60 = 180. After a tolerated-class failure returns, the helper checks clock() - renewed_at > 180. If true, or if the call was strict, it raises <provider>_broker_renew_failed. Otherwise it returns False. At exactly 180 s the failure is still tolerated; above 180 s it fails. The time is measured after the failed attempt, so the attempt's own duration counts (up to about 30 s: 15 s ID-token watchdog plus 15 s POST watchdog).

Clock. time.monotonic() in the worker, which every adapter already uses. The helper takes an injectable clock for tests. The live_loop clock is not involved. The server stamps lease_until_ms with its own wall clock plus lease_seconds, so only durations are compared.

Where lease_seconds comes from. The worker cannot observe it: it is not on Lease, not in the acquire reply ({lease:{fence,version,lease_id}, credential_b64}), not in the renew reply ({ok:true}), and not in live-worker.json. So broker_renew.LEASE_SECONDS = 240 mirrors dynamic_broker.Policy.lease_seconds. A test in test_live_loop.py pins it to Policy's dataclass default. The deployed live-broker.json sets lease_seconds per policy explicitly (range 30..600), and the operator must confirm it is 240. The 180 defaults in Binding and CloudCredentialBroker, and grok's '180s lease' docstring, are not the live value.

Anchor, also called renewed_at. On success it is the monotonic time taken just before the broker.renew call that returned OK. The broker writes lease_until after the request starts, so lease_until >= renewed_at + 240, which is conservative. A MutationUncertain never moves it.

First-renew baseline. entrypoint.py (COMMON, ships in all five packs) takes acquire_started = time.monotonic() immediately before broker.acquire(...) and calls broker_renew.record_acquire_start(lease.lease_id, acquire_started). LeaseClock.for_lease(session.lease, ...) uses that value, so the baseline is lease acquisition, which is strictly conservative. When nothing was recorded (unit tests, any other harness) it falls back to clock() at prepare() entry, which is optimistic by the acquire-to-prepare gap (typically about 1 s, worst about 45 s). The first renew is not strict: grok and copilot pumps can renew during a slow startup, and a claude preflight can take up to 180 s with no renew. If that preflight uses up the window, the first transient failure fails at once, which is correct because the lease really is near its end.

Retry timing. A tolerated failure schedules the next attempt at clock_after_attempt + RETRY_SECONDS (10). In idle this is the next maintain tick, because production poll_seconds is 10 and report and claim add latency. In native pumps (4-50 Hz) it stops the hammering, and 10 s is at least the client socket timeout. On success the adapter's normal cadence is unchanged: 20 s (grok, copilot, claude, codex handle), 25 s (codex NativeRPC) or 8 s (cursor).

Worst-case detection is about 180 s + one retry gap (10-25 s) + one attempt (<=30 s, DNS unbounded), roughly 235 s after the last confirmed renew started. Typical is about 190-200 s. See the risks.

## Shared helper (location, packaging, full source)

LOCATION AND PACKAGING
- New top-level module live-worker-runtime/broker_renew.py, imported as `import broker_renew`, the same way providers import provider_errors. entrypoint.py puts live/ first on sys.path.
- live-image-workers/refresh_packs.py:30: add 'broker_renew.py' to COMMON so it ships in all five packs.
- live-image-workers/README.md: add it to the live/ file list.
- tests/test_refresh_packs.py::test_built_packs_keep_every_import_pin_and_tag already follows imports from providers/<p>.py. It fails, naming broker_renew, if COMMON is not updated.
- Do not put the helper in agent-hub/agent_hub: packs take agent_hub/ unchanged from the base pack.
- The helper defines no exception class, so the static ProviderErrorTests scan of providers/*.py is unaffected. It always raises the adapter's own ProviderCodeError class. This is required because copilot's except blocks re-raise only CopilotError, and codex's _fail passes through only LiveCodexError.

ENTRYPOINT (COMMON; ships now; harmless for adapters that do not read it yet)
- Add `import time` and `import broker_renew` next to the live_loop import.
- Replace `lease = broker.acquire(broker.execution, request_id)` with:
    acquire_started = time.monotonic()
    lease = broker.acquire(broker.execution, request_id)
    broker_renew.record_acquire_start(lease.lease_id, acquire_started)

FULL SOURCE: live-worker-runtime/broker_renew.py
(Prototyped in memory against the real agent_hub classes. Every case behaves as described.)

"""Bounded tolerance for transient credential-lease renew failures.

Shared by all five provider adapters; refresh_packs.COMMON ships it in every
pack's live/. The worker cannot read the broker's lease length (it is not on
Lease, the renew reply or live-worker.json), so LEASE_SECONDS mirrors
dynamic_broker.Policy.lease_seconds and a test pins it.

Tolerated: MutationUncertain (outcome unknown: client timeout, 5xx, lost or
invalid ack) and BoundaryError from the metadata ID-token fetch, which fails
before any broker request. Both are tolerated only until TOLERANCE_SECONDS have
passed since the start of the last renew the broker confirmed. Every other
broker error fails at once, including Conflict (a 409 can be a real fence
loss). Codes are raised as the adapter's own ProviderCodeError class and are
never live_loop retry or drain codes.
"""
import time

import provider_errors
from agent_hub.cloud_credential_broker import BrokerError, MutationUncertain
from agent_hub.credential_broker_service import BoundaryError

LEASE_SECONDS = 240          # dynamic_broker.Policy.lease_seconds; live-broker.json sets it per policy
CLOSE_RESERVE_SECONDS = 60   # left for close(): native stop, assert_current, commit, release
TOLERANCE_SECONDS = LEASE_SECONDS - CLOSE_RESERVE_SECONDS
RETRY_SECONDS = 10           # after a tolerated failure; >= the client's 10 s socket timeout
# Raised by BrokerHTTPClient._id_token before any broker request. The other
# BoundaryError codes on the renew path (credential_fence_lost, request_invalid,
# message_too_large) are deterministic local checks and never heal.
TRANSIENT_BOUNDARY_CODES = frozenset(('transport_failed', 'transport_limit', 'metadata_identity_unavailable'))
PROVIDERS = frozenset(('grok', 'codex', 'copilot', 'claude', 'cursor'))

_acquire_started = {}


def record_acquire_start(lease_id, started):
    """entrypoint only: time.monotonic() taken just before broker.acquire."""
    _acquire_started[lease_id] = started


def transient(error):
    """True only for broker-client failures that may heal on a later attempt."""
    if isinstance(error, MutationUncertain):
        return True
    return isinstance(error, BoundaryError) and str(error) in TRANSIENT_BOUNDARY_CODES


def next_due(renewed, cadence, clock=time.monotonic):
    """Next renew time. Call only after renew returned; None (legacy callbacks) counts as success."""
    return clock() + (RETRY_SECONDS if renewed is False else cadence)


class LeaseClock:
    """The last confirmed renew of one lease, and the renew-failure policy."""

    def __init__(self, error_class, provider, anchor, *, clock=time.monotonic):
        if not (isinstance(error_class, type) and issubclass(error_class, provider_errors.ProviderCodeError)
                and provider in PROVIDERS and type(anchor) in (int, float)):
            raise TypeError('lease_clock_invalid')
        self.error_class, self.clock = error_class, clock
        self.failed_code = provider + '_broker_renew_failed'
        self.rejected_code = provider + '_broker_renew_rejected'
        self.renewed_at = anchor   # monotonic start of the last confirmed renew, or of the acquire
        self.failures = 0          # tolerated failures since then

    @classmethod
    def for_lease(cls, lease, error_class, provider, *, clock=time.monotonic):
        """Anchor at the recorded acquire start, else now (prepare entry; tests)."""
        anchor = _acquire_started.get(getattr(lease, 'lease_id', None))
        return cls(error_class, provider, clock() if anchor is None else anchor, clock=clock)

    @property
    def degraded(self):
        return self.failures > 0

    def renew(self, broker, lease, *, strict=False):
        """Renew once. True: confirmed. False: tolerated; retry after RETRY_SECONDS.

        Raises error_class(<provider>_broker_renew_rejected) for any other
        BrokerError, and error_class(<provider>_broker_renew_failed) for a
        transient failure when strict or past the window. Non-broker exceptions
        and BaseException (SIGTERM's KeyboardInterrupt) propagate unchanged.
        """
        started = self.clock()
        try:
            broker.renew(lease)
        except BrokerError as error:
            if not transient(error):
                raise self.error_class(self.rejected_code) from None
            self.failures += 1
            # Measured after the failed attempt, so its duration (up to ~30 s) counts.
            if strict or self.clock() - self.renewed_at > TOLERANCE_SECONDS:
                raise self.error_class(self.failed_code) from None
            return False
        # The broker stamps lease_until after the request starts: conservative.
        self.renewed_at, self.failures = started, 0
        return True

    def check(self):
        """Backstop for paths that can skip renew attempts (cursor idle_pump)."""
        if self.clock() - self.renewed_at > TOLERANCE_SECONDS:
            raise self.error_class(self.failed_code) from None

HOW EVERY ADAPTER USES IT
- Handle gains `lease_clock: object = field(default=None, repr=False)`. prepare() sets it to broker_renew.LeaseClock.for_lease(session.lease, <AdapterError>, '<provider>') when it builds the Handle, before any native process or pump exists. A None default fails closed.
- `_renew(handle[, strict])` becomes `renewed = handle.lease_clock.renew(handle.session.broker, handle.session.lease[, strict=strict])`. It then runs the existing `need(handle.heartbeat() is True, '<hub-loss code>')`, which always runs, including after a tolerated failure. Finally it returns renewed.
- Every place that sets `next_renew = now + cadence` after a renew becomes `next_renew = broker_renew.next_due(<renew call>, cadence)`. A raise still skips the assignment, so the renew stays due, and the heartbeat-lost contract from 4640c63 holds.

TEST RUN (local): from live-worker-runtime, run `PYTHONPATH="<repo>/agent-hub;." python -B -m unittest test_live_loop test_copilot_provider tests.test_grok`, plus `python -B -m unittest tests.test_refresh_packs` from the repo root.

## Per-adapter changes

### grok (live-worker-runtime/providers/grok.py + providers/_grok_protocol.py) (NOW on 2cb35c6): fail code `grok_broker_renew_failed (transient failure past 180 s, or the strict pre-prompt renew failed) and grok_broker_renew_rejected (Conflict, denied, or a local BoundaryError). Both are raised as _grok_protocol.NativeError(ProviderCodeError), `from None`. Neither is in _IDLE_RETRY_CODES or _IDLE_DRAIN_CODES. grok_hub_heartbeat_lost is unchanged and stays a retry code.`

grok.py:
(1) Add `import broker_renew` before the relative import at :19. Amend the docstring at :3-4 to read 'No commissioning room, browser receipt or automatic task claim is imported; broker exception types come only through broker_renew.'
(2) Handle (:184-196): add `lease_clock: object = field(default=None, repr=False)`.
(3) Replace _renew (:208-210) with:
    def _renew(handle):
        renewed = handle.lease_clock.renew(handle.session.broker, handle.session.lease)
        need(handle.heartbeat() is True, 'grok_hub_heartbeat_lost')
        return renewed
(4) prepare :241 becomes `handle = Handle(session=session, heartbeat=heartbeat, lease_clock=broker_renew.LeaseClock.for_lease(session.lease, NativeError, 'grok'))`. This runs before Native starts, so startup pump renews already have an anchor.
(5) maintain: update the docstring at :281 to say the lease is 240 s and transient renew failures are tolerated for 180 s. Lines :284-286 become:
    if time.monotonic() >= handle.native.next_renew:
        handle.native.next_renew = broker_renew.next_due(_renew(handle), 20)
(6) execute: insert between :331 (_fresh_metadata) and :332 (heartbeat):
    if handle.lease_clock.degraded:  # never send the prompt on an unconfirmed lease
        handle.lease_clock.renew(handle.session.broker, handle.session.lease, strict=True)
        handle.native.next_renew = time.monotonic() + 20

_grok_protocol.py:
- Add `import broker_renew` after :4.
- Line :117 becomes `if now>=self.next_renew:self.next_renew=broker_renew.next_due(self.renew(),20)`. The argument is evaluated first, so the time is read after renew returns. A raise skips the assignment. `lambda: None` renewers keep the 20 s cadence.

No live_loop change.

Prototyped in memory: all 22 existing test_grok tests pass unchanged. One MutationUncertain gives readiness, events [] and a retry in 10 s or less, and a success heals it. Past the window it raises grok_broker_renew_failed and maintain_fault returns 'fail'. A Conflict raises grok_broker_renew_rejected and 'fail'. close() then records ['stop','commit-release'].

Code change today: a Conflict or a denied renew now surfaces as grok_broker_renew_rejected instead of native_or_connection_failure.

Tests:
- tests/test_grok.py preamble: put the runtime root and the sibling Path(__file__).resolve().parents[2]/'agent-hub' on sys.path, as tests/test_claude.py:13-17 does but without the stale C:/Users/9 path. Import broker_renew, live_loop, provider_errors, MutationUncertain, Conflict and BrokerError from agent_hub.cloud_credential_broker, and BoundaryError from agent_hub.credential_broker_service.
- test_transient_idle_renew_failure_keeps_session_and_retries: subTest over MutationUncertain('broker_http_outcome_uncertain') and BoundaryError transport_failed, transport_limit and metadata_identity_unavailable. With next_renew due, maintain() returns ready_for_project_prompt True. Also assert: fixture.events == []; finish and quarantine are not called; the heartbeat is called once; lease_clock.renewed_at is unchanged and degraded is True; now < next_renew <= now + broker_renew.RETRY_SECONDS. Then clear side_effect and make the renew due again: renew.call_count == 2, degraded is False, renewed_at has advanced and next_renew is about now+20.
- test_renew_rejections_fail_idle_at_once: with a fresh anchor, check Conflict('broker_operation_rejected'), BrokerError('broker_request_denied'), BoundaryError('credential_fence_lost') and BoundaryError('request_invalid'). Each gives assertRaisesRegex(g.NativeError, '^grok_broker_renew_rejected$') on the first call, with __cause__ None, __suppress_context__ True, no broker text in the message, and maintain_fault == 'fail'. maintain does not close (events == []). A later g.close gives ['stop','commit-release'].
- test_renew_failure_past_window_fails_with_vetted_code: set renewed_at = monotonic() - broker_renew.TOLERANCE_SECONDS - 1 and raise MutationUncertain('private detail'). Expect '^grok_broker_renew_failed$', with 'private' absent. provider_errors.error_code gives the code, maintain_fault == 'fail', and the code is in neither live_loop._IDLE_RETRY_CODES nor _IDLE_DRAIN_CODES.
- test_tolerated_renew_failure_still_heartbeats: MutationUncertain with heartbeat False raises '^grok_hub_heartbeat_lost$'. maintain_fault == 'retry', next_renew is unchanged (still due), and the native process is still alive.
- test_prepare_anchors_lease_clock: without a recorded start, before <= renewed_at <= after around prepare(). With patch.dict(broker_renew._acquire_started, {'x'*32: t0}) and lease_id 'x'*32, renewed_at == t0. session.broker.renew is not called by prepare.
- TransportSmoke test_pump_rearms_after_renew_outcome: drive the real wire.Native with next_renew due. A renew returning False sets next_renew - now to about RETRY_SECONDS, and next_renew is >= the monotonic time the renew recorded plus RETRY_SECONDS. Returning True or None sets it to about 20. A raising renew propagates and leaves next_renew unchanged.
- Fixture(pump=True): FakeNative.request mirrors :117, as in test_copilot_provider.py:77-80. Then test_transient_renew_failure_during_turn_keeps_the_turn: renew side_effect [None, MutationUncertain, None...] during session/prompt; the answer is returned, there is exactly one session/prompt, events == ['stop','commit-release'] and the heartbeat ran after the tolerated failure.
- test_renew_failure_past_window_during_turn_fails_with_vetted_code: backdate renewed_at and fail the renew inside the prompt pump. execute raises grok_broker_renew_failed, close ran, and there is no second prompt. test_conflict_during_turn_is_rejected_at_once gives grok_broker_renew_rejected.
- test_degraded_lease_gets_strict_renew_before_prompt: make lease_clock degraded (one tolerated idle failure). Success variant: execute calls broker.renew once more before session/prompt, and the answer is returned. Failure variant (MutationUncertain again): '^grok_broker_renew_failed$', no session/prompt in fixture.calls, and events ['stop','commit-release']. Also, when not degraded, execute makes no extra renew call (success path unchanged).
- test_startup_pump_tolerates_then_fails_past_window: with the pump fixture during prepare, one transient failure lets prepare succeed. With an anchor older than the window, prepare raises grok_broker_renew_failed and events == ['stop','commit-release'].
- test_worker_survives_one_idle_blip_and_fails_after_window: drive live_loop.Worker(Settings('grok', ...)) with the real grok module and a clock of time.monotonic() plus an offset (the test_live_loop Clock at 100 trips grok_deadline_invalid). One MutationUncertain gives outcome idle_drained and last_exit 0. A persistent failure with an aged anchor gives outcome failed, error_code grok_broker_renew_failed, last_exit 1, one failing maintain and no retry.
- Keep test_idle_maintain_renews_and_heartbeat_failure_aborts (:154) unchanged; it passes in the prototype.

### copilot (live-worker-runtime/providers/copilot.py; tests in test_copilot_provider.py) (NOW on 2cb35c6): fail code `copilot_broker_renew_failed and copilot_broker_renew_rejected, as CopilotError `from None`, both fail codes. copilot_idle_maintain_failed replaces copilot_idle_renew_failed as the fallback for opaque idle errors; the lead may pick another name, but it must be a fail code. copilot_hub_heartbeat_lost is unchanged (retry, handle kept open).`

(1) Add `import broker_renew` next to `import provider_errors` (:24). Add `RENEW_SECONDS = 20`. Update the comment at :30-33: copilot_broker_renew_failed and copilot_broker_renew_rejected are fail codes and must never join IDLE_SESSION_LOSS. If they did, the copilot_warm_session_lost idle-drain path would exit 0.
(2) Native.__init__ :194 becomes `self.next_renew = time.monotonic()+RENEW_SECONDS`.
(3) Native._tick :265-267 becomes:
    if time.monotonic() >= self.next_renew:
        self.next_renew = broker_renew.next_due(self.renew(), RENEW_SECONDS)
A raise leaves the renew due, which keeps the 4640c63 contract. A tolerated failure re-arms at +10 s, so frame() and pump() cannot hammer the broker at 4 Hz.
(4) Handle (:441-454): add `lease_clock: object = field(default=None, repr=False)`.
(5) Replace _renew (:465-467) with:
    def _renew(handle):
        renewed = handle.lease_clock.renew(handle.session.broker, handle.session.lease)
        need(handle.heartbeat() is True, 'copilot_hub_heartbeat_lost')
        return renewed
(6) prepare :528 becomes `handle = Handle(session, heartbeat, deadline, lease_clock=broker_renew.LeaseClock.for_lease(session.lease, CopilotError, 'copilot'))`.
(7) maintain: rewrite the docstring (:575-591). Transient renew failures are absorbed in Native._tick. Exhausted or rejected renews arrive as CopilotError codes and go through the existing close() then re-raise path. Line :609 (superseded) becomes `raise CopilotError('copilot_idle_maintain_failed') from None`. Only opaque non-broker, non-OSError errors reach it now.
(8) execute: insert before :623 (assert_current):
    if handle.lease_clock.degraded:  # never send the prompt on an unconfirmed lease
        handle.lease_clock.renew(handle.session.broker, handle.session.lease, strict=True)
        handle.native.next_renew = time.monotonic() + RENEW_SECONDS
The existing assert_current and heartbeat stay.

No change is needed in prepare or execute beyond this. A renew failure during startup or a turn now surfaces as copilot_broker_renew_failed or copilot_broker_renew_rejected through the existing isinstance(CopilotError) re-raise, instead of copilot_prepare_requires_reconciliation or copilot_task_outcome_requires_reconciliation.

Prototyped in memory: 34 of 37 existing tests pass. The 3 failures are the tests this design replaces. A tolerated idle failure keeps the session. With a degraded clock, execute strictly renews and commits. A failing gate sends 0 prompts and raises copilot_broker_renew_failed.

Tests:
- Harness: FakeNative.maintain (:76-80) calls c.Native._tick(self) instead of its hand copy. FakeNative.request and next_event also call c.Native._tick(self) first; next_renew defaults to +3600, so existing tests are unaffected. Import Conflict from agent_hub.cloud_credential_broker, BoundaryError from agent_hub.credential_broker_service, and broker_renew.
- Replace test_broker_renew_errors_close_as_idle_renew_failed (:278-293) with test_transient_renew_failure_keeps_idle_session_and_retries. subTest over MutationUncertain('private ...') and BoundaryError transport_failed and metadata_identity_unavailable. maintain() returns readiness with ready_for_project_prompt True. Also assert: handle not finished or close_failed; handle.native is native and still alive; renew.call_count == 1; heartbeat called; finish and quarantine not called; renewed_at unchanged; now < native.next_renew <= now + RETRY_SECONDS. Then clear the error and make the renew due: count 2, renewed_at advanced, degraded False, next_renew about now+20.
- test_renew_tolerance_exhausted_fails_with_vetted_code: renewed_at = monotonic() - TOLERANCE_SECONDS - 1 and MutationUncertain('private detail'). Expect '^copilot_broker_renew_failed$', 'private' absent, __cause__ None and __suppress_context__ True. The handle is finished, native is None and native_stopped; session.finish is called once and quarantine is not; no session.send.
- test_conflict_denied_and_local_boundary_are_rejected_at_once: Conflict, BrokerError('broker_request_denied') and BoundaryError('credential_fence_lost') with a fresh anchor. The first renew raises '^copilot_broker_renew_rejected$', renew.call_count == 1 and the handle is closed.
- test_tolerated_renew_still_heartbeats_and_lost_heartbeat_keeps_renew_due: MutationUncertain with heartbeat False raises '^copilot_hub_heartbeat_lost$'. The handle stays open, native.next_renew == due and the heartbeat was called.
- test_success_anchor_is_attempt_start_and_uncertain_never_moves_it: the renew side_effect records monotonic() on entry, and renewed_at <= that time. A later MutationUncertain leaves renewed_at unchanged.
- test_prepare_anchors_lease_clock: without a record, the anchor is at prepare entry. With patch.dict(broker_renew._acquire_started) and a lease_id, the anchor is the recorded value.
- Transport (bare_native) test_tick_throttles_retry_after_tolerated_renew: with renew = Mock(return_value=False) and next_renew due, native.maintain() calls renew once. An immediate second maintain() does not call it. next_renew is in (now, now+RETRY_SECONDS]. With return_value None, next_renew is about now+20. Keep test_native_maintain_leaves_renew_due_when_renew_raises (:422-433) unchanged.
- test_execute_turn_survives_one_uncertain_renew: renew is due during the turn and side_effect is [MutationUncertain('x'), None]. execute returns the answer, credential_writeback is 'committed', session.finish is called once and session.send is sent once.
- test_execute_renew_past_window_is_broker_renew_failed: with a stale anchor and MutationUncertain during the turn, expect '^copilot_broker_renew_failed$', not copilot_task_outcome_requires_reconciliation. The handle is finished and there is at most 1 send.
- test_degraded_lease_gets_strict_renew_before_send: after one tolerated idle failure, execute renews before assert_current. On success there is exactly 1 send. On failure: '^copilot_broker_renew_failed$', 0 sends, handle finished. When not degraded there is no extra renew call.
- Update test_opaque_idle_failure_is_idle_renew_failed_not_session_loss (:306-314) to expect '^copilot_idle_maintain_failed$', still closed, with no leak.
- Keep test_hub_heartbeat_lost_keeps_native_session_and_retries_renew (:255-276) unchanged; it passes in the prototype.

### claude (live-worker-runtime/providers/claude.py) (LATER, on a head that contains origin/cursor/fix-claude-renew-during-execute 52345d1. That change moves renew into Native.tick for idle and in-flight prompts. Re-anchor the line numbers.): fail code `claude_broker_renew_failed and claude_broker_renew_rejected as claude NativeError(ProviderCodeError, RuntimeError), both fail codes. claude_hub_heartbeat_lost stays a retry code. Under 52345d1, raw broker codes such as NativeError('broker_http_outcome_uncertain') or 'claude_lease_renew_failed' no longer come from renew.`

(1) Add `import broker_renew`. Handle: add `lease_clock` (field default None). prepare: `Handle(session=session, heartbeat=heartbeat, lease_clock=broker_renew.LeaseClock.for_lease(session.lease, NativeError, 'claude'))`. Preflight has no renew, so the anchor from entrypoint's acquire start matters most here.
(2) _renew:
    renewed = handle.lease_clock.renew(handle.session.broker, handle.session.lease)
    need(handle.heartbeat() is True, 'claude_hub_heartbeat_lost')
    return renewed
(3) Native.tick (52345d1 :191-203): inside the existing try, `renewed = self.renew()`. After the except, `self.next_renew = broker_renew.next_due(renewed, 20)` replaces `time.monotonic() + 20`. The except wrapper still re-raises vetted codes, and the helper's codes are vetted. Its str(error) fallback no longer sees broker errors from renew. A tolerated failure re-arms at +10 s, which limits how long frames go undrained (the _Frames queue is 32, put_nowait).
(4) execute: insert before the heartbeat and `handle.attempted = True`:
    if handle.lease_clock.degraded:
        handle.lease_clock.renew(handle.session.broker, handle.session.lease, strict=True)
        handle.native.next_renew = time.monotonic() + 20
(5) If the head does NOT contain 52345d1: apply the same maintain pattern as grok (`next_renew = next_due(_renew(handle), 20)`) and the same gate. Then flag that a turn has no renew or task heartbeat at all, which is a separate fix.

Tests:
- tests/test_claude.py (run with PYTHONPATH=<repo>/agent-hub). test_transient_idle_renew_failure_keeps_session_and_retries: MutationUncertain and BoundaryError transport_failed / metadata_identity_unavailable. maintain returns readiness; events == []; renewed_at unchanged; next_renew in (now, now+RETRY_SECONDS]; heartbeat called; finish and quarantine not called. After success, degraded is False and next_renew is about +20.
- test_renew_failure_past_window_raises_claude_broker_renew_failed: with a backdated anchor, NativeError 'claude_broker_renew_failed', maintain_fault 'fail', __suppress_context__ True, maintain does not stop native, and c.close then gives ['stop','commit-release'].
- test_conflict_and_denied_are_rejected_at_once: Conflict, BrokerError('broker_request_denied') and BrokerError('credential_lease_not_active') give 'claude_broker_renew_rejected' on the first call, and next_renew stays due.
- test_tolerated_failure_still_heartbeats: MutationUncertain with heartbeat False gives claude_hub_heartbeat_lost and maintain_fault 'retry'.
- test_tick_during_prompt_tolerates_one_blip: a prompt_hook makes the renew due and raises one MutationUncertain. The answer is returned, there is 1 prompt and the heartbeat ran. Variant with a stale anchor: claude_broker_renew_failed, then close.
- test_degraded_lease_gets_strict_renew_before_prompt: on failure, fixture.prompts == [], claude_broker_renew_failed and events ['stop','commit-release']. On success the prompt is sent. When not degraded, renew is not called by execute.
- test_prepare_anchors_lease_clock_without_renewing: the anchor is the recorded acquire start, else prepare entry, and session.broker.renew.assert_not_called().
- Integration: live_loop.Worker(Settings('claude', ...)) with the real maintain. The sequence [MutationUncertain, BoundaryError, None] gives idle_drained and exit 0. An aged anchor gives failed, claude_broker_renew_failed, exit 1, one maintain call.

### codex (live-worker-runtime/providers/codex.py) (LATER, on the newer head (Cursor is editing codex.py). Re-anchor the line numbers.): fail code `codex_broker_renew_failed and codex_broker_renew_rejected as LiveCodexError(ProviderCodeError, RuntimeError), both fail codes. Today every broker renew error becomes codex_live_operation_failed. hub_lease_lost stays a retry code.`

(1) Add `import broker_renew`. Handle: add lease_clock. prepare: create the Handle with `lease_clock=broker_renew.LeaseClock.for_lease(session.lease, LiveCodexError, 'codex')` before WarmRPC is built.
(2) Replace _renew (:198-201) with:
    def _renew(handle, *, strict=False):
        renewed = handle.lease_clock.renew(handle.session.broker, handle.session.lease, strict=strict)
        need(handle.heartbeat() is True, 'hub_lease_lost')
        handle.next_renew = broker_renew.next_due(renewed, RENEW_SECONDS)
        if renewed is False and handle.native is not None:
            handle.native.renew_retry_at = handle.next_renew
        return renewed
The tick callback at :340 and the direct maintain path share this function, so the tolerance must live here and not in maintain's except.
(3) WarmRPC: add class attribute `renew_retry_at = None` (FixtureRPC skips __init__) and an override:
    def tick(self):
        super().tick()  # base-image NativeRPC.tick sets next_renew = now+25 once renew() returns
        retry, self.renew_retry_at = self.renew_retry_at, None
        if retry is not None:
            self.next_renew = min(self.next_renew, retry)
(4) execute: the pre-prompt renew at :396 becomes `_renew(handle, strict=True)`. The success path is identical. prepare's final `_renew(handle)` at :350 stays tolerant.
(5) maintain:
- Put poll_idle() and the direct renew under one guard that swallows only hub_lease_lost. Today a hub loss on the tick path closes the handle, and it must not.
- Raise the per-call native.deadline from now+30 to min(warm_deadline, now+45). A failing attempt can take about 30 s, and NativeRPC.tick checks the deadline after renew (native_metadata_timeout).
(6) _fail already passes LiveCodexError codes through, so the new codes survive.

Tests:
- tests/test_codex.py (runs only in the image build; needs the base transport, metadata and protocol_gate). test_idle_transient_renew_failure_keeps_warm_process: MutationUncertain and BoundaryError metadata_identity_unavailable with handle.next_renew=0. maintain returns readiness; state 'ready'; finish not called; renewed_at unchanged; handle.next_renew <= now+RETRY_SECONDS; heartbeat called. After success, renewed_at advances.
- test_tick_path_transient_renew_failure_is_rearmed_for_retry: native.next_renew=0 with handle.next_renew far away. renew is called once per maintain, native.next_renew <= now+RETRY_SECONDS (not +25) and state is 'ready'.
- test_tick_path_hub_heartbeat_loss_keeps_warm_process: native.next_renew=0 and heartbeat False. maintain returns, state 'ready', finish not called. This is a regression test for the unguarded poll_idle tick.
- test_renew_tolerance_is_bounded: a backdated anchor raises '^codex_broker_renew_failed$' on both the direct and tick paths. state 'closed', maintain_fault 'fail'.
- test_rejections_are_immediate: Conflict, BrokerError('broker_request_denied') and BoundaryError('credential_fence_lost') give '^codex_broker_renew_rejected$' with a fresh anchor.
- test_execute_pre_prompt_renew_is_strict: MutationUncertain at the :396 renew gives codex_broker_renew_failed and prompt_count()==0.
- test_execute_pump_tolerates_one_blip_during_turn: renew side_effect [None, MutationUncertain, None...] after the prompt. Text returned, 'committed', prompt_count()==1, heartbeat called after the failure. Variant with a stale anchor: codex_broker_renew_failed.
- WireWrites-style test_warm_rpc_tick_lowers_next_renew_to_retry: with renew_retry_at set by the callback, tick() leaves next_renew == retry and clears the attribute. A plain success gives about +25.
- Keep test_idle_hub_heartbeat_loss_keeps_the_warm_process and test_idle_maintenance_renews_without_inference_and_drains_expiry unchanged.

### cursor (live-worker-runtime/providers/cursor.py) (LATER, on a head that contains origin/cursor/fix-cursor-idle-deadline 768ed3a. Without it, the idle pump dies at 180 s anyway. Line numbers below are from 768ed3a.): fail code `cursor_broker_renew_failed (past the window, the strict gate, or the check() backstop) and cursor_broker_renew_rejected, as cursor NativeError(ProviderCodeError, RuntimeError), both fail codes. cursor_hub_heartbeat_lost and cursor_lease_lost stay retry codes; cursor_lease_lost is now only reachable for a non-bool.`

(1) Add `import broker_renew`. Handle: add lease_clock. prepare: create the Handle with `lease_clock=broker_renew.LeaseClock.for_lease(session.lease, NativeError, 'cursor')` before the first metadata pump. That pump renews immediately.
(2) Replace _renew (:291-293) with:
    renewed = handle.lease_clock.renew(handle.session.broker, handle.session.lease)
    need(handle.heartbeat() is True, 'cursor_hub_heartbeat_lost')
    return renewed
(3) LiveProcess.pump (:116-118):
    renewed = self.heartbeat()
    need(type(renewed) is bool, 'cursor_lease_lost')
    self.next_heartbeat = broker_renew.next_due(renewed, HEARTBEAT_SECONDS)
This matters because False must not become cursor_lease_lost. That is a retry code, and live_loop's 3-retry cap would override the 180 s bound.
(4) HeartbeatMetadata.pump (:252-254): the same change, keeping 'cursor_lease_lost_during_metadata' for a non-bool.
(5) Pre-prompt gate (:155). This renew is already immediately before the prompt, so it becomes strict:
    renewed = self.heartbeat()
    need(renewed is not False, 'cursor_broker_renew_failed')
    need(renewed is True, 'cursor_lease_lost_before_prompt')
(6) maintain: after idle_pump, add `handle.lease_clock.check()` as a backstop. idle_pump skips pump() when the selector map is empty, and then no renew happens at all.
(7) 768ed3a's _idle_code: leave it as is. The helper's NativeError codes pass through it (maintain re-raises without closing; live_loop closes). Its BrokerError→cursor_idle_renew_failed mapping is now unreachable from renew.
HEARTBEAT_SECONDS stays 8. Update the module docstring (:7-8, 'No retries').

Tests:
- First fix the pre-existing harness at 2cb35c6, where 10 of 17 fail with native_account_owner_mismatch: patch c.ACCOUNT_REF to sha256(OWNER), add agent-hub to sys.path, and make FakeMetadata and FakeACP call their heartbeat when due.
- test_renew_tolerates_transient_errors_until_window: with an injected LeaseClock clock, success at t=0, MutationUncertain at t=10, BoundaryError transport_failed at t=100 and MutationUncertain at t=180 all return False and the hub heartbeat is called. At t=181 the result is 'cursor_broker_renew_failed' with __cause__ None.
- test_renew_rejections_are_immediate: Conflict, BrokerError('broker_request_denied') and BoundaryError('credential_fence_lost') give 'cursor_broker_renew_rejected', and renewed_at is unchanged.
- test_tolerated_renew_still_raises_hub_heartbeat_lost: the result is cursor_hub_heartbeat_lost with maintain_fault 'retry'.
- LiveProtocol test_pump_rearms_after_tolerated_failure: a real LiveProcess via __new__ with get_map={1:..}, select=lambda t: [] and heartbeat=lambda: c._renew(handle). One MutationUncertain sets next_heartbeat == now + RETRY_SECONDS. A success sets it to about +8. A non-bool gives cursor_lease_lost.
- test_metadata_pump_tolerance: HeartbeatMetadata.pump with a tolerated failure re-arms at the retry time. Past the window it raises cursor_broker_renew_failed.
- test_maintain_survives_transient_failures_until_window: maintain returns readiness and the handle stays open until the anchor is aged. Then cursor_broker_renew_failed, the handle is not closed by maintain, and maintain_fault 'fail'.
- test_maintain_backstop_without_pump: with an empty selector map and an aged anchor, maintain raises cursor_broker_renew_failed.
- test_pre_prompt_gate_is_strict: a tolerated-class failure at the gate gives cursor_broker_renew_failed with no session/prompt call. On success the prompt is sent.
- test_execute_turn_survives_one_transient_renew: the prompt_hook triggers one tolerated failure. The verified answer is returned and events end with stop, then commit-release.

## Renew failures during an execute turn

Principle: a billed prompt is never written while the most recent renew attempt failed. Once a prompt is in flight, a transient blip does not kill it.

1) Execute setup, before the prompt. Pumps that renew here (grok's _fresh_metadata requests, copilot's _fresh_metadata/getCurrent, codex's _collect/poll_idle, cursor's setup requests) follow the idle rules: tolerated within the 180 s window, retried after 10 s, heartbeat still called.
- Then a pre-prompt gate runs.
- grok, copilot and claude: if lease_clock.degraded (a tolerated failure happened since the last confirmed renew), make one strict renew right before the prompt. In grok this goes after _fresh_metadata and before the heartbeat/attempted. In copilot it goes before assert_current at :623. In claude it goes before heartbeat/attempted.
- codex and cursor already renew immediately before the prompt (codex :396; cursor's stage-6 gate). That call becomes strict.
- On success nothing changes: grok, copilot and claude make no extra call when the clock is not degraded, and codex and cursor make the same call they make today.
- If the strict renew fails, raise <provider>_broker_renew_failed (Conflict or denied gives _rejected). The prompt is not sent. The adapter's execute except closes: native stop, then session.finish commit/release while the lease is still valid. live_loop completes the room with exit_code 1 and the code in the message, and the process exits 1. live_loop's own model_call_attempted flag is already True because it is set before execute, as today.

2) Prompt in flight: grok's session/prompt pump, copilot's _response frames, codex's next_event/finish_turn, cursor's request read loop, and claude's Native.tick under 52345d1.
- A transient renew failure is tolerated under the same bound, measured from the last confirmed renew. The pump retries at most every RETRY_SECONDS (10 s) rather than at 4-50 Hz.
- The hub heartbeat() still runs after every attempt. During a turn it is the only thing extending the 45 s hub task lease.
- If the heartbeat returns False, the adapter's hub-loss code fails the turn (retry codes do not apply during execute).
- Once the bound is exceeded, <provider>_broker_renew_failed is raised from inside the pump. The adapter closes and re-raises its own code; copilot's and codex's wrappers pass it through as a vetted code. live_loop completes the room with exit 1. The answer is lost and never replayed.
- Conflict or denied during a turn raises _rejected immediately, as today but with a vetted code.

3) After the answer, close() does the final commit/release. It needs lease_until > now. The 60 s reserve protects it except in the overshoot case described in the risks.

Pre-existing claude caveat: if 52345d1 is not on the head, claude has no renew or task heartbeat during a turn. The gate is then the only protection, and turns longer than the remaining lease fail at close. That needs a separate fix.

## Interaction with live_loop

No live_loop.py change. The contract is used as it is:
- A tolerated failure never leaves the adapter. maintain() returns readiness, and Worker.run resets maintain_retries to 0. The 180 s bound is therefore independent of MAX_MAINTAIN_RETRIES=3. The loop keeps reporting and claiming, and a claimed task goes through the pre-prompt gate.
- A tolerated failure followed by heartbeat() False raises the existing <p>_hub_heartbeat_lost (hub_lease_lost for codex; cursor_lease_lost stays only for a non-bool). Those codes are in _IDLE_RETRY_CODES, so the existing path applies: at most 3 consecutive retries while handle_released() is False, with the renew still due because the next_renew assignment is skipped.
- <p>_broker_renew_failed and <p>_broker_renew_rejected are ProviderCodeError instances whose text matches SAFE_CODE. provider_errors.error_code returns the code. It is in neither _IDLE_DRAIN_CODES nor _IDLE_RETRY_CODES, so maintain_fault returns 'fail' (and idle_fault also returns 'fail'). The loop re-raises at :285-287 on the first occurrence.
- The outer except (:348) records error_code=<code>. It then closes the handle if the adapter has not already closed it: copilot and codex close inside maintain, while grok, claude and cursor leave closing to the loop. If close fails, the outcome is credential_cleanup_failed (the existing behavior). Otherwise last_exit=1, the runcrew_live_result line carries the code, and entrypoint returns 1. The controller counts a consecutive failure (backoff, 3-strike block). An unreleased credential blocks the slot at once.
- The copilot_warm_session_lost idle-drain special case (:360-375) is not reachable, because the new codes must never be added to copilot.IDLE_SESSION_LOSS.
- During execute the loop's existing failure completion delivers 'The cloud worker stopped before it could deliver a verified answer (<code>)...' with exit_code 1.
- Guard tests go in test_live_loop.py because it is COMMON and runs in every image build:
  - For all five providers and both codes, maintain_fault and idle_fault return 'fail' and the code is absent from both sets.
  - A Worker whose maintain raises CodeError('grok_broker_renew_failed') calls maintain once, makes 0 claims, ends 'failed' with that error_code, has last_exit 1 and calls close.
  - BrokerRenewTests unit-test the helper, pin LEASE_SECONDS to dynamic_broker.Policy, and check statically that entrypoint.py takes time.monotonic() before broker.acquire( and calls broker_renew.record_acquire_start after it.

## Risks

- lease_seconds cannot be observed by the worker. LEASE_SECONDS=240 is pinned only to dynamic_broker.Policy's code default, while the live value is per-policy in the protected live-broker.json (range 30..600). If a policy sets less than 240 (Binding and CloudCredentialBroker default to 180, and grok's docstring says 180), a 180 s window can outlive the lease. The next renew would get a 409 credential_lease_not_active (rejected), and close() would hit lease_expired_reconciliation_required, leading to quarantine and re-enrollment. The operator must confirm 240 before deploying. The durable fix is for the broker to return lease_seconds or lease_until in the acquire and renew replies.
- Worst-case detection overshoot. The bound is evaluated only when a failed attempt returns. Detection can arrive about 180 + 10-25 s (retry or idle tick gap, including claim and report latency) + 30 s (one attempt; DNS unbounded), roughly 235 s after the last confirmed renew started. That leaves little of the 240 s lease for close's native stop plus assert_current, commit and release. Typical detection is about 190-200 s. If the broker is still down, close fails anyway (quarantine, credential_cleanup_failed), just as an immediate failure would today.
- Conflict stays fatal by the lead's rule, but the broker maps transient upstream read failures (google_read_failed, live_binding_read_unavailable, metadata failures) to plain BrokerError and then 409. The Copilot incident's reply was a 409 at 10.135 s; had the client waited 0.14 s longer, this design would still end the session, now as copilot_broker_renew_rejected. A server-side follow-up should make error_reply answer 503 for read-only upstream failures.
- Self-inflicted Conflict: a retry that overlaps a server request the client already abandoned can lose the Firestore updateTime CAS and come back as 409, which is not tolerated. RETRY_SECONDS=10, measured after the failed attempt returns, reduces this but does not remove it.
- A failing attempt blocks the main thread for up to about 30 s inside native pumps. Copilot, cursor and codex stdout is not drained; the grok reader queue (256) blocks; claude's _Frames queue (32, put_nowait) under 52345d1 can overflow into claude_protocol_output_invalid; and a grok attempt can overrun the task deadline by up to about 30 s. The 10 s retry floor limits this to one attempt per 10 s, but it does not bound the attempt itself.
- live_loop keeps claiming while renews are degraded, because readiness is ignored. The pre-prompt gate stops a billed prompt on an unconfirmed lease, but a room claimed during an outage still fails with exit 1. A turn whose window expires mid-flight loses a billed answer.
- Tolerated failures are invisible: nothing is logged and nothing appears in the outcome record. Follow-up: report lease_clock.failures, or the maximum elapsed time, in the outcome.
- Error codes change: renew-caused failures move from native_or_connection_failure (grok, claude, cursor); copilot_idle_renew_failed, copilot_prepare_requires_reconciliation and copilot_task_outcome_requires_reconciliation (copilot); and codex_live_operation_failed (codex) to <p>_broker_renew_failed or _rejected. copilot_idle_renew_failed is renamed copilot_idle_maintain_failed. Alerts or dashboards keyed on the old codes must be updated. The _rejected code is a designer addition that separates fence loss or 409 from an exhausted outage; the lead can fold it into _failed.
- Neither new code may ever be added to _IDLE_RETRY_CODES, _IDLE_DRAIN_CODES or copilot.IDLE_SESSION_LOSS. IDLE_SESSION_LOSS would turn a lease failure into an exit-0 idle drain. The test_live_loop guard covers the sets; copilot's comment documents IDLE_SESSION_LOSS.
- The anchor registry is module-global state keyed by lease_id and written by entrypoint. Any other harness falls back to prepare() entry, which is optimistic by the acquire-to-prepare gap (up to about 45 s). Tests must use patch.dict on broker_renew._acquire_started.
- Packaging: broker_renew.py must be in refresh_packs.COMMON (test_refresh_packs catches this). Class identity depends on the base pack's agent_hub providing credential_broker_service.BoundaryError and cloud_credential_broker.MutationUncertain, which dynamic_broker already imports in every image. Confirm that the base pack's agent_hub matches this checkout's renew error mapping.
- grok.py and copilot.py now import agent_hub through broker_renew. tests/test_grok.py needs agent-hub on sys.path (it has no such preamble today), and grok's 'no cloud client imported' docstring must be amended.
- Codex: the base-image NativeRPC.tick sets next_renew to +25 once renew() returns, so without the WarmRPC.tick override a tolerated failure would silently delay the retry by 25 s. maintain's native.deadline of now+30 can also trip native_metadata_timeout after a slow failed attempt, which is why it is raised to +45. The poll_idle hub-loss guard fixes an existing tick-path bug and can be split out.
- Concurrent edits: claude depends on 52345d1 (tick renew and its except wrapper) and cursor on 768ed3a (_idle_code, WARM_SECONDS), and codex.py is changing. Re-verify the line numbers and shapes before writing. Without 52345d1, claude still has no renew during a turn.
- A MutationUncertain may hide a renew that actually succeeded. Leaving the anchor unchanged is conservative, and repeating a renew is safe because it never changes fence, phase or version.
- The worker's monotonic clock and the server's wall clock can drift; a server wall-clock jump shortens the effective lease. The 60 s reserve absorbs small skew only.

## Design narrative

# Bounded broker-renew tolerance

## Rule
- **Tolerated** (the call returns False):
  - `MutationUncertain`, any code (the outcome is unknown).
  - `BoundaryError` with code `transport_failed`, `transport_limit` or `metadata_identity_unavailable`. These come from the metadata ID-token fetch, before any broker request.
- A tolerated failure leaves the anchor unchanged, still calls the hub `heartbeat()`, and makes the next attempt due 10 s (`RETRY_SECONDS`) after the failed attempt returned.
  - In idle this is the next maintain tick, since production poll_seconds is 10.
  - In native pumps (4-50 Hz) it prevents hammering the broker.
- **Bound.** After a failed attempt returns, if `monotonic() - renewed_at > 180` (`LEASE_SECONDS 240 - 60`), raise `<provider>_broker_renew_failed`.
  - `renewed_at` is the monotonic time taken just before the last renew the broker confirmed. The server stamps lease_until later, so this is conservative.
  - The first anchor is the monotonic time just before `broker.acquire`, recorded by entrypoint. If nothing was recorded, it falls back to prepare() entry.
- **Rejected at once**, as `<provider>_broker_renew_rejected`: every other `BrokerError`.
  - `Conflict` (a 409 may be real fence loss).
  - `broker_request_denied`.
  - The local BoundaryErrors `credential_fence_lost`, `request_invalid` and `message_too_large`. They are deterministic, which is why BoundaryError is narrowed.
- Non-broker exceptions and BaseException (the SIGTERM KeyboardInterrupt) propagate unchanged.
- Both codes are raised as the adapter's own ProviderCodeError class, `from None`. Both are fail codes and never enter `_IDLE_RETRY_CODES`, `_IDLE_DRAIN_CODES` or copilot `IDLE_SESSION_LOSS`.
- When a renew succeeds, nothing changes: the same call, heartbeat and cadence.

## Shared helper
`live-worker-runtime/broker_renew.py` (imported like `provider_errors`):
- Added to `refresh_packs.COMMON` and the README list. `test_refresh_packs` fails if it is forgotten.
- API: `LeaseClock.for_lease(lease, ErrorClass, provider)`, `.renew(broker, lease, strict=False) -> bool`, `.degraded`, `.check()`, `next_due(renewed, cadence)`, `transient(error)`, `record_acquire_start(lease_id, t)`.
- `entrypoint.py` (COMMON) wraps `broker.acquire` with `acquire_started = time.monotonic()` and `record_acquire_start(lease.lease_id, acquire_started)`.

Common adapter pattern:
- `Handle.lease_clock` is set in prepare() before any pump.
- `_renew` returns `lease_clock.renew(...)`, then calls heartbeat.
- `next_renew = broker_renew.next_due(<renew>, cadence)`. A raise skips the assignment, so the renew stays due.

## Execute policy
- **Before the prompt:** no prompt is written while the last attempt failed.
  - grok, copilot and claude run one strict renew when `degraded`.
  - codex (`:396`) and cursor (stage-6 gate) already renew right before the prompt; that renew becomes strict.
  - A failure raises `_failed` (or `_rejected`). The prompt is not sent, the adapter closes, and the room gets exit 1.
- **In flight:** pump renews are tolerated under the same bound, retried every 10 s, and the heartbeat keeps the 45 s hub task lease alive.
  - Past the bound the turn fails with `_failed`, closes and is not replayed.
  - Conflict gives `_rejected` immediately.

## live_loop
No change:
- A tolerated failure means maintain returns readiness and the retry counter resets.
- A lost heartbeat uses the existing path: retry at most 3 times, with the renew still due.
- `_failed` or `_rejected` means `maintain_fault` returns `fail`, then close, then the error_code is recorded, the process exits 1 and the controller backs off.

## Adapters
- **grok (now):**
  - `grok.py`: `import broker_renew`, Handle field, `_renew` returns bool, prepare anchor, maintain `next_due(_renew(handle), 20)`, degraded gate in execute. Fix the '180s' docstring.
  - `_grok_protocol.py:117`: `self.next_renew = broker_renew.next_due(self.renew(), 20)`.
  - Prototype: the 22 existing tests pass.
- **copilot (now):**
  - `Native._tick` uses `next_due(self.renew(), RENEW_SECONDS)`. Plus the Handle field, `_renew` returning bool, the prepare anchor, and a degraded gate before `assert_current`.
  - `:609` becomes `copilot_idle_maintain_failed`, now only for opaque non-broker errors.
  - Prototype: 34 of 37 existing tests pass; the 3 failures are the tests this design replaces.
- **claude (later, on 52345d1):** `Native.tick` uses `next_due`, plus the degraded gate.
- **codex (later):**
  - `_renew(strict)` sets both schedules.
  - A `WarmRPC.tick` override lowers the base tick's +25.
  - The pre-prompt renew becomes strict.
  - maintain guards poll_idle for `hub_lease_lost` and gets a +45 s deadline.
- **cursor (later, on 768ed3a):**
  - The pumps accept `False` and use `next_due(…, 8)`.
  - The stage-6 gate is strict.
  - maintain adds a `lease_clock.check()` backstop for the empty-selector idle path.

## Tests
- **test_live_loop (COMMON, runs in every image):**
  - Helper unit tests: classification, window edge (180 tolerated, 180+ fails), attempt duration counts, success anchors at the start, rejection, strict, BaseException propagates, anchor registry, `next_due`.
  - `LEASE_SECONDS == dynamic_broker.Policy` default.
  - Static check of the entrypoint acquire stamp.
  - For all five providers, both codes give `fail` and are absent from the sets.
  - A Worker whose maintain raises `grok_broker_renew_failed` makes one maintain call and 0 claims, ends failed with exit 1, and closes.
- **grok:**
  - Transient idle failure: kept and retried.
  - Rejections are immediate.
  - Window exceeded gives a vetted code.
  - A tolerated failure still heartbeats.
  - Prepare anchor.
  - Pump re-arm.
  - In-turn blip survives; past the window it fails.
  - Degraded strict gate.
  - Startup pump.
  - Worker integration: blip gives exit 0, persistent failure gives exit 1.
- **copilot:**
  - Replace the idle_renew_failed test.
  - Exhausted, rejected, heartbeat, anchor.
  - `_tick` throttle.
  - FakeNative uses the real `_tick`.
  - Execute blip, past window, gate.
  - Fallback code renamed.
- **claude, codex, cursor:** the same matrix on their heads. Fix cursor's harness first.

## Lead decisions
1. Keep `_rejected` separate, or fold it into `_failed`.
2. The copilot fallback name.
3. Confirm the deployed `lease_seconds` is 240.
4. Accept the worst-case detection of about 235 s, or budget one attempt.
