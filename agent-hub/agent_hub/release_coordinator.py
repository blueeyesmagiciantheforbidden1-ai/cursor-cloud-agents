"""Internal durable release controller; no public endpoint or deployment adapter.

The constructor requires three separately injected trusted backend functions:

* authorize(auth_context, request) -> Authorization: authenticate the caller and
  authorize this exact operation/scope/request digest. Do not derive this from a
  model-provided role or subject string.
* verify_evidence(request) -> EvidenceApproval: independently authenticate the
  verifier, retrieve/hash actual reports and candidate manifests, check the
  actual repository base, enforce the
  protected policy, check real reservation ownership, and verify deployment
  operation outcomes (including terminal reconciliation). The request contains
  the complete canonical input and stored release, never just agent prose.
* observe(request) -> DeploymentObservation: derive the actual immutable image,
  source revision, traffic and CAS generation from the fixed service. ``healthy``
  must reflect independently checked health/connection evidence; readiness alone
  cannot establish it. Unknown observations are allowed only to record an
  uncertain action, never for dispatch, successful acknowledgment or retirement.

These are trusted-controller injection points, not authentication implemented by
this library. There are deliberately no accepting defaults. An approval object's
type or hash alone is not a signature. Production adapters and their credentials
must be isolated from candidate authors; tests use explicitly synthetic adapters.

All callbacks execute before the store transaction. Only pure checks/reducer work
run inside mutate_states, which may retry its callback. A release document, one
service slot and one repository slot change atomically. Each slot holds a monotonic
fence; slots stay held through canary monitoring and unresolved outcomes. The
deployer must still enforce dispatch.expected against the real service's CAS
generation, because a database transaction cannot lock an external deployment.

Only a successfully returned apply(... claim ...) result can contain dispatch.
Never dispatch from read() or a stored action. Any uncertain storage result raises
CommitUncertain without returning dispatch; reread and reconcile, do not replay.
Successful retirement requires a known terminal state AND current verified healthy
100% traffic. Old records remain retired forever and cannot roll back successors.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
import json
import re
import time

from .improvement_release import ReleaseError, ReleaseConflict, create_release, digest, transition, validate_spec


class CoordinatorError(ReleaseError):
    pass


class AccessDenied(CoordinatorError):
    pass


class CommitUncertain(CoordinatorError):
    """Storage may have committed. No dispatch is authorized by this exception."""


@dataclass(frozen=True)
class Authorization:
    subject: str
    operation: str
    scope_sha256: str
    request_sha256: str
    proof_sha256: str
    expires_at: int


@dataclass(frozen=True)
class EvidenceApproval:
    issuer_subject: str
    request_sha256: str
    proof_sha256: str
    verified_at: int


@dataclass(frozen=True)
class DeploymentObservation:
    service: str
    target: dict | None
    observed_at: int
    proof_sha256: str
    healthy: bool | None


@dataclass(frozen=True)
class CoordinatorResult:
    record: dict
    retired: bool
    dispatch: dict | None = None
    replayed: bool = False


def _require(condition, reason, error=CoordinatorError):
    if not condition:
        raise error(reason)


def _copy(value):
    # Reuse the reducer's strict size/finite/UTF-8 canonical-JSON validation.
    digest(value)
    return json.loads(json.dumps(value, allow_nan=False))


def _id(value):
    _require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value) is not None,
             "invalid_identifier")


def _hash(value):
    _require(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None, "sha256_required")


def _integer(value):
    _require(type(value) is int and 0 <= value <= 10**12, "invalid_integer")


def _target(value):
    _require(isinstance(value, dict) and set(value) == {"revision", "artifact_sha256", "generation", "traffic_percent"},
             "invalid_deployment_observation")
    _require(isinstance(value["revision"], str)
             and re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", value["revision"]) is not None, "invalid_revision")
    _hash(value["artifact_sha256"]); _id(value["generation"])
    _require(type(value["traffic_percent"]) is int and 1 <= value["traffic_percent"] <= 100, "invalid_traffic")


class ReleaseCoordinator:
    def __init__(self, store, *, service, repository, authorize, verify_evidence, observe,
                 trusted_verifier_subjects, clock=time.time, max_observation_age_seconds=10):
        _require(callable(getattr(store, "mutate_states", None)) and callable(getattr(store, "get_state", None)),
                 "atomic_state_store_required")
        _require(all(callable(callback) for callback in (authorize, verify_evidence, observe, clock)),
                 "trusted_adapters_required")
        _require(authorize is not verify_evidence and observe is not verify_evidence, "separate_verifier_required")
        _require(isinstance(service, str) and 1 <= len(service) <= 256
                 and re.fullmatch(r"[A-Za-z0-9_./:-]+", service) is not None, "fixed_service_required")
        _id(repository)
        _require(isinstance(trusted_verifier_subjects, (tuple, list, set, frozenset))
                 and 1 <= len(trusted_verifier_subjects) <= 16, "verifier_allowlist_required")
        for subject in trusted_verifier_subjects:
            _id(subject)
        _require(type(max_observation_age_seconds) is int and 1 <= max_observation_age_seconds <= 60,
                 "bounded_observation_age_required")
        self.store = store
        self.scope = {"service": service, "repository": repository}
        self.scope_sha256 = digest(self.scope)
        self.authorize = authorize
        self.verify_evidence = verify_evidence
        self.observe = observe
        self.verifiers = frozenset(trusted_verifier_subjects)
        self.clock = clock
        self.max_age = max_observation_age_seconds
        # Separate service and repository locks prevent conflicting namespaces
        # from bypassing a combined service/repository-only lock.
        self.service_key = "release-service-" + digest(service)
        self.repository_key = "release-repository-" + digest(repository)

    def record_key(self, release_id):
        _hash(release_id)
        return "release-record-" + digest({**self.scope, "release_id": release_id})

    def _now(self):
        value = self.clock()
        _require(type(value) in (int, float) and 0 <= value <= 10**12, "invalid_clock")
        return int(value)

    def _fresh(self, timestamp, now):
        _integer(timestamp)
        _require(timestamp <= now and now - timestamp <= self.max_age, "trusted_receipt_expired")

    def _authorize(self, auth_context, operation, request):
        request = {"operation": operation, "scope": self.scope, "request_sha256": digest(request)}
        try:
            result = self.authorize(auth_context, _copy(request))
        except Exception:
            raise AccessDenied("authorization_failed") from None
        _require(type(result) is Authorization, "authenticated_authorization_required", AccessDenied)
        _id(result.subject); _hash(result.proof_sha256)
        _require(result.operation == operation and result.scope_sha256 == self.scope_sha256
                 and result.request_sha256 == request["request_sha256"], "authorization_binding_mismatch", AccessDenied)
        self._authorization_current(result, self._now())
        return result

    @staticmethod
    def _authorization_current(authorization, now):
        _integer(authorization.expires_at)
        _require(now <= authorization.expires_at <= now + 300, "authorization_expired_or_unbounded", AccessDenied)

    def _observation(self, operation, release_id):
        try:
            result = self.observe({"scope": _copy(self.scope), "operation": operation, "release_id": release_id})
        except Exception:
            raise CoordinatorError("deployment_observation_unavailable") from None
        _require(type(result) is DeploymentObservation and result.service == self.scope["service"],
                 "independent_deployment_observation_required")
        _hash(result.proof_sha256)
        _require(result.healthy is None or type(result.healthy) is bool, "explicit_health_observation_required")
        if result.target is not None:
            _target(result.target)
        self._fresh(result.observed_at, self._now())
        return DeploymentObservation(result.service, _copy(result.target), result.observed_at,
                                     result.proof_sha256, result.healthy)

    def _approval(self, operation, request, authorization, observation, creator, record=None):
        verification = {"scope": self.scope, "operation": operation, "input": request,
                        "authorization": asdict(authorization), "observation": asdict(observation),
                        "creator_subject": creator, "record": record}
        verification = _copy(verification)
        try:
            result = self.verify_evidence(_copy(verification))
        except Exception:
            raise AccessDenied("evidence_verification_failed") from None
        _require(type(result) is EvidenceApproval, "authenticated_evidence_approval_required", AccessDenied)
        _hash(result.proof_sha256)
        _require(result.issuer_subject in self.verifiers and result.issuer_subject != creator
                 and result.request_sha256 == digest(verification), "independent_verifier_or_evidence_binding_failed", AccessDenied)
        self._fresh(result.verified_at, self._now())
        return result

    def _current(self, authorization, approval, observation):
        now = self._now()
        self._authorization_current(authorization, now)
        self._fresh(approval.verified_at, now)
        self._fresh(observation.observed_at, now)
        return now

    def _load(self, release_id):
        try:
            envelope = self.store.get_state(self.record_key(release_id))
        except Exception:
            raise CoordinatorError("state_read_unavailable") from None
        if envelope:
            self._validate_envelope(envelope, release_id)
        return envelope

    def _validate_envelope(self, envelope, release_id):
        _require(envelope.get("schema") == 1 and envelope.get("scope") == self.scope
                 and envelope.get("record", {}).get("id") == release_id, "stored_release_binding_invalid")
        record = envelope["record"]
        _require(record.get("state_sha256") == digest({key: value for key, value in record.items() if key != "state_sha256"})
                 and record["id"] == digest(validate_spec(record["spec"])), "stored_release_integrity_invalid")
        _require(type(envelope.get("retired")) is bool and isinstance(envelope.get("events"), dict), "stored_release_invalid")
        return envelope

    def _mutate(self, release_id, callback):
        try:
            # No auth/verifier/observer or deployment side effects run here.
            return self.store.mutate_states((self.record_key(release_id), self.service_key, self.repository_key), callback)
        except ReleaseError:
            raise
        except Exception:
            raise CommitUncertain("storage_commit_not_confirmed_no_dispatch") from None

    @staticmethod
    def _result(envelope, *, dispatch=None, replayed=False):
        return CoordinatorResult(_copy(envelope["record"]), envelope["retired"], _copy(dispatch), replayed)

    def _slots(self, states, envelope):
        record = envelope["record"]
        service, repository = states[self.service_key], states[self.repository_key]
        _require(service.get("service") == self.scope["service"] and repository.get("repository") == self.scope["repository"]
                 and service.get("active_release") == record["id"] and repository.get("active_release") == record["id"]
                 and service.get("active_repository") == self.scope["repository"]
                 and repository.get("active_service") == self.scope["service"]
                 and service.get("fence") == envelope["service_fence"]
                 and repository.get("fence") == envelope["repository_fence"], "release_slot_lost", ReleaseConflict)
        _require(service.get("observed") == record["observed"], "slot_generation_mismatch", ReleaseConflict)
        return service, repository

    def create(self, spec, *, auth_context):
        spec = validate_spec(spec)
        _require(spec["repository"] == self.scope["repository"], "repository_outside_fixed_scope")
        _require("agent_hub/release_coordinator.py" not in {path.casefold() for path in spec["changed_files"]},
                 "release_coordinator_is_protected")
        release_id = digest(spec)
        authorization = self._authorize(auth_context, "release.create", spec)
        existing = self._load(release_id)
        if existing:
            return self._result(existing, replayed=True)
        observation = self._observation("release.create", release_id)
        initial = create_release(spec, now=self._now())
        _require(observation.target == initial["observed"] and observation.healthy is True,
                 "verified_known_good_base_required")
        approval = self._approval("release.create", spec, authorization, observation, authorization.subject)

        def commit(states):
            self._current(authorization, approval, observation)
            key = self.record_key(release_id)
            if states[key]:
                return self._result(self._validate_envelope(states[key], release_id), replayed=True)
            service, repository = states[self.service_key], states[self.repository_key]
            _require(not service.get("active_release") and not repository.get("active_release"),
                     "another_release_owns_slot", ReleaseConflict)
            if service:
                _require(service.get("service") == self.scope["service"]
                         and service.get("known_good") == initial["observed"], "service_base_changed", ReleaseConflict)
            if repository:
                _require(repository.get("repository") == self.scope["repository"]
                         and repository.get("head_revision") == spec["base_revision"], "repository_base_changed", ReleaseConflict)
            service_fence, repository_fence = service.get("fence", 0) + 1, repository.get("fence", 0) + 1
            envelope = {"schema": 1, "scope": _copy(self.scope), "record": initial, "retired": False,
                        "creator_subject": authorization.subject, "creation_proof_sha256": approval.proof_sha256,
                        "service_fence": service_fence, "repository_fence": repository_fence,
                        "events": {}, "retirement": None}
            states[key] = _copy(envelope)
            states[self.service_key] = {"schema": 1, "service": self.scope["service"],
                "fence": service_fence, "active_release": release_id, "active_repository": self.scope["repository"],
                "observed": _copy(initial["observed"]), "known_good": _copy(initial["observed"]),
                "known_good_proof_sha256": observation.proof_sha256}
            states[self.repository_key] = {"schema": 1, "repository": self.scope["repository"],
                "fence": repository_fence, "active_release": release_id, "active_service": self.scope["service"],
                "head_revision": spec["base_revision"]}
            return self._result(envelope)

        return self._mutate(release_id, commit)

    def read(self, release_id, *, auth_context):
        self._authorize(auth_context, "release.read", {"release_id": release_id})
        envelope = self._load(release_id)
        _require(bool(envelope), "release_missing")
        return self._result(envelope)

    def apply(self, release_id, event, *, expected_version, auth_context):
        """Accept an event without an observation; the trusted observer supplies it.

        For acknowledgment/reconciliation, observed_at is also derived by the
        controller. Other receipt timestamps remain measured verifier evidence.
        Neither auth_context nor callback diagnostics are persisted or returned.
        """
        _integer(expected_version); _hash(release_id)
        event = _copy(event)
        _require(isinstance(event, dict) and set(event) == {"id", "kind", "payload"}
                 and isinstance(event["payload"], dict), "invalid_event")
        _id(event["id"])
        _require(event["kind"] in ("evidence", "stage", "claim", "acknowledge", "uncertain", "reconcile", "health", "promote", "abort"),
                 "unknown_transition")
        _require("observation" not in event["payload"], "observations_are_controller_derived")
        input_hash = digest(event)
        request = {"release_id": release_id, "event": event, "expected_version": expected_version}
        operation = "release." + event["kind"]
        authorization = self._authorize(auth_context, operation, request)
        if event["kind"] in ("claim", "acknowledge"):
            _require(event["payload"].get("executor") == authorization.subject, "authenticated_executor_mismatch", AccessDenied)
        snapshot = self._load(release_id)
        _require(bool(snapshot), "release_missing")
        prior = snapshot["events"].get(event["id"])
        if prior is not None:
            _require(prior["input_sha256"] == input_hash, "idempotency_key_reused", ReleaseConflict)
            return self._result(snapshot, replayed=True)
        _require(not snapshot["retired"], "release_retired", ReleaseConflict)
        _require(snapshot["record"]["version"] == expected_version, "release_version_changed", ReleaseConflict)
        observation = self._observation(operation, release_id)
        canonical = _copy(event)
        payload, kind = canonical["payload"], canonical["kind"]
        uncertain = kind == "uncertain" or (kind == "acknowledge" and payload.get("outcome") == "unknown")
        _require(observation.target is not None or uncertain, "current_deployment_unknown")
        if kind in ("stage", "claim", "health", "promote", "abort", "acknowledge", "reconcile"):
            payload["observation"] = None if uncertain else observation.target
        if kind in ("acknowledge", "reconcile"):
            payload["observed_at"] = observation.observed_at
        if kind not in ("acknowledge", "reconcile") and not uncertain:
            _require(observation.target == snapshot["record"]["observed"], "external_deployment_changed", ReleaseConflict)
        approval = self._approval(operation, canonical, authorization, observation,
                                  snapshot["creator_subject"], snapshot["record"])

        def commit(states):
            now = self._current(authorization, approval, observation)
            envelope = self._validate_envelope(states[self.record_key(release_id)], release_id)
            old = envelope["events"].get(event["id"])
            if old is not None:
                _require(old["input_sha256"] == input_hash, "idempotency_key_reused", ReleaseConflict)
                return self._result(envelope, replayed=True)
            _require(not envelope["retired"], "release_retired", ReleaseConflict)
            _require(envelope["record"]["version"] == expected_version
                     and envelope["record"]["state_sha256"] == snapshot["record"]["state_sha256"],
                     "release_version_changed", ReleaseConflict)
            service, _ = self._slots(states, envelope)
            outcome = transition(envelope["record"], canonical, expected_version=expected_version, now=now)
            envelope["record"] = outcome.record
            envelope["events"][event["id"]] = {"input_sha256": input_hash, "event_sha256": digest(canonical),
                "authorization_subject": authorization.subject, "authorization_proof_sha256": authorization.proof_sha256,
                "evidence_proof_sha256": approval.proof_sha256, "observer_proof_sha256": observation.proof_sha256}
            service["observed"] = _copy(outcome.record["observed"])
            dispatch = _copy(outcome.dispatch)
            if dispatch is not None:
                dispatch["coordination"] = {**self.scope, "service_fence": envelope["service_fence"],
                    "repository_fence": envelope["repository_fence"], "record_version": outcome.record["version"],
                    "event_id": event["id"]}
            return self._result(envelope, dispatch=dispatch, replayed=outcome.replayed)

        return self._mutate(release_id, commit)

    def retire(self, release_id, *, event_id, expected_version, auth_context):
        """Release slots only after a fresh, healthy terminal target is verified."""
        _id(event_id); _integer(expected_version); _hash(release_id)
        request = {"release_id": release_id, "event_id": event_id, "expected_version": expected_version}
        authorization = self._authorize(auth_context, "release.retire", request)
        snapshot = self._load(release_id)
        _require(bool(snapshot), "release_missing")
        if snapshot["retired"]:
            _require(snapshot["retirement"]["request_sha256"] == digest(request), "release_already_retired", ReleaseConflict)
            return self._result(snapshot, replayed=True)
        _require(snapshot["record"]["version"] == expected_version, "release_version_changed", ReleaseConflict)
        record = snapshot["record"]
        _require(record["state"] in ("promoted", "rolled_back", "rejected")
                 and all(action["state"] in ("applied", "not_applied", "cancelled") for action in record["actions"]),
                 "release_not_terminal_known_good", ReleaseConflict)
        observation = self._observation("release.retire", release_id)
        _require(observation.target == record["observed"] and observation.target["traffic_percent"] == 100
                 and observation.healthy is True, "fresh_healthy_terminal_target_required")
        approval = self._approval("release.retire", request, authorization, observation, snapshot["creator_subject"], record)

        def commit(states):
            now = self._current(authorization, approval, observation)
            envelope = self._validate_envelope(states[self.record_key(release_id)], release_id)
            if envelope["retired"]:
                _require(envelope["retirement"]["request_sha256"] == digest(request), "release_already_retired", ReleaseConflict)
                return self._result(envelope, replayed=True)
            _require(envelope["record"]["version"] == expected_version
                     and envelope["record"]["state_sha256"] == record["state_sha256"], "release_version_changed", ReleaseConflict)
            service, repository = self._slots(states, envelope)
            envelope["retired"] = True
            envelope["retirement"] = {"event_id": event_id, "request_sha256": digest(request), "retired_at": now,
                "authorization_subject": authorization.subject, "authorization_proof_sha256": authorization.proof_sha256,
                "evidence_proof_sha256": approval.proof_sha256, "observer_proof_sha256": observation.proof_sha256}
            service.update(active_release=None, active_repository=None, known_good=_copy(observation.target),
                           known_good_proof_sha256=observation.proof_sha256)
            repository.update(active_release=None, active_service=None, head_revision=observation.target["revision"])
            return self._result(envelope)

        return self._mutate(release_id, commit)
