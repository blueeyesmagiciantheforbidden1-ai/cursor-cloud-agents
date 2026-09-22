"""Serializable, evidence-bound release transitions; no deployment or other I/O.

Integration contract
--------------------
Only a trusted controller may create specs, authenticate verifiers/executors,
verify the referenced evidence bytes, and derive changed files from the actual
artifact. Hashes identify those inputs; they do not authenticate their author.
The five gates include connection continuity, so a builder's prose cannot stand
in for a passing connection test. Protected paths are a lexical additional guard,
not a sandbox or proof that an allowed edit cannot change privileged behavior.

Create records with create-if-absent semantics keyed by their content-derived ID;
never overwrite an existing release with a new initial record. Persist the entire
returned record with an atomic compare-and-swap of ``version``
(or a Firestore transaction). Serialize it with the service's active release slot
and actual deployment-generation observation, across *all* candidate records.
The reducer only guards this record: it cannot observe Git, traffic or another
release. A deployment adapter must enforce action.expected["generation"] as an
external resource precondition, and implement each exact traffic percentage.

``transition`` may run inside a retried transaction: it is pure. Only the result
of a confirmed committed ``claim`` may be dispatched. Repeated event IDs return
``dispatch=None``; a claimed action is never claimable again. If commit/dispatch
acknowledgment is lost, do NOT dispatch from a reread or repeat the claim. Mark
the action uncertain, independently establish its terminal external outcome,
then reconcile using the same action ID. This sacrifices automatic retry for
safety; it is not a claim of exactly-once external execution.

Seed the base only from an independently verified known-good deployment; this
module cannot establish that fact from its revision hash. Keep its artifact
available. Failed health creates a rollback
intent; only a verified applied rollback receipt records ``rolled_back``. Failed
or uncertain rollback needs operator recovery. No code here deploys or rolls back
anything. Keep the active slot through monitoring; after releasing it, use global
generation/slot checks so old health events cannot roll back a newer release.
The budget gate's real reservation must cover the entire release including its
rollback and remain held until settlement/reconciliation. It is not merely an
estimate; this reducer checks supplied accounting but cannot reserve money.
Storage/authentication, artifact availability, real tests, timers and dispatch
are deliberately injected by the controller, not implemented by this module.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re

from .contracts import ContractError, _path


GATES = ("unit", "integration", "connection_continuity", "security", "budget")
MAX_EVENTS = 128
MAX_RECORD_BYTES = 120_000
PROTECTED_WORDS = frozenset(("evaluator", "evaluators", "permission", "permissions",
                             "budget", "budgets", "credential", "credentials", "secret",
                             "secrets", "auth", "oauth", "token", "tokens", "policy", "policies"))
PROTECTED_MODULES = frozenset(("core", "server", "store", "worker", "contracts", "research", "workflows", "transfer",
                              "routing", "model_policy", "model_runtime", "subscription_auth",
                              "credential_state", "cloud_credential_broker", "improvement_release"))


class ReleaseError(ValueError):
    pass


class ReleaseConflict(ReleaseError):
    pass


def _require(condition, reason, error=ReleaseError):
    if not condition:
        raise error(reason)


def _json(value):
    try:
        data = json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ReleaseError("finite_json_required") from None
    _require(len(data) <= MAX_RECORD_BYTES, "record_size_limit")
    return data


def digest(value):
    """SHA-256 of bounded canonical JSON, also used for a changed-file manifest."""
    return hashlib.sha256(_json(value)).hexdigest()


def _copy(value):
    return json.loads(_json(value))


def _keys(value, required):
    _require(isinstance(value, dict) and set(value) == set(required), "missing_or_unknown_fields")


def _hash(value):
    _require(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None, "sha256_required")


def _revision(value):
    _require(isinstance(value, str) and re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", value) is not None,
             "exact_revision_required")


def _id(value):
    _require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value) is not None,
             "bounded_identifier_required")


def _integer(value, low=0, high=10**12):
    _require(type(value) is int and low <= value <= high, "bounded_integer_required")


def _target(revision, artifact, generation, traffic_percent=100):
    _revision(revision); _hash(artifact); _id(generation); _integer(traffic_percent, 1, 100)
    return {"revision": revision, "artifact_sha256": artifact, "generation": generation,
            "traffic_percent": traffic_percent}


def validate_spec(spec):
    """Return an isolated immutable-by-identity spec supplied by the controller."""
    spec = _copy(spec)
    _keys(spec, ("repository", "base_revision", "base_artifact_sha256", "base_generation",
                 "candidate_revision", "candidate_artifact_sha256", "changed_files", "policy"))
    _id(spec["repository"])
    _target(spec["base_revision"], spec["base_artifact_sha256"], spec["base_generation"])
    _revision(spec["candidate_revision"]); _hash(spec["candidate_artifact_sha256"])
    _require(spec["candidate_revision"] != spec["base_revision"]
             and spec["candidate_artifact_sha256"] != spec["base_artifact_sha256"], "distinct_candidate_required")
    files = spec["changed_files"]
    _require(isinstance(files, list) and 1 <= len(files) <= 256
             and all(isinstance(item, str) for item in files), "changed_files_required")
    _require(files == sorted(set(files)) and len({item.casefold() for item in files}) == len(files),
             "canonical_unique_changed_files_required")
    for path in files:
        try:
            _path(path)
        except ContractError:
            raise ReleaseError("protected_or_unsafe_path") from None
        parts = path.casefold().split("/")
        words = {word for part in parts for word in re.split(r"[_.-]", part)}
        module = parts[-1].removesuffix(".py")
        _require(parts[0] not in ("deploy", "connectors") and not words.intersection(PROTECTED_WORDS)
                 and not any(part == ".env" or part.startswith(".env.") for part in parts)
                 and not (parts[0] == "agent_hub" and module in PROTECTED_MODULES), "protected_or_unsafe_path")
    policy = spec["policy"]
    _keys(policy, ("evaluator_sha256", "permissions_sha256", "budget_sha256", "gate_definitions",
                   "health_definition_sha256", "max_cost_microusd", "canary_percent",
                   "min_canary_seconds", "min_promotion_seconds", "receipt_max_age_seconds"))
    for key in ("evaluator_sha256", "permissions_sha256", "budget_sha256", "health_definition_sha256"):
        _hash(policy[key])
    _keys(policy["gate_definitions"], GATES)
    for value in policy["gate_definitions"].values():
        _hash(value)
    _integer(policy["max_cost_microusd"], 1)
    _integer(policy["canary_percent"], 1, 25)
    _integer(policy["min_canary_seconds"], 1, 3600)
    _integer(policy["min_promotion_seconds"], 1, 3600)
    _integer(policy["receipt_max_age_seconds"], 1, 86400)
    _require(policy["receipt_max_age_seconds"] >= policy["min_canary_seconds"], "impossible_canary_evidence_window")
    return spec


def _seal(record):
    record.pop("state_sha256", None)
    record["state_sha256"] = digest(record)
    return record


def create_release(spec, *, now):
    """Create a serializable record; caller must atomically reserve the release slot."""
    _integer(now)
    spec = validate_spec(spec)
    return _seal({"schema": 1, "id": digest(spec), "spec": spec, "version": 0,
                  "created_at": now, "updated_at": now, "state": "collecting",
                  "gates": {}, "actions": [], "events": {}, "last_health": None,
                  "phase_started_at": None, "blocked_phase": None, "abort": None,
                  "observed": _target(spec["base_revision"], spec["base_artifact_sha256"], spec["base_generation"])})


@dataclass(frozen=True)
class Transition:
    record: dict
    dispatch: dict | None = None
    replayed: bool = False


def _fresh(record, receipt, now, *, earliest=None):
    _integer(receipt["observed_at"])
    _require((record["created_at"] if earliest is None else earliest) <= receipt["observed_at"] <= now
             and now - receipt["observed_at"] <= record["spec"]["policy"]["receipt_max_age_seconds"],
             "stale_or_future_evidence")


def _receipt(record, receipt, now, definition, *, earliest=None):
    _require(receipt["release_id"] == record["id"], "evidence_release_mismatch")
    _require(receipt["definition_sha256"] == definition
             and receipt["evaluator_sha256"] == record["spec"]["policy"]["evaluator_sha256"],
             "evaluator_or_definition_mismatch")
    _hash(receipt["report_sha256"]); _id(receipt["verifier"])
    _require(receipt["status"] in ("passed", "failed", "unknown"), "explicit_test_status_required")
    _fresh(record, receipt, now, earliest=earliest)


RECEIPT_FIELDS = ("release_id", "status", "definition_sha256", "evaluator_sha256", "report_sha256",
                  "verifier", "observed_at")


def _ready(record, now):
    _require(set(record["gates"]) == set(GATES), "missing_gates", ReleaseConflict)
    for receipt in record["gates"].values():
        _require(receipt["status"] == "passed", "gate_not_passed", ReleaseConflict)
        _fresh(record, receipt, now)


def _guard(record, observation):
    _keys(observation, ("revision", "artifact_sha256", "generation", "traffic_percent"))
    _target(observation["revision"], observation["artifact_sha256"], observation["generation"], observation["traffic_percent"])
    _require(observation == record["observed"], "deployment_revision_or_generation_changed", ReleaseConflict)


def _intent(record, kind):
    _require(len(record["actions"]) < 3 and not any(item["kind"] == kind for item in record["actions"]),
             "action_already_created", ReleaseConflict)
    spec = record["spec"]
    action = {"id": digest({"release_id": record["id"], "kind": kind}), "release_id": record["id"],
              "kind": kind, "state": "queued", "expected": _copy(record["observed"]),
              "target_revision": spec["base_revision"] if kind == "rollback" else spec["candidate_revision"],
              "target_artifact_sha256": spec["base_artifact_sha256"] if kind == "rollback" else spec["candidate_artifact_sha256"],
              "traffic_percent": spec["policy"]["canary_percent"] if kind == "canary" else 100,
              "evidence_sha256": digest(record["gates"]), "health_sha256": digest(record["last_health"]),
              "claimed_by": None, "claimed_at": None, "receipt": None}
    record["actions"].append(action)
    record["state"] = kind + "_pending"
    record["blocked_phase"] = None


def _action(record, identifier):
    _hash(identifier)
    found = next((item for item in record["actions"] if item["id"] == identifier), None)
    _require(found is not None and found is record["actions"][-1], "not_current_action", ReleaseConflict)
    return found


def _apply_ack(record, action, payload, now):
    outcome = payload["outcome"]
    _require(outcome in ("applied", "not_applied", "unknown"), "explicit_action_outcome_required")
    _hash(payload["receipt_sha256"])
    _fresh(record, payload, now, earliest=action["claimed_at"])
    action["receipt"] = _copy(payload)
    if outcome == "unknown":
        _require(payload["observation"] is None, "unknown_outcome_cannot_assert_target")
        action["state"], record["state"] = "uncertain", "reconcile_required"
        return
    observation = payload["observation"]
    _keys(observation, ("revision", "artifact_sha256", "generation", "traffic_percent"))
    _target(observation["revision"], observation["artifact_sha256"], observation["generation"], observation["traffic_percent"])
    if outcome == "not_applied":
        _require(observation == action["expected"], "not_applied_state_mismatch")
        action["state"] = "not_applied"
        if action["kind"] == "promote":
            _intent(record, "rollback")
        else:
            record["state"] = "rollback_failed" if action["kind"] == "rollback" else "rejected"
        return
    _require(observation["revision"] == action["target_revision"]
             and observation["artifact_sha256"] == action["target_artifact_sha256"]
             and observation["traffic_percent"] == action["traffic_percent"]
             and observation["generation"] != action["expected"]["generation"], "applied_target_or_generation_mismatch")
    action["state"], record["observed"] = "applied", _copy(observation)
    record["phase_started_at"] = payload["observed_at"]
    record["last_health"] = None
    record["state"] = {"canary": "canary", "promote": "promotion_verifying", "rollback": "rolled_back"}[action["kind"]]


def transition(record, event, *, expected_version, now):
    """Pure transition; event = {id, kind, payload}. Commit before using dispatch.

    Kinds: evidence(receipt plus gate); stage(observation); claim(action_id,
    executor, observation); acknowledge(action_id, executor, outcome,
    receipt_sha256, observation, observed_at); uncertain(action_id);
    reconcile(acknowledgment fields plus execution_terminal=True);
    health(test receipt plus observation and window_started_at); promote(observation);
    abort(observation, reason, report_sha256). Abort is an authenticated controller
    operation, including when gates or reservations expire after staging. It
    does not require inventing a failed health report. In-flight external effects
    must first be marked uncertain and reconciled to a known terminal outcome.

    ``reconcile`` is a separately authenticated operator/controller route. An
    observation must be fetched from the deployment API, not supplied by agents.
    A health report must measure the specified deployment generation throughout
    the controller's requested bounded observation window. The reducer checks
    the minimum elapsed time, but cannot itself run a continuous health probe.
    """
    _integer(now); _integer(expected_version)
    record, event = _copy(record), _copy(event)
    seal = record.pop("state_sha256", None)
    _require(record.get("schema") == 1 and seal == digest(record), "corrupt_release_record")
    _require(record["id"] == digest(validate_spec(record["spec"])), "immutable_spec_changed")
    _keys(event, ("id", "kind", "payload")); _id(event["id"])
    event_hash = digest(event)
    prior = record["events"].get(event["id"])
    if prior is not None:
        _require(prior == event_hash, "idempotency_key_reused", ReleaseConflict)
        return Transition(_seal(record), replayed=True)
    _require(record["version"] == expected_version, "release_version_changed", ReleaseConflict)
    _require(now >= record["updated_at"], "clock_went_backwards")
    _require(len(record["events"]) < MAX_EVENTS, "event_limit_requires_operator_reconciliation")
    kind, payload, state = event["kind"], event["payload"], record["state"]
    _require(isinstance(kind, str) and isinstance(payload, dict), "invalid_event")
    dispatch = None
    if kind == "evidence":
        _require(state in ("collecting", "blocked", "ready"), "evidence_frozen", ReleaseConflict)
        gate = payload.get("gate")
        _require(gate in GATES, "unknown_gate")
        extra = ("spent_microusd", "reserved_microusd", "reservation_sha256") if gate == "budget" else ()
        _keys(payload, (*RECEIPT_FIELDS, "gate", *extra))
        _receipt(record, payload, now, record["spec"]["policy"]["gate_definitions"][gate])
        old = record["gates"].get(gate)
        _require(old is None or payload["observed_at"] > old["observed_at"], "nonnewer_gate_evidence", ReleaseConflict)
        if gate == "budget":
            _integer(payload["spent_microusd"]); _integer(payload["reserved_microusd"], 1)
            _hash(payload["reservation_sha256"])
            _require(payload["status"] != "passed" or payload["spent_microusd"] + payload["reserved_microusd"]
                     <= record["spec"]["policy"]["max_cost_microusd"], "budget_overrun_cannot_pass")
        record["gates"][gate] = payload
        statuses = [item["status"] for item in record["gates"].values()]
        record["state"] = ("rejected" if "failed" in statuses else "blocked" if "unknown" in statuses
                           else "ready" if len(statuses) == len(GATES) else "collecting")
    elif kind in ("stage", "promote"):
        _keys(payload, ("observation",))
        _require(state == ("ready" if kind == "stage" else "canary_healthy"), "release_not_ready", ReleaseConflict)
        _guard(record, payload["observation"]); _ready(record, now)
        if kind == "promote":
            _fresh(record, record["last_health"], now, earliest=record["phase_started_at"])
        _intent(record, "canary" if kind == "stage" else "promote")
    elif kind == "claim":
        _keys(payload, ("action_id", "executor", "observation")); _id(payload["executor"])
        action = _action(record, payload["action_id"])
        _require(action["state"] == "queued" and state == action["kind"] + "_pending",
                 "action_already_claimed_or_blocked", ReleaseConflict)
        _guard(record, payload["observation"])
        if action["kind"] != "rollback":
            _ready(record, now)
            if action["kind"] == "promote":
                _fresh(record, record["last_health"], now, earliest=record["phase_started_at"])
        # Pending promotion may have been blocked and then received fresh health.
        # Freeze the evidence used at the one dispatch boundary, not an older poll.
        action.update(state="claimed", claimed_by=payload["executor"], claimed_at=now,
                      evidence_sha256=digest(record["gates"]), health_sha256=digest(record["last_health"]))
        dispatch = _copy(action)
    elif kind in ("acknowledge", "reconcile"):
        fields = ("action_id", "executor", "outcome", "receipt_sha256", "observation", "observed_at")
        _keys(payload, (*fields, "execution_terminal") if kind == "reconcile" else fields)
        action = _action(record, payload["action_id"])
        _require(action["state"] == ("uncertain" if kind == "reconcile" else "claimed"),
                 "action_outcome_already_recorded", ReleaseConflict)
        _require(payload["executor"] == action["claimed_by"], "executor_mismatch", ReleaseConflict)
        if kind == "reconcile":
            _require(payload["execution_terminal"] is True and payload["outcome"] != "unknown",
                     "verified_terminal_outcome_required")
        _apply_ack(record, action, payload, now)
    elif kind == "uncertain":
        _keys(payload, ("action_id",))
        action = _action(record, payload["action_id"])
        _require(action["state"] == "claimed", "action_not_in_flight", ReleaseConflict)
        action["state"], record["state"] = "uncertain", "reconcile_required"
    elif kind == "abort":
        _keys(payload, ("observation", "reason", "report_sha256"))
        _require(payload["reason"] in ("operator_requested", "evidence_expired", "budget_unavailable",
                                        "connection_at_risk", "superseded"), "explicit_abort_reason_required")
        _hash(payload["report_sha256"]); _guard(record, payload["observation"])
        _require(state in ("collecting", "blocked", "ready", "canary_pending", "canary", "canary_healthy",
                           "promote_pending", "promotion_verifying", "promoted", "health_blocked"),
                 "abort_requires_known_outcome", ReleaseConflict)
        record["abort"] = _copy(payload)
        if record["actions"]:
            action = record["actions"][-1]
            _require(action["state"] in ("queued", "applied"), "inflight_action_requires_reconciliation", ReleaseConflict)
            if action["state"] == "queued":
                action["state"] = "cancelled"
        if record["observed"]["revision"] == record["spec"]["base_revision"]:
            record["state"] = "rejected"
        else:
            _intent(record, "rollback")
    elif kind == "health":
        _keys(payload, (*RECEIPT_FIELDS, "observation", "window_started_at"))
        _require(state in ("canary", "canary_healthy", "promote_pending", "promotion_verifying", "promoted", "health_blocked"),
                 "health_not_expected", ReleaseConflict)
        if state == "promote_pending" or record["blocked_phase"] == "promote_pending":
            _require(record["actions"][-1]["state"] == "queued", "inflight_action_requires_reconciliation", ReleaseConflict)
        _guard(record, payload["observation"])
        _receipt(record, payload, now, record["spec"]["policy"]["health_definition_sha256"],
                 earliest=record["phase_started_at"])
        _integer(payload["window_started_at"])
        _require(record["phase_started_at"] <= payload["window_started_at"] <= payload["observed_at"],
                 "health_window_wrong_phase")
        previous = record["last_health"]
        _require(previous is None or payload["observed_at"] > previous["observed_at"], "nonnewer_health_evidence", ReleaseConflict)
        phase = record["blocked_phase"] if state == "health_blocked" else state
        record["last_health"] = payload
        if payload["status"] == "failed":
            if phase == "promote_pending":
                record["actions"][-1]["state"] = "cancelled"
            _intent(record, "rollback")
        elif payload["status"] == "unknown":
            record["blocked_phase"], record["state"] = phase, "health_blocked"
        else:
            is_canary = phase in ("canary", "canary_healthy", "promote_pending")
            wait = record["spec"]["policy"]["min_canary_seconds" if is_canary else "min_promotion_seconds"]
            _require(payload["observed_at"] - payload["window_started_at"] >= wait, "health_window_incomplete")
            record["state"] = "promote_pending" if phase == "promote_pending" else "canary_healthy" if is_canary else "promoted"
            record["blocked_phase"] = None
    else:
        raise ReleaseError("unknown_transition")
    # Preserve room for failed health and its rollback lifecycle in a bounded run.
    _require(len(record["events"]) < MAX_EVENTS - 16 or kind in ("uncertain", "reconcile", "acknowledge", "abort")
             or (kind == "health" and payload["status"] == "failed")
             or (kind == "claim" and record["actions"][-1]["kind"] == "rollback"), "normal_event_budget_exhausted")
    record["events"][event["id"]] = event_hash
    record["version"] += 1
    record["updated_at"] = now
    return Transition(_seal(record), dispatch=dispatch)
