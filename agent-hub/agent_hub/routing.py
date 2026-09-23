"""Offline, deterministic routing/admission policy. This module executes nothing.

Attestations and permissions are receipts from a trusted controller/verifier;
the Python objects do not authenticate their issuer or reproduce evaluations.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
import re
from typing import Mapping

EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
MODES = ("frontier", "hybrid", "self_hosted_only")
ESCALATIONS = ("capability_gap", "context_limit", "resources_unavailable")


class RoutingError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise RoutingError(message)


def fingerprint(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    require(len(encoded) <= 128_000, "Policy record exceeds its byte bound")
    return hashlib.sha256(encoded).hexdigest()


def identifier(value):
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,159}", value), "Invalid bounded policy identifier")
    return value


def sha(value):
    require(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value), "Evidence must use an exact SHA-256 digest")
    return value


def integer(value, minimum=0, maximum=2**50):
    require(type(value) is int and minimum <= value <= maximum, "Invalid bounded integer measurement")
    return value


def timestamp(value):
    require(type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1e11, "Invalid observation time")
    return value


@dataclass(frozen=True)
class Input:
    name: str
    kind: str
    required: bool = True

    def __post_init__(self):
        identifier(self.name)
        require(self.kind in ("string", "integer", "number", "boolean"), "Unsupported input type")
        require(type(self.required) is bool, "Input required flag must be boolean")


@dataclass(frozen=True)
class Condition:
    field: str
    operator: str
    value: str | int | float | bool | None = None

    def __post_init__(self):
        identifier(self.field)
        require(self.field.startswith(("input.", "fact.")), "Conditions must address explicit input or fact fields")
        require(self.operator in ("present", "eq", "ne", "gte", "lte"), "Unsupported condition operator")
        require(self.value is None or type(self.value) in (str, int, float, bool), "Condition values must be scalar")
        fingerprint(asdict(self))
        if self.operator in ("gte", "lte"):
            require(type(self.value) in (int, float) and math.isfinite(self.value), "Ordered conditions need a finite number")

    def satisfied(self, inputs, facts):
        scope, name = self.field.split(".", 1)
        values = inputs if scope == "input" else facts
        if self.operator == "present":
            return name in values and values[name] is not None
        if name not in values:
            return False
        actual = values[name]
        if self.operator in ("eq", "ne"):
            equal = type(actual) is type(self.value) and actual == self.value
            return equal if self.operator == "eq" else not equal
        if type(actual) not in (int, float) or not math.isfinite(actual):
            return False
        return actual >= self.value if self.operator == "gte" else actual <= self.value


@dataclass(frozen=True)
class Effect:
    kind: str
    target: str

    def __post_init__(self):
        require(self.kind in ("read", "write", "network", "model_call"), "Unsupported effect kind")
        identifier(self.target)


@dataclass(frozen=True)
class Capability:
    id: str
    artifact_sha256: str
    inputs: tuple[Input, ...]
    preconditions: tuple[Condition, ...]
    effects: tuple[Effect, ...]
    postconditions: tuple[Condition, ...]
    fallbacks: tuple[str, ...] = ()

    def __post_init__(self):
        identifier(self.id); sha(self.artifact_sha256)
        for values, expected in ((self.inputs, Input), (self.preconditions, Condition), (self.effects, Effect), (self.postconditions, Condition)):
            require(type(values) is tuple and len(values) <= 32 and all(isinstance(value, expected) for value in values), "Capability fields must be bounded typed tuples")
        require(bool(self.postconditions), "Reusable capabilities require explicit postconditions")
        require(len({item.name for item in self.inputs}) == len(self.inputs), "Duplicate input name")
        require(type(self.fallbacks) is tuple and len(self.fallbacks) <= 8 and len(set(self.fallbacks)) == len(self.fallbacks), "Invalid bounded fallback list")
        for fallback in self.fallbacks:
            identifier(fallback)
        fingerprint(asdict(self))

    @property
    def digest(self):
        return fingerprint(asdict(self))

    def accepts(self, values):
        require(isinstance(values, Mapping) and len(values) <= 32, "Inputs must be a bounded mapping")
        declared = {item.name: item for item in self.inputs}
        if not set(values) <= set(declared):
            return False
        for name, spec in declared.items():
            if name not in values:
                if spec.required:
                    return False
                continue
            value = values[name]
            if spec.kind == "string" and not (isinstance(value, str) and len(value) <= 8000):
                return False
            if spec.kind == "integer" and not (type(value) is int and abs(value) <= 2**50):
                return False
            if spec.kind == "number" and not (type(value) in (int, float) and math.isfinite(value) and abs(value) <= 2**50):
                return False
            if spec.kind == "boolean" and type(value) is not bool:
                return False
        return True

    def verify_postconditions(self, inputs, facts):
        validate_facts(facts)
        return self.accepts(inputs) and all(condition.satisfied(inputs, facts) for condition in self.postconditions)


def validate_facts(facts):
    require(isinstance(facts, Mapping) and len(facts) <= 64, "Facts must be a bounded explicit mapping")
    for key, value in facts.items():
        identifier(key)
        require(value is None or type(value) in (str, int, float, bool), "Facts must be scalar observations")
    fingerprint(dict(facts))


class CapabilityRegistry:
    def __init__(self, capabilities, *, max_fallback_hops=4):
        integer(max_fallback_hops, 0, 8)
        require(isinstance(capabilities, (tuple, list)) and len(capabilities) <= 128, "Capability registry is bounded")
        require(all(isinstance(item, Capability) for item in capabilities), "Typed capabilities required")
        self._items = {item.id: item for item in capabilities}
        require(len(self._items) == len(capabilities), "Duplicate capability ID")
        self.max_fallback_hops = max_fallback_hops
        depths = {}
        def visit(name, path):
            require(name not in path, "Fallback cycle detected")
            if name in depths:
                return depths[name]
            item = self._items[name]
            depth = 0
            for fallback in item.fallbacks:
                require(fallback in self._items, "Fallback capability is not registered")
                child = self._items[fallback]
                require(child.inputs == item.inputs and child.postconditions == item.postconditions,
                        "Fallback must preserve the typed input contract and postconditions")
                require(set(child.effects) <= set(item.effects), "Fallback cannot expand allowed effects")
                depth = max(depth, 1 + visit(fallback, (*path, name)))
                require(depth <= max_fallback_hops, "Fallback chain exceeds its bound")
            depths[name] = depth
            return depth
        for name in self._items:
            visit(name, ())

    def resolve(self, name, inputs, facts, allowed_effects):
        require(name in self._items, "Capability is not registered")
        validate_facts(facts)
        require(type(allowed_effects) is tuple and len(allowed_effects) <= 64
                and all(isinstance(effect, Effect) for effect in allowed_effects), "Explicit typed effect permissions required")
        permissions = set(allowed_effects)
        pending = [(name, ())]
        visited = set()
        while pending:
            current, chain = pending.pop(0)
            if current in visited:
                continue
            visited.add(current)
            item = self._items[current]
            path = (*chain, current)
            if item.accepts(inputs) and set(item.effects) <= permissions and all(condition.satisfied(inputs, facts) for condition in item.preconditions):
                return item, path
            if len(chain) < self.max_fallback_hops:
                pending.extend((fallback, path) for fallback in item.fallbacks)
        return None, ()


@dataclass(frozen=True)
class ModelSelection:
    provider: str
    model: str
    effort: str

    def __post_init__(self):
        identifier(self.provider); identifier(self.model)
        require(self.model not in ("auto", "latest", "default"), "Register an exact model ID, not an automatic alias")
        require(self.effort in EFFORTS, "Unknown reasoning effort")


@dataclass(frozen=True)
class ModelConfiguration:
    selection: ModelSelection
    actual_model_id: str
    location: str
    approved: bool
    quality_rank: int
    supported_efforts: tuple[str, ...]
    estimated_cost_microusd: int
    deployment_id: str | None = None

    def __post_init__(self):
        require(isinstance(self.selection, ModelSelection), "Exact typed selection required")
        identifier(self.actual_model_id)
        require(self.location in ("frontier", "google_cloud"), "Self-hosted inference must be in Google Cloud; PC/local runtimes are forbidden")
        require(type(self.approved) is bool, "Approval must be explicit")
        integer(self.quality_rank, 0, 1_000_000); integer(self.estimated_cost_microusd)
        require(type(self.supported_efforts) is tuple and 1 <= len(self.supported_efforts) <= len(EFFORTS)
                and all(value in EFFORTS for value in self.supported_efforts)
                and len(set(self.supported_efforts)) == len(self.supported_efforts)
                and self.selection.effort in self.supported_efforts, "Unsupported or unknown model effort")
        if self.location == "google_cloud":
            identifier(self.deployment_id)
        else:
            require(self.deployment_id is None, "Frontier configurations do not name a self-hosted deployment")


@dataclass(frozen=True)
class CapabilityAttestation:
    selection: ModelSelection
    actual_model_id: str
    capability_digest: str
    evidence_sha256: str
    verifier_id: str
    passed: bool
    observed_at: float
    context_tokens: int
    concurrency: int
    peak_ram_bytes: int

    def __post_init__(self):
        require(isinstance(self.selection, ModelSelection), "Attestation needs the exact model/effort")
        identifier(self.actual_model_id); identifier(self.verifier_id)
        sha(self.capability_digest); sha(self.evidence_sha256)
        require(type(self.passed) is bool, "Attestation outcome must be boolean")
        timestamp(self.observed_at); integer(self.context_tokens, 1, 2_000_000)
        integer(self.concurrency, 1, 64); integer(self.peak_ram_bytes)


@dataclass(frozen=True)
class CloudResources:
    deployment_id: str
    location: str
    loaded_model_ids: tuple[str, ...]
    measured_ram_budget_bytes: int
    active_requests: int
    context_tokens_in_use: int
    observed_at: float
    measurement_sha256: str

    def __post_init__(self):
        identifier(self.deployment_id)
        require(self.location == "google_cloud", "Personal-PC resource measurements are not eligible")
        require(type(self.loaded_model_ids) is tuple and len(self.loaded_model_ids) <= 8, "Loaded models must be explicitly counted")
        for model in self.loaded_model_ids:
            identifier(model)
        integer(self.measured_ram_budget_bytes); integer(self.active_requests, 0, 64)
        integer(self.context_tokens_in_use, 0, 128_000_000); timestamp(self.observed_at); sha(self.measurement_sha256)


@dataclass(frozen=True)
class RoutingPolicy:
    inference_authorized: bool = False
    training_authorized: bool = False
    adapter_promotion_authorized: bool = False
    max_attestation_age_seconds: int = 86400
    max_resource_age_seconds: int = 60
    ram_reserve_bytes: int = 256_000_000

    def __post_init__(self):
        for flag in (self.inference_authorized, self.training_authorized, self.adapter_promotion_authorized):
            require(type(flag) is bool, "Policy authorization flags must be explicit booleans")
        integer(self.max_attestation_age_seconds, 1, 2_592_000)
        integer(self.max_resource_age_seconds, 1, 300); integer(self.ram_reserve_bytes)


@dataclass(frozen=True)
class RoutingDecision:
    action: str
    mode: str
    reason: str
    selection: ModelSelection | None = None
    capability_digest: str | None = None
    fallback_chain: tuple[str, ...] = ()
    evidence_sha256: str | None = None
    resource_measurement_sha256: str | None = None
    estimated_cost_microusd: int | None = None
    escalation_reason: str | None = None

    @property
    def digest(self):
        return fingerprint(asdict(self))


class Router:
    def __init__(self, capabilities, configurations, attestations, *, policy=None):
        require(isinstance(capabilities, CapabilityRegistry), "A trusted capability registry is required")
        require(isinstance(configurations, (tuple, list)) and len(configurations) <= 128
                and all(isinstance(item, ModelConfiguration) for item in configurations), "Bounded typed model configurations required")
        require(isinstance(attestations, (tuple, list)) and len(attestations) <= 512
                and all(isinstance(item, CapabilityAttestation) for item in attestations), "Bounded typed attestations required")
        require(policy is None or isinstance(policy, RoutingPolicy), "Typed routing policy required")
        self.capabilities, self.policy = capabilities, policy if policy is not None else RoutingPolicy()
        self.models = {item.selection: item for item in configurations}
        require(len(self.models) == len(configurations), "Duplicate exact model configuration")
        self.attestations = tuple(attestations)
        declared = {}
        for item in configurations:
            key = item.selection.provider, item.selection.model
            properties = item.actual_model_id, item.location, item.quality_rank, frozenset(item.supported_efforts), item.deployment_id
            require(key not in declared or declared[key] == properties, "Conflicting declarations for the same model")
            declared[key] = properties

    def _attestation(self, model, capability, now):
        receipts = [item for item in self.attestations if item.selection == model.selection
                    and item.actual_model_id == model.actual_model_id and item.capability_digest == capability.digest
                    and 0 <= now - item.observed_at <= self.policy.max_attestation_age_seconds]
        if not receipts:
            return None
        latest_time = max(item.observed_at for item in receipts)
        latest_receipts = {item for item in receipts if item.observed_at == latest_time}
        if len(latest_receipts) != 1:
            return None  # Conflicting simultaneous receipts cannot establish verification.
        latest = next(iter(latest_receipts))
        return latest if latest.passed else None

    def _admission(self, model, receipt, context, resources, now):
        if context > receipt.context_tokens:
            return "context_limit"
        if model.location == "frontier":
            return None
        if (not isinstance(resources, CloudResources) or resources.deployment_id != model.deployment_id
                or not 0 <= now - resources.observed_at <= self.policy.max_resource_age_seconds
                or resources.loaded_model_ids != (model.actual_model_id,) or receipt.peak_ram_bytes <= 0
                or receipt.peak_ram_bytes + self.policy.ram_reserve_bytes > resources.measured_ram_budget_bytes
                or resources.active_requests + 1 > receipt.concurrency
                or resources.context_tokens_in_use + context > receipt.context_tokens * receipt.concurrency):
            return "resources_unavailable"
        return None

    def route(self, capability_id, *, mode, inputs, facts, allowed_effects, context_tokens,
              budget_microusd, now, resources=None, requested_model=None, escalation_reason=None):
        mode = "self_hosted_only" if mode in ("local-only", "local_only") else mode
        require(mode in MODES, "Unknown routing mode")
        timestamp(now); integer(context_tokens, 1, 2_000_000); integer(budget_microusd)
        require(requested_model is None or isinstance(requested_model, ModelSelection), "Requested model must be an exact selection")
        require(escalation_reason is None or escalation_reason in ESCALATIONS, "Escalation requires a declared reason")
        require(escalation_reason is None or mode == "hybrid", "Only hybrid mode can escalate")
        capability, chain = self.capabilities.resolve(capability_id, inputs, facts, allowed_effects)
        def deny(reason):
            return RoutingDecision("abstain", mode, reason, capability_digest=capability.digest if capability else None, fallback_chain=chain, escalation_reason=escalation_reason)
        if capability is None:
            return deny("capability_preconditions_or_permissions_failed")
        if not self.policy.inference_authorized:
            return deny("inference_not_authorized")
        if requested_model is not None and requested_model not in self.models:
            return deny("exact_configuration_not_registered")
        def assess(model):
            if not model.approved:
                return None, "configuration_not_approved"
            if requested_model is not None and model.selection != requested_model:
                return None, "exact_configuration_mismatch"
            receipt = self._attestation(model, capability, now)
            if receipt is None:
                return None, "capability_gap"
            reason = self._admission(model, receipt, context_tokens, resources, now)
            if reason:
                return None, reason
            if model.estimated_cost_microusd > budget_microusd:
                return None, "budget_insufficient"
            return receipt, None
        def choose(model, receipt, escalated=None):
            return RoutingDecision("route", mode, "verified_exact_configuration", model.selection, capability.digest, chain,
                                   receipt.evidence_sha256, resources.measurement_sha256 if model.location == "google_cloud" else None,
                                   model.estimated_cost_microusd, escalated)
        failures = set()
        if mode != "frontier":
            candidates = sorted((item for item in self.models.values() if item.location == "google_cloud" and item.approved),
                                key=lambda item: (-item.quality_rank, -EFFORTS.index(item.selection.effort), item.estimated_cost_microusd,
                                                  item.selection.provider, item.selection.model))
            if candidates:
                strongest = candidates[0]
                highest = max(strongest.supported_efforts, key=EFFORTS.index)
                exact = ModelSelection(strongest.selection.provider, strongest.selection.model, highest)
                candidate = self.models.get(exact)
                if candidate is None or not candidate.approved:
                    failures.add("highest_supported_effort_not_approved")
                else:
                    receipt, failure = assess(candidate)
                    if receipt:
                        return choose(candidate, receipt)
                    failures.add(failure)
            if not candidates:
                failures.add("capability_gap")
            if mode == "self_hosted_only":
                return deny("self_hosted_abstention:" + ",".join(sorted(failures)))
            if escalation_reason is None:
                return deny("explicit_hybrid_escalation_required")
            if escalation_reason not in failures:
                return deny("escalation_reason_does_not_match_observed_failure")
        frontier = [item for item in self.models.values() if item.location == "frontier" and item.approved]
        if not frontier:
            return deny("frontier_not_registered")
        strongest = sorted(frontier, key=lambda item: (-item.quality_rank, item.selection.provider, item.selection.model))[0]
        highest = max(strongest.supported_efforts, key=EFFORTS.index)
        exact = ModelSelection(strongest.selection.provider, strongest.selection.model, highest)
        candidate = self.models.get(exact)
        if candidate is None or not candidate.approved:
            return deny("highest_supported_effort_not_approved")
        receipt, failure = assess(candidate)
        return deny(failure) if receipt is None else choose(candidate, receipt, escalation_reason if mode == "hybrid" else None)


@dataclass(frozen=True)
class TrainingDataPermission:
    dataset_sha256: str
    purpose: str
    permission_evidence_sha256: str
    granted_by: str
    expires_at: float

    def __post_init__(self):
        sha(self.dataset_sha256); sha(self.permission_evidence_sha256); identifier(self.granted_by)
        require(self.purpose in ("inference", "training"), "Explicit data permission purpose required")
        timestamp(self.expires_at)


@dataclass(frozen=True)
class AdapterCandidate:
    adapter_sha256: str
    quantized_artifact_sha256: str
    dataset_sha256: str
    capability_digest: str

    def __post_init__(self):
        for value in asdict(self).values():
            sha(value)


@dataclass(frozen=True)
class QuantizedEvaluation:
    candidate: AdapterCandidate
    evidence_sha256: str
    verifier_id: str
    passed: bool
    observed_at: float

    def __post_init__(self):
        require(isinstance(self.candidate, AdapterCandidate), "Evaluation must bind the exact adapter and quantized artifact")
        sha(self.evidence_sha256); identifier(self.verifier_id); timestamp(self.observed_at)
        require(type(self.passed) is bool, "Evaluation result must be explicit")


def check_training_permission(dataset_sha256, permission, *, policy, now):
    sha(dataset_sha256); timestamp(now)
    return (isinstance(policy, RoutingPolicy) and policy.training_authorized
            and isinstance(permission, TrainingDataPermission) and permission.purpose == "training"
            and permission.dataset_sha256 == dataset_sha256 and now < permission.expires_at)


def check_adapter_promotion(candidate, permission, evaluation, *, policy, now):
    require(isinstance(candidate, AdapterCandidate), "Typed adapter candidate required")
    timestamp(now)
    return (isinstance(policy, RoutingPolicy) and policy.adapter_promotion_authorized
            and check_training_permission(candidate.dataset_sha256, permission, policy=policy, now=now)
            and isinstance(evaluation, QuantizedEvaluation) and evaluation.candidate == candidate and evaluation.passed
            and 0 <= now - evaluation.observed_at <= policy.max_attestation_age_seconds)
