"""A deliberately small, auditable experiment in improving a repair method.

The learned object is an executable search policy, not a set of test answers.
Each inferential unit starts a new development lineage, freezes its policy and
optional repair-macro library, and then runs a paired 2x2 experiment.  A task's
private scenario/required_guards fields never enter proposal or training code.

This is a bounded DSL pilot: exhaustive success means success inside the
domain verifier's finite horizon, not correctness of a production workflow.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from hashlib import sha256
import json
import math
from pathlib import Path
from typing import Any, Iterable

from . import domain


GUARDS = ("terminal", "fence", "lease", "owner", "once")
MASKS = tuple(range(1 << len(GUARDS)))
ARMS = ("M0_C0", "M0_C1", "M1_C0", "M1_C1")
CONDITIONS = ("fresh", "shift")
CONTRASTS = ("method_without_memory", "memory_without_method",
             "method_with_memory", "joint", "half_interaction")
METHOD_FAMILY_SIZE = 4


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value: Any) -> str:
    return sha256(_json(value).encode()).hexdigest()


def _seed(master: int, unit: int, stage: str) -> int:
    # Domain-separated deterministic streams. No seed is chosen after a result.
    return int.from_bytes(sha256(f"frontier-v1:{master}:{unit}:{stage}".encode()).digest()[:8], "big")


def _mask(candidate: dict) -> int:
    names = set(domain.guard_names(candidate))
    return sum(1 << i for i, name in enumerate(GUARDS) if name in names)


def _empty_cost() -> dict[str, int]:
    return {k: 0 for k in ("proposal_calls", "generated_candidates", "verifier_calls",
                           "failed_candidates", "checked_states", "checked_transitions",
                           "checked_policy_decisions", "counterexamples", "source_bytes")}


def _add_cost(target: dict, other: dict) -> None:
    for key in target:
        target[key] += other.get(key, 0)


def _verification_cost(result: Any) -> dict[str, int]:
    cost = _empty_cost()
    cost.update(verifier_calls=1, failed_candidates=int(not result.acceptance_eligible),
                checked_states=result.checked_states,
                checked_transitions=result.checked_transitions,
                checked_policy_decisions=result.checked_policy_decisions,
                counterexamples=int(bool(result.counterexample)))
    return cost


@dataclass(frozen=True)
class ResearchMethod:
    """Frozen, serializable policy; no task labels, answers, or mutable state."""

    name: str
    strategy: str = "breadth"
    guard_order: tuple[int, ...] = (0, 1, 2, 3, 4)
    failure_rules: tuple[tuple[str, int], ...] = ()
    development_digest: str = ""
    capability_admission: str = "memory_first"
    final_slot_fallback_mask: int | None = None

    def __post_init__(self) -> None:
        if self.strategy not in ("breadth", "cumulative", "counterexample", "all_guards_first"):
            raise ValueError("unknown method strategy")
        if self.capability_admission not in ("memory_first", "after_success"):
            raise ValueError("unknown capability admission policy")
        if self.final_slot_fallback_mask is not None and (
                type(self.final_slot_fallback_mask) is not int or self.final_slot_fallback_mask != 31):
            raise ValueError("only the independently known complete-guard mask 31 is a supported fallback")
        if sorted(self.guard_order) != list(range(len(GUARDS))):
            raise ValueError("guard_order must be a permutation")
        labels = [label for label, _ in self.failure_rules]
        if len(labels) != len(set(labels)) or any(i not in range(5) for _, i in self.failure_rules):
            raise ValueError("failure rules must have unique labels and valid guards")

    def to_dict(self) -> dict:
        return {"name": self.name, "strategy": self.strategy,
                "guard_order": list(self.guard_order),
                "failure_rules": [[k, v] for k, v in self.failure_rules],
                "development_digest": self.development_digest,
                "capability_admission": self.capability_admission,
                "final_slot_fallback_mask": self.final_slot_fallback_mask}

    @classmethod
    def from_dict(cls, data: dict) -> "ResearchMethod":
        return cls(data["name"], data["strategy"], tuple(data["guard_order"]),
                   tuple((str(k), int(v)) for k, v in data["failure_rules"]),
                   data.get("development_digest", ""),
                   data.get("capability_admission", "memory_first"),
                   data.get("final_slot_fallback_mask"))

    @property
    def digest(self) -> str:
        return _hash(self.to_dict())


@dataclass(frozen=True)
class Capability:
    """An optional external tool library, kept distinct from the search policy."""

    macro_masks: tuple[int, ...] = ()
    development_digest: str = ""

    def __post_init__(self) -> None:
        if len(set(self.macro_masks)) != len(self.macro_masks) or any(m not in MASKS for m in self.macro_masks):
            raise ValueError("capability macros must be unique masks in the bounded DSL")

    def to_dict(self) -> dict:
        return {"macro_masks": list(self.macro_masks),
                "development_digest": self.development_digest,
                "kind": "external_repair_macro_memory"}


ORIGINAL_METHOD = ResearchMethod("original_breadth")
ORIGINAL_CAPABILITY = Capability()
ALL_GUARDS_BASELINE = ResearchMethod("human_all_guards_first", "all_guards_first")


def _proposal_core(spec: dict, base_mask: int, observations: list[dict],
                   macro_masks: Iterable[int] = (), total_budget: int | None = None) -> int:
    """Closed policy interpreter. Its arguments contain no task oracle data.

    Failure feedback at evaluation time is allowed and charged to the same
    verifier-call budget. It does not update the frozen cross-task policy.
    """
    seen = {row["mask"] for row in observations}
    strategy = spec["strategy"]
    admission = spec["capability_admission"]
    if admission not in ("memory_first", "after_success"):
        raise ValueError("unknown capability admission policy")
    fallback = spec["final_slot_fallback_mask"]
    if fallback is not None:
        if type(fallback) is not int or fallback != 31:
            raise ValueError("unsupported completion fallback")
        if type(total_budget) is not int or not 1 <= total_budget <= 32:
            raise ValueError("fallback-enabled policies require total_budget in [1,32]")
        if len(observations) >= total_budget:
            raise ValueError("candidate budget exhausted")
        # This is an explicit known-domain completion rule, not a learned
        # general search advantage. Its verification consumes the final slot.
        if (len(observations) + 1 == total_budget and fallback not in seen and
                not any(row["passed"] for row in observations)):
            return fallback
    if not observations:
        return 31 if strategy == "all_guards_first" else base_mask
    order = spec["guard_order"]
    preferred: list[int] = []
    if strategy == "counterexample":
        rules = dict(spec["failure_rules"])
        last = observations[-1]
        repair = rules.get(last.get("failure_kind"))
        if not last["passed"] and repair is not None:
            preferred.append(last["mask"] | (1 << repair))
    # A memory proposal consumes a real candidate slot. The revised policy
    # protects every pre-success method slot; memory may compete only after
    # an incumbent is verified. The original memory-first control is retained.
    if admission == "memory_first" or any(row["passed"] for row in observations):
        preferred.extend(base_mask | int(m) for m in macro_masks)
    if strategy == "cumulative":
        current = base_mask
        for guard in order:
            current |= 1 << guard
            preferred.append(current)
    rank = {guard: i for i, guard in enumerate(order)}
    def score(mask: int) -> tuple:
        edits = mask ^ base_mask
        selected = tuple(sorted(rank[i] for i in range(5) if edits & (1 << i)))
        return (edits.bit_count(), selected, mask)
    preferred.extend(sorted(range(32), key=score))
    for mask in preferred:
        if mask not in seen:
            return mask
    raise ValueError("candidate lattice exhausted")


def propose(method: ResearchMethod, base_mask: int, observations: list[dict],
            capability: Capability = ORIGINAL_CAPABILITY,
            total_budget: int | None = None) -> int:
    """Propose one candidate; completion-enabled policies require total_budget.

    The caller must supply the same declared total budget on every proposal
    of a task. This argument carries resource information, never an answer.
    """
    if base_mask not in MASKS:
        raise ValueError("base mask outside bounded DSL")
    return _proposal_core(method.to_dict(), base_mask, observations,
                          capability.macro_masks, total_budget)


def compile_method(method: ResearchMethod) -> str:
    """Export an executable policy with the same total_budget call contract."""
    import inspect
    core = inspect.getsource(_proposal_core)
    return ("# Frozen development-only search policy. Standard library only.\n"
            "from __future__ import annotations\nfrom typing import Iterable\n\n"
            f"METHOD = {method.to_dict()!r}\n\n" + core +
            "\ndef propose(base_mask, observations, macro_masks=(), total_budget=None):\n"
            "    return _proposal_core(METHOD, base_mask, observations, macro_masks, total_budget)\n")


def run_search(task: Any, method: ResearchMethod, capability: Capability,
               budget: int) -> dict:
    """Run exactly budget checks, including failures and checks after success.

    Equal candidate slots are enforced, but primitive verification work can
    differ because failing candidates can have short counterexamples. Actual
    graph work is therefore reported rather than called equal compute.
    """
    if isinstance(budget, bool) or not isinstance(budget, int) or not 1 <= budget <= 32:
        raise ValueError("budget must be an integer in [1,32]")
    base = _mask(task.initial_candidate)
    observations: list[dict] = []
    cost = _empty_cost()
    admission = {"policy": method.capability_admission,
                 "macro_count": len(capability.macro_masks),
                 "eligible_slots": 0, "deferred_slots": 0,
                 "proposals_matching_macro": 0}
    macro_masks = {base | m for m in capability.macro_masks}
    fallback_audit = {"mask": method.final_slot_fallback_mask,
                      "invocations": 0, "accepted": False,
                      "provenance": "fixed complete-guard domain baseline; not a learned primitive"}
    for slot in range(budget):
        memory_eligible = bool(macro_masks) and bool(observations) and (
            method.capability_admission == "memory_first" or
            any(row["passed"] for row in observations))
        if macro_masks:
            admission["eligible_slots" if memory_eligible else "deferred_slots"] += 1
        fallback_invoked = (method.final_slot_fallback_mask is not None and slot + 1 == budget and
                            not any(row["passed"] for row in observations) and
                            method.final_slot_fallback_mask not in {row["mask"] for row in observations})
        mask = propose(method, base, observations, capability, total_budget=budget)
        admission["proposals_matching_macro"] += int(memory_eligible and mask in macro_masks)
        spec = domain.candidate_spec(mask)
        result = domain.verify(task, spec)
        if fallback_invoked:
            fallback_audit["invocations"] += 1
            fallback_audit["accepted"] = bool(result.acceptance_eligible)
        _add_cost(cost, _verification_cost(result))
        cost["proposal_calls"] += 1
        cost["generated_candidates"] += 1
        observations.append({"slot": slot + 1, "mask": mask,
                             "passed": bool(result.acceptance_eligible),
                             "bounded_verification_passed": bool(result.passed),
                             "acceptance_eligible": bool(result.acceptance_eligible),
                             "failure_kind": result.failure_kind,
                             "checked_states": result.checked_states,
                             "checked_transitions": result.checked_transitions,
                             "checked_policy_decisions": result.checked_policy_decisions,
                             "memory_eligible": memory_eligible,
                             "matches_memory_macro": bool(memory_eligible and mask in macro_masks),
                             "fallback_invoked": fallback_invoked,
                             "counterexample": result.to_dict()["counterexample"]})
    successful = [row for row in observations if row["passed"]]
    best = min(successful, key=lambda r: (r["mask"].bit_count(), r["mask"])) if successful else None
    return {"task_id": task.task_id, "solved": bool(successful),
            "first_success_slot": successful[0]["slot"] if successful else None,
            "selected_mask": best["mask"] if best else None,
            "selected_guard_count": best["mask"].bit_count() if best else None,
            "observations": observations, "cost": cost, "capability_admission": admission,
            "completion_fallback": fallback_audit}


def _discover(tasks: list[Any]) -> dict:
    """Controlled development interventions over all 32 closed-DSL programs."""
    evidence: dict[str, Counter] = defaultdict(Counter)
    failures: Counter = Counter()
    frequency: Counter = Counter()
    macro_frequency: Counter = Counter()
    records = []
    cost = _empty_cost()
    for task in tasks:
        rows = {}
        for mask in MASKS:
            result = domain.verify(task, domain.candidate_spec(mask))
            _add_cost(cost, _verification_cost(result))
            cost["generated_candidates"] += 1
            rows[mask] = {"passed": bool(result.acceptance_eligible),
                          "bounded_verification_passed": bool(result.passed),
                          "failure_kind": result.failure_kind}
            if not result.acceptance_eligible:
                failures[str(result.failure_kind)] += 1
        # An intervention must remove/change the observed failure. The learner
        # never assumes that a label happens to spell an operator's name.
        for mask, result in rows.items():
            if result["passed"]:
                continue
            label = str(result["failure_kind"])
            for guard in range(len(GUARDS)):
                if mask & (1 << guard):
                    continue
                repaired = rows[mask | (1 << guard)]
                if repaired["passed"] or repaired["failure_kind"] != result["failure_kind"]:
                    evidence[label][guard] += 1
                    frequency[guard] += 1
        successful = [m for m, r in rows.items() if r["passed"]]
        if successful:
            minimum = min(m.bit_count() for m in successful)
            for mask in successful:
                if mask.bit_count() == minimum:
                    macro_frequency[mask] += 1
        records.append({"task_id": task.task_id,
                        "candidate_results": [{"mask": m, **rows[m]} for m in MASKS]})
    digest = _hash(records)
    order = tuple(sorted(range(5), key=lambda g: (-frequency[g], g)))
    rules = tuple((label, min(counts, key=lambda g: (-counts[g], g)))
                  for label, counts in sorted(evidence.items()))
    macros = tuple(sorted(macro_frequency,
                          key=lambda m: (-macro_frequency[m], m.bit_count(), m))[:2])
    return {"guard_order": order, "failure_rules": rules,
            "capability": Capability(macros, digest), "development_digest": digest,
            "failure_counts": dict(sorted(failures.items())),
            "intervention_evidence": {label: {str(g): c for g, c in sorted(counts.items())}
                                      for label, counts in sorted(evidence.items())},
            "records": records, "cost": cost}


def _method_family(discovery: dict) -> tuple[ResearchMethod, ...]:
    order = discovery["guard_order"]
    rules = discovery["failure_rules"]
    digest = discovery["development_digest"]
    # Even the baseline-like learned option carries the revised composition
    # and known-domain completion rules. ORIGINAL_METHOD remains unchanged.
    return (ResearchMethod("original_breadth", development_digest=digest,
                           capability_admission="after_success", final_slot_fallback_mask=31),
            ResearchMethod("learned_order", "breadth", order, (), digest, "after_success", 31),
            ResearchMethod("learned_cumulative", "cumulative", order, (), digest, "after_success", 31),
            ResearchMethod("learned_counterexample", "counterexample", order, rules, digest, "after_success", 31))


def _pareto(rows: list[dict]) -> list[str]:
    """Archive the development accuracy / transition-work frontier."""
    def dominates(a: dict, b: dict) -> bool:
        av = (a["solved_tasks"], -a["cost"]["checked_transitions"])
        bv = (b["solved_tasks"], -b["cost"]["checked_transitions"])
        return all(x >= y for x, y in zip(av, bv)) and any(x > y for x, y in zip(av, bv))
    return [r["method_digest"] for r in rows if not any(dominates(o, r) for o in rows)]


def learn_method(development: list[Any], selection: list[Any], budget: int) -> dict:
    """Only development data are accepted; confirmation access is rejected."""
    if not development or not selection:
        raise ValueError("both development discovery and selection tasks are required")
    if any(task.split != "development" for task in development + selection):
        raise ValueError("method learning accepts development tasks only")
    ids = [task.task_id for task in development + selection]
    if len(ids) != len(set(ids)):
        raise ValueError("discovery and selection task identities must be disjoint")
    discovery = _discover(development)
    family = _method_family(discovery)
    archive = []
    total = dict(discovery["cost"])
    for method in family:
        results = [run_search(t, method, ORIGINAL_CAPABILITY, budget) for t in selection]
        cost = _empty_cost()
        for row in results:
            _add_cost(cost, row["cost"])
        _add_cost(total, cost)
        archive.append({"method": method.to_dict(), "method_digest": method.digest,
                        "parent_digest": ORIGINAL_METHOD.digest,
                        "mutation": method.strategy + ":development_order",
                        "solved_tasks": sum(r["solved"] for r in results),
                        "first_success_total": sum(r["first_success_slot"] or (budget + 1) for r in results),
                        "cost": cost, "results": results})
    # This rule and tie break are fixed before confirmation. Method selection
    # uses original capability, preventing repair-memory gains deciding M1.
    selected = min(range(len(archive)), key=lambda i: (
        -archive[i]["solved_tasks"], archive[i]["first_success_total"],
        archive[i]["cost"]["checked_transitions"], i))
    method = family[selected]
    freeze_record = {"discovery_ids": [t.task_id for t in development],
                     "selection_ids": [t.task_id for t in selection],
                     "method_digest": method.digest,
                     "capability": discovery["capability"].to_dict()}
    return {"method": method, "capability": discovery["capability"],
            "freeze_digest": _hash(freeze_record), "freeze_record": freeze_record,
            "discovery": discovery, "archive": archive,
            "pareto_method_digests": _pareto(archive), "cost": total}


def paired_hoeffding(differences: Iterable[float], alpha: float = 0.05,
                    candidates: int = 1, conditions: int = 1,
                    comparisons: int = 1) -> dict:
    """Two-sided union-bound intervals for independent differences in [-1,1].

    P(|mean - E mean| > e) <= 2 exp(-n e^2/2). Therefore
    e = sqrt(2 log(2 * candidates * conditions * comparisons / alpha) / n).
    Pairing occurs inside a lineage; n is never the number of tasks or checks.
    """
    values = list(differences)
    if not values or any(not math.isfinite(v) or not -1 <= v <= 1 for v in values):
        raise ValueError("finite independent-unit differences in [-1,1] are required")
    if not 0 < alpha < 1:
        raise ValueError("alpha must be in (0,1)")
    if any(isinstance(v, bool) or not isinstance(v, int) or v < 1
           for v in (candidates, conditions, comparisons)):
        raise ValueError("multiplicity factors must be positive integers")
    multiplicity = candidates * conditions * comparisons
    mean = sum(values) / len(values)
    radius = math.sqrt(2 * math.log(2 * multiplicity / alpha) / len(values))
    return {"n_independent_lineages": len(values), "mean": mean,
            "lower": max(-1.0, mean - radius), "upper": min(1.0, mean + radius),
            "radius": radius, "alpha_familywise": alpha,
            "multiplicity": multiplicity, "range": [-1, 1],
            "formula": "sqrt(2*log(2*candidates*conditions*comparisons/alpha)/n)"}


def _contrast(scores: dict[str, float]) -> dict[str, float]:
    a, b, c, d = (scores[k] for k in ARMS)
    return {"method_without_memory": c - a, "memory_without_method": b - a,
            "method_with_memory": d - b, "joint": d - a,
            "half_interaction": ((d - b) - (c - a)) / 2}


def run_pilot(seed: int = 20260921, units: int = 8, budget: int = 6,
              development_tasks: int = 2, selection_tasks: int = 2,
              confirmation_tasks: int = 3, alpha: float = 0.05,
              effect_margin: float = 0.0) -> dict:
    """Run a fixed-design, standard-library-only, JSON-serializable pilot.

    units is the count of independently developed lineages, *not* the number
    of held-out tasks. All configuration is frozen before any data generation.
    No optional stopping, extra candidates after seeing confirmation, or
    automatic deployment/promotion occurs in this function.
    """
    for name, value in (("units", units), ("development_tasks", development_tasks),
                        ("selection_tasks", selection_tasks), ("confirmation_tasks", confirmation_tasks)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer")
    if isinstance(budget, bool) or not isinstance(budget, int) or not 1 <= budget <= 32:
        raise ValueError("budget must be an integer in [1,32]")
    if not 0 < alpha < 1 or not 0 <= effect_margin < 1:
        raise ValueError("invalid alpha or effect margin")
    design = {"seed": seed, "independent_lineages": units,
              "method_revision": "protected-completion-v2",
              "task_success": "acceptance_eligible: bounded verification passes and at least one candidate-controlled policy decision was checked",
              "candidate_budget_per_task_arm": budget,
              "development_tasks_per_lineage": development_tasks,
              "selection_tasks_per_lineage": selection_tasks,
              "confirmation_tasks_per_condition_lineage": confirmation_tasks,
              "conditions": list(CONDITIONS), "arms": list(ARMS),
              "alpha": alpha, "effect_margin": effect_margin,
              "method_family_size": METHOD_FAMILY_SIZE,
              "comparisons": list(CONTRASTS),
              "selection_rule": "max solved, min first-success sum, min transitions, fixed family order",
              "confirmation_rule": "method-without-memory lower>margin and method-with-memory lower>=0 in every condition",
              "inference_unit": "independently developed lineage",
              "capability_composition": {"original_control": "memory_first",
                                         "learned_family": "after_success"},
              "completion_fallback": {"original_control": None,
                                      "learned_family_final_slot_mask": 31,
                                      "trigger": "last slot, no accepted incumbent, mask 31 not already checked",
                                      "provenance": "known complete-guard domain baseline; human-designed, not learned"},
              "secondary_metric": "selected_guard_count among solved tasks; descriptive, not a promotion endpoint",
              "estimand": "Expected paired solved-fraction effect of the development-and-selection procedure; each independent lineage can select a different frozen policy.",
              "evaluation_backend": "closed policy interpreter; exported standalone Python runs the same proposal core (parity checked in tests)",
              "fixed_baseline": "human-known complete guard set (mask 31), proposed first under the same candidate budget",
              "auto_promote": False}
    report = {"schema_version": "1.2", "design": design,
              "design_digest": _hash(design), "seed_manifest": [],
              "development": {"lineages": [], "total_cost": _empty_cost()},
              "frozen_methods": [], "factorial": {"units": [], "summary": {}},
              "fixed_baseline": {"name": "human_all_guards_first", "summary": {}},
              "lineage_archive": [], "artifacts": {},
              "limits": ["Bounded finite workflow DSL; no production correctness claim.",
                         "Fresh seeds are fresh instances of a finite family, not new problem classes.",
                         "Candidate/verifier-call budgets are equal; primitive graph work can differ and is reported.",
                         "M1 and C1 have additional development costs. Equal solving slots do not mean equal total research compute; lifecycle accounting includes discovery and all rejected methods.",
                         "Inference is about the learning-and-selection procedure across lineages, not repeated independent retraining of one common frozen artifact.",
                         "Macro memory is external capability, not a model-weight update.",
                         "All five known guards already provide a fixed solution; gains over breadth search do not beat that simple baseline.",
                         "Learning changes search within an unchanged 32-program grammar; it invents no new primitive or algorithm class.",
                         "Revised learned policies admit memory only after a repair is verified. They preserve primary success and first-success slot exactly relative to the same policy without memory, rather than claiming memory improves solve rate.",
                         "The composition guarantee does not cover post-success guard count, verification cost, or arbitrary untrusted verifier feedback.",
                         "Learned-family policies now reserve their final unsolved slot for the existing complete-guard baseline. Completeness comes from this known domain solution, not from novel learned reasoning.",
                         "No-loss across older incompatible schedules is restricted to tasks where that fallback is nonvacuously valid. It is not a generic fixed-budget search guarantee.",
                         "Lineage-seed independence is a design assumption, not proof of broad scientific transfer."]}
    report["capability_composition_guarantee"] = {
        "policy": "after_success",
        "assumptions": ["same task, deterministic policy and verifier", "same candidate budget",
                        "verified-success history is trusted and preserved"],
        "argument": "Before the first success memory is ineligible, so both runs have identical proposals and verifier observations by induction. Their first success occurs in the same slot, or neither succeeds. Later memory proposals cannot delete the successful incumbent.",
        "guaranteed": ["equal solved-within-budget indicator", "equal first-success slot"],
        "not_guaranteed": ["smaller selected program", "lower primitive verification work"]}
    report["completion_fallback_guarantee"] = {
        "condition": "The task's fixed full-guard mask 31 is a nonvacuously valid bounded repair.",
        "argument": "If a prior candidate was accepted, the incumbent is retained. Otherwise the final slot checks mask 31. A prior check of mask 31 would already have accepted under the condition, so an unsolved run cannot have exhausted that fallback earlier.",
        "guaranteed": "An accepted repair within any declared candidate budget from 1 through 32, under the condition.",
        "accounting": "The fallback replaces one scheduled candidate; its verification is counted in the same fixed budget.",
        "not_guaranteed": ["validity in another grammar", "acceptance with no policy-decision coverage",
                           "lower verification cost", "smaller programs", "earlier success than the human baseline"],
        "novelty": "No new primitive or universal search advantage: this reuses the already-known human all-guards solution."}
    all_task_ids: set[str] = set()
    for unit in range(units):
        seeds = {stage: _seed(seed, unit, stage) for stage in ("discovery", "selection", *CONDITIONS)}
        report["seed_manifest"].append({"lineage": unit, **seeds})
        development = domain.make_tasks(seeds["discovery"], development_tasks, split="development")
        selection = domain.make_tasks(seeds["selection"], selection_tasks, split="development")
        learned = learn_method(development, selection, budget)
        method, capability = learned["method"], learned["capability"]
        # Compile/freeze before confirmation tasks are even requested.
        source = compile_method(method)
        artifact_id = "method_" + method.digest[:16]
        report["artifacts"][artifact_id] = {"kind": "search_method", "sha256": sha256(source.encode()).hexdigest(),
                                           "spec": method.to_dict(), "source": source}
        learned["cost"]["source_bytes"] += len(source.encode())
        report["frozen_methods"].append({"lineage": unit, "spec": method.to_dict(),
                                          "method_digest": method.digest,
                                          "freeze_digest": learned["freeze_digest"],
                                          "freeze_record": learned["freeze_record"],
                                          "capability": capability.to_dict(), "artifact_id": artifact_id})
        discovery = learned["discovery"]
        report["development"]["lineages"].append({
            "lineage": unit, "failure_counts": discovery["failure_counts"],
            "intervention_evidence": discovery["intervention_evidence"],
            "task_ids": learned["freeze_record"]["discovery_ids"],
            "selection_task_ids": learned["freeze_record"]["selection_ids"],
            "discovery_records": discovery["records"], "cost": learned["cost"]})
        _add_cost(report["development"]["total_cost"], learned["cost"])
        report["lineage_archive"].append({"lineage": unit, "candidates": learned["archive"],
                                          "selected_digest": method.digest,
                                          "pareto_method_digests": learned["pareto_method_digests"]})
        used_ids = [t.task_id for t in development + selection]
        unit_result = {"lineage": unit, "conditions": {}}
        for condition in CONDITIONS:
            tasks = domain.make_tasks(seeds[condition], confirmation_tasks,
                                      split="confirmation" if condition == "fresh" else "shift")
            used_ids.extend(t.task_id for t in tasks)
            arms = {}
            for arm, active_method, active_capability in (
                ("M0_C0", ORIGINAL_METHOD, ORIGINAL_CAPABILITY),
                ("M0_C1", ORIGINAL_METHOD, capability),
                ("M1_C0", method, ORIGINAL_CAPABILITY),
                ("M1_C1", method, capability)):
                results = [run_search(t, active_method, active_capability, budget) for t in tasks]
                cost = _empty_cost()
                for row in results:
                    _add_cost(cost, row["cost"])
                    if row["selected_mask"] is not None:
                        mask = row["selected_mask"]
                        candidate = domain.candidate_spec(mask)
                        candidate_id = "candidate_" + _hash(candidate)[:16]
                        if candidate_id not in report["artifacts"]:
                            candidate_source = domain.compile_candidate(candidate)
                            report["artifacts"][candidate_id] = {
                                "kind": "workflow_candidate", "spec": candidate,
                                "source": candidate_source,
                                "sha256": sha256(candidate_source.encode()).hexdigest()}
                        row["artifact_id"] = candidate_id
                guard_counts = [r["selected_guard_count"] for r in results if r["solved"]]
                arms[arm] = {"score": sum(r["solved"] for r in results) / len(results),
                             "mean_selected_guard_count_among_solved": (sum(guard_counts) / len(guard_counts)
                                                                        if guard_counts else None),
                             "cost": cost, "tasks": results}
            scores = {arm: value["score"] for arm, value in arms.items()}
            baseline_results = [run_search(t, ALL_GUARDS_BASELINE, ORIGINAL_CAPABILITY, budget) for t in tasks]
            baseline_cost = _empty_cost()
            for result in baseline_results:
                _add_cost(baseline_cost, result["cost"])
            unit_result["conditions"][condition] = {
                "arms": arms, "contrasts": _contrast(scores),
                "fixed_baseline": {"score": sum(t["solved"] for t in baseline_results) / len(tasks),
                                   "cost": baseline_cost, "tasks": baseline_results}}
        if len(used_ids) != len(set(used_ids)) or all_task_ids.intersection(used_ids):
            raise ValueError("task identity collision between development/confirmation/lineages")
        all_task_ids.update(used_ids)
        if method.digest != learned["freeze_record"]["method_digest"]:
            raise AssertionError("frozen method changed during confirmation")
        report["factorial"]["units"].append(unit_result)
    intervals = {}
    confirmation_cost = _empty_cost()
    for condition in CONDITIONS:
        summaries = {}
        for arm in ARMS:
            values = [u["conditions"][condition]["arms"][arm] for u in report["factorial"]["units"]]
            cost = _empty_cost()
            for value in values:
                _add_cost(cost, value["cost"])
            _add_cost(confirmation_cost, cost)
            summaries[arm] = {"mean_solved_fraction": sum(v["score"] for v in values) / units,
                              "total_cost": cost, "lineage_scores": [v["score"] for v in values]}
            guard_counts = [task["selected_guard_count"] for value in values
                            for task in value["tasks"] if task["solved"]]
            summaries[arm]["mean_selected_guard_count_among_solved"] = (
                sum(guard_counts) / len(guard_counts) if guard_counts else None)
            summaries[arm]["secondary_solved_task_count"] = len(guard_counts)
        report["factorial"]["summary"][condition] = summaries
        baseline_values = [u["conditions"][condition]["fixed_baseline"] for u in report["factorial"]["units"]]
        baseline_cost = _empty_cost()
        for value in baseline_values:
            _add_cost(baseline_cost, value["cost"])
        _add_cost(confirmation_cost, baseline_cost)
        report["fixed_baseline"]["summary"][condition] = {
            "mean_solved_fraction": sum(v["score"] for v in baseline_values) / units,
            "total_cost": baseline_cost, "lineage_scores": [v["score"] for v in baseline_values]}
        intervals[condition] = {contrast: paired_hoeffding(
            [u["conditions"][condition]["contrasts"][contrast] for u in report["factorial"]["units"]],
            alpha=alpha, candidates=METHOD_FAMILY_SIZE,
            conditions=len(CONDITIONS), comparisons=len(CONTRASTS)) for contrast in CONTRASTS}
    passes = all(intervals[c]["method_without_memory"]["lower"] > effect_margin and
                 intervals[c]["method_with_memory"]["lower"] >= 0 for c in CONDITIONS)
    report["confirmation"] = {"decision": "evidence_passed_manual_review_required" if passes else "inconclusive",
                              "promoted": False, "intervals": intervals,
                              "total_cost": confirmation_cost,
                              "half_interaction_note": "Multiply mean and interval endpoints by 2 for the raw factorial interaction.",
                              "reason": "Fixed simultaneous bounds across independent lineages; no automatic promotion or optional stopping."}
    total_cost = dict(report["development"]["total_cost"])
    _add_cost(total_cost, confirmation_cost)
    report["lifecycle_cost"] = total_cost
    report["artifact_manifest"] = {key: {k: v for k, v in value.items() if k != "source"}
                                   for key, value in report["artifacts"].items()}
    # Detect accidental non-JSON data before returning an allegedly portable run.
    _json(report)
    return report


def export_artifacts(report: dict, directory: str | Path) -> list[str]:
    """Write the already-frozen policies/candidates plus their exact specs."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for identifier, artifact in sorted(report["artifacts"].items()):
        if not identifier.replace("_", "").isalnum():
            raise ValueError("invalid artifact identifier")
        for suffix, content in ((".py", artifact["source"]),
                                (".json", json.dumps(artifact["spec"], indent=2, sort_keys=True) + "\n")):
            path = directory / (identifier + suffix)
            path.write_text(content, encoding="utf-8")
            written.append(str(path))
    return written
