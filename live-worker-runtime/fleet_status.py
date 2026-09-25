"""Advisory per-slot status documents for the hub (MH-008 C2).

The controller publishes a small plain-JSON document after each tick. The hub
reads it for readiness reasons only; it never feeds dispatch, lease, or claim.
Nothing here talks to Firestore yet.
"""
from __future__ import annotations

STATUS_SCHEMA = 1
STATUS_KEYS = frozenset({
    'schema', 'provider', 'phase', 'reason', 'next_launch_at',
    'consecutive_failures', 'published_at',
})

PROVISIONING_PHASES = frozenset({
    'binding_intent', 'binding_ready', 'launch_intent', 'launch_submitted', 'grant_intent',
})


def status_reason(state, now):
    """Fixed reason code for a controller slot state at ``now`` (epoch seconds).

    Idle waiting (``now < next_launch_at``) maps as::

        error == provider_quota_exhausted  -> parked
        consecutive_failures > 0           -> backoff
        otherwise (incl. launch interval)  -> idle_launching

    The normal 60 s post-``binding_ready`` launch interval (failures == 0, no
    quota error) is ``idle_launching``, not ``unknown``.
    """
    if not isinstance(state, dict):
        return 'unknown'
    if state.get('slot_enabled') is False:
        return 'disabled'
    phase = state.get('phase')
    if not isinstance(phase, str):
        # A corrupted or foreign state document must read as unknown, never raise
        # (an unhashable phase used to fail the set lookup below).
        return 'unknown'
    if phase == 'active':
        return 'ready_or_running'
    if phase in PROVISIONING_PHASES:
        return 'provisioning'
    if phase == 'blocked':
        return 'blocked'
    if phase == 'idle':
        next_launch_at = state.get('next_launch_at')
        waiting = type(next_launch_at) in (int, float) and now < next_launch_at
        if waiting:
            if state.get('error') == 'provider_quota_exhausted':
                return 'parked'
            failures = state.get('consecutive_failures', 0)
            if type(failures) is int and failures > 0:
                return 'backoff'
            return 'idle_launching'
        return 'idle_launching'
    return 'unknown'


def status_document(state, now, provider):
    """Build the advisory status dict. Keys are whitelisted; secrets never appear."""
    state = state if isinstance(state, dict) else {}
    failures = state.get('consecutive_failures', 0)
    if type(failures) is not int:
        failures = 0
    document = {
        'schema': STATUS_SCHEMA,
        'provider': provider,
        'phase': state.get('phase'),
        'reason': status_reason(state, now),
        'next_launch_at': state.get('next_launch_at'),
        'consecutive_failures': failures,
        'published_at': int(now),
    }
    # Drop anything that is not on the whitelist (defence in depth).
    return {key: document[key] for key in ('schema', 'provider', 'phase', 'reason',
                                           'next_launch_at', 'consecutive_failures', 'published_at')}


class StatusPublisher:
    """Sink for advisory per-slot status documents."""

    def publish(self, provider, document):
        raise NotImplementedError


class FakeStatusPublisher(StatusPublisher):
    """In-memory publisher for tests."""

    def __init__(self):
        self.published = []
        self.fail_next = False
        self.failures = 0

    def publish(self, provider, document):
        if self.fail_next:
            self.fail_next = False
            self.failures += 1
            raise RuntimeError('status_publish_failed')
        self.published.append((provider, document))


class FirestoreStatusPublisher(StatusPublisher):
    """Firestore sink for advisory fleet status (not implemented).

    Intended collection: ``runcrew_fleet_status``, one document per provider
    (document id = provider). Written only by the fleet controller's service
    account; read only by the hub's service account. The hub must not be granted
    access to ``runcrew_fleet_state`` or archived receipts. Document body is the
    plain-JSON dict from ``status_document`` (schema 1).
    """

    def publish(self, provider, document):
        raise NotImplementedError(
            'FirestoreStatusPublisher is not implemented; write to '
            'runcrew_fleet_status/{provider} with the controller service account only'
        )
