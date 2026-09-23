"""Code-development translation of the supplied codex_tool_lab.py.

Pure planning and durable repair memory, shared by any provider. This module
does not run candidate code, call models, reserve usage, modify a repository or
deploy. Controllers must authenticate evidence and reserve real usage outside
this module. Planning data and model agreement cannot grant execution authority.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath

from .improvement_release import PROTECTED_MODULES, PROTECTED_WORDS
from .research import hoeffding_lower_bound

SOURCE_SHA256 = "e7614f3ddd7ef94b0b80b3d4f654224746b419c959b60272d497f8d5aa500fdb"
PROVIDERS = ("codex", "claude", "cursor", "copilot", "grok")
STAGES = ("preflight", "matched_controls", "regime_confirmation", "hidden_replay")
MAX_BYTES = 240_000
MAX_MEMORY = 48

# All 19 original theory identities are retained; objectives are software work.
THEORIES = (
    ("A", "tool_specificity", "Target one reproducible failure with a small repair tool."),
    ("B", "matched_control_lift", "Compare the patch and unchanged baseline on identical inputs."),
    ("C", "observation_to_repair", "Turn failure traces into a concrete patch and regression test."),
    ("D", "near_miss_repair", "Repair a nearly passing candidate without relaxing its tests."),
    ("E", "tool_retirement", "Quarantine redundant or repeatedly ineffective tools; retain evidence."),
    ("F", "failure_memory", "Change the failed mechanism instead of renaming the same patch."),
    ("G", "repair_diversity", "Compare different repair families and independent model approaches."),
    ("H", "verification_efficiency", "Run cheap decisive checks before costly integration tests."),
    ("I", "test_leakage", "Keep withheld tests out of candidate prompts and optimization feedback."),
    ("J", "runner_reliability", "Bound work, preserve checkpoints and detect uncertain execution."),
    ("K", "workload_curriculum", "Test progressively harder workloads in isolated trials."),
    ("L", "useful_execution", "Replace repeated planning with the next bounded testable change."),
    ("M", "candidate_nursery", "Retain promising failed patches with precise blockers and next fixes."),
    ("N", "causal_tournament", "Compare causal repair hypotheses against the same base revision."),
    ("O", "compute_allocation", "Prioritize verified improvement per unit of work while retaining diversity."),
    ("S", "cross_component_transfer", "Transfer scoped lessons between tools, repairs, tests and scheduling."),
    ("P", "adversarial_review", "Ask an independent reviewer to disprove the claimed improvement."),
    ("Q", "regression_generation", "Add a deterministic test that fails before the repair and passes after."),
    ("R", "measured_progress", "Require changed behavior and implementation evidence, not activity counts."),
)
THEORY_IDS = frozenset(row[0] for row in THEORIES)
NEXT_FIX = {
    "compile_failed": "Repair syntax and run the smallest relevant check.",
    "regression": "Reproduce the new regression and repair it against the unchanged baseline.",
    "no_lift": "Change the repair mechanism and repeat the matched comparison.",
    "overfit": "Generalize the repair using development cases; obtain new withheld tests later.",
    "protected_change": "Restore protected controls and move the repair to its allowed files.",
    "unknown_execution": "Reconcile the existing attempt before scheduling more work.",
    "missing_evidence": "Collect the missing artifact-bound test evidence.",
    "review_rejected": "Address the independent review's reproducible objection.",
    "budget_exhausted": "Wait for a new verified improvement allocation; preserve project usage.",
}


class ToolLabError(ValueError):
    """Static validation diagnostics; no request content is included."""


def require(condition, code):
    if not condition:
        raise ToolLabError(code)


def canonical(value):
    try:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ToolLabError("finite_json_required") from None
    require(len(raw) <= MAX_BYTES, "record_size_limit")
    return raw


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def clone(value):
    return json.loads(canonical(value))


def keys(value, required):
    require(type(value) is dict and set(value) == set(required), "fields_invalid")


def text(value, limit=1000):
    require(type(value) is str and value.strip() and len(value.encode("utf-8")) <= limit,
            "bounded_text_required")


def identity(value):
    require(type(value) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", value), "identity_invalid")


def sha(value):
    require(type(value) is str and re.fullmatch(r"[a-f0-9]{64}", value), "sha256_required")


def integer(value, low=0, high=10**12):
    require(type(value) is int and low <= value <= high, "integer_invalid")


def allowed_file(value):
    text(value, 240)
    path = PurePosixPath(value)
    parts = path.parts
    require(path.as_posix() == value and not path.is_absolute() and ".." not in parts
            and not any(c in value for c in "\\:\x00") and parts, "path_invalid")
    lowered = [part.casefold() for part in parts]
    words = {word for part in lowered for word in re.split(r"[_.-]", part)}
    require(not any(part.startswith(".") for part in lowered)
            and lowered[0] not in ("deploy", "connectors", "vendor", "tests")
            and not words.intersection(PROTECTED_WORDS)
            and not (lowered[0] == "agent_hub" and lowered[-1].removesuffix(".py") in
                     PROTECTED_MODULES | {"tool_lab_core", "release_coordinator"}), "protected_file")


def validate_ticket(ticket):
    ticket = clone(ticket)
    keys(ticket, ("workspace", "repository", "base_revision", "failure_sha256", "failure",
                  "allowed_files", "metric", "regimes", "symptoms", "evaluator_sha256"))
    identity(ticket["workspace"]); identity(ticket["repository"])
    require(type(ticket["base_revision"]) is str and re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}",
            ticket["base_revision"]), "exact_revision_required")
    sha(ticket["failure_sha256"]); sha(ticket["evaluator_sha256"])
    text(ticket["failure"], 2000); text(ticket["metric"], 500)
    files = ticket["allowed_files"]
    require(type(files) is list and 1 <= len(files) <= 32 and all(type(f) is str for f in files)
            and len(set(files)) == len(files), "files_invalid")
    require(len({f.casefold() for f in files if type(f) is str}) == len(files), "files_invalid")
    for file in files:
        allowed_file(file)
    regimes = ticket["regimes"]
    require(type(regimes) is list and 3 <= len(regimes) <= 8 and all(type(r) is str for r in regimes)
            and len(set(regimes)) == len(regimes), "regimes_invalid")
    for regime in regimes:
        identity(regime)
    symptoms = ticket["symptoms"]
    require(type(symptoms) is list and 1 <= len(symptoms) <= 19 and all(type(s) is str and s in THEORY_IDS for s in symptoms)
            and len(set(symptoms)) == len(symptoms), "symptoms_invalid")
    return ticket


def make_plan(ticket, roster, allocation, *, now, generation=0, memory=()):
    """Return deterministic work packets, never permission to dispatch.

    Roster and allocation are trusted-controller snapshots. This planner checks
    consistency/freshness, not provider truth or entitlement. Real admission must
    recheck identity, usage and reserve the complete work before model execution.
    Budgets are in one controller-defined comparable work unit, not mixed tokens.
    """
    ticket = validate_ticket(ticket)
    integer(now); integer(generation, 0, 1_000_000)
    roster, allocation, memory = clone(roster), clone(allocation), clone(list(memory))
    require(type(roster) is list and len(roster) <= 5, "roster_invalid")
    keys(allocation, ("receipt_sha256", "observed_at", "expires_at", "improvement_units",
                      "project_reserved_units", "per_variant_units", "funding", "urgent_project"))
    sha(allocation["receipt_sha256"])
    for key in ("observed_at", "expires_at", "improvement_units", "project_reserved_units", "per_variant_units"):
        integer(allocation[key])
    require(type(allocation["urgent_project"]) is bool, "urgent_project_invalid")
    require(allocation["funding"] in ("included_subscription", "unknown", "credits"), "funding_invalid")
    require(allocation["per_variant_units"] >= 1, "variant_budget_required")
    require(len(memory) <= MAX_MEMORY, "memory_limit")
    reasons = []
    if not allocation["observed_at"] <= now < allocation["expires_at"] <= allocation["observed_at"] + 900:
        reasons.append("usage_stale_or_unknown")
    if allocation["funding"] != "included_subscription":
        reasons.append("improvement_requires_included_usage")
    if allocation["urgent_project"]:
        reasons.append("urgent_project_first")
    if allocation["project_reserved_units"] < 3 * allocation["improvement_units"]:
        reasons.append("project_reserve_below_75_percent")
    ready, seen_agents, seen_models = [], set(), set()
    for agent in roster:
        keys(agent, ("agent", "model", "effort", "catalog_sha256", "verified_until"))
        require(agent["agent"] in PROVIDERS and agent["agent"] not in seen_agents, "agent_duplicate_or_invalid")
        text(agent["model"], 120); text(agent["effort"], 40); sha(agent["catalog_sha256"])
        integer(agent["verified_until"])
        require(agent["model"].casefold() not in seen_models, "underlying_model_must_be_distinct")
        seen_agents.add(agent["agent"]); seen_models.add(agent["model"].casefold())
        if now < agent["verified_until"] <= now + 900:
            ready.append(agent)
    ready.sort(key=lambda item: PROVIDERS.index(item["agent"]))
    count = min(5, len(ready), allocation["improvement_units"] // allocation["per_variant_units"])
    if count < 3:
        reasons.append("three_funded_distinct_variants_required")
    family_penalties = {}
    shared_memory = []
    for card in memory:
        validate_card(card)
        require(card["workspace"] == ticket["workspace"] and card["repository"] == ticket["repository"],
                "memory_scope_mismatch")
        family_penalties[card["theory"]] = family_penalties.get(card["theory"], 0) + card["attempts"]
        # No raw output or holdout inputs can enter the shared prompt packet.
        shared_memory.append({k: card[k] for k in ("id", "theory", "outcome", "blockers", "next_fixes", "candidate_sha256")})
    theories = list(THEORIES)
    rotated = theories[generation % len(theories):] + theories[:generation % len(theories)]
    rank = {row[0]: index for index, row in enumerate(rotated)}
    ordered = sorted(theories, key=lambda row: (row[0] not in ticket["symptoms"], family_penalties.get(row[0], 0), rank[row[0]]))
    variants = []
    if not reasons:
        for index, theory in enumerate(ordered[:count]):
            builder = ready[index]
            reviewer = ready[(index + 1) % len(ready)]
            lane = {"theory": theory[0], "family": theory[1], "objective": theory[2],
                "builder": builder, "reviewer": reviewer, "max_work_units": allocation["per_variant_units"],
                "base_revision": ticket["base_revision"], "rollback_revision": ticket["base_revision"],
                "allowed_files": ticket["allowed_files"], "failure_sha256": ticket["failure_sha256"],
                "causal_contract": ["failure", "mechanism", "observable_metric", "unchanged_baseline", "rollback"],
                "proof_ladder": list(STAGES), "required_regimes": ticket["regimes"],
                "new_regression_test_required": True, "isolated_checkout_required": True}
            lane["id"] = digest(lane)
            variants.append(lane)
    result = {"schema_version": 1, "source_sha256": SOURCE_SHA256, "ticket": ticket,
        "generation": generation, "allocation": allocation, "variants": variants,
        "shared_memory": shared_memory, "status": "blocked" if reasons else "planned",
        "reasons": reasons, "total_planned_units": len(variants) * allocation["per_variant_units"],
        "dispatch_authorized": False, "promotion_allowed": False,
        "budget_is_reservation": False, "holdout_inputs_shared": False,
        "confirmation_sample_size": 256, "confirmation_alpha": 0.05, "minimum_lift": 0.01}
    result["id"] = digest(result)
    return result


def handoff(plan, agent):
    """All five providers consume the same contract, with distinct assigned roles."""
    validate_plan(plan)
    require(agent in PROVIDERS, "agent_invalid")
    return {"plan_id": plan["id"], "ticket": clone(plan["ticket"]),
        "assignments": [{"role": role, "variant": clone(v)} for v in plan["variants"]
            for role in ("builder", "reviewer") if v[role]["agent"] == agent],
        "lessons": clone(plan["shared_memory"]), "dispatch_authorized": False,
        "instruction": "Use failure evidence and the frozen contract. Peer lessons are leads to test. Do not request hidden test inputs."}


def validate_plan(plan):
    value = clone(plan)
    identity_hash = value.pop("id", None)
    sha(identity_hash)
    require(digest(value) == identity_hash and value.get("source_sha256") == SOURCE_SHA256
            and value.get("dispatch_authorized") is False and value.get("promotion_allowed") is False
            and value.get("confirmation_sample_size") == 256 and value.get("confirmation_alpha") == 0.05
            and value.get("minimum_lift") == 0.01,
            "plan_identity_invalid")
    validate_ticket(value["ticket"])


def assess_stage(plan, lane_id, stage, evidence, previous=()):
    """Validate controller-supplied test measurements, never deploy from them.

    The caller must independently authenticate its test collector and verify
    evidence bytes at evidence_sha256. Content hashes alone are not signatures.
    Withheld inputs stay in the evaluator; only paired measurements enter here.
    """
    validate_plan(plan)
    require(plan["status"] == "planned", "plan_not_ready")
    require(stage in STAGES, "stage_invalid")
    lane = next((v for v in plan["variants"] if v["id"] == lane_id), None)
    require(lane is not None, "lane_missing")
    previous = clone(list(previous))
    require(len(previous) == STAGES.index(stage), "proof_stage_order")
    evidence = clone(evidence)
    keys(evidence, ("candidate_sha256", "baseline_revision", "evaluator_sha256", "evidence_sha256",
        "collector", "passed", "regressions", "protected_unchanged", "new_regression_test",
        "work_units", "split", "regimes", "negative_controls_passed", "review_passed",
        "pairs", "sample_ids", "alpha", "minimum_lift"))
    for name in ("candidate_sha256", "evaluator_sha256", "evidence_sha256"):
        sha(evidence[name])
    require(evidence["baseline_revision"] == plan["ticket"]["base_revision"]
            and evidence["evaluator_sha256"] == plan["ticket"]["evaluator_sha256"], "evidence_binding_mismatch")
    for index, prior in enumerate(previous):
        require(prior.get("stage") == STAGES[index] and prior.get("lane_id") == lane_id
            and prior.get("plan_id") == plan["id"] and prior.get("passed") is True
            and prior.get("candidate_sha256") == evidence["candidate_sha256"], "previous_proof_invalid")
    require(evidence["collector"] == lane["reviewer"]["agent"], "independent_collector_required")
    for name in ("passed", "protected_unchanged", "new_regression_test", "negative_controls_passed", "review_passed"):
        require(type(evidence[name]) is bool, "boolean_evidence_required")
    integer(evidence["regressions"]); integer(evidence["work_units"])
    total_work = sum(p["work_units"] for p in previous) + evidence["work_units"]
    reasons = []
    if not evidence["passed"] or not evidence["new_regression_test"]:
        reasons.append("missing_evidence")
    if not evidence["protected_unchanged"]:
        reasons.append("protected_change")
    if evidence["regressions"]:
        reasons.append("regression")
    if total_work > lane["max_work_units"]:
        reasons.append("budget_exhausted")
    expected_split = "withheld" if stage == "hidden_replay" else "development"
    require(evidence["split"] == expected_split, "split_invalid")
    regimes = evidence["regimes"]
    require(type(regimes) is list and len(regimes) <= 8 and all(type(r) is str for r in regimes)
            and len(set(regimes)) == len(regimes)
            and set(regimes) <= set(plan["ticket"]["regimes"]), "regimes_invalid")
    sample_ids = evidence["sample_ids"]
    pairs = evidence["pairs"]
    require(type(pairs) is list and len(pairs) <= 1024 and type(sample_ids) is list
            and all(type(s) is str for s in sample_ids)
            and len(sample_ids) == len(pairs) and len(set(sample_ids)) == len(sample_ids), "paired_samples_invalid")
    for sample in sample_ids:
        sha(sample)
    if stage == "hidden_replay":
        require(len(pairs) == plan["confirmation_sample_size"], "fixed_confirmation_sample_required")
        used = {sample for prior in previous for sample in prior["sample_ids"]}
        require(not used.intersection(sample_ids), "withheld_sample_reused")
    deltas, pair_work = [], 0
    for pair in pairs:
        keys(pair, ("candidate", "baseline", "candidate_budget", "baseline_budget"))
        for k in ("candidate", "baseline"):
            require(type(pair[k]) in (int, float) and 0 <= pair[k] <= 1, "paired_score_invalid")
        integer(pair["candidate_budget"]); integer(pair["baseline_budget"])
        require(pair["candidate_budget"] == pair["baseline_budget"], "matched_budget_required")
        pair_work += pair["candidate_budget"] + pair["baseline_budget"]
        deltas.append(pair["candidate"] - pair["baseline"])
    require(evidence["work_units"] >= pair_work, "paired_work_must_be_counted")
    lower = None
    if stage != "preflight":
        if not pairs or not evidence["negative_controls_passed"]:
            reasons.append("missing_evidence")
        elif sum(deltas) <= 0:
            reasons.append("no_lift")
    if stage in ("regime_confirmation", "hidden_replay") and set(regimes) != set(plan["ticket"]["regimes"]):
        reasons.append("overfit")
    # Fixed family-wise test parameters cannot be tuned by candidate output.
    require(evidence["alpha"] == 0.05 and type(evidence["minimum_lift"]) in (int, float)
            and evidence["minimum_lift"] == 0.01, "confirmation_policy_fixed")
    if stage == "hidden_replay":
        if not evidence["review_passed"]:
            reasons.append("review_rejected")
        if deltas:
            lower = hoeffding_lower_bound(deltas, 0.05 / len(plan["variants"]))
        if lower is None or lower <= 0.01:
            reasons.append("no_lift")
    return {"plan_id": plan["id"], "lane_id": lane_id, "stage": stage,
        "candidate_sha256": evidence["candidate_sha256"], "evidence_sha256": evidence["evidence_sha256"],
        "passed": not reasons, "reasons": sorted(set(reasons)), "work_units": evidence["work_units"],
        "sample_ids": sample_ids, "lower_bound": lower,
        "ready_for_release_review": stage == "hidden_replay" and not reasons,
        "promotion_allowed": False, "dispatch_authorized": False}


def make_card(plan, lane_id, candidate_sha256, outcome, blockers, evidence_sha256, *, attempts=1):
    validate_plan(plan)
    lane = next((v for v in plan["variants"] if v["id"] == lane_id), None)
    require(lane is not None, "lane_missing")
    sha(candidate_sha256); sha(evidence_sha256); integer(attempts, 1, 3)
    require(outcome in ("helped", "harmed", "neutral", "uncertain"), "outcome_invalid")
    require(type(blockers) is list and 1 <= len(blockers) <= len(NEXT_FIX)
            and all(type(b) is str and b in NEXT_FIX for b in blockers)
            and len(set(blockers)) == len(blockers), "blockers_invalid")
    card = {"workspace": plan["ticket"]["workspace"], "repository": plan["ticket"]["repository"],
        "plan_id": plan["id"], "theory": lane["theory"], "failure_sha256": plan["ticket"]["failure_sha256"],
        "candidate_sha256": candidate_sha256, "evidence_sha256": evidence_sha256,
        "outcome": outcome, "blockers": sorted(blockers), "attempts": attempts,
        "next_fixes": [NEXT_FIX[b] for b in sorted(blockers)],
        "retry_allowed": attempts < 3 and outcome != "uncertain" and "unknown_execution" not in blockers,
        "promotion_allowed": False}
    card["id"] = digest(card)
    return card


def validate_card(card):
    value = clone(card)
    keys(value, ("id", "workspace", "repository", "plan_id", "theory", "failure_sha256",
        "candidate_sha256", "evidence_sha256", "outcome", "blockers", "attempts", "next_fixes",
        "retry_allowed", "promotion_allowed"))
    saved = value.pop("id")
    require(saved == digest(value), "memory_identity_invalid")
    for key in ("plan_id", "failure_sha256", "candidate_sha256", "evidence_sha256"):
        sha(value[key])
    identity(value["workspace"]); identity(value["repository"])
    require(type(value["theory"]) is str and value["theory"] in THEORY_IDS
            and value["promotion_allowed"] is False
            and value["outcome"] in ("helped", "harmed", "neutral", "uncertain"), "memory_scope_invalid")
    integer(value["attempts"], 1, 3)
    require(type(value["blockers"]) is list and value["blockers"]
            and all(type(b) is str and b in NEXT_FIX for b in value["blockers"])
            and value["blockers"] == sorted(set(value["blockers"])), "blockers_invalid")
    require(value["next_fixes"] == [NEXT_FIX[b] for b in value["blockers"]], "memory_recipe_invalid")
    require(value["retry_allowed"] is (value["attempts"] < 3 and value["outcome"] != "uncertain"
            and "unknown_execution" not in value["blockers"]), "memory_retry_invalid")


class PatchGarden:
    """Bounded, workspace-isolated immutable repair records using the hub store.

    Store transactions may retry callbacks. No external work occurs inside them.
    At capacity, fail visibly; never delete active/uncertain evidence to make room.
    """
    def __init__(self, store, workspace, repository):
        identity(workspace); identity(repository)
        self.store, self.workspace, self.repository = store, workspace, repository
        self.key = "tool_lab_" + digest([workspace, repository])

    def list(self):
        value = self.store.get_state(self.key)
        cards = list(value.get("cards", {}).values())
        for card in cards:
            validate_card(card)
        return sorted(cards, key=lambda c: c["id"])

    def remember(self, card):
        card = clone(card); validate_card(card)
        require((card["workspace"], card["repository"]) == (self.workspace, self.repository), "memory_scope_mismatch")
        def change(state):
            cards = state.setdefault("cards", {})
            if card["id"] in cards:
                require(cards[card["id"]] == card, "memory_conflict")
                return clone(card)
            require(len(cards) < MAX_MEMORY, "memory_full_review_retention")
            # A renamed candidate cannot reset the failure-family retry ceiling.
            related = [c for c in cards.values() if c["failure_sha256"] == card["failure_sha256"]
                       and c["theory"] == card["theory"]]
            require(not any(c["outcome"] == "uncertain" or "unknown_execution" in c["blockers"] for c in related),
                    "uncertain_attempt_requires_reconciliation")
            require(card["attempts"] == 1 + max((c["attempts"] for c in related), default=0), "retry_count_mismatch")
            require(not any(c["candidate_sha256"] == card["candidate_sha256"] for c in related),
                    "unchanged_candidate_is_not_a_repair")
            cards[card["id"]] = card
            canonical(state)
            return clone(card)
        return self.store.mutate_state(self.key, change)
