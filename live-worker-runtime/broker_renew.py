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


def next_due(renewed, cadence, clock=None):
    """Next renew time. Call only after renew returned; None (legacy callbacks) counts as success."""
    # Resolved at call time so a test patch of time.monotonic is visible. A
    # default argument would freeze the function object from import.
    if clock is None:
        clock = time.monotonic
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
